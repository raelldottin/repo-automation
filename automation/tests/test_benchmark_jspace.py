"""Lane E must administer the canonical J-Space skill, or admit that it did not.

Every case here is one way the pinned artifact can fail to be what the lock claims. The
expected outcome is always the same: refuse, name the reason, and never hand lane E some
other text. A silent substitution would make ``E - D`` measure our paraphrase.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from automation.benchmark import jspace
from automation.benchmark.jspace import JSpaceUnavailable

SKILL_TEXT = "# J-Space\n\nInstall at `<skill-root>`; invoke with `<python-command>`.\n"
ARTIFACT_PATH = "j-space/SKILL.md"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@contextmanager
def checkout(text: str = SKILL_TEXT, artifact: str = ARTIFACT_PATH) -> Iterator[tuple[Path, Path]]:
    """A Git checkout holding ``text``, plus a lock file pinned to it. Yields (root, lock)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "checkout"
        (root / artifact).parent.mkdir(parents=True, exist_ok=True)
        (root / artifact).write_text(text, encoding="utf-8")

        _git(root.parent, "init", "-q", "-b", "main", str(root))
        _git(root, "config", "user.email", "fixture@example.invalid")
        _git(root, "config", "user.name", "Fixture")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "pin")

        lock = Path(tmp) / "jspace.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "source": "fixture/j-space",
                    "revision": _git(root, "rev-parse", "HEAD"),
                    "artifact": artifact,
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        yield root, lock


def rewrite_lock(lock: Path, **changes: Optional[str]) -> None:
    data = json.loads(lock.read_text(encoding="utf-8"))
    data.update(changes)
    lock.write_text(json.dumps(data), encoding="utf-8")


class ResolutionTests(unittest.TestCase):
    def test_a_matching_checkout_resolves_to_the_pinned_bytes(self) -> None:
        with checkout() as (root, lock):
            artifact = jspace.resolve(root=root, lock_path=lock)
        self.assertEqual("fixture/j-space", artifact.source)
        self.assertEqual(ARTIFACT_PATH, artifact.artifact)
        self.assertEqual(hashlib.sha256(SKILL_TEXT.encode("utf-8")).hexdigest(), artifact.sha256)
        self.assertEqual(SKILL_TEXT, artifact.text)

    def test_the_prompt_block_is_the_canonical_text_plus_only_its_open_names(self) -> None:
        with checkout() as (root, lock):
            block = jspace.resolve(root=root, lock_path=lock).prompt_block()
            self.assertIn(SKILL_TEXT.strip(), block)  # verbatim, not paraphrased
            self.assertIn(f"<skill-root> is `{root.resolve()}`", block)
            self.assertIn("<python-command> is `python3`", block)

    def test_provenance_names_the_source_revision_artifact_and_hash(self) -> None:
        with checkout() as (root, lock):
            provenance = jspace.resolve(root=root, lock_path=lock).provenance()
        self.assertEqual({"source", "revision", "artifact", "sha256", "root"}, set(provenance))
        self.assertEqual(40, len(provenance["revision"]))

    def test_an_unset_root_refuses_and_says_what_to_point_at(self) -> None:
        with checkout() as (_, lock):
            with self.assertRaises(JSpaceUnavailable) as caught:
                with unset_root():
                    jspace.resolve(lock_path=lock)
        self.assertIn(jspace.ROOT_ENV_VAR, str(caught.exception))
        self.assertIn("fixture/j-space", str(caught.exception))

    def test_the_environment_variable_is_used_when_no_root_is_passed(self) -> None:
        with checkout() as (root, lock):
            with unset_root(str(root)):
                artifact = jspace.resolve(lock_path=lock)
        self.assertEqual(SKILL_TEXT, artifact.text)

    def test_a_root_that_is_not_a_directory_refuses(self) -> None:
        with checkout() as (root, lock):
            with self.assertRaises(JSpaceUnavailable) as caught:
                jspace.resolve(root=root / ARTIFACT_PATH, lock_path=lock)
        self.assertIn("not a directory", str(caught.exception))

    def test_an_unversioned_directory_refuses_even_with_the_right_bytes(self) -> None:
        with checkout() as (root, lock):
            with tempfile.TemporaryDirectory() as plain:
                copy = Path(plain) / ARTIFACT_PATH
                copy.parent.mkdir(parents=True)
                copy.write_text(SKILL_TEXT, encoding="utf-8")
                with self.assertRaises(JSpaceUnavailable) as caught:
                    jspace.resolve(root=Path(plain), lock_path=lock)
        self.assertIn("not a Git checkout", str(caught.exception))
        del root

    def test_a_different_revision_refuses(self) -> None:
        with checkout() as (root, lock):
            (root / "NOTES.md").write_text("later work\n", encoding="utf-8")
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "drift")
            with self.assertRaises(JSpaceUnavailable) as caught:
                jspace.resolve(root=root, lock_path=lock)
        self.assertIn("not the pinned", str(caught.exception))

    def test_a_missing_artifact_refuses(self) -> None:
        with checkout() as (root, lock):
            rewrite_lock(lock, artifact="j-space/MOVED.md")
            with self.assertRaises(JSpaceUnavailable) as caught:
                jspace.resolve(root=root, lock_path=lock)
        self.assertIn("cannot read", str(caught.exception))

    def test_content_that_does_not_match_the_pinned_hash_refuses(self) -> None:
        with checkout() as (root, lock):
            rewrite_lock(lock, sha256="f" * 64)
            with self.assertRaises(JSpaceUnavailable) as caught:
                jspace.resolve(root=root, lock_path=lock)
        self.assertIn("hashes to", str(caught.exception))

    def test_a_missing_lock_file_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(JSpaceUnavailable) as caught:
                jspace.resolve(root=Path(tmp), lock_path=Path(tmp) / "absent.json")
        self.assertIn("lock file missing", str(caught.exception))

    def test_an_incomplete_lock_file_refuses(self) -> None:
        with checkout() as (root, lock):
            rewrite_lock(lock, revision="")
            with self.assertRaises(JSpaceUnavailable) as caught:
                jspace.resolve(root=root, lock_path=lock)
        self.assertIn("missing revision", str(caught.exception))


class ShippedLockTests(unittest.TestCase):
    """The lock in the repository is the claim lane E's provenance rests on."""

    def test_the_shipped_lock_pins_a_full_revision_and_hash(self) -> None:
        lock = jspace.load_lock()
        self.assertEqual(40, len(lock["revision"]))
        self.assertEqual(64, len(lock["sha256"]))
        self.assertTrue(lock["artifact"].endswith(".md"), lock["artifact"])

    def test_the_skill_text_is_not_vendored_into_this_repository(self) -> None:
        """No copy of the skill means no way to silently drift from the pinned bytes."""
        benchmark = Path(jspace.__file__).resolve().parent
        vendored = [path for path in benchmark.rglob("*") if path.name == "SKILL.md"]
        self.assertEqual([], vendored)


@contextmanager
def unset_root(value: Optional[str] = None) -> Iterator[None]:
    previous = os.environ.get(jspace.ROOT_ENV_VAR)
    if value is None:
        os.environ.pop(jspace.ROOT_ENV_VAR, None)
    else:
        os.environ[jspace.ROOT_ENV_VAR] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(jspace.ROOT_ENV_VAR, None)
        else:
            os.environ[jspace.ROOT_ENV_VAR] = previous


if __name__ == "__main__":
    unittest.main()
