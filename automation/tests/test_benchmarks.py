"""CodSpeed benchmarks for the reusable supervisor.

Benchmarks are pytest fixtures with the ``benchmark`` marker provided by
``pytest-codspeed``. CodSpeed runs each benchmark function under instrumentation
and reports performance deltas across commits.

These benchmarks are intentionally light: they exercise the schema loader,
context bundle builder, and policy selection helpers without external I/O
beyond reading bundled example payloads.
"""

from __future__ import annotations

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
    benchmark(policy.load_queue, EXAMPLE_QUEUE, SLICE_SCHEMA)


@pytest.mark.benchmark
def test_select_next_slice_benchmark(benchmark):
    queue_data = policy.load_queue(EXAMPLE_QUEUE, SLICE_SCHEMA)
    benchmark(policy.select_next_slice, queue_data)


@pytest.mark.benchmark
def test_build_context_bundle_benchmark(benchmark, tmp_path):
    queue_data = policy.load_queue(EXAMPLE_QUEUE, SLICE_SCHEMA)
    slice_id = queue_data["slices"][0]["slice_id"]
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir()

    def run():
        return build_context_bundle(
            repo_root=REPO_ROOT,
            queue_path=EXAMPLE_QUEUE,
            handoff_dir=handoff_dir,
            slice_id=slice_id,
            max_doc_chars=200,
        )

    benchmark(run)
