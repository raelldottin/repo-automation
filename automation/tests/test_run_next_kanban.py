"""``run_next`` with a Kanban board as its queue (RA-CP-005).

The board, the stand-in ``hermes`` and its call log come from ``test_kanban_writer``. The
consumer repository is a temporary git checkout, and the agent is a short Python script.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from automation.supervisor import run_next
from automation.tests import test_kanban_writer
from automation.tests.test_kanban_writer import BOARD, READY, calls, execute, row

board = test_kanban_writer.board
hermes = test_kanban_writer.hermes

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY = REPO_ROOT / "automation/examples/kanban-overlay.json"
AGENT = """
import json, sys
handoff, slice_id, exit_code = sys.argv[1], sys.argv[2], int(sys.argv[3])
if exit_code:
    sys.exit(exit_code)
json.dump({"slice_id": slice_id, "status": "done", "summary": "Nothing to change.", "files_touched": [],
           "validations_passed": [], "validations_failed": [], "proof_level": "doc-only", "missing_proof_levels": [],
           "contract_status_changes": [], "residual_risks": ["None known."], "recommended_next_slice": "",
           "recommended_next_reason": "", "repo_clean_status": "clean", "git_mirror_status": "not-checked",
           "dirty_paths_outside_scope": [], "timestamp": "2026-10-29T00:00:00Z"}, open(handoff, "w"))
"""


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    repo = tmp_path / "consumer"
    repo.mkdir()
    (repo / ".gitignore").write_text("automation/handoffs/\n", encoding="utf-8")
    shutil.copytree(REPO_ROOT / "automation/prompts", repo / "automation/prompts")  # as the consumer sync ships them
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    monkeypatch.setattr(run_next, "REPO_ROOT", repo)
    return repo


def supervise(monkeypatch, board_dir: Path, hermes_cmd: tuple[str, ...], agent: Path, exit_code: int = 0) -> int:
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} {exit_code}"
    argv = ["run_next", "--kanban-board", str(board_dir), "--kanban-overlay", str(OVERLAY), "--agent-cmd", command]
    monkeypatch.setattr(sys, "argv", [*argv, "--autonomous-limit", "1", "--hermes", shlex.join(hermes_cmd)])
    return run_next._cli_entry()


@pytest.fixture
def agent(tmp_path):
    path = tmp_path / "agent.py"
    path.write_text(AGENT, encoding="utf-8")
    return path


def test_a_done_slice_is_claimed_then_sent_to_review(consumer, board, hermes, agent, monkeypatch, capsys, tmp_path):
    assert supervise(monkeypatch, board, hermes, agent) == 0

    report = json.loads(capsys.readouterr().out)
    assert (report["slice_id"], report["queue_status"]) == (f"kanban.{BOARD}.{READY}", "done")
    assert row(board, "SELECT status FROM tasks WHERE id = ?", READY) == ("review",)
    assert [call["argv"][3] for call in calls(tmp_path)] == ["claim", "request-review"]
    assert calls(tmp_path)[0]["argv"][-1] == str(1800 + run_next.CLAIM_MARGIN_SECONDS)
    assert not (consumer / "automation/queue").exists()  # no JSON queue is written beside the board


def test_a_failed_agent_blocks_the_task_without_a_handoff(consumer, board, hermes, agent, monkeypatch, capsys, tmp_path):
    assert supervise(monkeypatch, board, hermes, agent, exit_code=3) == 3

    assert json.loads(capsys.readouterr().out)["decision"] == "stop_failed"
    assert row(board, "SELECT status FROM tasks WHERE id = ?", READY) == ("blocked",)
    block = calls(tmp_path)[-1]["argv"]
    assert block[3:7] == ["block", READY, "--kind", "transient"] and block[7].endswith("(handoff sha256 none)")


def test_a_refused_claim_stops_before_the_agent_runs(consumer, board, hermes, agent, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_HERMES_MODE", "refuse")

    assert supervise(monkeypatch, board, hermes, agent) == 2

    assert "stop: hermes kanban claim refused" in capsys.readouterr().err
    assert row(board, "SELECT status FROM tasks WHERE id = ?", READY) == ("ready",)


def test_board_and_overlay_go_together(consumer, board, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_next", "--kanban-board", str(board)])

    assert run_next._cli_entry() == 2
    assert "go together" in capsys.readouterr().err


def test_an_unblock_in_kanban_is_the_retry(consumer, board, hermes, agent, monkeypatch, capsys, tmp_path):
    assert supervise(monkeypatch, board, hermes, agent, exit_code=3) == 3
    execute(board, f"UPDATE tasks SET status = 'ready' WHERE id = '{READY}';")  # a person unblocks it
    capsys.readouterr()

    assert supervise(monkeypatch, board, hermes, agent) == 0

    with sqlite3.connect(board / "kanban.db") as conn:
        runs = conn.execute("SELECT id, outcome FROM task_runs WHERE task_id = ? ORDER BY id", (READY,)).fetchall()
    assert runs == [(1, "blocked"), (2, "review_requested")]
    assert row(board, "SELECT status FROM tasks WHERE id = ?", READY) == ("review",)


def test_a_dry_run_never_writes_to_the_board(consumer, board, hermes, agent, monkeypatch, capsys, tmp_path):
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} 0"
    argv = ["run_next", "--kanban-board", str(board), "--kanban-overlay", str(OVERLAY), "--agent-cmd", command]
    monkeypatch.setattr(sys, "argv", [*argv, "--hermes", shlex.join(hermes), "--dry-run"])

    assert run_next._cli_entry() == 0

    assert json.loads(capsys.readouterr().out)["selected_slice"] == f"kanban.{BOARD}.{READY}"
    assert calls(tmp_path) == []
    assert row(board, "SELECT status FROM tasks WHERE id = ?", READY) == ("ready",)


def test_a_board_edited_after_projection_is_never_claimed(consumer, board, hermes, agent, monkeypatch, capsys, tmp_path):
    real_build = run_next.build_context_bundle

    def build_then_edit(**kwargs):
        execute(board, "UPDATE tasks SET priority = 9 WHERE id = 't_000000a1';")  # someone edits the board
        return real_build(**kwargs)

    monkeypatch.setattr(run_next, "build_context_bundle", build_then_edit)

    assert supervise(monkeypatch, board, hermes, agent) == 2

    assert "not the expected" in capsys.readouterr().err  # StaleRevisionError, before any write
    assert calls(tmp_path) == []
    assert row(board, "SELECT status FROM tasks WHERE id = ?", READY) == ("ready",)


def test_another_running_task_stops_the_supervisor(consumer, board, hermes, agent, monkeypatch, capsys, tmp_path):
    execute(board, "UPDATE tasks SET status = 'running' WHERE id = 't_000000a2';")

    assert supervise(monkeypatch, board, hermes, agent) == 2

    assert "already has in_progress slice" in capsys.readouterr().err
    assert calls(tmp_path) == []
