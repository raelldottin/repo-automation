"""The runner is the harness the invocation came from, one to one, or an explicit choice.

Codex -> Codex, Claude Code -> Claude Code, Hermes -> Hermes. ``auto`` used to mean "Claude Code
if it looks like Claude Code, otherwise the first installed CLI", so an invocation from inside
Hermes could launch Codex because ``codex`` happened to be on PATH. Now ``auto`` means the
nearest enclosing harness, an explicit runner that contradicts it is refused, and a headless
invocation with neither has no original agent to preserve and fails closed.

The ancestry is described to the wrapper through ``conftest.py``'s ``ps`` double, nearest first.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "automation/supervisor/run_agent.sh"

NOT_FOUND = {
    "codex": "Codex CLI not found",
    "claude": "Claude Code CLI not found",
    "hermes": "Hermes CLI not found",
}

# (comm, args) as `ps` really reports them, one per harness. Codex and Hermes are scripts, so an
# interpreter is the executable and the script is its first argument; Claude Code's native
# install reports a versioned binary whose argv[0] is still `claude`.
CODEX = ("node", "node /usr/local/bin/codex exec --sandbox workspace-write")
CLAUDE = ("/Users/someone/.local/share/claude/versions/2.1.3", "claude --print")
HERMES = ("/opt/hermes/venv/bin/python3", "/opt/hermes/venv/bin/python3 /opt/hermes/venv/bin/hermes chat -q task")
SHELL = ("-zsh", "-zsh")
LOGIN = ("/usr/bin/login", "/usr/bin/login -fpl someone")


class RunnerSelectionTests(unittest.TestCase):
    def run_wrapper(self, ancestry: list[tuple[str, str]], runner: Optional[str] = None, **extra_env: str):
        """Run the wrapper under ``ancestry`` with every agent CLI absent.

        Each runner's branch names its own missing CLI, so the stderr says which runner was
        selected without any agent being launched.
        """
        temp = Path(tempfile.mkdtemp(prefix="runner-selection-"))
        repo = temp / "repo"
        (repo / ".git").mkdir(parents=True)
        (temp / "prompt.md").write_text("prompt", encoding="utf-8")
        (temp / "context.json").write_text("{}", encoding="utf-8")
        tree = temp / "tree.tsv"
        lines = []
        for depth, (comm, args) in enumerate(ancestry):
            parent = depth + 101 if depth + 1 < len(ancestry) else 0
            lines.append(f"{depth + 100}\t{parent}\t{comm}\t{args}")
        tree.write_text("\n".join(lines) + "\n", encoding="utf-8")

        env = {key: value for key, value in os.environ.items() if not key.startswith("REPO_AUTOMATION_")}
        env.update(
            {
                "FAKE_PS_TREE": str(tree) if ancestry else "",
                "REPO_AUTOMATION_CODEX_BIN": str(temp / "absent-codex"),
                "REPO_AUTOMATION_CLAUDE_BIN": str(temp / "absent-claude"),
                "REPO_AUTOMATION_HERMES_BIN": str(temp / "absent-hermes"),
                **extra_env,
            }
        )
        env.pop("OWLORY_CODEX_BIN", None)
        if runner is not None:
            env["REPO_AUTOMATION_AGENT_RUNNER"] = runner
        return subprocess.run(
            [
                str(SCRIPT),
                "--repo-root",
                str(repo),
                "--prompt-file",
                str(temp / "prompt.md"),
                "--context-file",
                str(temp / "context.json"),
                "--handoff-file",
                str(temp / "handoff.json"),
                "--slice-id",
                "slice-a",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    def assertSelected(self, runner: str, result: subprocess.CompletedProcess) -> None:
        self.assertIn(NOT_FOUND[runner], result.stderr)
        for other, message in NOT_FOUND.items():
            if other != runner:
                self.assertNotIn(message, result.stderr)

    def test_auto_selects_the_enclosing_harness_for_each_of_the_three(self) -> None:
        for name, process in (("codex", CODEX), ("claude", CLAUDE), ("hermes", HERMES)):
            with self.subTest(harness=name):
                self.assertSelected(name, self.run_wrapper([SHELL, process, LOGIN]))

    def test_the_nearest_enclosing_harness_wins_over_one_further_up(self) -> None:
        """Harnesses nest: Claude Code driving a Hermes session that calls this is Hermes's call."""
        self.assertSelected("hermes", self.run_wrapper([SHELL, HERMES, SHELL, CLAUDE, LOGIN]))
        self.assertSelected("claude", self.run_wrapper([SHELL, CLAUDE, SHELL, HERMES, LOGIN]))
        self.assertSelected("codex", self.run_wrapper([CODEX, CLAUDE, HERMES]))

    def test_inherited_environment_markers_do_not_outvote_the_nearest_ancestor(self) -> None:
        """A Hermes child inherits CLAUDECODE from the Claude Code above it; that is not nearness."""
        result = self.run_wrapper([SHELL, HERMES, CLAUDE], CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli")
        self.assertSelected("hermes", result)

    def test_a_harness_named_as_an_argument_is_not_a_harness(self) -> None:
        """`headroom wrap claude` is a wrapper, and a path under ~/.claude is not Claude Code."""
        wrapper = (
            "/Users/someone/.local/share/uv/tools/headroom-ai/bin/python",
            "/Users/someone/.local/share/uv/tools/headroom-ai/bin/python /Users/someone/.local/bin/headroom wrap claude",
        )
        config_path = ("/bin/cat", "cat /Users/someone/.claude/settings.json /Users/someone/.codex/config.toml")
        result = self.run_wrapper([config_path, wrapper, SHELL, LOGIN])
        self.assertEqual(78, result.returncode, result.stderr)
        self.assertIn("REPO_AUTOMATION_AGENT_RUNNER", result.stderr)

    def test_an_explicit_runner_that_matches_the_enclosing_harness_is_honoured(self) -> None:
        self.assertSelected("claude", self.run_wrapper([SHELL, CLAUDE], runner="claude"))

    def test_an_explicit_runner_that_contradicts_the_enclosing_harness_is_refused(self) -> None:
        for runner, process, harness in (("hermes", CLAUDE, "claude"), ("codex", HERMES, "hermes"), ("claude", CODEX, "codex")):
            with self.subTest(runner=runner, harness=harness):
                result = self.run_wrapper([SHELL, process], runner=runner)
                self.assertEqual(78, result.returncode, result.stderr)
                self.assertIn(f"REPO_AUTOMATION_AGENT_RUNNER={runner}", result.stderr)
                self.assertIn(f"inside {harness}", result.stderr)
                for message in NOT_FOUND.values():
                    self.assertNotIn(message, result.stderr)

    def test_headless_with_an_explicit_runner_runs_that_runner(self) -> None:
        """CI, cron, a plain shell: nothing to inherit, so the configured runner is the choice."""
        for runner in ("codex", "claude", "hermes"):
            with self.subTest(runner=runner):
                self.assertSelected(runner, self.run_wrapper([SHELL, LOGIN], runner=runner))
                self.assertSelected(runner, self.run_wrapper([], runner=runner))

    def test_headless_without_an_explicit_runner_fails_closed_even_with_every_cli_installed(self) -> None:
        """The old `auto` picked Codex here because it was on PATH. There is no original agent."""
        temp = Path(tempfile.mkdtemp(prefix="installed-"))
        launched = temp / "launched"
        for name in ("codex", "claude", "hermes"):
            (temp / name).write_text(f'#!/usr/bin/env bash\necho {name} >> "{launched}"\n', encoding="utf-8")
            (temp / name).chmod(0o755)
        for runner in (None, "auto"):
            with self.subTest(runner=runner):
                result = self.run_wrapper(
                    [SHELL, LOGIN],
                    runner=runner,
                    REPO_AUTOMATION_CODEX_BIN=str(temp / "codex"),
                    REPO_AUTOMATION_CLAUDE_BIN=str(temp / "claude"),
                    REPO_AUTOMATION_HERMES_BIN=str(temp / "hermes"),
                )
                self.assertEqual(78, result.returncode, result.stderr)
                self.assertIn("No enclosing agent harness", result.stderr)
                self.assertIn("REPO_AUTOMATION_AGENT_RUNNER", result.stderr)
                self.assertFalse(launched.exists(), "an agent CLI was launched without a harness or a choice")

    def test_an_unsupported_runner_is_still_a_usage_error(self) -> None:
        result = self.run_wrapper([SHELL, CLAUDE], runner="gemini")
        self.assertEqual(64, result.returncode, result.stderr)
        self.assertIn("Unsupported REPO_AUTOMATION_AGENT_RUNNER: gemini", result.stderr)


if __name__ == "__main__":
    unittest.main()
