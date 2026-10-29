"""Contract tests for the Kanban write-back adapter (RA-CP-005).

The board is the synthetic ``automation/examples/kanban-board.sql`` plus the run columns the
writer reads. ``hermes`` is a stand-in that applies Hermes's own guards (``claim_task``,
``request_review`` and ``block_task`` with ``expected_run_id`` in ``hermes_cli/kanban_db.py`` at
31bde0d) to that file, so no test touches ``~/.hermes`` or a real board.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from automation.supervisor import kanban_projection as kp
from automation.supervisor import kanban_writer as kw

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_SQL = REPO_ROOT / "automation/examples/kanban-board.sql"
BOARD = "example-board"
READY = "t_000000a3"
RUN_COLUMNS = """
ALTER TABLE tasks ADD COLUMN assignee TEXT;
ALTER TABLE tasks ADD COLUMN current_run_id INTEGER;
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, status TEXT NOT NULL,
    outcome TEXT, summary TEXT, metadata TEXT
);
"""
STAND_IN = r"""
import json, os, sqlite3, sys
args = sys.argv[1:]
with open(os.environ["FAKE_HERMES_LOG"], "a") as log:
    log.write(json.dumps({"argv": args, "task": os.environ.get("HERMES_KANBAN_TASK"),
                          "run": os.environ.get("HERMES_KANBAN_RUN_ID")}) + "\n")
mode = os.environ.get("FAKE_HERMES_MODE", "")
if mode == "refuse":
    sys.exit("kanban: cannot do that")
assert args[:2] == ["kanban", "--board"], args
board, command, task = args[2], args[3], args[4]
conn = sqlite3.connect(os.path.join(os.environ["FAKE_KANBAN_ROOT"], board, "kanban.db"))
if mode == "silent":
    sys.exit(0)
if command == "claim":
    if conn.execute("UPDATE tasks SET status = 'running' WHERE id = ? AND status = 'ready'", (task,)).rowcount != 1:
        sys.exit(f"cannot claim {task}")
    run = conn.execute("INSERT INTO task_runs (task_id, status) VALUES (?, 'running')", (task,)).lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run, task))
else:
    expected = os.environ.get("HERMES_KANBAN_RUN_ID") if os.environ.get("HERMES_KANBAN_TASK") == task else None
    current = conn.execute("SELECT current_run_id FROM tasks WHERE id = ? AND status = 'running'", (task,)).fetchone()
    if current is None or (expected is not None and str(current[0]) != expected):
        sys.exit(f"cannot {command} {task}: run is not current")
    if command == "request-review":
        status, outcome = "review", "review_requested"
        metadata = args[args.index("--metadata") + 1]
    else:
        status, outcome, metadata = "blocked", "blocked", None
    conn.execute("UPDATE task_runs SET status = ?, outcome = ?, metadata = ? WHERE id = ?",
                 (status, outcome, metadata, current[0]))
    conn.execute("UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ?", (status, task))
conn.commit()
"""


@pytest.fixture
def board(tmp_path, monkeypatch):
    board_dir = tmp_path / "boards" / BOARD
    board_dir.mkdir(parents=True)
    with sqlite3.connect(board_dir / "kanban.db") as conn:
        conn.executescript(BOARD_SQL.read_text(encoding="utf-8") + RUN_COLUMNS)
    monkeypatch.setenv("FAKE_KANBAN_ROOT", str(board_dir.parent))
    monkeypatch.setenv("FAKE_HERMES_LOG", str(tmp_path / "hermes.log"))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_ffffffff")  # an ambient worker scope must not leak in
    return board_dir


@pytest.fixture
def hermes(tmp_path):
    script = tmp_path / "hermes.py"
    script.write_text(STAND_IN, encoding="utf-8")
    return (sys.executable, str(script))


@pytest.fixture
def handoff(tmp_path):
    path = tmp_path / "handoff.json"
    path.write_text('{"status": "done"}\n', encoding="utf-8")
    return path


def calls(tmp_path: Path) -> list[dict]:
    log = tmp_path / "hermes.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def row(board_dir: Path, sql: str, *params: object) -> tuple:
    with sqlite3.connect(board_dir / "kanban.db") as conn:
        return conn.execute(sql, params).fetchone()


def execute(board_dir: Path, sql: str) -> None:
    with sqlite3.connect(board_dir / "kanban.db") as conn:
        conn.executescript(sql)


def take(board_dir: Path, hermes) -> kw.Claim:
    return kw.claim(board_dir, READY, kp.read_board(board_dir).revision, hermes, ttl_seconds=600)


def test_a_done_verdict_claims_then_asks_for_review_on_our_run(board, hermes, handoff, tmp_path):
    held = take(board, hermes)

    assert kw.record(board, held, "done", "validated", handoff, hermes) is True

    assert held == kw.Claim(BOARD, READY, 1)
    assert row(board, "SELECT status, current_run_id FROM tasks WHERE id = ?", READY) == ("review", None)
    status, outcome, metadata = row(board, "SELECT status, outcome, metadata FROM task_runs WHERE id = 1")
    assert (status, outcome) == ("review", "review_requested")
    assert json.loads(metadata) == {
        "handoff_sha256": hashlib.sha256(handoff.read_bytes()).hexdigest(),
        "slice_id": kp.slice_id_for(BOARD, READY),
    }
    claim_call, review_call = calls(tmp_path)
    assert claim_call == {"argv": ["kanban", "--board", BOARD, "claim", READY, "--ttl", "600"], "task": None, "run": None}
    assert review_call["argv"][3:7] == ["request-review", READY, "--summary", "validated"]
    assert (review_call["task"], review_call["run"]) == (READY, "1")


@pytest.mark.parametrize(("verdict", "kind"), [("blocked", "needs_input"), ("failed", "transient")])
def test_blocked_and_failed_verdicts_block_the_task(board, hermes, handoff, tmp_path, verdict, kind):
    held = take(board, hermes)

    assert kw.record(board, held, verdict, "scope check failed", handoff, hermes) is True

    assert row(board, "SELECT status FROM tasks WHERE id = ?", READY) == ("blocked",)
    argv = calls(tmp_path)[-1]["argv"]
    assert argv[3:7] == ["block", READY, "--kind", kind]
    assert argv[7].startswith("scope check failed (handoff sha256 ")


def test_recording_twice_writes_once(board, hermes, handoff, tmp_path):
    held = take(board, hermes)
    kw.record(board, held, "done", "validated", handoff, hermes)

    assert kw.record(board, held, "done", "validated", handoff, hermes) is False
    assert len(calls(tmp_path)) == 2


def test_a_board_that_moved_since_the_projection_is_not_claimed(board, hermes, tmp_path):
    revision = kp.read_board(board).revision
    execute(board, "UPDATE tasks SET priority = 9 WHERE id = 't_000000a2'")

    with pytest.raises(kp.StaleRevisionError):
        kw.claim(board, READY, revision, hermes)
    assert calls(tmp_path) == []


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (f"UPDATE tasks SET assignee = 'opnory-builder' WHERE id = '{READY}'", "gateway owns it"),
        (f"UPDATE tasks SET status = 'todo' WHERE id = '{READY}'", "is todo, not ready"),
    ],
)
def test_a_task_the_supervisor_may_not_take_is_refused(board, hermes, tmp_path, sql, message):
    execute(board, sql)

    with pytest.raises(kw.KanbanWriteError, match=message):
        take(board, hermes)
    assert calls(tmp_path) == []


def test_a_reclaimed_run_is_not_ours_to_close(board, hermes, handoff, tmp_path):
    held = take(board, hermes)
    execute(
        board,
        f"""UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed' WHERE id = {held.run_id};
        INSERT INTO task_runs (task_id, status) VALUES ('{READY}', 'running');
        UPDATE tasks SET current_run_id = 2 WHERE id = '{READY}';""",
    )

    with pytest.raises(kw.KanbanWriteError, match="reclaimed; not ours"):
        kw.record(board, held, "done", "validated", handoff, hermes)
    assert len(calls(tmp_path)) == 1


def test_hermes_still_guards_the_run_if_the_board_moves_after_our_check(board, hermes, handoff, monkeypatch):
    held = take(board, hermes)
    stale = kw.Claim(BOARD, READY, held.run_id)
    execute(board, f"INSERT INTO task_runs (task_id, status) VALUES ('{READY}', 'running')")
    monkeypatch.setattr(kw, "_task", lambda *_: {"current_run_id": held.run_id})  # our check saw the old run

    execute(board, f"UPDATE tasks SET current_run_id = 2 WHERE id = '{READY}'")
    with pytest.raises(kw.KanbanWriteError, match="run is not current"):
        kw.record(board, stale, "done", "validated", handoff, hermes)


@pytest.mark.parametrize(("mode", "message"), [("refuse", "refused: kanban: cannot do that"), ("silent", "board shows ready")])
def test_hermes_refusals_and_unapplied_claims_fail_closed(board, hermes, monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_HERMES_MODE", mode)

    with pytest.raises(kw.KanbanWriteError, match=message):
        take(board, hermes)


def test_a_review_hermes_reports_but_did_not_apply_is_refused(board, hermes, handoff, monkeypatch):
    held = take(board, hermes)
    monkeypatch.setenv("FAKE_HERMES_MODE", "silent")

    with pytest.raises(kw.KanbanWriteError, match="is not review_requested"):
        kw.record(board, held, "done", "validated", handoff, hermes)


def test_unknown_verdicts_and_foreign_boards_are_refused(board, hermes, handoff, tmp_path):
    held = take(board, hermes)

    with pytest.raises(kw.KanbanWriteError, match="no Kanban transition"):
        kw.record(board, held, "queued", "", handoff, hermes)
    with pytest.raises(kw.KanbanWriteError, match="claim is for board"):
        kw.record(board, kw.Claim("other-board", READY, held.run_id), "done", "", handoff, hermes)
    assert len(calls(tmp_path)) == 1


def test_a_missing_hermes_is_a_refusal(board, tmp_path):
    with pytest.raises(kw.KanbanWriteError, match="cannot run"):
        take(board, (str(tmp_path / "no-hermes"),))


def test_a_failure_before_any_handoff_is_recorded_without_a_digest(board, hermes, tmp_path):
    held = take(board, hermes)

    assert kw.record(board, held, "failed", "agent exited 3", tmp_path / "never-written.json", hermes) is True

    assert calls(tmp_path)[-1]["argv"][-1] == "agent exited 3 (handoff sha256 absent)"


def test_a_done_verdict_without_its_handoff_is_refused(board, hermes, tmp_path):
    held = take(board, hermes)

    with pytest.raises(kw.KanbanWriteError, match="needs the handoff"):
        kw.record(board, held, "done", "validated", None, hermes)

    assert [call["argv"][3] for call in calls(tmp_path)] == ["claim"]
