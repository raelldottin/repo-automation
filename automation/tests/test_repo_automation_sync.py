"""Guards on the canonical -> consumer import path.

repo-automation is the canonical source of reusable automation. Consumers vendor a pinned
snapshot of it and never write back. These tests pin the seven protections that make that
direction unbreakable; they are the canonical copy, and consumers are expected to keep
their own equivalents against their vendored checkout.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_TOOL = REPO_ROOT / "Tools" / "repo-automation-sync.sh"


class RepoAutomationImportGuardTests(unittest.TestCase):
    """The seven protections that make the canonical -> consumer direction unbreakable.

    repo-automation is canonical and Owlory vendors a pinned snapshot of it. Every test
    here drives the real tool the way `make repo-automation-import` drives it: a pinned,
    published, clean canonical source importing into a consumer checkout. Each protection
    must exit non-zero and leave the filesystem untouched.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="repo-automation-guard-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.source = self.tmpdir / "canonical"
        self.target = self.tmpdir / "consumer"
        self.source.mkdir()
        self.target.mkdir()
        # Outside the source tree on purpose: a manifest written into the canonical checkout
        # would leave it dirty, which protection 4 correctly refuses.
        self.manifest = self.tmpdir / "manifest.json"
        self.remote = "https://github.com/raelldottin/repo-automation.git"

    # --- helpers ---------------------------------------------------------

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, msg=f"git {' '.join(args)}: {result.stderr}")
        return result

    def write_source(self, relative: str, contents: str) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def write_target(self, relative: str, contents: str) -> Path:
        path = self.target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def write_manifest(self, entries: list[dict[str, Any]]) -> None:
        self.manifest.write_text(
            json.dumps(
                {"version": 1, "default_target": str(self.target), "entries": entries},
                indent=2,
            ),
            encoding="utf-8",
        )

    def entry(
        self,
        source: str,
        destination: str,
        kind: str = "file",
        delete_stale: bool = False,
        template: bool = False,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "destination": destination,
            "kind": kind,
            "preserve_executable": False,
            "delete_stale": delete_stale,
            "template": template,
        }

    def init_canonical_source(self) -> None:
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.email", "canonical@example.com")
        self.git(self.source, "config", "user.name", "Canonical")
        self.git(self.source, "remote", "add", "origin", self.remote)

    def publish_source(self, message: str = "canonical state") -> str:
        """Commit everything in the canonical checkout and return the published sha."""
        self.git(self.source, "add", "-A")
        self.git(self.source, "commit", "-m", message)
        return self.git(self.source, "rev-parse", "HEAD").stdout.strip()

    def canonical_source_with(self, *files: tuple[str, str]) -> str:
        """A published, clean canonical checkout containing `files`. Returns its HEAD sha."""
        self.init_canonical_source()
        for relative, contents in files or (("seed.txt", "seed\n"),):
            self.write_source(relative, contents)
        return self.publish_source()

    def init_consumer(self) -> None:
        self.git(self.target, "init", "-b", "main")
        self.git(self.target, "config", "user.email", "consumer@example.com")
        self.git(self.target, "config", "user.name", "Consumer")

    def commit_consumer(self) -> None:
        self.git(self.target, "add", "-A")
        self.git(self.target, "commit", "-m", "consumer state")

    def run_tool(self, *args: str, pin: str | None = None) -> subprocess.CompletedProcess[str]:
        provenance: list[str] = ["--pin", pin, "--expect-remote", self.remote] if pin else []
        return subprocess.run(
            [
                str(SYNC_TOOL),
                *args,
                *provenance,
                "--source",
                str(self.source),
                "--manifest",
                str(self.manifest),
                "--target",
                str(self.target),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    # --- protection 1 ----------------------------------------------------

    def test_1_no_import_can_mutate_the_canonical_source(self) -> None:
        """Owlory has no write path outward: the tool refuses to write into its own source."""
        pin = self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        result = subprocess.run(
            [
                str(SYNC_TOOL),
                "--sync",
                "--pin",
                pin,
                "--expect-remote",
                self.remote,
                "--source",
                str(self.source),
                "--manifest",
                str(self.manifest),
                "--target",
                str(self.source),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("canonical source", result.stderr)
        self.assertEqual(
            pin,
            self.git(self.source, "rev-parse", "HEAD").stdout.strip(),
            msg="the canonical source must be byte-for-byte untouched",
        )
        self.assertEqual([], [line for line in self.git(self.source, "status", "--short").stdout.splitlines()])

    def test_1b_the_outward_auto_update_mode_no_longer_exists(self) -> None:
        """--auto-update was the outward mutation path. It must not be reachable at all."""
        pin = self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        result = self.run_tool("--sync", "--auto-update", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unrecognized arguments: --auto-update", result.stderr)
        self.assertNotIn("--auto-update", self.run_tool("--help").stdout)

    # --- protection 2 ----------------------------------------------------

    def test_2_unpinned_import_is_an_error(self) -> None:
        self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        result = self.run_tool("--sync")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("--pin", result.stderr)
        self.assertFalse((self.target / "seed.txt").exists())

    def test_2b_explicit_development_mode_is_the_only_way_past_the_pin(self) -> None:
        self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        result = self.run_tool("--sync", "--allow-unverified-source")

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("seed\n", (self.target / "seed.txt").read_text(encoding="utf-8"))
        self.assertIn("unverified source", result.stdout + result.stderr)

    # --- protection 3 ----------------------------------------------------

    def test_3_source_repository_identity_must_match_the_lock(self) -> None:
        pin = self.canonical_source_with()
        self.git(self.source, "remote", "set-url", "origin", "https://github.com/attacker/repo-automation.git")
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("identity", result.stderr)
        self.assertFalse((self.target / "seed.txt").exists())

    def test_3b_a_source_with_no_origin_fails_identity_rather_than_skipping_it(self) -> None:
        """An absent remote must be loud. Silently passing is the case you want caught."""
        pin = self.canonical_source_with()
        self.git(self.source, "remote", "remove", "origin")
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("identity", result.stderr)
        self.assertFalse((self.target / "seed.txt").exists())

    # --- protection 4 ----------------------------------------------------

    def test_4_dirty_canonical_source_is_refused(self) -> None:
        pin = self.canonical_source_with()
        self.write_source("seed.txt", "uncommitted local edit\n")
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("uncommitted", result.stderr)
        self.assertFalse((self.target / "seed.txt").exists())

    def test_4b_source_on_a_different_commit_than_the_pin_is_refused(self) -> None:
        pin = self.canonical_source_with()
        self.write_source("seed.txt", "moved on\n")
        self.publish_source("second")
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("pinned commit", result.stderr)
        self.assertFalse((self.target / "seed.txt").exists())

    def test_4c_check_mode_verifies_the_pin_too(self) -> None:
        """A drift report against the wrong commit answers the wrong question.

        This is the path the consumer's pre-push hook runs, so it has to notice that the
        source has moved rather than reporting the snapshot as current.
        """
        pin = self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "seed.txt")])
        self.assertEqual(0, self.run_tool("--sync", pin=pin).returncode)

        self.write_source("seed.txt", "moved on\n")
        moved = self.publish_source("second")

        result = self.run_tool("--check", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("pinned commit", result.stderr)
        self.assertIn(moved[:12], result.stderr)

    # --- protection 5 ----------------------------------------------------

    def test_5_import_cannot_write_to_a_consumer_owned_destination(self) -> None:
        """The canonical side supplies the file list, so it must not be able to aim it.

        An upstream entry whose destination is .githooks/pre-push or Makefile would be
        executed by the consumer on the very next push or make invocation.
        """
        self.init_canonical_source()
        self.write_source("payload.txt", "payload\n")
        pin = self.publish_source()
        self.init_consumer()
        self.write_target(".githooks/pre-push", "#!/bin/sh\nexit 0\n")
        self.write_target("Makefile", "real:\n\t@echo real\n")
        self.write_target("automation/queue/slices.json", '{"slices": []}\n')
        self.commit_consumer()

        for destination in (".githooks/pre-push", "Makefile", "automation/queue/slices.json"):
            with self.subTest(destination=destination):
                self.write_manifest([self.entry("payload.txt", destination)])

                result = self.run_tool("--sync", pin=pin)

                self.assertNotEqual(0, result.returncode)
                self.assertIn("destination", result.stderr)
                self.assertNotIn(
                    "payload",
                    (self.target / destination).read_text(encoding="utf-8"),
                    msg=f"{destination} is consumer-owned and must never be written by an import",
                )

    # --- protection 6 ----------------------------------------------------

    def test_6_stale_deletion_never_reaches_outside_canonical_owned_paths(self) -> None:
        """delete_stale is how the 1937-line deletion happened. Bound it to what it owns."""
        pin = self.canonical_source_with(("harness/keep.py", "keep\n"))
        self.init_consumer()
        self.write_target("automation/handoffs/closed.json", "{}\n")
        self.write_target("automation/proofs/slice/proof.md", "proof\n")
        self.write_target("automation/queue/slices.json", '{"slices": []}\n')
        self.write_target("vendored/harness/gone.py", "upstream deleted this\n")
        self.commit_consumer()
        self.write_manifest(
            [
                self.entry("harness", "vendored/harness", kind="directory", delete_stale=True),
                self.entry("harness", "automation", kind="directory", delete_stale=True),
            ]
        )

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("destination", result.stderr)
        for survivor in (
            "automation/handoffs/closed.json",
            "automation/proofs/slice/proof.md",
            "automation/queue/slices.json",
            "vendored/harness/gone.py",
        ):
            self.assertTrue(
                (self.target / survivor).exists(),
                msg=f"a refused import must delete nothing at all, including {survivor}",
            )

    def test_6b_a_clean_import_deletes_stale_files_only_under_its_own_destination(self) -> None:
        pin = self.canonical_source_with(("harness/keep.py", "keep\n"))
        self.init_consumer()
        self.write_target("vendored/harness/gone.py", "upstream deleted this\n")
        self.write_target("automation/handoffs/closed.json", "{}\n")
        self.commit_consumer()
        self.write_manifest([self.entry("harness", "vendored/harness", kind="directory", delete_stale=True)])

        result = self.run_tool("--sync", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertFalse((self.target / "vendored/harness/gone.py").exists())
        self.assertTrue((self.target / "vendored/harness/keep.py").exists())
        self.assertTrue((self.target / "automation/handoffs/closed.json").exists())

    # --- protection 7 ----------------------------------------------------

    def test_7_import_refuses_to_clobber_a_locally_modified_file(self) -> None:
        """A customized vendored file is uncommitted work. Overwriting it loses it silently."""
        pin = self.canonical_source_with(("harness/tool.py", "canonical\n"))
        self.init_consumer()
        self.write_target("vendored/harness/tool.py", "canonical\n")
        self.commit_consumer()
        self.write_target("vendored/harness/tool.py", "local customization in progress\n")
        self.write_manifest([self.entry("harness", "vendored/harness", kind="directory", delete_stale=True)])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("locally modified", result.stderr)
        self.assertEqual(
            "local customization in progress\n",
            (self.target / "vendored/harness/tool.py").read_text(encoding="utf-8"),
        )

    def test_7b_template_entries_are_not_overwritten_by_an_ordinary_import(self) -> None:
        pin = self.canonical_source_with(("prompts/slice.md", "canonical prompt\n"))
        self.init_consumer()
        self.write_target("automation/prompts/slice.md", "consumer prompt\n")
        self.commit_consumer()
        self.write_manifest([self.entry("prompts", "automation/prompts", kind="directory", template=True)])

        result = self.run_tool("--sync", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(
            "consumer prompt\n",
            (self.target / "automation/prompts/slice.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
