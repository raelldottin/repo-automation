"""Tools/check-commit-trailers.sh rejects agent attribution, keeps everything else."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "Tools" / "check-commit-trailers.sh"


class CommitTrailerGuardTest(unittest.TestCase):
    def run_guard(self, messages: list[str]) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            env = {
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": tmp,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            }

            def git(*args: str) -> None:
                subprocess.run(["git", *args], cwd=work, env=env, check=True, capture_output=True)

            git("init", "-q", "-b", "main")
            (work / "file.txt").write_text("base\n")
            git("add", "file.txt")
            git("commit", "-qm", "base commit")
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=work,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            for index, message in enumerate(messages):
                (work / "file.txt").write_text(f"change {index}\n")
                git("add", "file.txt")
                git("commit", "-qm", message)

            return subprocess.run(
                [str(SCRIPT), f"{base}..HEAD"],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
            )

    def test_clean_commits_pass(self) -> None:
        result = self.run_guard(["Add feature", "Fix bug\n\nSigned-off-by: Test <test@example.com>"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_claude_co_author_fails(self) -> None:
        result = self.run_guard(["Add feature\n\nCo-Authored-By: Claude <noreply@anthropic.com>"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Agent attribution trailers found in 1 commit", result.stderr)

    def test_claude_session_trailer_fails(self) -> None:
        result = self.run_guard(["Add feature\n\nClaude-Session: 0123abcd"])
        self.assertEqual(result.returncode, 1)

    def test_prose_and_other_co_authors_pass(self) -> None:
        result = self.run_guard(
            [
                "Document how Claude Code drives the harness",
                "Pair fix\n\nCo-Authored-By: Someone Else <someone@example.com>",
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
