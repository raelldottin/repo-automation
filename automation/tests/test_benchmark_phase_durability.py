"""A phase killed at its ceiling must still leave its artifact and its transcript.

This is the probe the A-E campaign stalled on. In runs 35715428932 and 35736569601 every
session that hit its ceiling archived a 0-byte log and no ``research.json``, so six
multi-phase cells were scored on a handoff the harness had written for itself, and the
provider-validity verdict rested on evidence that no longer existed. Two mechanisms were
at fault, and this exercises both together on the real path - ``run_agent.sh``'s Hermes
branch, the real budget kill in ``_subprocess_runner`` - with a stand-in for the Hermes
binary so the probe costs seconds and no tokens:

* the session is asked to stop (SIGTERM) before it is compelled to (SIGKILL), and
* its stdout is unbuffered, so what it printed is in the log rather than in a buffer.

The stand-in checkpoints its artifact immediately and then refuses to finish, which is
exactly what the phase prompt now asks a real agent to do.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from automation.benchmark.adapter import AGENT_SESSIONS_DIR, _default_command_template, _subprocess_runner
from automation.benchmark.instances import TaskSpec
from automation.benchmark.strategies import (
    AGENT_TIMEOUT_RETURNCODE,
    PHASE_CENSORED,
    RESEARCH_ARTIFACT,
    RESEARCH_SPEC,
    RPI_DIR,
    ExecutionContext,
    artifact_instruction,
    compose_prompt,
    envelope,
    read_artifact,
    run_phase,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = TaskSpec("owner__proj.abc1234", "owner/proj", "abc1234", "c", "easy")
PHASE_BUDGET_SECONDS = 2

# Prints before it writes and after, so a buffered run leaves an empty log either way.
FAKE_HERMES = '''#!/usr/bin/env python3
"""Stand in for the Hermes CLI: checkpoint, then outlive the budget."""
import json, os, time
from pathlib import Path

print("session start: reading the workspace")
artifact = Path(os.environ["TERMINAL_CWD"]) / "{rpi_dir}" / "{filename}"
artifact.parent.mkdir(parents=True, exist_ok=True)
artifact.write_text(json.dumps({payload}), encoding="utf-8")
print("CHECKPOINT-WRITTEN")
time.sleep(600)
'''

CHECKPOINT = {
    "task": "rebuild owner/proj",
    "relevant_files": [],
    "findings": ["reads a terminal size"],
    "constraints": [],
    "unknowns": ["everything not yet read"],
    "risks": [],
    "evidence": [],
}


class KilledPhaseDurabilityTests(unittest.TestCase):
    def _run_research_phase(self, tmp: Path):
        workspace = tmp / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)

        hermes = tmp / "fake-hermes"
        hermes.write_text(
            FAKE_HERMES.format(rpi_dir=RPI_DIR, filename=RESEARCH_ARTIFACT, payload=json.dumps(CHECKPOINT)),
            encoding="utf-8",
        )
        hermes.chmod(0o755)

        sessions_dir = tmp / AGENT_SESSIONS_DIR
        env = dict(os.environ)
        env.update(
            {
                "REPO_AUTOMATION_AGENT_RUNNER": "hermes",
                "REPO_AUTOMATION_HERMES_BIN": str(hermes),
                "REPO_AUTOMATION_HERMES_USAGE_DIR": str(sessions_dir),
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
        prompt = compose_prompt(f"# Research: {TASK.repository}", envelope(TASK), artifact_instruction(RESEARCH_SPEC))
        result = run_phase(ctx, "research", prompt, {"objective": TASK.objective}, PHASE_BUDGET_SECONDS)
        return result, workspace, sessions_dir

    def test_a_killed_research_phase_leaves_a_valid_artifact_and_a_readable_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result, workspace, sessions_dir = self._run_research_phase(Path(raw))

            self.assertEqual(AGENT_TIMEOUT_RETURNCODE, result.returncode, "the session was supposed to be killed")

            data, text = read_artifact(workspace, RESEARCH_ARTIFACT)
            self.assertIsNotNone(data, "the killed phase left no artifact")
            self.assertEqual([], RESEARCH_SPEC.validate(data))
            self.assertGreater(len(text), 0)

            logs = sorted(sessions_dir.glob("*.log"))
            self.assertEqual(1, len(logs), f"expected one session log, found {logs}")
            transcript = logs[0].read_text(encoding="utf-8")
            self.assertIn("CHECKPOINT-WRITTEN", transcript, "the session died with its transcript still buffered")

        # Censored, not invalid: the phase ran out of its ceiling having done what it owed.
        self.assertEqual(PHASE_CENSORED, result.state)
        # The budget bought working time; the shutdown grace is recorded, never charged.
        self.assertLessEqual(result.seconds, PHASE_BUDGET_SECONDS)
        self.assertGreaterEqual(result.grace_seconds, 0.0)


if __name__ == "__main__":
    unittest.main()
