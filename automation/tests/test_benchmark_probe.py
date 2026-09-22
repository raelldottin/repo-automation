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
from pathlib import Path

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

    def test_a_truncated_last_line_does_not_lose_the_stream(self) -> None:
        """A session killed mid-write leaves a partial line; the rest is still evidence."""
        from automation.benchmark.probe import _events

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "s.stream.jsonl"
            path.write_text('{"type": "system", "timestamp": 1}\n{"type": "tool_u', encoding="utf-8")
            self.assertEqual([{"type": "system", "timestamp": 1}], _events(path))


if __name__ == "__main__":
    unittest.main()
