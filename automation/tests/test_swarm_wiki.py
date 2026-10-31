"""Tests for the pinned Swarm Wiki compiler wrapper (RA-CP-004).

The unit tests run a stand-in compiler in a temporary git checkout. The end-to-end tests run the
real pinned compiler and are skipped unless ``SWARM_WIKI_COMPILER`` names a checkout of it (CI's
``Swarm Wiki compiler`` workflow sets it). Every input is synthetic and every output is written
into pytest's temporary directories.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from automation.context import memory, swarm_wiki

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/swarm-wiki-compiler.yml"
STAND_IN = """
import json, sys
if "--crash" in sys.argv:
    print("swarm-wiki: boom", file=sys.stderr)
    raise SystemExit(2)
print(json.dumps({"argv": sys.argv[1:], "matches": True}))
print(json.dumps({"class": "stale-memory-source", "target": "s.json#m1"}), file=sys.stderr)
print("plain warning", file=sys.stderr)
raise SystemExit(1 if "--unresolved" in sys.argv else 0)
"""


def git(checkout: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    checkout = tmp_path / "compiler"
    (checkout / "ops").mkdir(parents=True)
    (checkout / swarm_wiki.COMPILER_ENTRY).write_text(STAND_IN, encoding="utf-8")
    git(checkout, "init", "-q")
    git(checkout, "add", "-A")
    git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "stand-in")
    monkeypatch.setattr(swarm_wiki, "COMPILER_COMMIT", git(checkout, "rev-parse", "HEAD"))
    return checkout


def test_pinned_build_returns_the_compiler_evidence(stand_in):
    evidence = swarm_wiki.run(stand_in, ["--board", "b"], check=False)

    assert evidence["compiler"] == {
        "repository": "raelldottin/hermes-swarm-protocol",
        "commit": swarm_wiki.COMPILER_COMMIT,
        "entry": "ops/build_wiki.py",
    }
    assert evidence["mode"] == "build" and evidence["argv"] == ["--board", "b"] and evidence["exit_code"] == 0
    assert evidence["report"] == {"argv": ["--board", "b"], "matches": True}
    assert evidence["findings"] == [{"class": "stale-memory-source", "target": "s.json#m1"}]
    assert evidence["errors"] == ["plain warning"]


def test_check_mode_adds_the_check_flag_and_keeps_the_exit_code(stand_in):
    evidence = swarm_wiki.run(stand_in, ["--unresolved"], check=True)

    assert evidence["mode"] == "check" and evidence["argv"] == ["--unresolved", "--check"]
    assert evidence["exit_code"] == 1


def test_compiler_errors_are_recorded_not_parsed(stand_in):
    evidence = swarm_wiki.run(stand_in, ["--crash"], check=False)

    assert evidence["exit_code"] == 2 and evidence["report"] is None
    assert evidence["errors"] == ["swarm-wiki: boom"] and evidence["findings"] == []


def test_another_commit_is_refused_before_running(stand_in, monkeypatch):
    monkeypatch.setattr(swarm_wiki, "COMPILER_COMMIT", "0" * 40)

    with pytest.raises(swarm_wiki.CompilerPinError, match="not the pinned"):
        swarm_wiki.run(stand_in, [], check=False)


@pytest.mark.parametrize("change", ["untracked", "edited"])
def test_a_checkout_with_local_changes_is_refused(stand_in, change):
    if change == "untracked":
        (stand_in / "extra.py").write_text("", encoding="utf-8")
    else:
        (stand_in / swarm_wiki.COMPILER_ENTRY).write_text("print('patched')\n", encoding="utf-8")

    with pytest.raises(swarm_wiki.CompilerPinError, match="local changes"):
        swarm_wiki.run(stand_in, [], check=False)


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path):
    with pytest.raises(swarm_wiki.CompilerPinError, match="not a readable git checkout"):
        swarm_wiki.verify_compiler(tmp_path)


def test_the_operator_cannot_pass_the_wrappers_own_flags(stand_in):
    with pytest.raises(swarm_wiki.CompilerPinError, match="check subcommand"):
        swarm_wiki.run(stand_in, ["--check"], check=False)


def test_a_compiler_that_hangs_is_stopped(stand_in):
    with pytest.raises(swarm_wiki.CompilerPinError, match="did not finish"):
        swarm_wiki.run(stand_in, [], check=False, python=sys.executable, timeout=0.000001)


def test_cli_writes_evidence_and_returns_the_compiler_exit_code(stand_in, tmp_path, capsys):
    evidence_path = tmp_path / "evidence.json"

    code = swarm_wiki.main(["check", "--compiler", str(stand_in), "--evidence", str(evidence_path), "--", "--unresolved"])

    assert code == 1
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["argv"] == ["--unresolved", "--check"]
    assert swarm_wiki.main(["build", "--compiler", str(tmp_path / "missing")]) == 2
    assert "stop:" in capsys.readouterr().err


def test_ci_runs_the_same_pinned_commit():
    assert f"ref: {swarm_wiki.COMPILER_COMMIT}" in WORKFLOW.read_text(encoding="utf-8")


# End-to-end with the real pinned compiler.

COMPILER = os.environ.get("SWARM_WIKI_COMPILER")
needs_compiler = pytest.mark.skipif(not COMPILER, reason="SWARM_WIKI_COMPILER is not set")
STATUSES = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]
SOURCE = "docs/decision.md"


@pytest.fixture
def board(tmp_path):
    """A synthetic project: one Kanban task, one ByteRover node, one source file, one snapshot root."""
    workspace = tmp_path / "workspace"
    (workspace / ".swarm").mkdir(parents=True)
    (workspace / ".swarm/SWARM_WIKI.md").write_text("# Rules\n", encoding="utf-8")
    (workspace / "docs").mkdir()
    (workspace / SOURCE).write_text("Decision: issuer lookup is tenant scoped.\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "node.md").write_text("---\nauthor: a\ncreatedAt: x\n---\nByteRover node\n", encoding="utf-8")
    hermes = tmp_path / "hermes"
    (hermes / "hermes_cli").mkdir(parents=True)
    (hermes / "hermes_cli/__init__.py").write_text("", encoding="utf-8")
    (hermes / "hermes_cli/kanban_db.py").write_text(f"VALID_STATUSES = {STATUSES!r}\n", encoding="utf-8")
    db = tmp_path / "kanban.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, created_by TEXT,
              created_at INTEGER, assignee TEXT, completed_at INTEGER, result TEXT, workspace_path TEXT);
            CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
            CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
            CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, run_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
            CREATE TABLE task_runs(id TEXT PRIMARY KEY, task_id TEXT, profile TEXT, started_at INTEGER,
              ended_at INTEGER, summary TEXT, error TEXT, status TEXT, outcome TEXT);
            """
        )
        conn.execute(
            "INSERT INTO tasks VALUES ('t1', 't1 objective', '', 'running', 'builder', 10, 'builder', NULL, NULL, ?)",
            (str(workspace),),
        )
    snapshots = tmp_path / "hindsight"
    snapshots.mkdir()
    out = tmp_path / "wiki"
    args = [
        "--board", "fixture", "--project", "p", "--workspace", str(workspace), "--db", str(db),
        "--tree", str(tree), "--rules", str(workspace / ".swarm/SWARM_WIKI.md"), "--hermes-source", str(hermes),
        "--maintainer", "maintainer", "--memory", str(snapshots), "--memory-bank", "p-bank", "--out", str(out),
    ]  # fmt: skip
    return {"workspace": workspace, "snapshots": snapshots, "out": out, "args": args}


def capture(board: dict, metadata: dict, total: int = 1) -> Path:
    document = {
        "format": memory.SNAPSHOT_FORMAT, "project": "p", "bank_id": "p-bank", "captured_at": "2026-10-29T00:00:00Z",
        "captured_by": "operator", "hindsight_revision": memory.HINDSIGHT_REVISION,
        "memories": [{"id": "m1", "text": "Issuer lookup is tenant scoped", "fact_type": "world",
                      "metadata": metadata, "state": "valid"}],
        "total": total,
    }  # fmt: skip
    return memory.write_snapshot(document, board["snapshots"])


def compile_wiki(board: dict, check: bool) -> dict:
    python = os.environ.get("SWARM_WIKI_PYTHON", sys.executable)
    return swarm_wiki.run(Path(os.environ["SWARM_WIKI_COMPILER"]), board["args"], check=check, python=python)


def snapshot_findings(evidence: dict, snapshot: Path) -> set[str]:
    return {f["class"] for f in evidence["findings"] if f["target"].startswith(snapshot.name)}


def unresolved(evidence: dict) -> set[tuple[str, str]]:
    return {(f["class"], f["target"]) for f in evidence["findings"] if f.get("outcome") == "unresolved"}


@needs_compiler
def test_pinned_compiler_builds_and_checks_a_hindsight_backed_wiki(board):
    digest = memory.sha256_file(board["workspace"] / SOURCE)
    snapshot = capture(board, {"source_path": SOURCE, "source_sha256": digest, "evidence_ref": "commit:abc1234"})

    built = compile_wiki(board, check=False)
    checked = compile_wiki(board, check=True)

    # The synthetic ByteRover node is linked to no Kanban work, so the compiler reports it and
    # exits 1. That finding is expected; nothing about the snapshot may be unresolved.
    assert built["exit_code"] == 1 and built["report"]["matches"] is True, built
    assert unresolved(built) == {("orphan-topic", "node.md")}
    assert snapshot_findings(built, snapshot) == set()
    pages = {path.relative_to(board["out"]).as_posix() for path in board["out"].rglob("*.md")}
    assert any(page.startswith("memory/") for page in pages)
    assert any(page.startswith("architecture/knowledge-") for page in pages)
    memory_page = next(board["out"].rglob("memory/*.md")).read_text(encoding="utf-8")
    assert "UNVERIFIED" in memory_page and snapshot.name.removesuffix(".json") in memory_page
    assert checked["report"]["matches"] is True and unresolved(checked) == unresolved(built)
    assert {path: path.read_bytes() for path in board["snapshots"].iterdir()} == {snapshot: snapshot.read_bytes()}


@needs_compiler
def test_pinned_compiler_shows_invalid_provenance_and_changed_snapshots(board):
    missing = capture(board, {"source_path": SOURCE})
    changed = capture(board, {"source_path": SOURCE, "source_sha256": "0" * 64, "evidence_ref": "commit:abc1234"})
    changed.write_bytes(changed.read_bytes().replace(b"tenant scoped", b"global"))

    evidence = compile_wiki(board, check=False)

    assert "invalid-provenance" in snapshot_findings(evidence, missing)
    assert "changed-snapshot" in snapshot_findings(evidence, changed)
    pages = [page.read_text(encoding="utf-8") for page in board["out"].rglob("memory/*.md")]
    assert any("CHANGED SNAPSHOT" in page for page in pages)
    assert not any("Issuer lookup is global" in page for page in pages)


@needs_compiler
def test_pinned_compiler_flags_a_snapshot_holding_part_of_a_bank(board):
    partial = capture(board, {"source_path": SOURCE, "evidence_ref": "commit:abc1234"}, total=2)

    evidence = compile_wiki(board, check=False)

    assert "incomplete-memory-snapshot" in snapshot_findings(evidence, partial)
    assert any("INCOMPLETE SNAPSHOT" in page.read_text(encoding="utf-8") for page in board["out"].rglob("memory/*.md"))
