from __future__ import annotations

import json
import shlex
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Optional

from automation.benchmark import lanes as lanes_module
from automation.benchmark.adapter import SupervisorAgentAdapter, build_context_bundle, build_queue_data, build_slice_record
from automation.benchmark.instances import TaskSpec
from automation.benchmark.scoring import EffectivenessReport, InstanceScore
from automation.benchmark.strategies import (
    ALL_LANES,
    PLAN_ARTIFACT,
    RESEARCH_ARTIFACT,
    RPI_DIR,
    build_strategy,
)
from automation.supervisor.run_next import render_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = TaskSpec("owner__proj.abc1234", "owner/proj", "abc1234", "c", "easy")


class FakeAgent:
    """Stand in for the agent CLI: records each session and writes plausible artifacts.

    The phase is read back out of ``--slice-id``, exactly as a real run would identify
    itself, so the double exercises the same command plumbing every lane shares.
    """

    def __init__(self, write_artifacts: bool = True, returncode: int = 0) -> None:
        self.write_artifacts = write_artifacts
        self.returncode = returncode
        self.sessions: list[dict] = []

    def __call__(self, command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
        argv = shlex.split(command)
        options = dict(zip(argv, argv[1:]))
        slice_id = options["--slice-id"]
        phase = slice_id.split(":", 1)[1] if ":" in slice_id else "implement"
        prompt = Path(options["--prompt-file"]).read_text(encoding="utf-8")
        self.sessions.append({"phase": phase, "prompt": prompt, "timeout": timeout, "options": options})

        if self.write_artifacts:
            self._write_artifact(workspace, phase)
        if phase == "implement":
            (workspace / "compile.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (workspace / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
        return self.returncode

    def _write_artifact(self, workspace: Path, phase: str) -> None:
        payloads = {
            "research": (
                RESEARCH_ARTIFACT,
                {
                    "task": "rebuild owner/proj",
                    "relevant_files": [],
                    "findings": ["FINDING-MARKER: renders a grid"] * 40,
                    "constraints": ["must ship compile.sh"],
                    "unknowns": [],
                    "risks": [],
                    "evidence": [],
                },
            ),
            "research_compact": (
                "research.compact.json",
                {
                    "task": "rebuild owner/proj",
                    "relevant_files": [],
                    "findings": ["FINDING-MARKER: renders a grid"],
                    "constraints": ["must ship compile.sh"],
                    "unknowns": [],
                    "risks": [],
                    "evidence": [],
                },
            ),
            "plan": (
                PLAN_ARTIFACT,
                {
                    "goal": "PLAN-MARKER rebuild owner/proj",
                    "implementation_steps": ["write main.c"] * 40,
                    "files_expected": ["main.c", "compile.sh"],
                    "validation_plan": ["sh compile.sh"],
                    "risks": [],
                    "research_refs": [],
                },
            ),
            "plan_compact": (
                "plan.compact.json",
                {
                    "goal": "PLAN-MARKER rebuild owner/proj",
                    "implementation_steps": ["write main.c"],
                    "files_expected": ["main.c", "compile.sh"],
                    "validation_plan": ["sh compile.sh"],
                    "risks": [],
                    "research_refs": [],
                },
            ),
        }
        if phase not in payloads:
            return
        filename, data = payloads[phase]
        target = workspace / RPI_DIR
        target.mkdir(parents=True, exist_ok=True)
        (target / filename).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def phases(self) -> list[str]:
        return [session["phase"] for session in self.sessions]

    def prompt_for(self, phase: str) -> str:
        return next(session["prompt"] for session in self.sessions if session["phase"] == phase)


def run_lane(lane: str, agent: Optional[FakeAgent] = None, timeout_seconds: int = 1800):
    """Drive one lane end-to-end against the fake agent; return (agent, result, tar_path)."""
    agent = agent or FakeAgent()
    adapter = SupervisorAgentAdapter(
        repo_root=REPO_ROOT,
        runner=agent,
        timeout_seconds=timeout_seconds,
        strategy=build_strategy(lane),
    )
    tmp = tempfile.mkdtemp(prefix=f"lane-{lane}-")
    out_tar = Path(tmp) / "submission.tar.gz"
    result = adapter.produce_submission(TASK, out_tar)
    return agent, result, out_tar


class LaneBIsTheUnchangedHarnessTests(unittest.TestCase):
    """Lane B is the control for the shipped harness; it must not drift."""

    def test_lane_b_prompt_is_exactly_the_shipped_slice_prompt(self) -> None:
        agent, _, _ = run_lane("B")
        self.assertEqual(["implement"], agent.phases())

        session = agent.sessions[0]
        slice_record = build_slice_record(TASK)
        # The context bundle does not depend on the command template, so only the handoff
        # path (a temp dir) has to be taken from the observed session.
        expected = render_prompt(
            repo_root=REPO_ROOT,
            slice_record=slice_record,
            context_bundle=build_context_bundle(build_queue_data(slice_record, "unused-template", 1800), slice_record),
            handoff_path=Path(session["options"]["--handoff-file"]),
        )
        self.assertEqual(expected, session["prompt"])

    def test_lane_b_is_the_adapter_default(self) -> None:
        adapter = SupervisorAgentAdapter(repo_root=REPO_ROOT, runner=FakeAgent())
        self.assertEqual("B", adapter.lane)


class LaneTreatmentTests(unittest.TestCase):
    def test_lane_a_gets_the_objective_without_harness_context(self) -> None:
        agent, _, _ = run_lane("A")
        prompt = agent.prompt_for("implement")
        self.assertEqual(["implement"], agent.phases())
        self.assertIn("compile.sh", prompt)
        self.assertIn("owner/proj", prompt)
        self.assertNotIn("Supervised Slice Run", prompt)  # base.md never reaches lane A
        self.assertNotIn("Acceptance Checks", prompt)

    def test_lane_c_runs_three_phases_and_passes_artifacts_forward(self) -> None:
        agent, result, _ = run_lane("C")
        self.assertEqual(["research", "plan", "implement"], agent.phases())
        # The plan session sees the research artifact, not the research conversation.
        self.assertIn("FINDING-MARKER", agent.prompt_for("plan"))
        # The implement session sees the plan, and never the research.
        self.assertIn("PLAN-MARKER", agent.prompt_for("implement"))
        self.assertNotIn("FINDING-MARKER", agent.prompt_for("implement"))
        assert result.strategy is not None
        self.assertEqual({}, result.strategy.compaction)
        self.assertEqual(0, result.strategy.phase_failures)

    def test_every_phase_repeats_the_immutable_objective(self) -> None:
        agent, _, _ = run_lane("C")
        for session in agent.sessions:
            self.assertIn("Objective (immutable)", session["prompt"])
            self.assertIn("owner/proj", session["prompt"])

    def test_lane_d_compacts_between_phases_and_records_the_loss(self) -> None:
        agent, result, _ = run_lane("D")
        self.assertEqual(["research", "research_compact", "plan", "plan_compact", "implement"], agent.phases())
        assert result.strategy is not None
        compaction = result.strategy.compaction
        self.assertLess(compaction["research_compacted_chars"], compaction["research_raw_chars"])
        self.assertLess(compaction["research_compression_ratio"], 1.0)
        self.assertLess(compaction["plan_compression_ratio"], 1.0)
        # The implement phase receives the compacted plan, so it is smaller than lane C's.
        self.assertLess(len(agent.prompt_for("implement")), len(run_lane("C")[0].prompt_for("implement")))

    def test_lane_e_is_lane_d_plus_jspace_only(self) -> None:
        agent_d, _, _ = run_lane("D")
        agent_e, _, _ = run_lane("E")
        self.assertEqual(agent_d.phases(), agent_e.phases())
        for session in agent_e.sessions:
            self.assertIn("J-Space Ledger", session["prompt"])
        for session in agent_d.sessions:
            self.assertNotIn("J-Space Ledger", session["prompt"])

    def test_phase_artifacts_are_kept_but_never_graded(self) -> None:
        _, result, out_tar = run_lane("C")
        with tarfile.open(out_tar, "r:gz") as tar:
            names = tar.getnames()
        self.assertIn("compile.sh", names)
        self.assertNotIn(RPI_DIR, names)
        self.assertFalse(any(name.startswith(RPI_DIR) for name in names))
        # Kept beside the submission for reproducibility.
        self.assertTrue((out_tar.parent / "rpi" / RESEARCH_ARTIFACT).is_file())

    def test_missing_artifact_is_recorded_and_does_not_abort_the_lane(self) -> None:
        agent, result, _ = run_lane("C", agent=FakeAgent(write_artifacts=False))
        self.assertEqual(["research", "plan", "implement"], agent.phases())
        assert result.strategy is not None
        self.assertEqual(2, result.strategy.phase_failures)
        research_phase = result.strategy.phases[0]
        self.assertFalse(research_phase.artifact_valid)
        self.assertIn("artifact missing or unparseable", research_phase.artifact_errors)


class BudgetTests(unittest.TestCase):
    def test_multi_phase_lanes_share_one_instance_budget(self) -> None:
        """A three-session lane must not get three times the wall clock of lane A."""
        agent, _, _ = run_lane("D", timeout_seconds=600)
        granted = [session["timeout"] for session in agent.sessions]
        self.assertTrue(all(value <= 600 for value in granted), granted)
        self.assertEqual(sorted(granted, reverse=True), granted)  # budget only shrinks


class MatrixTests(unittest.TestCase):
    def test_interleave_covers_every_cell_exactly_once(self) -> None:
        instances = ["i1", "i2", "i3"]
        cells = lanes_module.interleave(instances, ALL_LANES, repeats=2)
        self.assertEqual(len(instances) * len(ALL_LANES) * 2, len(cells))
        self.assertEqual(len(cells), len({(c.lane, c.instance_id, c.repeat) for c in cells}))

    def test_interleave_rotates_lane_order_between_instances(self) -> None:
        cells = lanes_module.interleave(["i1", "i2"], ALL_LANES, repeats=1)
        first = [c.lane for c in cells if c.instance_id == "i1"]
        second = [c.lane for c in cells if c.instance_id == "i2"]
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(first), sorted(second))

    def test_run_experiment_lays_out_cells_and_records_provenance(self) -> None:
        agents: list[FakeAgent] = []

        def factory(lane: str) -> SupervisorAgentAdapter:
            agent = FakeAgent()
            agents.append(agent)
            return SupervisorAgentAdapter(repo_root=REPO_ROOT, runner=agent, strategy=build_strategy(lane))

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            lanes_module.run_experiment(
                run_dir=run_dir,
                instances=["inst-a"],
                adapter_factory=factory,
                repo_root=REPO_ROOT,
                lanes=["A", "C"],
                repeats=1,
            )
            provenance = json.loads((run_dir / "C" / "r1" / "inst-a" / "run.json").read_text(encoding="utf-8"))
            self.assertTrue((run_dir / "A" / "r1" / "inst-a" / "submission.tar.gz").is_file())

        self.assertEqual("C", provenance["lane"])
        self.assertEqual(3, provenance["strategy"]["agent_invocations"])
        self.assertNotEqual("unknown", provenance["repo_automation_sha"])
        self.assertIn("started_at", provenance)

    def test_comparison_reports_adjacent_lane_deltas(self) -> None:
        summaries = [
            {
                "lane": "A",
                "primary": {"resolve_rate": 0.2, "near_resolve_rate": 0.2, "mean_pass_fraction": 0.5},
                "efficiency": {"agent_invocations": 2, "wall_clock_seconds": 100.0},
            },
            {
                "lane": "B",
                "primary": {"resolve_rate": 0.4, "near_resolve_rate": 0.4, "mean_pass_fraction": 0.6},
                "efficiency": {"agent_invocations": 2, "wall_clock_seconds": 120.0},
            },
            {
                "lane": "C",
                "primary": {"resolve_rate": 0.3, "near_resolve_rate": 0.5, "mean_pass_fraction": 0.7},
                "efficiency": {"agent_invocations": 6, "wall_clock_seconds": 300.0},
            },
        ]
        deltas = lanes_module.lane_deltas(summaries)
        self.assertEqual(["B - A", "C - B"], [delta["comparison"] for delta in deltas])
        self.assertAlmostEqual(0.2, deltas[0]["resolve_rate"])
        self.assertAlmostEqual(-0.1, deltas[1]["resolve_rate"])  # RPI cost resolve rate here
        self.assertEqual(4, deltas[1]["agent_invocations"])

    def test_summarize_lane_reports_stability_across_repeats(self) -> None:
        reports = [
            EffectivenessReport((InstanceScore("i1", 10, 10),)),
            EffectivenessReport((InstanceScore("i1", 10, 0),)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            summary = lanes_module.summarize_lane(Path(tmp), "C", repeats=2, reports=reports)
        self.assertEqual(0.5, summary["primary"]["resolve_rate"])
        self.assertGreater(summary["stability"]["resolve_rate_stdev"], 0.0)

    def test_markdown_comparison_renders_both_tables(self) -> None:
        comparison = {
            "lanes": [
                {
                    "lane": "A",
                    "primary": {"resolve_rate": 0.2, "near_resolve_rate": 0.2, "mean_pass_fraction": 0.5},
                    "efficiency": {"agent_invocations": 2, "wall_clock_seconds": 100.0},
                    "stability": {"resolve_rate_stdev": 0.1},
                    "process": {"phase_failures": 0, "nonzero_returncodes": 0},
                }
            ],
            "deltas": [],
            "note": "Primary metric is ProgramBench correctness.",
        }
        markdown = lanes_module.render_comparison_markdown(comparison)
        self.assertIn("Primary outcome", markdown)
        self.assertIn("one session, raw objective", markdown)


if __name__ == "__main__":
    unittest.main()
