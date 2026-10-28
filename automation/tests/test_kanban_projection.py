"""Contract tests for the read-only Kanban projection (RA-CP-002).

Every board here is built from the synthetic ``automation/examples/kanban-board.sql`` so the
tests never touch ``~/.hermes`` or a real board.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from automation.supervisor import kanban_projection as kp
from automation.supervisor import policy

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_SQL = REPO_ROOT / "automation/examples/kanban-board.sql"
OVERLAY = REPO_ROOT / "automation/examples/kanban-overlay.json"
BOARD = "example-board"


def sid(task_id: str) -> str:
    return kp.slice_id_for(BOARD, task_id)


def make_board(root: Path, extra_sql: str = "", board: str = BOARD) -> Path:
    board_dir = root / board
    board_dir.mkdir(parents=True)
    with sqlite3.connect(board_dir / "kanban.db") as conn:
        conn.executescript(BOARD_SQL.read_text(encoding="utf-8") + extra_sql)
    return board_dir


def execute(board_dir: Path, sql: str) -> None:
    with sqlite3.connect(board_dir / "kanban.db") as conn:
        conn.executescript(sql)


def overlay_with(**changes: object) -> kp.KanbanOverlay:
    document = json.loads(OVERLAY.read_text(encoding="utf-8"))
    document.update(changes)
    return kp.KanbanOverlay.model_validate(document)


def by_id(projection: kp.Projection) -> dict[str, dict]:
    return {record["slice_id"]: record for record in projection.queue["slices"]}


def test_projection_maps_status_priority_and_dependencies(tmp_path):
    projection = kp.project(kp.read_board(make_board(tmp_path)), kp.load_overlay(OVERLAY))
    slices = by_id(projection)

    assert [record["slice_id"] for record in projection.queue["slices"]] == [
        sid("t_000000a3"),
        sid("t_000000a1"),
        sid("t_000000a2"),
        sid("t_000000a4"),
        sid("t_000000a7"),
        sid("t_000000a8"),
    ]
    assert {key: record["status"] for key, record in slices.items()} == {
        sid("t_000000a1"): "done",
        sid("t_000000a2"): "queued",
        sid("t_000000a3"): "queued",
        sid("t_000000a4"): "blocked",
        sid("t_000000a7"): "blocked",
        sid("t_000000a8"): "deferred",
    }
    # Kanban's highest priority becomes the queue's lowest number.
    assert slices[sid("t_000000a3")]["priority"] == 0
    assert slices[sid("t_000000a2")]["priority"] == 1
    # The archived parent counts as satisfied and drops out, as recompute_ready treats it.
    assert slices[sid("t_000000a2")]["depends_on"] == [sid("t_000000a1")]
    assert slices[sid("t_000000a4")]["entry_condition"] == kp.STATUS_MAP["todo"][1]
    assert slices[sid("t_000000a8")]["entry_condition"] == "Vendor ships the upstream fix."
    assert slices[sid("t_000000a2")]["required_proof_level"] == "domain-tested"
    assert "required_proof_level" not in slices[sid("t_000000a3")]


def test_queue_picks_the_task_kanban_would_dispatch_first(tmp_path):
    projection = kp.project(kp.read_board(make_board(tmp_path)), kp.load_overlay(OVERLAY))

    selected = policy.select_next_slice(projection.queue)

    assert selected is not None and selected["slice_id"] == sid("t_000000a3")


def test_projected_queue_round_trips_through_load_queue(tmp_path):
    projection = kp.project(kp.read_board(make_board(tmp_path)), kp.load_overlay(OVERLAY))
    queue_path = tmp_path / "slices.json"
    policy.write_json(queue_path, projection.queue)

    assert policy.load_queue(queue_path) == projection.queue


def test_unapproved_card_is_never_projected(tmp_path):
    projection = kp.project(kp.read_board(make_board(tmp_path)), kp.load_overlay(OVERLAY))

    assert sid("t_000000a6") not in by_id(projection)
    # Its approved child is held instead of being left runnable behind an invisible parent.
    child = by_id(projection)[sid("t_000000a7")]
    assert child["status"] == "blocked"
    assert child["depends_on"] == []
    assert "t_000000a6" in child["entry_condition"]


def test_creator_is_provenance_not_a_dependency(tmp_path):
    projection = kp.project(kp.read_board(make_board(tmp_path)), kp.load_overlay(OVERLAY))

    assert projection.provenance[sid("t_000000a3")]["spawned_by"] == "t_000000a1"
    assert by_id(projection)[sid("t_000000a3")]["depends_on"] == []
    # A malformed payload yields no provenance rather than an error.
    assert projection.provenance[sid("t_000000a8")]["spawned_by"] == ""


def test_repeat_projection_is_byte_identical(tmp_path):
    board_dir = make_board(tmp_path)
    first = kp.project(kp.read_board(board_dir), kp.load_overlay(OVERLAY))
    second = kp.project(kp.read_board(board_dir), kp.load_overlay(OVERLAY))

    assert json.dumps(first.queue, sort_keys=True) == json.dumps(second.queue, sort_keys=True)
    assert first.provenance == second.provenance
    assert first.revision == second.revision


def test_slice_id_traces_back_to_board_and_task():
    assert kp.parse_slice_id(sid("t_000000a2")) == (BOARD, "t_000000a2")
    assert kp.parse_slice_id("today-continue-ui-regression-coverage") is None
    assert kp.parse_slice_id("kanban.Bad Board.t_000000a2") is None
    assert kp.parse_slice_id("kanban.example-board.t_xyz") is None


def test_unknown_kanban_status_is_refused(tmp_path):
    board_dir = make_board(tmp_path, "UPDATE tasks SET status = 'failed' WHERE id = 't_000000a3';")

    with pytest.raises(kp.ProjectionError, match="unknown Kanban status 'failed'"):
        kp.project(kp.read_board(board_dir), kp.load_overlay(OVERLAY))


def test_two_running_tasks_cannot_become_one_queue(tmp_path):
    board_dir = make_board(tmp_path, "UPDATE tasks SET status = 'running' WHERE id IN ('t_000000a2', 't_000000a3');")

    with pytest.raises(kp.ProjectionError, match="at most one slice may be in_progress"):
        kp.project(kp.read_board(board_dir), kp.load_overlay(OVERLAY))


def test_stale_revision_is_refused(tmp_path):
    board_dir = make_board(tmp_path)
    expected = kp.read_board(board_dir).revision
    execute(board_dir, "UPDATE tasks SET status = 'running' WHERE id = 't_000000a3';")

    with pytest.raises(kp.StaleRevisionError):
        kp.require_revision(kp.read_board(board_dir), expected)


def test_concurrent_edit_leaves_the_snapshot_intact_and_moves_the_revision(tmp_path):
    board_dir = make_board(tmp_path)
    before = kp.read_board(board_dir)
    execute(board_dir, "INSERT INTO task_links VALUES ('t_000000a3', 't_000000a7');")
    after = kp.read_board(board_dir)

    assert ("t_000000a3", "t_000000a7") not in before.links
    assert ("t_000000a3", "t_000000a7") in after.links
    assert before.revision != after.revision
    kp.require_revision(after, after.revision)


def test_heartbeat_style_events_do_not_move_the_revision(tmp_path):
    board_dir = make_board(tmp_path)
    before = kp.read_board(board_dir)
    execute(
        board_dir, "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_000000a3', 'heartbeat', '{}', 900);"
    )
    after = kp.read_board(board_dir)

    assert after.event_watermark == before.event_watermark + 1
    assert after.revision == before.revision


def test_overlay_for_a_foreign_board_is_refused(tmp_path):
    snapshot = kp.read_board(make_board(tmp_path))

    with pytest.raises(kp.ProjectionError, match="not 'example-board'"):
        kp.project(snapshot, overlay_with(board="other-board"))


def test_overlay_approving_a_task_from_another_board_is_refused(tmp_path):
    document = json.loads(OVERLAY.read_text(encoding="utf-8"))
    document["approved"]["t_0000ffff"] = document["approved"]["t_000000a1"]

    with pytest.raises(kp.ProjectionError, match="not on board"):
        kp.project(kp.read_board(make_board(tmp_path)), kp.KanbanOverlay.model_validate(document))


def test_overlay_must_carry_the_policy_fields(tmp_path):
    document = json.loads(OVERLAY.read_text(encoding="utf-8"))
    document["approved"]["t_000000a1"]["allowed_paths"] = []
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(kp.ProjectionError, match="allowed_paths"):
        kp.load_overlay(path)


def test_missing_schema_column_is_refused(tmp_path):
    board_dir = make_board(tmp_path, "ALTER TABLE tasks DROP COLUMN priority;")

    with pytest.raises(kp.ProjectionError, match="lacks columns \\['priority'\\]"):
        kp.read_board(board_dir)


def test_board_directory_must_be_a_slug(tmp_path):
    with pytest.raises(kp.ProjectionError, match="not a Kanban board slug"):
        kp.read_board(make_board(tmp_path, board="Not A Slug"))


def test_reading_a_board_writes_nothing(tmp_path):
    board_dir = make_board(tmp_path)
    db = board_dir / "kanban.db"
    contents = db.read_bytes()
    db.chmod(stat.S_IRUSR)
    board_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        kp.project(kp.read_board(board_dir), kp.load_overlay(OVERLAY))
    finally:
        board_dir.chmod(stat.S_IRWXU)
        db.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert db.read_bytes() == contents
    assert sorted(os.listdir(board_dir)) == ["kanban.db"]


def test_cli_prints_the_projection_and_fails_cleanly(tmp_path, capsys):
    board_dir = make_board(tmp_path)

    assert kp.main(["--board-dir", str(board_dir), "--overlay", str(OVERLAY)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["revision"] == kp.read_board(board_dir).revision
    assert len(output["queue"]["slices"]) == 6

    assert kp.main(["--board-dir", str(tmp_path / "missing"), "--overlay", str(OVERLAY)]) == 1
    assert "missing board database" in capsys.readouterr().err
