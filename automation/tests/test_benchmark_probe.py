"""The diagnostic transport, exercised the way the last probe should have been.

``test_benchmark_phase_durability`` passed against a Python stand-in that printed early,
and concluded that unbuffering had made a killed session readable. It had not: the real
``--oneshot`` path discards the turn's stdout entirely, so run 35788842735 archived 0-byte
logs again. The stand-in here therefore imitates what the pinned Hermes actually does on
each transport - ``--oneshot`` says nothing until the turn ends, ``chat --format
stream-json`` flushes one JSON object per event - and the assertions are about which of
those survives being killed at a ceiling.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from automation.benchmark.adapter import AGENT_SESSIONS_DIR, _default_command_template, _subprocess_runner
from automation.benchmark.instances import TaskSpec, task_spec
from automation.benchmark.probe import STREAM_JSON_SUFFIX, artifact_mentions, timeline
from automation.benchmark.strategies import (
    AGENT_TIMEOUT_RETURNCODE,
    PHASE_CENSORED,
    ExecutionContext,
    research_prompt,
    run_phase,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = TaskSpec("owner__proj.abc1234", "owner/proj", "abc1234", "c", "easy")
PHASE_BUDGET_SECONDS = 2

# Both transports of the pinned CLI, chosen by the flags run_agent.sh passes. --oneshot
# buffers everything into a final answer it never reaches; chat --format stream-json
# flushes each event as it happens, which is the whole reason the probe exists.
FAKE_HERMES = '''#!/usr/bin/env python3
"""Stand in for the pinned Hermes CLI: one transport that speaks, one that does not."""
import json, sys, time

argv = sys.argv[1:]
stream = argv[0] == "chat" and "--format" in argv and "stream-json" in argv

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

if stream:
    emit({"type": "system", "subtype": "init", "timestamp": 1000})
    emit({"type": "tool_use", "name": "file_write", "input": {"path": ".rpi/research.json"}, "timestamp": 2000})
    emit({"type": "tool_result", "name": "file_write", "is_error": False, "timestamp": 3000})
    emit({"type": "text", "text": "still reading", "timestamp": 4000})
else:
    # Exactly the --oneshot contract: nothing on stdout until the turn returns.
    pass
time.sleep(600)
sys.stdout.write("final answer never reached\\n")
'''


def _run_phase(tmp: Path, transport: str):
    workspace = tmp / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)

    hermes = tmp / "fake-hermes"
    hermes.write_text(FAKE_HERMES, encoding="utf-8")
    hermes.chmod(0o755)

    sessions_dir = tmp / AGENT_SESSIONS_DIR
    env = dict(os.environ)
    env.update(
        {
            "REPO_AUTOMATION_AGENT_RUNNER": "hermes",
            "REPO_AUTOMATION_HERMES_BIN": str(hermes),
            "REPO_AUTOMATION_HERMES_USAGE_DIR": str(sessions_dir),
            "REPO_AUTOMATION_HERMES_TRANSPORT": transport,
        }
    )
    ctx = ExecutionContext(
        repo_root=REPO_ROOT,
        task=TASK,
        workspace=workspace,
        control_dir=tmp / "control",
        command_template=_default_command_template(REPO_ROOT),
        env=env,
        timeout_seconds=PHASE_BUDGET_SECONDS,
        runner=_subprocess_runner,
    )
    result = run_phase(ctx, "research", research_prompt(TASK), {"objective": TASK.objective}, PHASE_BUDGET_SECONDS)
    return result, sessions_dir


class StreamTransportTests(unittest.TestCase):
    def test_a_killed_stream_session_keeps_every_event_it_flushed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result, sessions_dir = _run_phase(Path(raw), "stream-json")

            self.assertEqual(AGENT_TIMEOUT_RETURNCODE, result.returncode)
            self.assertEqual(PHASE_CENSORED, result.state)

            streams = sorted(sessions_dir.glob(f"*{STREAM_JSON_SUFFIX}"))
            self.assertEqual(1, len(streams), f"expected one event stream, found {streams}")
            events = [json.loads(line) for line in streams[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                ["system", "tool_use", "tool_result", "text"],
                [event["type"] for event in events],
                "the killed session lost events it had already flushed",
            )

            # The four questions the probe exists to answer, off the archived stream alone.
            entries = timeline(events)
            self.assertEqual([0.0, 1.0, 2.0], [entry["at_seconds"] for entry in entries])
            mentions = artifact_mentions(events)
            self.assertEqual(1, len(mentions), "the write attempt is not visible in the stream")
            self.assertEqual("tool_use", mentions[0]["type"])

    def test_the_oneshot_transport_still_leaves_nothing_when_killed(self) -> None:
        """Not a regression - the reason the probe had to exist."""
        with tempfile.TemporaryDirectory() as raw:
            result, sessions_dir = _run_phase(Path(raw), "oneshot")

            self.assertEqual(AGENT_TIMEOUT_RETURNCODE, result.returncode)
            self.assertEqual([], sorted(sessions_dir.glob(f"*{STREAM_JSON_SUFFIX}")))
            logs = sorted(sessions_dir.glob("*.log"))
            self.assertEqual(1, len(logs))
            self.assertEqual("", logs[0].read_text(encoding="utf-8"))


class TransportContractTests(unittest.TestCase):
    def test_the_stream_transport_records_itself_and_claims_no_usage(self) -> None:
        """A probe must not look like a cell that was served and spent nothing."""
        with tempfile.TemporaryDirectory() as raw:
            _result, sessions_dir = _run_phase(Path(raw), "stream-json")

            controls = sorted(sessions_dir.glob("*.controls.json"))
            self.assertEqual(1, len(controls))
            self.assertEqual("stream-json", json.loads(controls[0].read_text(encoding="utf-8"))["transport"])
            self.assertEqual([], sorted(sessions_dir.glob("*.usage.json")))

    def test_the_default_transport_is_unchanged_and_still_reports_usage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _result, sessions_dir = _run_phase(Path(raw), "oneshot")

            controls = json.loads(sorted(sessions_dir.glob("*.controls.json"))[0].read_text(encoding="utf-8"))
            self.assertEqual("oneshot", controls["transport"])
            self.assertEqual(
                ["terminal", "file", "code_execution", "todo"],
                controls["toolsets"],
                "the transport switch must not disturb the pinned posture",
            )

    def test_an_unknown_transport_is_refused(self) -> None:
        script = REPO_ROOT / "automation" / "supervisor" / "run_agent.sh"
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "prompt.md").write_text("hello\n", encoding="utf-8")
            (tmp / "context.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", str(tmp)], check=True)
            # Present and runnable, so the refusal below is about the transport and not
            # about a missing CLI.
            hermes = tmp / "fake-hermes"
            hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hermes.chmod(0o755)
            env = dict(os.environ)
            env.update(
                {
                    "REPO_AUTOMATION_AGENT_RUNNER": "hermes",
                    "REPO_AUTOMATION_HERMES_BIN": str(hermes),
                    "REPO_AUTOMATION_HERMES_TRANSPORT": "tail -f",
                }
            )
            done = subprocess.run(
                [
                    str(script),
                    "--repo-root",
                    str(tmp),
                    "--prompt-file",
                    str(tmp / "prompt.md"),
                    "--context-file",
                    str(tmp / "context.json"),
                    "--handoff-file",
                    str(tmp / "handoff.json"),
                    "--slice-id",
                    "probe",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(64, done.returncode, done.stderr)
            self.assertIn("REPO_AUTOMATION_HERMES_TRANSPORT", done.stderr)


class ProbeRecordTests(unittest.TestCase):
    """The record has to stand on its own: the runner it came from will be gone."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def _runner(self, checkpoint: dict, events: list[dict]):
        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            artifact = workspace / ".rpi" / "research.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps(checkpoint), encoding="utf-8")
            sessions = Path(env["REPO_AUTOMATION_HERMES_USAGE_DIR"])
            sessions.mkdir(parents=True, exist_ok=True)
            stem = sessions / "20260101T000000Z-1"
            stem.with_suffix(".controls.json").write_text(
                json.dumps({"runner": "hermes", "transport": env["REPO_AUTOMATION_HERMES_TRANSPORT"]}),
                encoding="utf-8",
            )
            (sessions / f"20260101T000000Z-1{STREAM_JSON_SUFFIX}").write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )
            return AGENT_TIMEOUT_RETURNCODE

        return runner

    def test_the_probe_keeps_the_artifact_the_stream_and_the_verdict(self) -> None:
        from automation.benchmark.probe import PROBE_FILENAME, run_probe

        checkpoint = {
            "task": "rebuild cmatrix",
            "relevant_files": [],
            "findings": [],
            "constraints": [],
            "unknowns": [],
            "risks": [],
            "evidence": [],
        }
        events = [
            {"type": "system", "subtype": "init", "timestamp": 5000},
            {"type": "tool_use", "name": "file_write", "input": {"path": ".rpi/research.json"}, "timestamp": 9000},
        ]
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            record = run_probe(
                self.INSTANCE,
                out,
                repo_root=REPO_ROOT,
                budget_seconds=30,
                runner=self._runner(checkpoint, events),
            )

            self.assertEqual("stream-json", record["transport"])
            self.assertEqual(PHASE_CENSORED, record["phase_result"]["state"])
            self.assertTrue(record["artifact"]["present"])
            self.assertEqual([], record["artifact"]["schema_errors"])
            self.assertEqual(2, record["stream"]["events"])
            self.assertEqual([0.0, 4.0], [entry["at_seconds"] for entry in record["stream"]["timeline"]])
            self.assertEqual(1, len(record["stream"]["artifact_mentions"]))
            # The prompt is the lane's, not a restatement of it.
            self.assertEqual(len(research_prompt(task_spec(self.INSTANCE))), record["prompt_chars"])
            self.assertTrue((out / PROBE_FILENAME).is_file())
            self.assertTrue((out / "rpi" / "research.json").is_file())

    def test_artifact_mentions_are_attributed_to_the_event_that_named_them(self) -> None:
        """timeline() drops text events; zipping it against the unfiltered list misaligns both.

        Every entry after the first text delta was attributed to the wrong event, so the probe
        reported the wrong time and the wrong tool for the write attempt it exists to find.
        """
        events = [
            {"type": "system", "subtype": "init", "timestamp": 0},
            {"type": "text", "timestamp": 1000},
            {"type": "tool_use", "name": "shell", "input": {"command": "ls"}, "timestamp": 2000},
            {"type": "tool_use", "name": "file_write", "input": {"path": ".rpi/research.json"}, "timestamp": 3000},
        ]
        mentions = artifact_mentions(events)
        self.assertEqual(1, len(mentions))
        self.assertEqual("file_write", mentions[0]["name"])
        self.assertEqual(3.0, mentions[0]["at_seconds"])

    def test_a_truncated_last_line_does_not_lose_the_stream(self) -> None:
        """A session killed mid-write leaves a partial line; the rest is still evidence."""
        from automation.benchmark.probe import _events

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "s.stream.jsonl"
            path.write_text('{"type": "system", "timestamp": 1}\n{"type": "tool_u', encoding="utf-8")
            self.assertEqual([{"type": "system", "timestamp": 1}], _events(path))


if __name__ == "__main__":
    unittest.main()


class ProbeEnvironmentIsolationTests(unittest.TestCase):
    """The probe launches an agent against an untrusted repository, through shell=True.

    It inherited the launcher environment wholesale - the boundary
    ``audited_inherited_environment()`` exists to hold on the adapter path, absent one file
    over. What a lane refuses to hand an agent, a diagnostic does not get to hand it either.
    """

    INSTANCE = "abishekvashok__cmatrix.5c082c6"
    SENTINEL = "PROBE_LEAK_SENTINEL_NOT_A_REAL_SECRET"

    def _capturing_runner(self, captured: dict):
        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            captured["env"] = dict(env)
            artifact = workspace / ".rpi" / "research.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}", encoding="utf-8")
            sessions = Path(env["REPO_AUTOMATION_HERMES_USAGE_DIR"])
            sessions.mkdir(parents=True, exist_ok=True)
            return 0

        return runner

    def _probe_environment(self, probe_env=None) -> dict:
        from automation.benchmark.probe import run_probe

        captured: dict = {}
        with tempfile.TemporaryDirectory() as raw:
            run_probe(
                self.INSTANCE,
                Path(raw) / "probe",
                repo_root=REPO_ROOT,
                budget_seconds=30,
                runner=self._capturing_runner(captured),
                env=probe_env,
            )
        return captured["env"]

    def test_a_parent_only_variable_does_not_reach_the_probe_session(self) -> None:
        with unittest.mock.patch.dict(os.environ, {self.SENTINEL: "sentinel-value"}):
            environment = self._probe_environment()
        # Never assertNotIn: it would render the whole environment into the failure report.
        self.assertFalse(self.SENTINEL in environment, f"{self.SENTINEL} reached the probe session")

    def test_an_explicitly_selected_variable_reaches_the_probe_session(self) -> None:
        environment = self._probe_environment({self.SENTINEL: "sentinel-value"})
        self.assertEqual("sentinel-value", environment.get(self.SENTINEL))

    def test_the_probe_still_carries_what_the_runner_reads(self) -> None:
        environment = self._probe_environment()
        self.assertIn("PATH", environment)
        self.assertEqual("stream-json", environment["REPO_AUTOMATION_HERMES_TRANSPORT"])

    def test_the_cli_selects_launcher_variables_by_name(self) -> None:
        from automation.benchmark import probe as probe_module

        args = probe_module.build_parser().parse_args(
            ["--instance", self.INSTANCE, "--out-dir", "out", "--agent-env", self.SENTINEL]
        )
        self.assertEqual([self.SENTINEL], args.agent_env)


class ProbeArtifactLinkTests(unittest.TestCase):
    """`.rpi` is agent-created. Copying out of it is an evidence collector's job, not a
    dereference of whatever the agent decided to point at."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def _probe_over_a_linked_artifact(self, out: Path, target: Path):
        from automation.benchmark.probe import run_probe

        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            rpi = workspace / ".rpi"
            rpi.mkdir(parents=True, exist_ok=True)
            (rpi / "research.json").write_text("{}", encoding="utf-8")
            (rpi / "host-file.json").symlink_to(target)
            Path(env["REPO_AUTOMATION_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        return run_probe(self.INSTANCE, out, repo_root=REPO_ROOT, budget_seconds=30, runner=runner)

    def test_a_symlinked_artifact_is_not_copied_and_its_target_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            host = Path(raw) / "outside" / "host-sentinel.txt"
            host.parent.mkdir(parents=True)
            host.write_text("host sentinel, not agent evidence\n", encoding="utf-8")
            out = Path(raw) / "probe"

            self._probe_over_a_linked_artifact(out, host)

            copied = out / "rpi" / "host-file.json"
            self.assertFalse(copied.exists(), "a symlinked artifact was dereferenced into the run directory")
            self.assertFalse(copied.is_symlink(), "a symlinked artifact was reproduced in the run directory")
            self.assertEqual("host sentinel, not agent evidence\n", host.read_text(encoding="utf-8"))
            # The real artifact beside it is still collected: skipping is not refusing.
            self.assertTrue((out / "rpi" / "research.json").is_file())

    def test_the_skipped_link_is_recorded_rather_than_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            host = Path(raw) / "outside" / "host-sentinel.txt"
            host.parent.mkdir(parents=True)
            host.write_text("host sentinel\n", encoding="utf-8")
            out = Path(raw) / "probe"

            record = self._probe_over_a_linked_artifact(out, host)

            # A silent omission reads as "the agent wrote nothing". This one reads as a
            # finding about the session, which is the only thing a probe produces.
            self.assertEqual(["host-file.json"], record["rpi"]["skipped_links"])
            self.assertEqual(["research.json"], record["rpi"]["copied"])

    def test_a_symlinked_rpi_directory_is_not_followed_at_all(self) -> None:
        """The guard one level up.

        ``Path.is_dir()`` follows links, so gating the copy on it and then checking each
        entry leaves the whole container untrusted: an agent that links `.rpi` itself gets
        every entry of the target copied, each one a real file with is_symlink() False.
        Strictly more reach than the single-entry case, and invisible to a test that builds
        `.rpi` as a real directory.
        """
        from automation.benchmark.probe import run_probe

        with tempfile.TemporaryDirectory() as raw:
            host = Path(raw) / "outside"
            host.mkdir()
            (host / "id_rsa").write_text("host sentinel, not agent evidence\n", encoding="utf-8")
            out = Path(raw) / "probe"

            def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
                (workspace / ".rpi").symlink_to(host, target_is_directory=True)
                Path(env["REPO_AUTOMATION_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
                return 0

            record = run_probe(self.INSTANCE, out, repo_root=REPO_ROOT, budget_seconds=30, runner=runner)

            self.assertFalse((out / "rpi" / "id_rsa").exists(), "a linked artifact container was followed")
            self.assertFalse((out / "rpi").exists(), "a linked artifact container produced a run directory")
            self.assertEqual("host sentinel, not agent evidence\n", (host / "id_rsa").read_text(encoding="utf-8"))
            # Recorded, for the same reason an entry-level skip is: silence reads as "the
            # agent wrote nothing", which is the question the probe exists to answer.
            self.assertTrue(record["rpi"]["container_skipped"])

    def test_a_subdirectory_is_skipped_rather_than_destroying_the_collected_evidence(self) -> None:
        """One predicate covers this and the link case.

        read_bytes() on a directory raised mid-loop and the BaseException handler then
        removed the destination - so a session could delete its own collected evidence by
        leaving a directory in `.rpi`, which is the failure skip-rather-than-refuse exists
        to prevent.
        """
        from automation.benchmark.probe import run_probe

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"

            def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
                rpi = workspace / ".rpi"
                (rpi / "nested").mkdir(parents=True)
                (rpi / "research.json").write_text("{}", encoding="utf-8")
                Path(env["REPO_AUTOMATION_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
                return 0

            record = run_probe(self.INSTANCE, out, repo_root=REPO_ROOT, budget_seconds=30, runner=runner)

            self.assertTrue((out / "rpi" / "research.json").is_file(), "the real artifact was lost with the directory")
            self.assertFalse((out / "rpi" / "nested").exists())
            self.assertEqual(["nested"], record["rpi"]["skipped_non_files"])

    def test_a_run_with_no_links_records_an_empty_skip_list(self) -> None:
        from automation.benchmark.probe import run_probe

        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            rpi = workspace / ".rpi"
            rpi.mkdir(parents=True, exist_ok=True)
            (rpi / "research.json").write_text("{}", encoding="utf-8")
            Path(env["REPO_AUTOMATION_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            record = run_probe(self.INSTANCE, out, repo_root=REPO_ROOT, budget_seconds=30, runner=runner)
            self.assertEqual([], record["rpi"]["skipped_links"])
            self.assertEqual([], record["rpi"]["skipped_non_files"])
            self.assertFalse(record["rpi"]["container_skipped"])


class ProbeOutputOwnershipTests(unittest.TestCase):
    """The probe claims what it writes. It used to wipe a directory it had never created."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def _runner(self, calls: list, squats: Optional[Callable[[], None]] = None):
        """A phase that produces the artifact, and optionally claims an output mid-run.

        ``squats`` is the race the pre-flight refusal cannot see: a path that did not exist
        when the probe checked, and does by the time the probe writes.
        """

        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            calls.append(workspace)
            artifact = workspace / ".rpi" / "research.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}", encoding="utf-8")
            Path(env["REPO_AUTOMATION_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            if squats is not None:
                squats()
            return 0

        return runner

    def _run(self, out_dir: Path, calls: Optional[list] = None, squats: Optional[Callable[[], None]] = None):
        from automation.benchmark.probe import run_probe

        return run_probe(
            self.INSTANCE,
            out_dir,
            repo_root=REPO_ROOT,
            budget_seconds=30,
            runner=self._runner(calls if calls is not None else [], squats),
        )

    def test_a_pre_existing_artifact_directory_is_refused_and_left_intact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            nested = out / "rpi" / "earlier-run"
            nested.mkdir(parents=True)
            (nested / "phase-1.json").write_text("first attempt\n", encoding="utf-8")
            sentinel = out / "rpi" / "owner-sentinel.txt"
            sentinel.write_text("first attempt\n", encoding="utf-8")

            calls: list = []
            with self.assertRaises(FileExistsError) as refusal:
                self._run(out, calls)

            self.assertIn("refusing to overwrite", str(refusal.exception))
            # Refused before launch: a probe that runs the phase first has already spent the
            # budget the refusal exists to save.
            self.assertEqual([], calls)
            # Byte-for-byte and structurally unchanged: unlink() used to raise IsADirectoryError
            # on the subdirectory, after it had already destroyed the file beside it.
            self.assertEqual("first attempt\n", sentinel.read_text(encoding="utf-8"))
            self.assertEqual("first attempt\n", (nested / "phase-1.json").read_text(encoding="utf-8"))

    def test_a_pre_existing_probe_record_is_refused_not_overwritten(self) -> None:
        from automation.benchmark.probe import PROBE_FILENAME

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            out.mkdir(parents=True)
            record = out / PROBE_FILENAME
            record.write_text("first attempt\n", encoding="utf-8")
            calls: list = []
            with self.assertRaises(FileExistsError) as refusal:
                self._run(out, calls)
            self.assertIn("refusing to overwrite", str(refusal.exception))
            self.assertEqual("first attempt\n", record.read_text(encoding="utf-8"))
            self.assertEqual([], calls)

    def test_an_artifact_directory_that_appears_mid_run_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"

            def squat() -> None:
                (out / "rpi").mkdir(parents=True)
                (out / "rpi" / "other-run.json").write_text("not mine\n", encoding="utf-8")

            with self.assertRaises(FileExistsError) as refusal:
                self._run(out, squats=squat)
            self.assertIn("refusing to overwrite", str(refusal.exception))
            self.assertEqual("not mine\n", (out / "rpi" / "other-run.json").read_text(encoding="utf-8"))

    def test_a_probe_record_that_appears_mid_run_is_still_refused(self) -> None:
        from automation.benchmark.probe import PROBE_FILENAME

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"

            def squat() -> None:
                (out / PROBE_FILENAME).write_text("not mine\n", encoding="utf-8")

            with self.assertRaises(FileExistsError) as refusal:
                self._run(out, squats=squat)
            self.assertIn("refusing to overwrite", str(refusal.exception))
            self.assertEqual("not mine\n", (out / PROBE_FILENAME).read_text(encoding="utf-8"))

    def test_a_probe_into_an_empty_run_directory_writes_both(self) -> None:
        from automation.benchmark.probe import PROBE_FILENAME

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            self._run(out)
            self.assertTrue((out / PROBE_FILENAME).is_file())
            self.assertTrue((out / "rpi" / "research.json").is_file())


class ProbeHardLinkTests(unittest.TestCase):
    """The third way host content reaches the run directory.

    A hard link is not a symlink and ``is_file()`` is True for it, so both existing
    per-entry guards pass it straight through and the host's bytes are written out and
    listed under ``copied`` as though the session had produced them. It grants no read
    authority the agent lacks - HOME is already inherited - so the harm is to the evidence,
    not to the host: the record asserts a clean collection of something it did not collect.
    """

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def test_a_hardlinked_artifact_is_not_laundered_into_the_collected_evidence(self) -> None:
        from automation.benchmark.probe import run_probe

        with tempfile.TemporaryDirectory() as raw:
            host = Path(raw) / "outside" / "id_rsa"
            host.parent.mkdir(parents=True)
            host.write_text("host sentinel, not agent evidence\n", encoding="utf-8")
            out = Path(raw) / "probe"

            def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
                rpi = workspace / ".rpi"
                rpi.mkdir(parents=True, exist_ok=True)
                (rpi / "research.json").write_text("{}", encoding="utf-8")
                os.link(host, rpi / "id_rsa")
                Path(env["REPO_AUTOMATION_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
                return 0

            record = run_probe(self.INSTANCE, out, repo_root=REPO_ROOT, budget_seconds=30, runner=runner)

            self.assertFalse(
                (out / "rpi" / "id_rsa").exists(),
                "host content reached the run directory through a hard link",
            )
            self.assertEqual(["id_rsa"], record["rpi"]["skipped_links"])
            # Skipping is not refusing: the session's own artifact beside it still arrives.
            self.assertEqual(["research.json"], record["rpi"]["copied"])
            self.assertEqual("host sentinel, not agent evidence\n", host.read_text(encoding="utf-8"))


class ProbeSummaryRenderingTests(unittest.TestCase):
    """Agent-chosen names are printed to an operator. ``probe.json`` is safe because
    ``json.dumps`` escapes; the printed summary joins the names raw, so a name carrying a
    newline writes whole lines of its own into the operator's answer."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    FORGED = "artifact      present (9999 chars, schema valid)"

    def test_a_filename_cannot_forge_a_summary_line(self) -> None:
        from automation.benchmark.probe import run_probe, summarize

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            # Trailing newline too: the attacker owns the whole name, so the line the
            # summary was going to append lands on a line of its own rather than as a tail.
            name = f"decoy\n{self.FORGED}\n"

            def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
                (workspace / ".rpi" / name).mkdir(parents=True)
                Path(env["REPO_AUTOMATION_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
                return 0

            record = run_probe(self.INSTANCE, out, repo_root=REPO_ROOT, budget_seconds=30, runner=runner)
            summary = summarize(record)

            # The run this describes wrote no artifact at all.
            self.assertFalse(record["artifact"]["present"])
            self.assertFalse(
                self.FORGED in summary.splitlines(),
                "an agent-chosen filename wrote a whole line into the operator summary",
            )
            # Escaped for printing, kept verbatim in the record: the real name is evidence.
            self.assertEqual([name], record["rpi"]["skipped_non_files"])
