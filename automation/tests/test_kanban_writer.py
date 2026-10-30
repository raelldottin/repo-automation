"""Contract tests for the Kanban write-back adapter (RA-CP-005).

The board is the synthetic ``automation/examples/kanban-board.sql`` plus the run columns the
writer reads. ``hermes`` is a stand-in that applies Hermes's own guards (``claim_task``,
``request_review`` and ``block_task`` with ``expected_run_id`` in ``hermes_cli/kanban_db.py`` at
31bde0d) to that file, so no test touches ``~/.hermes`` or a real board.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
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


# The real Hermes CLI, on a throwaway HERMES_HOME. Skipped unless HERMES_AGENT_PYTHON names an
# interpreter with Hermes's dependencies and HERMES_AGENT_SOURCE a hermes-agent checkout.

HERMES_PYTHON = os.environ.get("HERMES_AGENT_PYTHON")
HERMES_SOURCE = os.environ.get("HERMES_AGENT_SOURCE")
needs_hermes = pytest.mark.skipif(not (HERMES_PYTHON and HERMES_SOURCE), reason="HERMES_AGENT_PYTHON/SOURCE not set")
SHIM = """
import sys
sys.path.insert(0, {source!r})
from hermes_cli.main import main
sys.argv[0] = "hermes"
sys.exit(main())
"""


@pytest.fixture
def real_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    shim = tmp_path / "hermes_shim.py"
    shim.write_text(SHIM.format(source=HERMES_SOURCE), encoding="utf-8")
    cli = (str(HERMES_PYTHON), "-I", str(shim))
    subprocess.run([*cli, "kanban", "boards", "create", "canary"], check=True, capture_output=True)
    tasks = []
    for title in ("done path", "failed path", "reclaimed path"):
        out = subprocess.run([*cli, "kanban", "--board", "canary", "create", title], check=True, capture_output=True, text=True)
        created = re.search(r"Created (t_[0-9a-f]{8})", out.stdout)
        assert created, out.stdout
        tasks.append(created.group(1))
    return tmp_path / "home/kanban/boards/canary", cli, tasks


@needs_hermes
def test_the_real_hermes_cli_honours_the_contract(real_board, handoff):
    board_dir, cli, (done_task, failed_task, reclaimed_task) = real_board

    with pytest.raises(kp.StaleRevisionError):
        kw.claim(board_dir, done_task, "0" * 64, cli)
    held = kw.claim(board_dir, done_task, kp.read_board(board_dir).revision, cli, ttl_seconds=600)
    assert kw.record(board_dir, held, "done", "validated", handoff, cli) is True
    assert kw.record(board_dir, held, "done", "validated", handoff, cli) is False
    failed = kw.claim(board_dir, failed_task, kp.read_board(board_dir).revision, cli, ttl_seconds=600)
    assert kw.record(board_dir, failed, "failed", "agent exited 3", None, cli) is True

    mine = kw.claim(board_dir, reclaimed_task, kp.read_board(board_dir).revision, cli, ttl_seconds=600)
    kw._hermes(cli, "canary", ["reclaim", reclaimed_task])
    theirs = kw.claim(board_dir, reclaimed_task, kp.read_board(board_dir).revision, cli, ttl_seconds=600)
    with pytest.raises(kw.KanbanWriteError, match="not ours to close"):
        kw.record(board_dir, mine, "done", "late", handoff, cli)
    with pytest.raises(kw.KanbanWriteError, match="refused"):  # Hermes alone refuses a superseded run
        kw._hermes(cli, "canary", ["request-review", reclaimed_task, "--summary", "late"], mine)

    with kp.read_only(board_dir) as conn:
        state = {row[0]: tuple(row[1:]) for row in conn.execute("SELECT id, status, block_kind, current_run_id FROM tasks")}
        review = conn.execute("SELECT outcome, metadata FROM task_runs WHERE id = ?", (held.run_id,)).fetchone()
    assert state[done_task][0] == "review" and state[failed_task][:2] == ("blocked", "transient")
    assert state[reclaimed_task] == ("running", None, theirs.run_id)
    assert review[0] == "review_requested" and json.loads(review[1])["slice_id"] == kp.slice_id_for("canary", done_task)
