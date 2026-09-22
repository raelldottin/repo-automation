from __future__ import annotations

import json
import os
import shlex
import tarfile
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Mapping, Optional

from automation.benchmark import lanes as lanes_module
from automation.benchmark.adapter import (
    AGENT_SESSIONS_DIR,
    SUBMISSION_MANIFEST_FILENAME,
    SupervisorAgentAdapter,
    build_context_bundle,
    build_queue_data,
    build_slice_record,
)
from automation.benchmark.instances import TaskSpec
from automation.benchmark.scoring import EffectivenessReport, InstanceScore
from automation.benchmark.jspace import JSpaceArtifact, JSpaceUnavailable
from automation.benchmark.strategies import (
    ALL_LANES,
    LANE_CEILING_SECONDS,
    PHASE_CEILING_SECONDS,
    PLAN_ARTIFACT,
    RESEARCH_ARTIFACT,
    RPI_DIR,
    RpiStrategy,
    build_strategy,
)
from automation.supervisor.run_next import render_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = TaskSpec("owner__proj.abc1234", "owner/proj", "abc1234", "c", "easy")

JSPACE_MARKER = "CANONICAL-JSPACE-FIXTURE"
FIXTURE_ARTIFACT = JSpaceArtifact(
    source="fixture/j-space",
    revision="b2023124a1fa08278e3ee82aefa8ed9faeade995",
    artifact="j-space/SKILL.md",
    sha256="0" * 64,
    root=Path("/fixture/j-space"),
    text=f"# {JSPACE_MARKER}\n\nRun `<python-command> <skill-root>/scripts/control.py`.\n",
)


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


def run_lane(lane: str, agent: Optional[FakeAgent] = None, timeout_seconds: int = 1800, strategy=None):
    """Drive one lane end-to-end against the fake agent; return (agent, result, tar_path).

    ``strategy`` is only supplied for lane E, whose real factory resolves the canonical
    J-Space checkout; the fixture stands in for that checkout, not for the treatment.
    """
    agent = agent or FakeAgent()
    adapter = SupervisorAgentAdapter(
        repo_root=REPO_ROOT,
        runner=agent,
        timeout_seconds=timeout_seconds,
        strategy=strategy or build_strategy(lane),
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
        self.assertNotIn("Execution constraints", prompt)

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
        agent_e, result_e, _ = run_lane("E", strategy=RpiStrategy(compaction=True, jspace=FIXTURE_ARTIFACT))
        self.assertEqual("E", result_e.strategy.lane if result_e.strategy else None)
        self.assertEqual(agent_d.phases(), agent_e.phases())
        for session in agent_e.sessions:
            self.assertIn(JSPACE_MARKER, session["prompt"])
        for session in agent_d.sessions:
            self.assertNotIn(JSPACE_MARKER, session["prompt"])

    def test_lane_e_records_which_artifact_it_administered(self) -> None:
        _, result, _ = run_lane("E", strategy=RpiStrategy(compaction=True, jspace=FIXTURE_ARTIFACT))
        assert result.strategy is not None
        self.assertEqual(FIXTURE_ARTIFACT.provenance(), result.strategy.to_dict()["jspace"])

    def test_no_other_lane_claims_a_jspace_artifact(self) -> None:
        for lane in ("A", "B", "C", "D"):
            with self.subTest(lane=lane):
                _, result, _ = run_lane(lane)
                assert result.strategy is not None
                self.assertIsNone(result.strategy.to_dict()["jspace"])

    def test_phase_artifacts_are_kept_but_never_graded(self) -> None:
        _, result, out_tar = run_lane("C")
        with tarfile.open(out_tar, "r:gz") as tar:
            names = tar.getnames()
        self.assertIn("compile.sh", names)
        self.assertNotIn(RPI_DIR, names)
        self.assertFalse(any(name.startswith(RPI_DIR) for name in names))
        # Kept beside the submission for reproducibility.
        self.assertTrue((out_tar.parent / "rpi" / RESEARCH_ARTIFACT).is_file())
        # The tarball is not uploaded; the manifest is how a finished run proves this.
        manifest = (out_tar.parent / SUBMISSION_MANIFEST_FILENAME).read_text(encoding="utf-8").split()
        self.assertIn("compile.sh", manifest)
        self.assertFalse([name for name in manifest if name.startswith(RPI_DIR)])

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
        """A five-session lane must not get five times the wall clock of lane A."""
        agent, _, _ = run_lane("D", timeout_seconds=600)
        granted = [session["timeout"] for session in agent.sessions]
        self.assertTrue(all(value <= 600 for value in granted), granted)
        # Phases hold their own ceilings now, so the grants no longer shrink monotonically;
        # what still has to hold is that together they fit inside the instance budget.
        self.assertLessEqual(sum(granted), 600)


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

    def test_provenance_names_the_runner_and_the_model_but_never_the_key(self) -> None:
        """A lane result is only reproducible if it records what executed it."""
        with unittest.mock.patch.dict(
            os.environ,
            {
                "REPO_AUTOMATION_AGENT_RUNNER": "hermes",
                "HERMES_INFERENCE_PROVIDER": "nvidia",
                "HERMES_INFERENCE_MODEL": "moonshotai/kimi-k3",
                "NVIDIA_API_KEY": "sk-should-never-be-recorded",
            },
            clear=False,
        ):
            fingerprint = lanes_module._environment_fingerprint()

        self.assertEqual("hermes", fingerprint["REPO_AUTOMATION_AGENT_RUNNER"])
        self.assertEqual("nvidia", fingerprint["HERMES_INFERENCE_PROVIDER"])
        self.assertEqual("moonshotai/kimi-k3", fingerprint["HERMES_INFERENCE_MODEL"])
        self.assertNotIn("sk-should-never-be-recorded", json.dumps(fingerprint))

    def test_an_unresolvable_jspace_skips_lane_e_instead_of_running_lane_d(self) -> None:
        agents: list[FakeAgent] = []

        def factory(lane: str) -> SupervisorAgentAdapter:
            if lane == "E":
                raise JSpaceUnavailable("jspace_unavailable: set JSPACE_ROOT to a checkout")
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
                lanes=["D", "E"],
                repeats=1,
            )
            skipped = json.loads((run_dir / "E" / "r1" / "inst-a" / "run.json").read_text(encoding="utf-8"))
            self.assertFalse((run_dir / "E" / "r1" / "inst-a" / "submission.tar.gz").exists())
            self.assertTrue((run_dir / "D" / "r1" / "inst-a" / "submission.tar.gz").is_file())

        self.assertTrue(skipped["skipped"])
        self.assertIsNone(skipped["strategy"])  # no treatment was administered under E's name
        self.assertIn("jspace_unavailable", skipped["skip_reason"])
        self.assertEqual(1, len(agents))  # lane E never reached the agent

    def test_summarize_lane_counts_skipped_cells_apart_from_attempts(self) -> None:
        reports = [EffectivenessReport((InstanceScore("i1", 10, 10),))]
        with tempfile.TemporaryDirectory() as tmp:
            cell_dir = Path(tmp) / "E" / "r1" / "inst-a"
            cell_dir.mkdir(parents=True)
            (cell_dir / "run.json").write_text(
                json.dumps({"lane": "E", "skipped": True, "skip_reason": "jspace_unavailable", "strategy": None}),
                encoding="utf-8",
            )
            summary = lanes_module.summarize_lane(Path(tmp), "E", repeats=1, reports=reports)
        self.assertEqual(1, summary["skipped"])
        self.assertEqual(0, summary["attempts"])
        self.assertIsNone(summary["jspace"])

    def test_summarize_lane_carries_the_administered_jspace_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cell_dir = Path(tmp) / "E" / "r1" / "inst-a"
            cell_dir.mkdir(parents=True)
            (cell_dir / "run.json").write_text(
                json.dumps(
                    {
                        "lane": "E",
                        "returncode": 0,
                        "strategy": {
                            "seconds": 1.0,
                            "agent_invocations": 3,
                            "phase_failures": 0,
                            "compaction": {},
                            "jspace": FIXTURE_ARTIFACT.provenance(),
                        },
                    }
                ),
                encoding="utf-8",
            )
            summary = lanes_module.summarize_lane(Path(tmp), "E", repeats=1, reports=[])
        self.assertEqual(FIXTURE_ARTIFACT.provenance(), summary["jspace"])

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

    def test_markdown_flags_unrun_cells_and_names_the_administered_artifact(self) -> None:
        lane = {
            "lane": "E",
            "skipped": 2,
            "jspace": FIXTURE_ARTIFACT.provenance(),
            "primary": {"resolve_rate": 0.0, "near_resolve_rate": 0.0, "mean_pass_fraction": 0.0},
            "efficiency": {"agent_invocations": 0, "wall_clock_seconds": 0.0},
            "stability": {"resolve_rate_stdev": 0.0},
            "process": {"phase_failures": 0, "nonzero_returncodes": 0},
        }
        markdown = lanes_module.render_comparison_markdown(
            {"lanes": [lane], "deltas": [], "note": "Primary metric is ProgramBench correctness."}
        )
        self.assertIn("Unrun cells", markdown)
        self.assertIn("| E | 2 |", markdown)
        self.assertIn(FIXTURE_ARTIFACT.revision, markdown)


if __name__ == "__main__":
    unittest.main()


class PhaseBudgetTests(unittest.TestCase):
    """A greedy first phase must not be able to spend the phase that writes the code.

    In run 35650966066 every multi-phase lane submitted nothing: research was handed the
    whole remaining budget and lane D spent 1799 of 1800 seconds on it, leaving implement
    one second. Ceilings also hold the decomposition together - C must not inherit the
    compaction time D and E spend, or ``D - C`` measures more than compaction.
    """

    def _budgets(self, lane: str, timeout_seconds: int = LANE_CEILING_SECONDS) -> dict[str, int]:
        strategy = RpiStrategy(compaction=lane != "C", jspace=FIXTURE_ARTIFACT if lane == "E" else None, name=lane)
        agent, result, _ = run_lane(lane, timeout_seconds=timeout_seconds, strategy=strategy)
        assert result.strategy is not None
        return {session["phase"]: session["timeout"] for session in agent.sessions}

    def test_implement_keeps_its_allowance_whatever_research_does(self) -> None:
        self.assertEqual(PHASE_CEILING_SECONDS["implement"], self._budgets("C")["implement"])

    def test_shared_phases_get_identical_ceilings_across_c_d_e(self) -> None:
        budgets = {lane: self._budgets(lane) for lane in ("C", "D", "E")}
        for phase in ("research", "plan", "implement"):
            with self.subTest(phase=phase):
                self.assertEqual(
                    {PHASE_CEILING_SECONDS[phase]},
                    {budgets[lane][phase] for lane in ("C", "D", "E")},
                )

    def test_c_does_not_reclaim_the_compaction_slots(self) -> None:
        spent = sum(self._budgets("C").values())
        forfeited = PHASE_CEILING_SECONDS["research_compact"] + PHASE_CEILING_SECONDS["plan_compact"]
        self.assertEqual(LANE_CEILING_SECONDS - forfeited, spent)

    def test_a_lane_never_exceeds_its_instance_budget(self) -> None:
        for lane in ("C", "D", "E"):
            with self.subTest(lane=lane):
                self.assertLessEqual(sum(self._budgets(lane).values()), LANE_CEILING_SECONDS)

    def test_a_smaller_instance_budget_scales_the_profile(self) -> None:
        budgets = self._budgets("D", timeout_seconds=LANE_CEILING_SECONDS // 2)
        self.assertEqual(PHASE_CEILING_SECONDS["implement"] // 2, budgets["implement"])

    def test_single_session_lanes_still_get_the_whole_budget(self) -> None:
        for lane in ("A", "B"):
            with self.subTest(lane=lane):
                agent, result, _ = run_lane(lane, timeout_seconds=LANE_CEILING_SECONDS)
                assert result.strategy is not None
                # Minus however long setup took: one session still means the whole budget.
                self.assertAlmostEqual(LANE_CEILING_SECONDS, agent.sessions[0]["timeout"], delta=5)
                self.assertEqual({"implement": LANE_CEILING_SECONDS}, result.strategy.to_dict()["phase_budgets"])

    def test_the_profile_is_recorded_with_the_result(self) -> None:
        _, result, _ = run_lane(
            "E", timeout_seconds=LANE_CEILING_SECONDS, strategy=RpiStrategy(compaction=True, jspace=FIXTURE_ARTIFACT)
        )
        assert result.strategy is not None
        self.assertEqual(PHASE_CEILING_SECONDS, result.strategy.to_dict()["phase_budgets"])


class AgentSessionProvenanceTests(unittest.TestCase):
    """A session killed at its budget writes no usage report, and must still be recorded."""

    def test_a_session_with_only_controls_is_still_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / AGENT_SESSIONS_DIR
            sessions.mkdir()
            (sessions / "20260921T214852Z-11073.controls.json").write_text('{"runner": "hermes"}', encoding="utf-8")
            (sessions / "20260921T221851Z-11720.controls.json").write_text('{"runner": "hermes"}', encoding="utf-8")
            (sessions / "20260921T221851Z-11720.usage.json").write_text('{"api_calls": 2}', encoding="utf-8")
            recorded = lanes_module._agent_sessions(Path(tmp))

        self.assertEqual(
            ["20260921T214852Z-11073", "20260921T221851Z-11720"],
            [session["session"] for session in recorded],
        )
        killed, completed = recorded
        self.assertIsNone(killed["usage"])
        self.assertEqual({"runner": "hermes"}, killed["controls"])
        self.assertEqual({"api_calls": 2}, completed["usage"])
