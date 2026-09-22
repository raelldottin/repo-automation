from __future__ import annotations

import contextlib
import io
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

    def test_parent_environment_is_not_implicitly_forwarded_to_agent(self) -> None:
        captured: dict[str, str] = {}

        def fake_runner(command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
            captured.update(env)
            return 0

        with unittest.mock.patch.dict(os.environ, {"BENCHMARK_PARENT_SECRET": "do-not-leak"}, clear=False):
            adapter = SupervisorAgentAdapter(repo_root=self.repo_root, runner=fake_runner)
            with tempfile.TemporaryDirectory() as tmp:
                adapter.produce_submission(self.task, Path(tmp) / "submission.tar.gz")

        self.assertNotIn("BENCHMARK_PARENT_SECRET", captured)

    def test_preexisting_phase_artifact_directory_is_refused_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            out_dir = root / "out"
            source = workspace / benchmark_adapter.RPI_DIR
            destination = out_dir / "rpi"
            source.mkdir(parents=True)
            destination.mkdir(parents=True)
            (source / "phase.json").write_text('{"phase": "new"}\n', encoding="utf-8")
            sentinel = destination / "owner-sentinel.txt"
            sentinel.write_bytes(b"owned-by-caller\n")
            before = {entry.name: entry.read_bytes() for entry in destination.iterdir() if entry.is_file()}

            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite pre-existing phase artifact directory"):
                benchmark_adapter._save_phase_artifacts(workspace, out_dir)

            after = {entry.name: entry.read_bytes() for entry in destination.iterdir() if entry.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(b"owned-by-caller\n", sentinel.read_bytes())

    def test_required_benign_launcher_environment_survives(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {
                "PATH": "/sentinel/bin",
                "HOME": "/sentinel/home",
                "LANG": "C.UTF-8",
                "HERMES_INFERENCE_PROVIDER": "nvidia",
                "BENCHMARK_PARENT_SECRET": "must-not-cross",
            },
            clear=True,
        ):
            environment = SupervisorAgentAdapter(repo_root=self.repo_root)._run_environment()

        self.assertEqual("/sentinel/bin", environment["PATH"])
        self.assertEqual("/sentinel/home", environment["HOME"])
        self.assertEqual("C.UTF-8", environment["LANG"])
        self.assertEqual("nvidia", environment["HERMES_INFERENCE_PROVIDER"])
        self.assertNotIn("BENCHMARK_PARENT_SECRET", environment)

    def test_explicit_agent_environment_is_forwarded(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            environment = SupervisorAgentAdapter(
                repo_root=self.repo_root,
                env={"OPENAI_API_KEY": "explicit-test-token"},
            )._run_environment()

        self.assertEqual("explicit-test-token", environment["OPENAI_API_KEY"])

    def test_explicit_agent_environment_overrides_inherited_benign_value(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"PATH": "parent-path"}, clear=True):
            environment = SupervisorAgentAdapter(
                repo_root=self.repo_root,
                env={"PATH": "explicit-path"},
            )._run_environment()

        self.assertEqual("explicit-path", environment["PATH"])

    def test_agent_env_cli_selects_exact_parent_variable(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"OPENAI_API_KEY": "cli-test-token"}, clear=True):
            args = benchmark_run.build_parser().parse_args(
                ["run", "--run-dir", "out", "--agent-env", "OPENAI_API_KEY"]
            )
            adapter = benchmark_run._build_adapter(args)

        self.assertEqual("cli-test-token", adapter._run_environment()["OPENAI_API_KEY"])

    def test_agent_env_cli_refuses_missing_variable_before_agent_execution(self) -> None:
        stderr = io.StringIO()
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.object(benchmark_run, "produce_submissions") as produce_submissions,
            contextlib.redirect_stderr(stderr),
        ):
            returncode = benchmark_run.main(
                ["run", "--run-dir", "out", "--agent-env", "DOES_NOT_EXIST"]
            )

        self.assertEqual(2, returncode)
        produce_submissions.assert_not_called()
        self.assertIn("DOES_NOT_EXIST", stderr.getvalue())
        self.assertNotIn("=", stderr.getvalue())

    def test_agent_env_cli_rejects_malformed_names(self) -> None:
        for value in ("A=B", "FOO-BAR", "$(evil)"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    benchmark_run.build_parser().parse_args(
                        ["run", "--run-dir", "out", "--agent-env", value]
                    )
            self.assertEqual(2, raised.exception.code)

    def test_fresh_phase_artifact_directory_copies_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            out_dir = root / "out"
            source = workspace / benchmark_adapter.RPI_DIR
            source.mkdir(parents=True)
            out_dir.mkdir()
            (source / "phase.json").write_bytes(b"phase-data\n")

            benchmark_adapter._save_phase_artifacts(workspace, out_dir)

            self.assertEqual(b"phase-data\n", (out_dir / "rpi" / "phase.json").read_bytes())

    def test_copy_failure_cleans_only_destination_owned_by_current_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            out_dir = root / "out"
            source = workspace / benchmark_adapter.RPI_DIR
            source.mkdir(parents=True)
            out_dir.mkdir()
            (source / "phase.json").write_bytes(b"phase-data\n")
            destination = out_dir / "rpi"

            with (
                unittest.mock.patch.object(
                    benchmark_adapter.shutil,
                    "copytree",
                    side_effect=OSError("injected copy failure"),
                ),
                self.assertRaisesRegex(OSError, "injected copy failure"),
            ):
                benchmark_adapter._save_phase_artifacts(workspace, out_dir)

            self.assertFalse(destination.exists())


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


if __name__ == "__main__":
    unittest.main()
