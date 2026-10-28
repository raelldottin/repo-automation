"""CodSpeed benchmarks for the reusable supervisor.

These benchmarks use the ``pytest-codspeed`` benchmark fixture for precise
measurement of the core harness paths CodSpeed should track across commits.
They stay intentionally light: each benchmark exercises bundled examples and
asserts the measured result without touching consumer repository state.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from automation.context import memory
from automation.context.build_context import build_context_bundle
from automation.schemas import models
from automation.supervisor import kanban_projection, policy


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_QUEUE = REPO_ROOT / "automation/examples/example-slices.json"
EXAMPLE_HANDOFF = REPO_ROOT / "automation/examples/example-handoff.json"
EXAMPLE_KANBAN_BOARD = REPO_ROOT / "automation/examples/kanban-board.sql"
EXAMPLE_KANBAN_OVERLAY = REPO_ROOT / "automation/examples/kanban-overlay.json"
EXAMPLE_MEMORY_CONFIG = REPO_ROOT / "automation/examples/memory-config.json"
EXAMPLE_MEMORY_RECALL = REPO_ROOT / "automation/examples/memory-recall-response.json"


@pytest.mark.benchmark
def test_load_queue_benchmark(benchmark):
    queue_data = benchmark(policy.load_queue, EXAMPLE_QUEUE)

    assert queue_data["version"] == 1
    assert len(queue_data["slices"]) == 3


@pytest.mark.benchmark
def test_select_next_slice_benchmark(benchmark):
    queue_data = policy.load_queue(EXAMPLE_QUEUE)
    selected_slice = benchmark(policy.select_next_slice, queue_data)

    assert selected_slice is not None
    assert selected_slice["slice_id"] == "today-continue-ui-regression-coverage"


@pytest.mark.benchmark
def test_validate_handoff_benchmark(benchmark):
    handoff = policy.load_json(EXAMPLE_HANDOFF)
    handoff_schema = models.Handoff

    validation = benchmark(policy.validate_document, handoff, handoff_schema)

    assert validation.is_valid
    assert validation.errors == []


@pytest.mark.benchmark
def test_build_context_bundle_benchmark(benchmark, tmp_path):
    queue_data = policy.load_queue(EXAMPLE_QUEUE)
    selected_slice = policy.select_next_slice(queue_data)
    assert selected_slice is not None
    slice_id = selected_slice["slice_id"]
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir()
    shutil.copy2(
        EXAMPLE_HANDOFF,
        handoff_dir / "20260421T153000Z-today-nonfocus-add-to-focus.json",
    )

    def run():
        return build_context_bundle(
            repo_root=REPO_ROOT,
            queue_path=EXAMPLE_QUEUE,
            handoff_dir=handoff_dir,
            slice_id=slice_id,
            max_doc_chars=200,
        )

    context_bundle = benchmark(run)

    assert context_bundle["slice"]["slice_id"] == slice_id
    assert context_bundle["previous_handoff"]["slice_id"] == "today-nonfocus-add-to-focus"


@pytest.mark.benchmark
def test_project_kanban_board_benchmark(benchmark, tmp_path):
    board_dir = tmp_path / "example-board"
    board_dir.mkdir()
    with sqlite3.connect(board_dir / "kanban.db") as conn:
        conn.executescript(EXAMPLE_KANBAN_BOARD.read_text(encoding="utf-8"))
    overlay = kanban_projection.load_overlay(EXAMPLE_KANBAN_OVERLAY)

    def run():
        return kanban_projection.project(kanban_projection.read_board(board_dir), overlay)

    projection = benchmark(run)

    assert len(projection.queue["slices"]) == 6
    selected_slice = policy.select_next_slice(projection.queue)
    assert selected_slice is not None
    assert selected_slice["slice_id"] == "kanban.example-board.t_000000a3"


@pytest.mark.benchmark
def test_shape_memory_recall_benchmark(benchmark):
    budgets = memory.load_config(EXAMPLE_MEMORY_CONFIG).budgets
    response = json.loads(EXAMPLE_MEMORY_RECALL.read_text(encoding="utf-8"))

    shaped = benchmark(memory.shape_recall, response, REPO_ROOT, budgets)

    assert [item["id"] for item in shaped["results"]] == ["fact-001", "fact-002"]
    assert [item["id"] for item in shaped["rejected"]] == ["fact-003", "fact-004", "fact-005", "fact-006"]
