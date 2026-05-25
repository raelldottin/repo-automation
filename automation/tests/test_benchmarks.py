"""CodSpeed benchmarks for the reusable supervisor.

These benchmarks use the ``pytest-codspeed`` benchmark fixture for precise
measurement of the core harness paths CodSpeed should track across commits.
They stay intentionally light: each benchmark exercises bundled examples and
asserts the measured result without touching consumer repository state.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from automation.context.build_context import build_context_bundle
from automation.supervisor import policy


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_QUEUE = REPO_ROOT / "automation/examples/example-slices.json"
EXAMPLE_HANDOFF = REPO_ROOT / "automation/examples/example-handoff.json"
SLICE_SCHEMA = REPO_ROOT / "automation/schemas/slice.schema.json"
HANDOFF_SCHEMA = REPO_ROOT / "automation/schemas/handoff.schema.json"


@pytest.mark.benchmark
def test_load_queue_benchmark(benchmark):
    queue_data = benchmark(policy.load_queue, EXAMPLE_QUEUE, SLICE_SCHEMA)

    assert queue_data["version"] == 1
    assert len(queue_data["slices"]) == 3


@pytest.mark.benchmark
def test_select_next_slice_benchmark(benchmark):
    queue_data = policy.load_queue(EXAMPLE_QUEUE, SLICE_SCHEMA)
    selected_slice = benchmark(policy.select_next_slice, queue_data)

    assert selected_slice is not None
    assert selected_slice["slice_id"] == "today-continue-ui-regression-coverage"


@pytest.mark.benchmark
def test_validate_handoff_benchmark(benchmark):
    handoff = policy.load_json(EXAMPLE_HANDOFF)
    handoff_schema = policy.load_schema(HANDOFF_SCHEMA)

    validation = benchmark(policy.validate_document, handoff, handoff_schema)

    assert validation.is_valid
    assert validation.errors == []


@pytest.mark.benchmark
def test_build_context_bundle_benchmark(benchmark, tmp_path):
    queue_data = policy.load_queue(EXAMPLE_QUEUE, SLICE_SCHEMA)
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
