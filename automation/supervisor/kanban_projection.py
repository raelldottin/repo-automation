"""Read-only projection of one Hermes Kanban board onto the supervisor's slice queue.

The adapter boundary for RA-CP-002 (``docs/architecture/kanban-projection.md``). It reads a
board's SQLite file the way the Swarm Wiki compiler does (``mode=ro``, ``query_only``, one
transaction) and never through ``hermes kanban``, whose ``connect()`` initialises and migrates
the database. It writes nothing anywhere: the JSON queue stays the operational authority, and
the projection is only a candidate document validated against the same contract.

Kanban owns identity, title, status, priority and dependencies. It has no source for the
engineering-policy fields, so those come from a repo-automation-owned overlay keyed by task id.
A task absent from the overlay is not approved and is never projected, so no worker can start
from a card nobody admitted.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Generator, Literal, Optional

from pydantic import Field

from automation.schemas.models import (
    Contract,
    NonEmptyStr,
    NonEmptyStrList,
    Omittable,
    ProofLevel,
    QueuePolicy,
    SliceQueue,
    StrList,
)
from automation.supervisor import policy

# Hermes's own formats: ``_BOARD_SLUG_RE`` and ``_new_task_id`` in ``hermes_cli/kanban_db.py``.
BOARD_SLUG_PATTERN = r"^[a-z0-9][a-z0-9\-_]{0,63}$"
TASK_ID_PATTERN = r"^t_[0-9a-f]{8}$"
SLICE_ID_PREFIX = "kanban"

# Columns the projection reads. Kanban's schema is unversioned (``user_version`` 0), so a
# missing column is refused instead of guessed at.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "tasks": frozenset({"id", "title", "status", "priority", "created_at"}),
    "task_links": frozenset({"parent_id", "child_id"}),
    "task_events": frozenset({"id", "task_id", "kind", "payload"}),
}

# Kanban status -> (slice status, entry condition when the queue needs one). ``archived`` is
# left out on purpose: archived tasks are not projected, and as parents they count as
# satisfied, exactly as ``recompute_ready`` treats them. Kanban has no ``failed`` task state.
STATUS_MAP: dict[str, tuple[str, str]] = {
    "ready": ("queued", ""),
    "running": ("in_progress", ""),
    "done": ("done", ""),
    "blocked": ("blocked", "Kanban task is blocked."),
    "todo": ("blocked", "Kanban task is waiting on open parents (todo)."),
    "review": ("blocked", "Kanban task is waiting for review."),
    "triage": ("deferred", "Kanban task is still in triage."),
    "scheduled": ("deferred", "Kanban task is scheduled for later."),
}
TERMINAL_STATUSES = frozenset({"done", "archived"})


class ProjectionError(ValueError):
    """The board or overlay cannot be projected into a valid queue."""


class StaleRevisionError(ProjectionError):
    """The board changed since the revision a caller was acting on."""


class KanbanSlicePolicy(Contract):
    """The policy fields Kanban has no source for, owned by repo-automation per task."""

    domain: NonEmptyStr
    allowed_paths: NonEmptyStrList
    required_validations: StrList
    max_files_changed: Annotated[int, Field(ge=1)]
    notes: str = ""
    entry_condition: str = ""
    recommended_unblocker: str = ""
    manual_proof: StrList = Field(default_factory=list)
    required_proof_level: Annotated[ProofLevel, Omittable]


class KanbanOverlay(Contract):
    version: Literal[1]
    board: Annotated[str, Field(pattern=BOARD_SLUG_PATTERN)]
    policy: QueuePolicy
    approved: dict[Annotated[str, Field(pattern=TASK_ID_PATTERN)], KanbanSlicePolicy]


@dataclass(frozen=True)
class BoardSnapshot:
    board: str
    tasks: tuple[dict[str, Any], ...]
    links: tuple[tuple[str, str], ...]
    creators: dict[str, str]
    event_watermark: int
    revision: str


@dataclass(frozen=True)
class Projection:
    queue: dict[str, Any]
    provenance: dict[str, dict[str, Any]]
    revision: str
    event_watermark: int


def slice_id_for(board: str, task_id: str) -> str:
    return f"{SLICE_ID_PREFIX}.{board}.{task_id}"


def parse_slice_id(slice_id: str) -> Optional[tuple[str, str]]:
    """Trace a projected slice id (and so a handoff's ``slice_id``) back to its board and task."""
    parts = slice_id.split(".")
    if len(parts) != 3 or parts[0] != SLICE_ID_PREFIX:
        return None
    _, board, task_id = parts
    if not re.match(BOARD_SLUG_PATTERN, board) or not re.match(TASK_ID_PATTERN, task_id):
        return None
    return board, task_id


def read_board(board_dir: Path) -> BoardSnapshot:
    """Snapshot ``<board_dir>/kanban.db`` in one read-only transaction.

    The board slug is the directory name, as in Hermes's ``boards/<slug>/kanban.db`` layout.
    """
    board = board_slug(board_dir)
    with read_only(board_dir) as conn:
        conn.execute("BEGIN")
        try:
            for table, required in REQUIRED_COLUMNS.items():
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if not required <= columns:
                    raise ProjectionError(f"board {board!r} table {table!r} lacks columns {sorted(required - columns)}")
            tasks = tuple(
                dict(row)
                for row in conn.execute(
                    "SELECT id, title, status, COALESCE(priority, 0) AS priority, created_at FROM tasks ORDER BY id"
                )
            )
            links = tuple(
                (row[0], row[1])
                for row in conn.execute("SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id")
            )
            created = conn.execute("SELECT task_id, payload FROM task_events WHERE kind = 'created' ORDER BY id").fetchall()
            watermark = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        finally:
            conn.rollback()

    creators: dict[str, str] = {}
    for task_id, payload in created:
        creator = _json_object(payload).get("creator_task_id")
        if isinstance(creator, str) and creator:
            creators[task_id] = creator
    revision = _digest({"board": board, "tasks": tasks, "links": links, "creators": creators})
    return BoardSnapshot(board, tasks, links, creators, int(watermark), revision)


def board_slug(board_dir: Path) -> str:
    board = board_dir.name
    if not re.match(BOARD_SLUG_PATTERN, board):
        raise ProjectionError(f"not a Kanban board slug: {board!r}")
    return board


@contextlib.contextmanager
def read_only(board_dir: Path) -> Generator[sqlite3.Connection]:
    """Open ``<board_dir>/kanban.db`` with ``mode=ro`` and ``query_only``; never through Hermes."""
    db = board_dir / "kanban.db"
    if not db.is_file() or db.is_symlink():
        raise ProjectionError(f"missing board database: {db}")
    uri = db.resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn


def require_revision(snapshot: BoardSnapshot, expected: str) -> None:
    """The guard ``kanban_writer.claim`` runs on a fresh snapshot before it writes."""
    if snapshot.revision != expected:
        raise StaleRevisionError(f"board {snapshot.board!r} is at revision {snapshot.revision}, not the expected {expected}")


def load_overlay(path: Path) -> KanbanOverlay:
    document = policy.load_json(path)
    validation = policy.validate_document(document, KanbanOverlay)
    if not validation.is_valid:
        raise ProjectionError("Invalid Kanban overlay:\n- " + "\n- ".join(validation.errors))
    return KanbanOverlay.model_validate(document)


def project(snapshot: BoardSnapshot, overlay: KanbanOverlay) -> Projection:
    if overlay.board != snapshot.board:
        raise ProjectionError(f"overlay is for board {overlay.board!r}, not {snapshot.board!r}")
    by_id = {task["id"]: task for task in snapshot.tasks}
    unknown = sorted(set(overlay.approved) - set(by_id))
    if unknown:
        raise ProjectionError(f"overlay approves tasks not on board {snapshot.board!r}: {unknown}")
    for task_id in overlay.approved:
        if by_id[task_id]["status"] not in STATUS_MAP and by_id[task_id]["status"] != "archived":
            raise ProjectionError(f"task {task_id!r} has unknown Kanban status {by_id[task_id]['status']!r}")

    projected = [by_id[task_id] for task_id in overlay.approved if by_id[task_id]["status"] != "archived"]
    projected_ids = {task["id"] for task in projected}
    # Kanban dispatches ``priority DESC, created_at ASC``; the queue picks the lowest priority,
    # then the earliest index. Emit in dispatch order and invert, so both pick the same task.
    projected.sort(key=lambda task: (-task["priority"], task["created_at"], task["id"]))
    top = max((task["priority"] for task in projected), default=0)
    parents_of: dict[str, list[str]] = {}
    for parent_id, child_id in snapshot.links:
        parents_of.setdefault(child_id, []).append(parent_id)

    slices = [
        _slice_record(snapshot, task, overlay.approved[task["id"]], top, parents_of, by_id, projected_ids) for task in projected
    ]
    queue = {"version": 1, "policy": overlay.policy.model_dump(), "slices": slices}
    validation = policy.validate_document(queue, SliceQueue)
    errors = validation.errors or policy.validate_queue_integrity(queue)
    if errors:
        raise ProjectionError("Projected queue is invalid:\n- " + "\n- ".join(errors))

    provenance = {
        slice_id_for(snapshot.board, task["id"]): {
            "board": snapshot.board,
            "task_id": task["id"],
            "kanban_status": task["status"],
            "kanban_priority": task["priority"],
            "spawned_by": snapshot.creators.get(task["id"], ""),
        }
        for task in projected
    }
    return Projection(queue, provenance, snapshot.revision, snapshot.event_watermark)


def _slice_record(
    snapshot: BoardSnapshot,
    task: dict[str, Any],
    approval: KanbanSlicePolicy,
    top: int,
    parents_of: dict[str, list[str]],
    by_id: dict[str, dict[str, Any]],
    projected_ids: set[str],
) -> dict[str, Any]:
    status, entry_condition = STATUS_MAP[task["status"]]
    depends_on: list[str] = []
    for parent_id in sorted(parents_of.get(task["id"], [])):
        parent_status = by_id.get(parent_id, {}).get("status", "")
        if parent_id in projected_ids:
            depends_on.append(slice_id_for(snapshot.board, parent_id))
        elif parent_status not in TERMINAL_STATUSES and status == "queued":
            # An open parent nobody approved: the queue cannot see it, so hold the child.
            status = "blocked"
            entry_condition = f"Kanban parent {parent_id} is open and not approved for projection."

    record = approval.model_dump(exclude_none=True)
    record.update(
        slice_id=slice_id_for(snapshot.board, task["id"]),
        title=task["title"],
        status=status,
        priority=top - task["priority"],
        depends_on=depends_on,
    )
    if status in {"blocked", "deferred"} and not approval.entry_condition.strip():
        record["entry_condition"] = entry_condition
    return record


def _json_object(payload: Optional[str]) -> dict[str, Any]:
    try:
        value = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Print a read-only Kanban board projection. Writes nothing.")
    parser.add_argument("--board-dir", type=Path, required=True, help="~/.hermes/kanban/boards/<slug>")
    parser.add_argument("--overlay", type=Path, required=True, help="repo-automation approval overlay JSON")
    args = parser.parse_args(argv)
    try:
        result = project(read_board(args.board_dir), load_overlay(args.overlay))
    except (ProjectionError, policy.ConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    output = {
        "revision": result.revision,
        "event_watermark": result.event_watermark,
        "provenance": result.provenance,
        "queue": result.queue,
    }
    print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
