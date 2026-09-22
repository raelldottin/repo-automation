from __future__ import annotations

import contextlib
import os
import tarfile
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from typing import Mapping

from automation.benchmark import adapter as benchmark_adapter
from automation.benchmark import run as benchmark_run
from automation.benchmark.adapter import SubmissionResult, SupervisorAgentAdapter, build_queue_data, build_slice_record
from automation.benchmark import evalrunner
from automation.benchmark.evalrunner import ProgramBenchEvalRunner, RecordedEvalRunner
from automation.benchmark.instances import TaskSpec, task_spec
from automation.benchmark.scoring import (
    EffectivenessReport,
    InstanceScore,
    score_eval_data,
    score_eval_file,
    score_run_dir,
)
from automation.schemas import models
from automation.supervisor import policy
from automation.supervisor.run_next import render_prompt


def _eval_data(passed: int, total: int, error_code: str | None = None) -> dict:
    results = [{"status": "passed"} for _ in range(passed)]
    results += [{"status": "failed"} for _ in range(total - passed)]
    return {"test_results": results, "error_code": error_code}


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixture = self.repo_root / "automation/tests/fixtures/cmatrix.eval.json"

    def test_fully_passing_submission_is_resolved(self) -> None:
        score = score_eval_data("x", _eval_data(20, 20))
        self.assertTrue(score.resolved)
        self.assertTrue(score.near_resolved)
        self.assertEqual(1.0, score.pass_fraction)

    def test_near_threshold_is_near_resolved_but_not_resolved(self) -> None:
        score = score_eval_data("x", _eval_data(19, 20))
        self.assertFalse(score.resolved)
        self.assertTrue(score.near_resolved)

    def test_below_near_threshold_is_neither(self) -> None:
        score = score_eval_data("x", _eval_data(18, 20))
        self.assertFalse(score.resolved)
        self.assertFalse(score.near_resolved)

    def test_zero_tests_never_counts_as_resolved(self) -> None:
        score = score_eval_data("x", _eval_data(0, 0))
        self.assertFalse(score.resolved)
        self.assertFalse(score.near_resolved)
        self.assertEqual(0.0, score.pass_fraction)

    def test_branch_balanced_fraction_can_disagree_with_the_pooled_one(self) -> None:
        """Branches contribute unequal numbers of executions, so the two weightings differ.

        Run 35715428932: the same 769 test names produced 769 executions in one lane and
        1649 in another, and re-weighting swapped two lanes. The pooled figure stays the
        score; this one says whether an ordering survives a change of weighting.
        """
        data = {
            "test_results": (
                [{"branch": "big", "name": f"t{index}", "status": "passed"} for index in range(90)]
                + [{"branch": "big", "name": f"t{index}", "status": "failed"} for index in range(10)]
                + [{"branch": "small", "name": "t0", "status": "failed"} for _ in range(2)]
            )
        }
        score = score_eval_data("x", data)
        self.assertAlmostEqual(90 / 102, score.pass_fraction)
        self.assertAlmostEqual((0.9 + 0.0) / 2, score.branch_macro_pass_fraction)
        self.assertEqual(102, score.total_tests)  # executions, not distinct tests
        self.assertEqual(90, score.unique_tests)

    def test_eval_output_without_branches_balances_to_the_pooled_fraction(self) -> None:
        score = score_eval_data("x", _eval_data(18, 20))
        self.assertAlmostEqual(score.pass_fraction, score.branch_macro_pass_fraction)

    def test_real_cmatrix_fixture_surfaces_error_code(self) -> None:
        score = score_eval_file(self.fixture)
        self.assertEqual("copy_executable_failed", score.error_code)
        self.assertFalse(score.resolved)

    def test_report_aggregates_rates_and_errors(self) -> None:
        report = EffectivenessReport(
            (
                InstanceScore("a", 10, 10),
                InstanceScore("b", 20, 19),
                InstanceScore("c", 10, 0, error_code="copy_executable_failed"),
                InstanceScore("d", 10, 0, error_code="copy_executable_failed"),
            )
        )
        self.assertEqual(4, report.instance_count)
        self.assertEqual(1, report.resolved_count)
        self.assertEqual(2, report.near_resolved_count)
        self.assertAlmostEqual(0.25, report.resolve_rate)
        self.assertAlmostEqual(0.5, report.near_resolve_rate)
        self.assertAlmostEqual((1.0 + 0.95 + 0.0 + 0.0) / 4, report.mean_pass_fraction)
        self.assertEqual({"copy_executable_failed": 2}, report.error_counts)
        self.assertIn("Harness Effectiveness", report.summary_table())

    def test_score_run_dir_reads_per_instance_eval_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            for iid, data in (("inst-a", _eval_data(10, 10)), ("inst-b", _eval_data(1, 10))):
                instance_dir = run_dir / iid
                instance_dir.mkdir()
                (instance_dir / f"{iid}.eval.json").write_text(__import__("json").dumps(data), encoding="utf-8")
            report = score_run_dir(run_dir)
        self.assertEqual(2, report.instance_count)
        self.assertEqual(1, report.resolved_count)
        self.assertEqual(["inst-a", "inst-b"], [s.instance_id for s in report.instances])


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.queue_schema = models.SliceQueue
        self.task = TaskSpec("owner__proj.abc1234", "owner/proj", "abc1234", "c", "easy")

    def test_generated_queue_matches_slice_schema(self) -> None:
        slice_record = build_slice_record(self.task)
        queue_data = build_queue_data(slice_record, "cmd {repo_root}", 1800)
        validation = policy.validate_document(queue_data, self.queue_schema)
        self.assertTrue(validation.is_valid, validation.errors)
        self.assertEqual([], policy.validate_queue_integrity(queue_data))

    def test_rendered_prompt_carries_rebuild_objective(self) -> None:
        slice_record = build_slice_record(self.task)
        queue_data = build_queue_data(slice_record, "cmd {repo_root}", 1800)
        bundle = benchmark_adapter.build_context_bundle(queue_data, slice_record)
        prompt = render_prompt(
            repo_root=self.repo_root,
            slice_record=slice_record,
            context_bundle=bundle,
            handoff_path=Path("/tmp/handoff.json"),
        )
        self.assertIn("owner/proj", prompt)
        self.assertIn("compile.sh", prompt)

    def test_produce_submission_archives_agent_workspace(self) -> None:
        captured: dict[str, str] = {}

        def fake_runner(command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
            captured["command"] = command
            (workspace / "compile.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (workspace / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            return 0

        adapter = SupervisorAgentAdapter(repo_root=self.repo_root, runner=fake_runner)
        with tempfile.TemporaryDirectory() as tmp:
            out_tar = Path(tmp) / "submission.tar.gz"
            result = adapter.produce_submission(self.task, out_tar)
            self.assertIsInstance(result, SubmissionResult)
            self.assertEqual(0, result.returncode)
            self.assertTrue(out_tar.is_file())
            with tarfile.open(out_tar, "r:gz") as tar:
                names = tar.getnames()
            # The tarball is not uploaded, so the manifest is the only evidence of what was
            # graded - that the cell submitted something, and that .rpi stayed out of it.
            manifest = (out_tar.parent / benchmark_adapter.SUBMISSION_MANIFEST_FILENAME).read_text(encoding="utf-8").split()
        self.assertIn("compile.sh", names)
        self.assertIn("main.c", names)
        self.assertNotIn(".git", names)
        self.assertEqual(sorted(names), sorted(manifest))
        # The agent was invoked with the rendered prompt + the workspace as repo root.
        self.assertIn("--prompt-file", captured["command"])
        self.assertIn("--slice-id", captured["command"])


class OrchestratorTests(unittest.TestCase):
    def test_run_benchmark_is_container_free_end_to_end(self) -> None:
        class FakeAdapter:
            def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
                out_tar.parent.mkdir(parents=True, exist_ok=True)
                with tarfile.open(out_tar, "w:gz"):
                    pass  # empty but present submission
                return SubmissionResult(task.instance_id, out_tar, out_tar.parent, 0)

        recorded = RecordedEvalRunner.from_mapping(
            {
                "inst-a": _eval_data(10, 10),
                "inst-b": _eval_data(0, 10, error_code="compile_failed"),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            report = benchmark_run.run_benchmark(
                run_dir=run_dir,
                instances=["inst-a", "inst-b"],
                adapter=FakeAdapter(),
                eval_runner=recorded,
            )
            self.assertIsNotNone(report)
            self.assertTrue((run_dir / benchmark_run.REPORT_FILENAME).is_file())
        assert report is not None
        self.assertEqual(2, report.instance_count)
        self.assertEqual(1, report.resolved_count)
        self.assertEqual({"compile_failed": 1}, report.error_counts)


class InstancesTests(unittest.TestCase):
    def test_task_spec_falls_back_to_instance_id_when_metadata_absent(self) -> None:
        spec = task_spec("someowner__someproj.deadbee")
        self.assertEqual("someowner__someproj.deadbee", spec.instance_id)
        self.assertIn("someowner", spec.repository)
        self.assertIn("from scratch", spec.objective)


class EvalContainerBudgetTests(unittest.TestCase):
    """Docker refuses a container asking for more CPUs than the host has, so the eval
    must never ask. Getting this wrong fails every container and yields no results."""

    def _argv(self, **kwargs) -> list[str]:
        recorded: list[list[str]] = []
        with unittest.mock.patch.object(evalrunner.subprocess, "run", lambda argv, **_: recorded.append(argv)):
            ProgramBenchEvalRunner(("programbench",), **kwargs).evaluate(Path("run-dir"))
        return recorded[0]

    def test_cpu_request_never_exceeds_the_host(self) -> None:
        argv = self._argv()
        requested = int(argv[argv.index("--docker-cpus") + 1])
        self.assertLessEqual(requested, os.cpu_count() or 1)
        self.assertGreaterEqual(requested, 1)

    def test_an_explicit_budget_is_passed_through(self) -> None:
        argv = self._argv(docker_cpus=2, workers=3)
        self.assertEqual(["--workers", "3", "--docker-cpus", "2"], argv[-4:])


class AgentBudgetTests(unittest.TestCase):
    """A cell that runs out of budget must be a recorded failure, not a crash.

    Letting the timeout raise took the whole matrix down with the first slow cell: a
    dispatched A-E run reported nothing, including for the lane that had finished.
    """

    def test_a_session_that_outlives_its_budget_reports_a_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            returncode = benchmark_adapter._subprocess_runner("sleep 30", Path(workspace), os.environ, 1)
        self.assertEqual(benchmark_adapter.AGENT_TIMEOUT_RETURNCODE, returncode)

    def test_a_session_is_asked_to_stop_before_it_is_compelled_to(self) -> None:
        """SIGKILL takes the session's buffered output with it; SIGTERM lets it flush."""
        with tempfile.TemporaryDirectory() as workspace:
            flushed = Path(workspace) / "flushed-before-exit"
            command = f"trap 'touch {flushed}; exit 0' TERM; sleep 30"
            returncode = benchmark_adapter._subprocess_runner(command, Path(workspace), os.environ, 1)
            self.assertTrue(flushed.exists(), "the session was killed without being asked to stop")
        self.assertEqual(benchmark_adapter.AGENT_TIMEOUT_RETURNCODE, returncode)

    def test_a_session_that_ignores_the_signal_still_cannot_outlive_its_budget(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            started = time.monotonic()
            with unittest.mock.patch.object(benchmark_adapter, "AGENT_TERMINATION_GRACE_SECONDS", 1):
                returncode = benchmark_adapter._subprocess_runner("trap '' TERM; sleep 30", Path(workspace), os.environ, 1)
            elapsed = time.monotonic() - started
        self.assertEqual(benchmark_adapter.AGENT_TIMEOUT_RETURNCODE, returncode)
        self.assertLess(elapsed, 5, "the grace became a second budget")

    def test_the_grace_is_short_enough_to_stay_shutdown_time(self) -> None:
        self.assertLessEqual(benchmark_adapter.AGENT_TERMINATION_GRACE_SECONDS, 15)
        self.assertGreater(benchmark_adapter.AGENT_TERMINATION_GRACE_SECONDS, 0)

    def test_the_timeout_collects_what_the_session_spawned(self) -> None:
        """An abandoned tool subprocess would spend the next cell's wall clock as well."""
        with tempfile.TemporaryDirectory() as workspace:
            marker = Path(workspace) / "outlived-the-kill"
            command = f"( sleep 2; touch {marker} ) & sleep 30"
            benchmark_adapter._subprocess_runner(command, Path(workspace), os.environ, 1)
            time.sleep(3)
            self.assertFalse(marker.exists(), "a subprocess of the agent survived the budget")


class AgentSessionReportTests(unittest.TestCase):
    """The session reports have to land beside the submission, wherever the run dir is.

    ``--run-dir out`` is relative and the agent runs with the workspace as its working
    directory, so a relative report path put every usage and controls report inside the
    workspace: archived into the submission, absent from ``run.json``.
    """

    def test_the_session_report_directory_is_absolute(self) -> None:
        captured: dict[str, str] = {}

        def fake_runner(command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
            captured["sessions"] = env["REPO_AUTOMATION_HERMES_USAGE_DIR"]
            return 0

        task = TaskSpec(
            instance_id="owner__proj.abc1234", repository="owner/proj", commit="abc1234", language="c", difficulty="easy"
        )
        adapter = SupervisorAgentAdapter(repo_root=Path.cwd(), runner=fake_runner)
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.chdir(tmp):
                adapter.produce_submission(task, Path("out/A/r1/owner__proj.abc1234/submission.tar.gz"))
            sessions = Path(captured["sessions"])
            self.assertTrue(sessions.is_absolute(), sessions)
            self.assertEqual(
                Path(tmp).resolve() / "out/A/r1/owner__proj.abc1234" / benchmark_adapter.AGENT_SESSIONS_DIR, sessions
            )


class AgentEnvironmentIsolationTests(unittest.TestCase):
    """A cell runs model-authored commands, so what it inherits is a security boundary.

    ``dict(os.environ)`` handed every session the launcher's whole environment: the operator's
    provider keys, GitHub tokens, cloud credentials, agent-harness markers. None of it is
    needed to rebuild a C repository. The property under test is the absence - a variable that
    is neither on the runner's list nor passed through ``env=`` is not there at all, whatever
    it happens to be called. Sentinel values only; a real credential never enters a test.
    """

    def _session_environment(self, adapter_env: Mapping[str, str] | None = None) -> dict[str, str]:
        captured: dict[str, dict[str, str]] = {}

        def fake_runner(command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
            captured["env"] = dict(env)
            return 0

        task = TaskSpec(
            instance_id="owner__proj.abc1234", repository="owner/proj", commit="abc1234", language="c", difficulty="easy"
        )
        adapter = SupervisorAgentAdapter(repo_root=Path.cwd(), env=adapter_env, runner=fake_runner)
        with tempfile.TemporaryDirectory() as tmp:
            adapter.produce_submission(task, Path(tmp) / "submission.tar.gz")
        return captured["env"]

    def test_an_unlisted_launcher_variable_does_not_reach_the_session(self) -> None:
        marker = "BENCHMARK_LEAK_SENTINEL_NOT_A_REAL_SECRET"
        with unittest.mock.patch.dict(os.environ, {marker: "sentinel-value"}):
            environment = self._session_environment()
        # assertFalse, not assertNotIn: assertNotIn renders the whole mapping into the failure
        # message, so the test that reports a leak would be the one printing the environment.
        self.assertFalse(marker in environment, f"{marker} reached the agent session")

    def test_the_allowlist_is_exact_names_not_a_prefix_rule(self) -> None:
        """A prefix rule fails open the first time somebody names a REPO_AUTOMATION_*_TOKEN."""
        marker = "REPO_AUTOMATION_UNLISTED_SENTINEL"
        with unittest.mock.patch.dict(os.environ, {marker: "sentinel-value"}):
            inherited = benchmark_adapter.audited_inherited_environment()
        self.assertFalse(marker in inherited, f"{marker} matched a pattern instead of a name")

    def test_what_the_agent_runner_reads_is_still_inherited(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"REPO_AUTOMATION_AGENT_RUNNER": "hermes"}):
            environment = self._session_environment()
        self.assertEqual("hermes", environment["REPO_AUTOMATION_AGENT_RUNNER"])
        # Without PATH the runner cannot find any agent CLI at all.
        self.assertIn("PATH", environment)

    def test_explicitly_supplied_configuration_reaches_the_session(self) -> None:
        """``env=`` is the caller's authorization, so it is forwarded whatever it is called."""
        environment = self._session_environment({"NVIDIA_API_KEY": "sentinel-key"})
        self.assertEqual("sentinel-key", environment["NVIDIA_API_KEY"])

    def test_an_explicit_value_wins_over_the_inherited_one(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"HERMES_INFERENCE_MODEL": "inherited-model"}):
            environment = self._session_environment({"HERMES_INFERENCE_MODEL": "explicit-model"})
        self.assertEqual("explicit-model", environment["HERMES_INFERENCE_MODEL"])


class PhaseArtifactOwnershipTests(unittest.TestCase):
    """The ``.rpi`` copy claims its destination; it never takes one over.

    The copy used to open with an unconditional ``rmtree`` of ``out/<cell>/rpi``. Re-running
    into a run directory that already held a finished cell's phase artifacts deleted them:
    the evidence of the attempt under investigation, removed by the attempt investigating it.
    """

    def _workspace_with_artifacts(self, tmp: Path) -> Path:
        workspace = tmp / "workspace"
        (workspace / benchmark_adapter.RPI_DIR).mkdir(parents=True)
        (workspace / benchmark_adapter.RPI_DIR / "phase-1.json").write_text("{}\n", encoding="utf-8")
        return workspace

    def test_phase_artifacts_are_copied_into_a_destination_this_call_creates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            benchmark_adapter._save_phase_artifacts(self._workspace_with_artifacts(Path(tmp)), out_dir)
            self.assertEqual("{}\n", (out_dir / "rpi" / "phase-1.json").read_text(encoding="utf-8"))

    def test_a_pre_existing_artifact_directory_is_refused_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            (out_dir / "rpi").mkdir(parents=True)
            sentinel = out_dir / "rpi" / "owner-sentinel.txt"
            sentinel.write_text("first attempt\n", encoding="utf-8")
            with self.assertRaises(FileExistsError) as refusal:
                benchmark_adapter._save_phase_artifacts(self._workspace_with_artifacts(Path(tmp)), out_dir)
            self.assertIn("refusing to overwrite", str(refusal.exception))
            self.assertEqual("first attempt\n", sentinel.read_text(encoding="utf-8"))

    def test_a_failed_copy_removes_only_what_this_call_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            with unittest.mock.patch.object(benchmark_adapter.shutil, "copytree", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    benchmark_adapter._save_phase_artifacts(self._workspace_with_artifacts(Path(tmp)), out_dir)
            self.assertFalse((out_dir / "rpi").exists())


class AgentEnvSelectionTests(unittest.TestCase):
    """``--agent-env`` names a launcher variable to forward. It never carries the value.

    Removing ambient inheritance removes the provider key with it, so a real run needs a way
    to say which variables it authorizes. Names only: ``--agent-env NAME=value`` would put the
    credential in argv, where every process listing on the host can read it.
    """

    def test_named_variables_are_copied_from_the_launcher(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"SENTINEL_PROVIDER_KEY": "sentinel-value"}):
            selected = benchmark_run.resolve_agent_env(["SENTINEL_PROVIDER_KEY"])
        self.assertEqual({"SENTINEL_PROVIDER_KEY": "sentinel-value"}, selected)

    def test_a_variable_that_is_not_set_is_refused_before_the_agent_launches(self) -> None:
        with self.assertRaises(SystemExit) as refusal:
            benchmark_run.resolve_agent_env(["SENTINEL_ABSENT_VARIABLE"])
        self.assertIn("SENTINEL_ABSENT_VARIABLE", str(refusal.exception))

    def test_an_argument_carrying_a_value_is_rejected_without_echoing_it(self) -> None:
        for argument in ("SENTINEL_KEY=sentinel-value", "FOO-BAR", "$(evil)", "", "2FAST"):
            with self.subTest(argument=argument):
                with self.assertRaises(SystemExit) as refusal:
                    benchmark_run.resolve_agent_env([argument])
                self.assertNotIn("sentinel-value", str(refusal.exception))

    def test_the_cli_forwards_the_selection_to_the_adapter(self) -> None:
        args = benchmark_run.build_parser().parse_args(["run", "--run-dir", "out", "--agent-env", "SENTINEL_PROVIDER_KEY"])
        with unittest.mock.patch.dict(os.environ, {"SENTINEL_PROVIDER_KEY": "sentinel-value"}):
            environment = benchmark_run._build_adapter(args)._run_environment()
            self.assertEqual("sentinel-value", environment["SENTINEL_PROVIDER_KEY"])

    def test_the_cli_refuses_an_unset_selection_before_building_an_adapter(self) -> None:
        args = benchmark_run.build_parser().parse_args(["run", "--run-dir", "out", "--agent-env", "SENTINEL_ABSENT_VARIABLE"])
        with self.assertRaises(SystemExit):
            benchmark_run._build_adapter(args)


if __name__ == "__main__":
    unittest.main()
