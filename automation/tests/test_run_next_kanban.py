"""End-to-end tests for ``run_next`` taking slices from a Kanban board (RA-CP-005).

Each test runs ``run_next.main`` in a throwaway git repository against the synthetic board and
the Hermes stand-in from ``test_kanban_writer``, so nothing touches ``~/.hermes`` or a real board.
The agent is a small script that writes the handoff the supervisor then judges.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from automation.supervisor import kanban_projection as kp
from automation.supervisor import run_next
from automation.tests import test_kanban_writer as writer_tests
from automation.tests.test_kanban_writer import READY, calls, execute, row

# The writer tests' synthetic board and Hermes stand-in, shared as fixtures.
board = writer_tests.board
hermes = writer_tests.hermes

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY = REPO_ROOT / "automation/examples/kanban-overlay.json"
AGENT = r"""
import json, os, subprocess, sys
handoff, slice_id, repo = sys.argv[1:4]
open(os.path.join(repo, "agent-ran"), "a").write(slice_id + "\n")
code = int(os.environ.get("FAKE_AGENT_EXIT", "0"))
if code:
    sys.exit(code)
os.makedirs(os.path.join(repo, "src"), exist_ok=True)
with open(os.path.join(repo, "src/loop.py"), "a") as handle:
    handle.write("# faster\n")
subprocess.run(["git", "-C", repo, "add", "src/loop.py"], check=True)
subprocess.run(["git", "-C", repo, "commit", "-qm", "speed up the loop"], check=True)
json.dump({
    "slice_id": slice_id, "status": "done", "summary": "Sped up the loop.", "files_touched": ["src/loop.py"],
    "validations_passed": [], "validations_failed": [], "proof_level": "domain-tested",
    "missing_proof_levels": [], "contract_status_changes": [], "residual_risks": ["none observed"],
    "recommended_next_slice": "", "recommended_next_reason": "", "repo_clean_status": "clean",
    "git_mirror_status": "not-checked", "dirty_paths_outside_scope": [], "timestamp": "2026-10-29T00:00:00Z",
}, open(handoff, "w"))
"""


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "automation/prompts", root / "automation/prompts")
    (root / ".gitignore").write_text("automation/handoffs/\nagent-ran\n", encoding="utf-8")
    for command in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *command], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT, encoding="utf-8")
    return root


def run(repo: Path, board_dir: Path, hermes_cmd, *extra: str) -> int:
    agent_cmd = f"{sys.executable} {repo.parent / 'agent.py'} {{handoff_file}} {{slice_id}} {{repo_root}}"
    argv = [
        "--kanban-board",
        str(board_dir),
        "--kanban-overlay",
        str(OVERLAY),
        "--hermes",
        " ".join(hermes_cmd),
        "--agent-cmd",
        agent_cmd,
        "--autonomous-limit",
        "1",
        *extra,
    ]
    return run_next.main(argv, repo_root=repo)


def task(board_dir: Path, task_id: str = READY) -> tuple:
    return row(board_dir, "SELECT status, current_run_id FROM tasks WHERE id = ?", task_id)


def outcomes(board_dir: Path) -> list[tuple]:
    with kp.read_only(board_dir) as conn:
        return [tuple(r) for r in conn.execute("SELECT id, task_id, outcome FROM task_runs ORDER BY id")]


def test_a_validated_slice_claims_the_task_and_asks_for_review(repo, board, hermes, tmp_path, capsys):
    assert run(repo, board, hermes) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["slice_id"] == kp.slice_id_for("example-board", READY)
    assert report["queue_status"] == "done"
    # A supervisor done is Kanban review, never done; a person approves it there.
    assert task(board) == ("review", None)
    assert outcomes(board) == [(1, READY, "review_requested")]
    assert [call["argv"][3:5] for call in calls(tmp_path)] == [["claim", READY], ["request-review", READY]]
    # The unapproved, higher-priority ready card was never claimed.
    assert task(board, "t_000000a6") == ("ready", None)
    assert not (repo / "automation/queue/slices.json").exists()


def test_a_failed_agent_blocks_the_task_and_a_human_unblock_retries_it(repo, board, hermes, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_AGENT_EXIT", "3")
    assert run(repo, board, hermes) == 3
    assert task(board) == ("blocked", None)
    assert outcomes(board) == [(1, READY, "blocked")]
    assert "handoff sha256 absent" in calls(tmp_path)[-1]["argv"][-1]

    # Unblocking in Kanban is the retry: the next run claims a fresh run and closes only that one.
    execute(board, f"UPDATE tasks SET status = 'ready' WHERE id = '{READY}';")
    monkeypatch.delenv("FAKE_AGENT_EXIT")
    capsys.readouterr()
    assert run(repo, board, hermes) == 0
    assert outcomes(board) == [(1, READY, "blocked"), (2, READY, "review_requested")]


def test_dry_run_reports_the_revision_and_writes_nothing(repo, board, hermes, tmp_path, capsys):
    assert run(repo, board, hermes, "--dry-run") == 0

    report = json.loads(capsys.readouterr().out)
    assert report["selected_slice"] == kp.slice_id_for("example-board", READY)
    assert report["kanban_revision"] == kp.read_board(board).revision
    assert calls(tmp_path) == []
    assert task(board) == ("ready", None)


def test_a_board_edited_after_projection_is_never_claimed(repo, board, hermes, tmp_path, monkeypatch, capsys):
    real_build = run_next.build_context_bundle

    def build_then_edit(**kwargs):
        execute(board, "UPDATE tasks SET priority = 9 WHERE id = 't_000000a1';")
        return real_build(**kwargs)

    monkeypatch.setattr(run_next, "build_context_bundle", build_then_edit)

    assert run(repo, board, hermes) == 2

    assert "stop: kanban:" in capsys.readouterr().err
    assert calls(tmp_path) == []
    assert not (repo / "agent-ran").exists()


def test_a_refused_claim_stops_before_the_agent_runs(repo, board, hermes, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_HERMES_MODE", "refuse")

    assert run(repo, board, hermes) == 2

    assert "refused" in capsys.readouterr().err
    assert not (repo / "agent-ran").exists()
    assert task(board) == ("ready", None)


def test_a_task_running_elsewhere_stops_the_supervisor(repo, board, hermes, tmp_path, capsys):
    execute(board, "UPDATE tasks SET status = 'running' WHERE id = 't_000000a2';")

    assert run(repo, board, hermes) == 2

    assert "already has in_progress slice" in capsys.readouterr().err
    assert calls(tmp_path) == []


def test_board_and_overlay_go_together():
    with pytest.raises(SystemExit):
        run_next.parse_args(["--kanban-board", "/nowhere"])


def test_without_a_board_the_json_queue_stays_the_authority(repo, tmp_path, capsys):
    queue = json.loads(OVERLAY.read_text(encoding="utf-8"))
    slice_record = {
        "slice_id": "speed-up-loop",
        "title": "Speed up the hot loop",
        "status": "queued",
        "priority": 0,
        **queue["approved"]["t_000000a3"],
        "depends_on": [],
        "notes": "",
    }
    queue_path = repo / "automation/queue/slices.json"
    queue_path.parent.mkdir(parents=True)
    queue_path.write_text(json.dumps({"version": 1, "policy": queue["policy"], "slices": [slice_record]}), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "automation/queue/slices.json"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "queue"], check=True)
    agent_cmd = f"{sys.executable} {tmp_path / 'agent.py'} {{handoff_file}} {{slice_id}} {{repo_root}}"

    assert run_next.main(["--agent-cmd", agent_cmd, "--autonomous-limit", "1"], repo_root=repo) == 0

    assert json.loads(capsys.readouterr().out)["queue_status"] == "done"
    assert json.loads(queue_path.read_text(encoding="utf-8"))["slices"][0]["status"] == "done"
