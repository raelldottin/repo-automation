"""Guards on the canonical -> consumer import path.

repo-automation is the canonical source of reusable automation. Consumers vendor a pinned
snapshot of it and never write back. These tests pin the eight protections that make that
direction unbreakable; they are the canonical copy, and consumers are expected to keep
their own equivalents against their vendored checkout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_TOOL = REPO_ROOT / "Tools" / "repo-automation-sync.sh"


class RepoAutomationImportGuardTests(unittest.TestCase):
    """The eight protections that make the canonical -> consumer direction unbreakable.

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

    def write_manifest(self, entries: list[dict[str, Any]], **extra: Any) -> None:
        self.manifest.write_text(
            json.dumps({"version": 1, **extra, "entries": entries}, indent=2),
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

    def run_tool(
        self,
        *args: str,
        pin: str | None = None,
        env_path: str | None = None,
        quality_command: str = "true",
    ) -> subprocess.CompletedProcess[str]:
        # Fixture sources are bare git repos with no build system, so the canonical quality
        # gate is stubbed out here. Protection 8 exercises the real gate directly.
        provenance: list[str] = (
            ["--pin", pin, "--expect-remote", self.remote, "--quality-command", quality_command] if pin else []
        )
        env = None
        if env_path is not None:
            # Used to take git away from the tool mid-run, which is one of the ways a guard
            # that reads returncode alone quietly stops guarding.
            env = {**os.environ, "PATH": env_path}
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
            env=env,
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

    def test_1c_a_sibling_checkout_of_the_canonical_repo_is_refused(self) -> None:
        """Containment does not establish identity.

        Two checkouts side by side contain neither the other, so the destructive case looked
        like an ordinary import. This is the one the default target used to point at.
        """
        self.canonical_source_with(("harness/keep.py", "CANONICAL\n"))

        sibling = self.tmpdir / "canonical-sibling"
        self.git(self.tmpdir, "clone", "--quiet", str(self.source), str(sibling))
        self.git(sibling, "remote", "set-url", "origin", self.remote)
        (sibling / "harness/only-upstream.py").write_text("upstream only\n", encoding="utf-8")
        self.git(sibling, "add", "-A")
        self.git(sibling, "-c", "user.email=s@example.com", "-c", "user.name=S", "commit", "-m", "sibling")
        before = (sibling / "harness/keep.py").read_text(encoding="utf-8")

        self.write_source("harness/keep.py", "PAYLOAD\n")
        pin = self.publish_source("payload")
        self.write_manifest([self.entry("harness", "harness", kind="directory", delete_stale=True)])

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
                str(sibling),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("canonical", result.stderr)
        self.assertEqual(before, (sibling / "harness/keep.py").read_text(encoding="utf-8"))
        self.assertTrue(
            (sibling / "harness/only-upstream.py").exists(),
            msg="delete_stale must never reach a checkout of the canonical repository",
        )

    def test_1d_a_target_whose_origin_is_the_canonical_repo_is_refused(self) -> None:
        """An independent clone shares no git dir, so identity falls back to the remote."""
        pin = self.canonical_source_with()
        self.init_consumer()
        self.git(self.target, "remote", "add", "origin", self.remote)
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("origin", result.stderr)
        self.assertFalse((self.target / "seed.txt").exists())

    def test_1e_there_is_no_default_destination(self) -> None:
        """A manifest cannot name where the import lands - the caller must say.

        default_target named the canonical repository, which was correct before the
        ownership flip and a --sync away from writing outward after it.
        """
        pin = self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "seed.txt")])

        no_target = subprocess.run(
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
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, no_target.returncode)
        self.assertIn("--target", no_target.stderr)

        self.write_manifest([self.entry("seed.txt", "seed.txt")], default_target=str(self.target))
        reintroduced = self.run_tool("--sync", pin=pin)
        self.assertNotEqual(0, reintroduced.returncode)
        self.assertIn("default_target", reintroduced.stderr)

    def test_1f_the_shipped_manifest_names_no_destination(self) -> None:
        """The real manifest, not a fixture: the field must be gone from the repository."""
        manifest = json.loads((REPO_ROOT / "automation/reusable-manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("default_target", manifest)

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

    def test_5b_the_destination_guard_is_case_insensitive(self) -> None:
        """On APFS and NTFS 'makefile' is the file the guard is protecting."""
        self.init_canonical_source()
        self.write_source("payload.txt", "payload\n")
        pin = self.publish_source()
        self.init_consumer()

        for destination in ("makefile", "MAKEFILE", ".GitHooks/pre-push", "Automation/Queue/slices.json"):
            with self.subTest(destination=destination):
                self.write_manifest([self.entry("payload.txt", destination)])

                result = self.run_tool("--sync", pin=pin)

                self.assertNotEqual(0, result.returncode)
                self.assertIn("destination", result.stderr)

    def test_5c_the_lock_file_is_consumer_owned(self) -> None:
        """The consumer's Makefile reads the lock and passes it to this tool.

        Whoever writes the lock chooses what `make` executes in the consumer, so an import
        must never be able to write it - the manifest comes from the same place an attacker
        would be.
        """
        self.init_canonical_source()
        self.write_source("payload.txt", "payload\n")
        pin = self.publish_source()
        self.init_consumer()
        self.write_target("automation/repo-automation.lock", '{"commit": "real"}\n')
        self.commit_consumer()
        self.write_manifest([self.entry("payload.txt", "automation/repo-automation.lock")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("destination", result.stderr)
        self.assertIn("real", (self.target / "automation/repo-automation.lock").read_text(encoding="utf-8"))

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

    def test_7c_a_git_failure_stops_the_import_rather_than_disabling_the_guard(self) -> None:
        """Protection 7 must fail closed.

        Returning "nothing is modified" whenever git exits non-zero means dubious ownership
        or a missing git silently removes the guard, and the import proceeds to overwrite
        the very work the guard exists to protect.
        """
        self.canonical_source_with(("harness/tool.py", "canonical v2\n"))
        self.init_consumer()
        self.write_target("vendored/harness/tool.py", "local work\n")
        self.write_manifest([self.entry("harness", "vendored/harness", kind="directory")])

        # git is present but refuses, which is what dubious ownership looks like. Exit 128 is
        # also what "not a git repository" returns, so returncode alone cannot tell them apart.
        shim = self.tmpdir / "shim"
        shim.mkdir()
        fake_git = shim / "git"
        fake_git.write_text(
            '#!/bin/sh\necho "fatal: detected dubious ownership in repository" >&2\nexit 128\n',
            encoding="utf-8",
        )
        fake_git.chmod(0o755)

        result = self.run_tool("--sync", "--allow-unverified-source", env_path=f"{shim}:{os.environ['PATH']}")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("uncommitted", result.stderr)
        self.assertEqual("local work\n", (self.target / "vendored/harness/tool.py").read_text(encoding="utf-8"))

    def test_7d_an_untracked_consumer_is_still_importable(self) -> None:
        """Fail-closed must not mean refusing a consumer that simply is not a Git checkout."""
        pin = self.canonical_source_with(("harness/tool.py", "canonical\n"))
        self.write_manifest([self.entry("harness", "vendored/harness", kind="directory")])

        result = self.run_tool("--sync", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("canonical\n", (self.target / "vendored/harness/tool.py").read_text(encoding="utf-8"))

    def test_7e_modified_paths_are_resolved_against_the_repository_root(self) -> None:
        """Status paths are relative to the toplevel, so a consumer in a subdirectory needs it."""
        pin = self.canonical_source_with(("harness/tool.py", "canonical v2\n"))
        self.init_consumer()

        nested = self.target / "nested/consumer"
        nested.mkdir(parents=True)
        (nested / "vendored").mkdir()
        (nested / "vendored/tool.py").write_text("canonical\n", encoding="utf-8")
        self.commit_consumer()
        (nested / "vendored/tool.py").write_text("local work\n", encoding="utf-8")
        self.write_manifest([self.entry("harness", "vendored", kind="directory")])

        result = subprocess.run(
            [
                str(SYNC_TOOL),
                "--sync",
                "--pin",
                pin,
                "--expect-remote",
                self.remote,
                "--quality-command",
                "true",
                "--source",
                str(self.source),
                "--manifest",
                str(self.manifest),
                "--target",
                str(nested),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("locally modified", result.stderr)
        self.assertEqual("local work\n", (nested / "vendored/tool.py").read_text(encoding="utf-8"))

    # --- protection 8 ----------------------------------------------------

    def test_8_source_failing_its_own_quality_gate_is_refused(self) -> None:
        """Pinned, clean and correctly identified is not the same as releasable."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "vendored/tool.py")])

        result = self.run_tool("--sync", pin=pin, quality_command="false")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("quality gate", result.stderr)
        self.assertFalse((self.target / "vendored/tool.py").exists())

    def test_8_quality_gate_failure_output_is_reported(self) -> None:
        """A refusal that does not say what failed sends the operator back to guessing."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "vendored/tool.py")])

        result = self.run_tool("--sync", pin=pin, quality_command="sh -c 'echo would-reformat-something >&2; exit 1'")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("would-reformat-something", result.stderr)

    def test_8_unrunnable_quality_gate_is_a_refusal_not_a_pass(self) -> None:
        """A gate that cannot run has not passed; the tool must not read that as success."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "vendored/tool.py")])

        result = self.run_tool("--sync", pin=pin, quality_command="repo-automation-no-such-gate")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("could not run the canonical quality gate", result.stderr)
        self.assertFalse((self.target / "vendored/tool.py").exists())

    def test_8_quality_gate_runs_in_the_source_not_the_consumer(self) -> None:
        """The gate describes the canonical snapshot, so it must execute there."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "vendored/tool.py")])
        marker = self.tmpdir / "gate-cwd.txt"

        result = self.run_tool("--sync", pin=pin, quality_command=f"sh -c 'pwd > {marker}'")

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(self.source.resolve(), Path(marker.read_text(encoding="utf-8").strip()).resolve())
        self.assertEqual("canonical\n", (self.target / "vendored/tool.py").read_text(encoding="utf-8"))

    def test_8_check_with_a_pin_also_requires_the_quality_gate(self) -> None:
        """--check with a pin answers "is this snapshot importable", which includes quality."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "vendored/tool.py")])

        result = self.run_tool("--check", pin=pin, quality_command="false")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("quality gate", result.stderr)

    def test_8_default_quality_gate_is_the_canonical_make_target(self) -> None:
        """The tool names one contract; the canonical repo owns which tools it runs."""
        self.assertIn("reusable-check:", (REPO_ROOT / "Makefile").read_text(encoding="utf-8"))
        help_text = subprocess.run([str(SYNC_TOOL), "--help"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
        self.assertIn("make reusable-check", help_text)


if __name__ == "__main__":
    unittest.main()
