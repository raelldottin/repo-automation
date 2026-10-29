"""Write supervisor decisions back to one Hermes Kanban board (RA-CP-005).

The contract is ``docs/architecture/kanban-cutover.md``. Every write goes through Hermes's own
``hermes kanban`` commands, never raw SQL, so Hermes keeps its locks, events and guards. This
module only decides whether a write may happen, from a read-only look at the board, and checks
afterwards that it did.

- ``claim`` takes an approved, unassigned ``ready`` task, but only if the board is still at the
  revision the supervisor projected. The claim is the one-writer lock: the gateway never
  dispatches a ``running`` task.
- ``record`` turns the supervisor's verdict into ``request-review`` (``done``) or ``block``
  (``blocked``, ``failed``). It is conditional on the supervisor's own run: Hermes refuses the
  write if that run is no longer current, so a reclaimed or edited task fails closed.
- A supervisor ``done`` becomes Kanban ``review``, never ``done``. A person approves it in Kanban.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from automation.supervisor import kanban_projection as kp

# ponytail: a CLI claim is locked to the short-lived CLI process (host:pid), so nothing can renew
# it. The TTL must cover the whole slice; after it, Hermes reclaims and ``record`` fails closed.
# Renew in-process through hermes_cli.kanban_db if slices outgrow any sensible TTL.
DEFAULT_CLAIM_TTL_SECONDS = 2 * 60 * 60
HERMES_TIMEOUT_SECONDS = 60

# Supervisor verdict -> (hermes kanban subcommand, its flags, outcome Hermes gives the closed run).
VERDICTS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "done": ("request-review", (), "review_requested"),
    "blocked": ("block", ("--kind", "needs_input"), "blocked"),
    "failed": ("block", ("--kind", "transient"), "blocked"),
}


class KanbanWriteError(kp.ProjectionError):
    """The write was refused before Hermes was asked, or Hermes refused or did not apply it."""


@dataclass(frozen=True)
class Claim:
    board: str
    task_id: str
    run_id: int


def claim(
    board_dir: Path,
    task_id: str,
    expected_revision: str,
    hermes: Sequence[str] = ("hermes",),
    ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
) -> Claim:
    """Claim an approved task for the supervisor, or refuse without writing."""
    snapshot = kp.read_board(board_dir)
    kp.require_revision(snapshot, expected_revision)
    task = _task(board_dir, task_id)
    if task["status"] != "ready":
        raise KanbanWriteError(f"task {task_id} is {task['status']}, not ready")
    if task["assignee"]:
        raise KanbanWriteError(f"task {task_id} is assigned to {task['assignee']!r}; the Hermes gateway owns it")
    _hermes(hermes, snapshot.board, ["claim", task_id, "--ttl", str(ttl_seconds)])
    claimed = _task(board_dir, task_id)
    if claimed["status"] != "running" or not claimed["current_run_id"]:
        raise KanbanWriteError(f"hermes reported a claim of {task_id} but the board shows {claimed['status']}")
    return Claim(snapshot.board, task_id, int(claimed["current_run_id"]))


def record(
    board_dir: Path,
    held: Claim,
    verdict: str,
    reason: str,
    handoff_path: Path,
    hermes: Sequence[str] = ("hermes",),
) -> bool:
    """Record a verdict on the claimed run. Returns False when it was already recorded."""
    if verdict not in VERDICTS:
        raise KanbanWriteError(f"no Kanban transition for verdict {verdict!r}")
    if kp.board_slug(board_dir) != held.board:
        raise KanbanWriteError(f"claim is for board {held.board!r}, not {board_dir.name!r}")
    command, flags, outcome = VERDICTS[verdict]
    run = _run(board_dir, held)
    if run["outcome"] == outcome:
        return False
    if run["status"] != "running" or _task(board_dir, held.task_id)["current_run_id"] != held.run_id:
        raise KanbanWriteError(f"run {held.run_id} of {held.task_id} is {run['outcome'] or run['status']}; not ours to close")

    digest = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
    if verdict == "done":
        evidence = {"slice_id": kp.slice_id_for(held.board, held.task_id), "handoff_sha256": digest}
        args = [command, held.task_id, "--summary", reason, "--metadata", json.dumps(evidence, sort_keys=True)]
    else:
        args = [command, held.task_id, *flags, f"{reason} (handoff sha256 {digest})"]
    _hermes(hermes, held.board, args, held)
    if _run(board_dir, held)["outcome"] != outcome:
        raise KanbanWriteError(f"hermes accepted {command} for {held.task_id} but run {held.run_id} is not {outcome}")
    return True


def _hermes(hermes: Sequence[str], board: str, args: list[str], held: Optional[Claim] = None) -> None:
    env = dict(os.environ)
    # Hermes makes a write conditional on the run it names here (``expected_run_id``).
    env.pop("HERMES_KANBAN_TASK", None)
    env.pop("HERMES_KANBAN_RUN_ID", None)
    if held is not None:
        env.update(HERMES_KANBAN_TASK=held.task_id, HERMES_KANBAN_RUN_ID=str(held.run_id))
    argv = [*hermes, "kanban", "--board", board, *args]
    try:
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=HERMES_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise KanbanWriteError(f"hermes kanban {args[0]} did not finish within {HERMES_TIMEOUT_SECONDS}s") from None
    except OSError as error:
        raise KanbanWriteError(f"cannot run {hermes[0]}: {error.strerror}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise KanbanWriteError(f"hermes kanban {args[0]} refused: {detail[-1] if detail else result.returncode}")


def _task(board_dir: Path, task_id: str) -> dict[str, Any]:
    with kp.read_only(board_dir) as conn:
        row = conn.execute("SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KanbanWriteError(f"no task {task_id} on board {board_dir.name!r}")
    return dict(row)


def _run(board_dir: Path, held: Claim) -> dict[str, Any]:
    with kp.read_only(board_dir) as conn:
        row = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE id = ? AND task_id = ?", (held.run_id, held.task_id)
        ).fetchone()
    if row is None:
        raise KanbanWriteError(f"run {held.run_id} of {held.task_id} is not on board {held.board!r}")
    return dict(row)
