from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from automation.context.build_context import build_context_bundle
from automation.schemas import models
from automation.supervisor import policy
from automation.supervisor.run_next import format_agent_command, make_decision_report, render_prompt


class AutomationHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.example_queue_path = self.repo_root / "automation/examples/example-slices.json"
        self.example_handoff_path = self.repo_root / "automation/examples/example-handoff.json"

    def require_slice_record(self, queue_data: dict[str, Any], slice_id: str) -> dict[str, Any]:
        slice_record = policy.find_slice(queue_data, slice_id)
        if slice_record is None:
            self.fail(f"Expected test fixture to contain slice {slice_id!r}.")
        return slice_record

    def require_selected_slice(self, queue_data: dict[str, Any]) -> dict[str, Any]:
        slice_record = policy.select_next_slice(queue_data)
        if slice_record is None:
            self.fail("Expected test fixture to have an eligible selected slice.")
        return slice_record

    def example_queue_data(self) -> dict[str, Any]:
        return policy.load_queue(self.example_queue_path)

    def example_slice_at(self, index: int) -> dict[str, Any]:
        return self.example_queue_data()["slices"][index]

    def example_slice_id_at(self, index: int) -> str:
        return self.example_slice_at(index)["slice_id"]

    def example_handoff_filename(self) -> str:
        return f"20260421T153000Z-{self.example_slice_id_at(0)}.json"

    def example_allowed_file(self, slice_record: dict[str, Any], filename: str = "Changed.swift") -> str:
        for allowed_path in slice_record["allowed_paths"]:
            if allowed_path.endswith("/"):
                return f"{allowed_path}{filename}"
        return slice_record["allowed_paths"][0]

    def example_markdown_path(self, slice_record: dict[str, Any]) -> str:
        for allowed_path in slice_record["allowed_paths"]:
            if allowed_path.endswith(".md"):
                return allowed_path
        return f"docs/product/domains/{slice_record['domain']}.md"

    def test_example_queue_matches_schema(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_schema = models.SliceQueue
        validation = policy.validate_document(queue_data, queue_schema)
        self.assertTrue(validation.is_valid, validation.errors)
        self.assertEqual([], policy.validate_queue_integrity(queue_data))

    def test_queue_schema_accepts_parked_slice_with_entry_condition(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_data["slices"][0]["status"] = "deferred"
        queue_data["slices"][0]["entry_condition"] = "External reviewer input exists."
        queue_schema = models.SliceQueue

        validation = policy.validate_document(queue_data, queue_schema)

        self.assertTrue(validation.is_valid, validation.errors)
        self.assertEqual([], policy.validate_queue_integrity(queue_data))

    def test_queue_integrity_rejects_parked_slice_without_entry_condition(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_data["slices"][0]["status"] = "blocked"
        queue_data["slices"][0].pop("entry_condition", None)

        errors = policy.validate_queue_integrity(queue_data)

        self.assertTrue(any("missing an explicit entry_condition" in error for error in errors), errors)

    def test_queue_integrity_rejects_unknown_recommended_unblocker(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_data["slices"][0]["status"] = "blocked"
        queue_data["slices"][0]["entry_condition"] = "External reviewer input exists."
        queue_data["slices"][0]["recommended_unblocker"] = "missing-unblocker"

        errors = policy.validate_queue_integrity(queue_data)

        self.assertTrue(any("recommends unknown unblocker" in error for error in errors), errors)

    def test_blocked_slice_reports_surface_entry_condition_and_unblocker(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_data["slices"].append(
            {
                "slice_id": "review-packet",
                "title": "Prepare review packet",
                "status": "done",
                "priority": 1,
                "domain": "localization",
                "allowed_paths": ["docs/"],
                "required_validations": ["make architecture"],
                "depends_on": [],
                "max_files_changed": 1,
                "notes": "",
            }
        )
        queue_data["slices"][0]["status"] = "blocked"
        queue_data["slices"][0]["entry_condition"] = "Reviewed values exist."
        queue_data["slices"][0]["recommended_unblocker"] = "review-packet"

        reports = policy.blocked_slice_reports(queue_data)

        self.assertEqual(1, len(reports))
        self.assertEqual(self.example_slice_id_at(0), reports[0].slice_id)
        self.assertEqual("Reviewed values exist.", reports[0].entry_condition)
        self.assertEqual("review-packet", reports[0].recommended_unblocker)

    def test_example_handoff_matches_schema(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff_schema = models.Handoff
        validation = policy.validate_document(handoff, handoff_schema)
        self.assertTrue(validation.is_valid, validation.errors)

    def test_handoff_schema_accepts_open_questions(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["open_questions"] = ["Should the empty-state copy be localized in this slice?"]
        handoff_schema = models.Handoff

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertTrue(validation.is_valid, validation.errors)

    def test_phase_reference_fragments_exist_and_are_nonempty(self) -> None:
        prompts_dir = self.repo_root / "automation/prompts"
        for fragment in (
            "intake.md",
            "triage.md",
            "design.md",
            "tdd.md",
            "diagnose.md",
            "resolve-conflicts.md",
        ):
            path = prompts_dir / fragment
            self.assertTrue(path.is_file(), f"missing fragment: {fragment}")
            self.assertTrue(path.read_text(encoding="utf-8").strip(), f"empty fragment: {fragment}")

    def test_handoff_schema_accepts_valid_proof_level(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["proof_level"] = "running-app-smoke"
        handoff["missing_proof_levels"] = ["flow-verified", "screenshot-verified"]
        handoff_schema = models.Handoff

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertTrue(validation.is_valid, validation.errors)

    def test_handoff_schema_rejects_missing_proof_level(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff.pop("proof_level")
        handoff_schema = models.Handoff

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(any("missing required property 'proof_level'" in error for error in validation.errors), validation.errors)

    def test_handoff_schema_rejects_invalid_proof_level(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["proof_level"] = "verified-in-simulator"
        handoff_schema = models.Handoff

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("$.proof_level" in error and "verified-in-simulator" in error for error in validation.errors), validation.errors
        )

    def test_handoff_schema_rejects_invalid_missing_proof_level(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["missing_proof_levels"] = ["manual-vibes"]
        handoff_schema = models.Handoff

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("$.missing_proof_levels[0]" in error and "manual-vibes" in error for error in validation.errors),
            validation.errors,
        )

    def test_handoff_schema_rejects_missing_residual_risks(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff.pop("residual_risks")
        handoff_schema = models.Handoff

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("missing required property 'residual_risks'" in error for error in validation.errors), validation.errors
        )

    def test_handoff_schema_rejects_empty_residual_risks(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["residual_risks"] = []
        handoff_schema = models.Handoff

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("$.residual_risks" in error and "expected at least 1 items" in error for error in validation.errors),
            validation.errors,
        )

    def test_handoff_schema_rejects_missing_contract_status_changes(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff.pop("contract_status_changes")
        handoff_schema = models.Handoff

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("missing required property 'contract_status_changes'" in error for error in validation.errors), validation.errors
        )

    def test_handoff_schema_rejects_invalid_repo_clean_status(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["repo_clean_status"] = "probably-clean"
        handoff_schema = models.Handoff

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("$.repo_clean_status" in error and "probably-clean" in error for error in validation.errors), validation.errors
        )

    def test_live_queue_configures_repo_owned_agent_command(self) -> None:
        queue_path = self.repo_root / "automation/queue/slices.json"
        if not queue_path.exists():
            self.skipTest("consumer live queue is not present in this checkout")
        queue_data = policy.load_queue(queue_path)
        command_template = queue_data["policy"]["agent_command_template"]

        self.assertIn("automation/supervisor/run_agent.sh", command_template)
        self.assertIn("{repo_root}", command_template)
        self.assertIn("{prompt_file}", command_template)
        self.assertIn("{context_file}", command_template)
        self.assertIn("{handoff_file}", command_template)
        self.assertIn("{slice_id}", command_template)
        script_path = self.repo_root / "automation/supervisor/run_agent.sh"
        self.assertTrue(script_path.exists())
        self.assertTrue(os.access(script_path, os.X_OK))

    def test_agent_wrapper_auto_selects_claude_for_claude_code_context(self) -> None:
        """Claude Code as the nearest enclosing harness, end to end: the fake CLI really runs.

        Described by ancestry, not by CLAUDE_CODE=1 - an inherited marker cannot say whether
        Claude Code is the nearest harness. ``test_runner_selection`` covers the rules.
        """
        script_path = self.repo_root / "automation/supervisor/run_agent.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            repo_root.mkdir()
            (repo_root / ".git").mkdir()
            prompt_path = temp_path / "prompt.md"
            prompt_path.write_text("slice prompt", encoding="utf-8")
            context_path = temp_path / "context.json"
            context_path.write_text("{}", encoding="utf-8")
            handoff_path = temp_path / "handoff.json"
            capture_path = temp_path / "capture.txt"
            bin_dir = temp_path / "bin"
            bin_dir.mkdir()
            self.write_fake_executable(
                bin_dir / "claude",
                """#!/usr/bin/env bash
{
  for arg in "$@"; do printf 'arg:%s\\n' "$arg"; done
  printf 'cwd:%s\\n' "$PWD"
  printf 'context:%s\\n' "${REPO_AUTOMATION_SUPERVISOR_CONTEXT_FILE:-}"
  printf 'handoff:%s\\n' "${REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE:-}"
  printf 'slice:%s\\n' "${REPO_AUTOMATION_SUPERVISOR_SLICE_ID:-}"
  printf 'legacy_context:%s\\n' "${OWLORY_SUPERVISOR_CONTEXT_FILE:-}"
  printf 'stdin:'
  cat
  printf '\\n'
} > "$CAPTURE_FILE"
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["CAPTURE_FILE"] = str(capture_path)
            tree = temp_path / "tree.tsv"
            tree.write_text(
                "100\t101\t-zsh\t-zsh\n101\t0\t/opt/homebrew/bin/claude\t/opt/homebrew/bin/claude\n", encoding="utf-8"
            )
            env["FAKE_PS_TREE"] = str(tree)
            env.pop("REPO_AUTOMATION_AGENT_RUNNER", None)

            result = subprocess.run(
                [
                    str(script_path),
                    "--repo-root",
                    str(repo_root),
                    "--prompt-file",
                    str(prompt_path),
                    "--context-file",
                    str(context_path),
                    "--handoff-file",
                    str(handoff_path),
                    "--slice-id",
                    "slice-a",
                ],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            capture = capture_path.read_text(encoding="utf-8")
            self.assertIn("arg:--print", capture)
            self.assertIn("arg:--input-format", capture)
            self.assertIn("arg:text", capture)
            self.assertIn("arg:--no-session-persistence", capture)
            self.assertIn("arg:--permission-mode", capture)
            self.assertIn("arg:bypassPermissions", capture)
            self.assertIn("arg:--add-dir", capture)
            self.assertIn(f"arg:{repo_root}", capture)
            self.assertIn(f"cwd:{repo_root}", capture)
            self.assertIn(f"context:{context_path}", capture)
            self.assertIn(f"handoff:{handoff_path}", capture)
            self.assertIn("slice:slice-a", capture)
            self.assertIn(f"legacy_context:{context_path}", capture)
            self.assertIn("stdin:slice prompt", capture)

    def test_agent_wrapper_can_still_launch_codex_when_requested(self) -> None:
        script_path = self.repo_root / "automation/supervisor/run_agent.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            repo_root.mkdir()
            (repo_root / ".git").mkdir()
            prompt_path = temp_path / "prompt.md"
            prompt_path.write_text("codex prompt", encoding="utf-8")
            context_path = temp_path / "context.json"
            context_path.write_text("{}", encoding="utf-8")
            handoff_path = temp_path / "handoff.json"
            capture_path = temp_path / "capture.txt"
            bin_dir = temp_path / "bin"
            bin_dir.mkdir()
            self.write_fake_executable(
                bin_dir / "codex",
                """#!/usr/bin/env bash
{
  for arg in "$@"; do printf 'arg:%s\\n' "$arg"; done
  printf 'cwd:%s\\n' "$PWD"
  printf 'stdin:'
  cat
  printf '\\n'
} > "$CAPTURE_FILE"
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["CAPTURE_FILE"] = str(capture_path)
            env["REPO_AUTOMATION_AGENT_RUNNER"] = "codex"

            result = subprocess.run(
                [
                    str(script_path),
                    "--repo-root",
                    str(repo_root),
                    "--prompt-file",
                    str(prompt_path),
                    "--context-file",
                    str(context_path),
                    "--handoff-file",
                    str(handoff_path),
                    "--slice-id",
                    "slice-b",
                ],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            capture = capture_path.read_text(encoding="utf-8")
            self.assertIn("arg:--ask-for-approval", capture)
            self.assertIn("arg:never", capture)
            self.assertIn("arg:exec", capture)
            self.assertIn("arg:--sandbox", capture)
            self.assertIn("arg:workspace-write", capture)
            self.assertIn("arg:-", capture)
            self.assertIn(f"cwd:{repo_root}", capture)
            self.assertIn("stdin:codex prompt", capture)

    def test_agent_wrapper_launches_hermes_in_the_benchmark_posture_with_the_prompt_verbatim(self) -> None:
        script_path = self.repo_root / "automation/supervisor/run_agent.sh"
        # Hermes takes the prompt as an argument rather than on stdin, so the wrapper has
        # to hand it over without a shell ever looking at it.
        prompt = "hermes prompt $(touch pwned) `touch pwned` \"quoted\" 'single'"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            repo_root.mkdir()
            (repo_root / ".git").mkdir()
            prompt_path = temp_path / "prompt.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            context_path = temp_path / "context.json"
            context_path.write_text("{}", encoding="utf-8")
            handoff_path = temp_path / "handoff.json"
            capture_path = temp_path / "capture.txt"
            bin_dir = temp_path / "bin"
            bin_dir.mkdir()
            self.write_fake_executable(
                bin_dir / "hermes",
                """#!/usr/bin/env bash
{
  for arg in "$@"; do printf 'arg:%s\\n' "$arg"; done
  printf 'cwd:%s\\n' "$PWD"
  printf 'slice:%s\\n' "${REPO_AUTOMATION_SUPERVISOR_SLICE_ID:-}"
  printf 'home:%s\\n' "${HERMES_HOME:-}"
  printf 'safemode:%s\\n' "${HERMES_SAFE_MODE:-}"
  printf 'ignoreconfig:%s\\n' "${HERMES_IGNORE_USER_CONFIG:-}"
  printf 'termcwd:%s\\n' "${TERMINAL_CWD:-}"
} > "$CAPTURE_FILE"
""",
            )
            usage_dir = temp_path / "sessions"
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["CAPTURE_FILE"] = str(capture_path)
            env["REPO_AUTOMATION_AGENT_RUNNER"] = "hermes"
            env["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str(usage_dir)
            env["HERMES_HOME"] = str(temp_path / "shared-home")

            result = subprocess.run(
                [
                    str(script_path),
                    "--repo-root",
                    str(repo_root),
                    "--prompt-file",
                    str(prompt_path),
                    "--context-file",
                    str(context_path),
                    "--handoff-file",
                    str(handoff_path),
                    "--slice-id",
                    "slice-c",
                ],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            capture = capture_path.read_text(encoding="utf-8")
            # --ignore-rules, not --safe-mode. The flag also sets HERMES_IGNORE_USER_CONFIG,
            # which discards the benchmark config profile and falls back to Hermes' own
            # defaults - a different operating posture than the one being measured.
            self.assertIn("arg:--ignore-rules", capture)
            self.assertNotIn("arg:--safe-mode", capture)
            self.assertIn("arg:--in", capture)
            self.assertIn(f"arg:{repo_root}", capture)
            self.assertIn("arg:--oneshot", capture)
            self.assertIn(f"arg:{prompt}", capture)
            self.assertIn(f"cwd:{repo_root}", capture)
            # The agent's tools pick their own working directory and prefer TERMINAL_CWD to
            # the process one, so --in alone let a session write outside the checkout it was
            # given: the benchmark archived an empty workspace and scored every lane
            # compile_failed while the work sat in the home directory.
            self.assertIn(f"termcwd:{repo_root}", capture)
            self.assertIn("slice:slice-c", capture)
            self.assertFalse((repo_root / "pwned").exists())
            self.assertFalse((self.repo_root / "pwned").exists())
            # Nothing else narrows the toolset: the default CLI set includes delegate_task,
            # memory and session_search. Pinning it is what keeps a session to one agent.
            self.assertIn("arg:--toolsets", capture)
            self.assertIn("arg:terminal,file,code_execution,todo", capture)
            # A throwaway home per invocation: a shared one carries sessions/ and memories/
            # from the previous session into the next.
            lines = capture.splitlines()
            args = [line.removeprefix("arg:") for line in lines if line.startswith("arg:")]
            home = next(line.removeprefix("home:") for line in lines if line.startswith("home:"))
            self.assertNotEqual(str(temp_path / "shared-home"), home)
            self.assertTrue(Path(home).is_dir())

            # Plugins, MCP servers, webhooks and shell hooks still go off, via the env var
            # rather than the flag, so the config profile survives.
            self.assertIn("safemode:1", capture)
            self.assertEqual("", next(line.removeprefix("ignoreconfig:") for line in lines if line.startswith("ignoreconfig:")))
            # The profile is what makes the benchmark reproduce the deployed posture rather
            # than Hermes' defaults, so it has to arrive in the throwaway home intact.
            profile = self.repo_root / "automation/supervisor/hermes-benchmark.yaml"
            self.assertEqual(profile.read_bytes(), (Path(home) / "config.yaml").read_bytes())

            controls = sorted(usage_dir.glob("*.controls.json"))
            self.assertEqual(1, len(controls), f"expected one controls report, got {controls}")
            recorded = json.loads(controls[0].read_text(encoding="utf-8"))
            self.assertEqual(["terminal", "file", "code_execution", "todo"], recorded["requested_toolsets"])
            # Requested, not effective: this transport never reports back which tools the
            # session was actually served, and restating the request as a confirmation is
            # how a controls receipt starts lying.
            self.assertIsNone(recorded["effective_tools"])
            self.assertEqual([], recorded["refused_tools"])
            self.assertEqual("local", recorded["sandbox"]["backend"])
            self.assertTrue(recorded["safe_mode_env"])
            self.assertTrue(recorded["ignore_rules"])
            # The claim the whole revision turns on: the config profile was not discarded.
            self.assertFalse(recorded["ignore_user_config"])
            self.assertEqual("kanban-benchmark-v1", recorded["config_profile"])
            self.assertEqual(
                hashlib.sha256((self.repo_root / "automation/supervisor/hermes-benchmark.yaml").read_bytes()).hexdigest(),
                recorded["config_sha256"],
            )
            self.assertEqual("slice-c", recorded["slice_id"])
            self.assertEqual(home, recorded["hermes_home"])
            self.assertEqual(str(repo_root), recorded["terminal_cwd"])
            # The usage report and the controls that qualify it name the same session.
            usage_arg = Path(args[args.index("--usage-file") + 1])
            self.assertEqual(controls[0].name.replace(".controls.json", ".usage.json"), usage_arg.name)

    def test_agent_wrapper_runs_the_tools_inside_the_cleanroom_image_when_given_one(self) -> None:
        """A named sandbox image moves the model-facing tools into it, and says so.

        Run 35799016896's research session spent 48 seconds cloning the upstream repository
        it was supposed to rebuild from observation. Nothing in the harness stopped it,
        because the tools ran on the runner's own filesystem with the runner's network.
        """
        script_path = self.repo_root / "automation/supervisor/run_agent.sh"
        image = "programbench/abishekvashok_1776_cmatrix.5c082c6:task_cleanroom_v6"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            (repo_root / ".git").mkdir(parents=True)
            prompt_path = temp_path / "prompt.md"
            prompt_path.write_text("rebuild it", encoding="utf-8")
            context_path = temp_path / "context.json"
            context_path.write_text("{}", encoding="utf-8")
            bin_dir = temp_path / "bin"
            bin_dir.mkdir()
            self.write_fake_executable(
                bin_dir / "hermes", '#!/usr/bin/env bash\nprintf \'home:%s\\n\' "$HERMES_HOME" > "$CAPTURE_FILE"\n'
            )
            self.write_fake_executable(bin_dir / "docker", "#!/usr/bin/env bash\nprintf 'sha256:c0ffee\\n'\n")
            usage_dir = temp_path / "sessions"
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["CAPTURE_FILE"] = str(temp_path / "capture.txt")
            env["REPO_AUTOMATION_AGENT_RUNNER"] = "hermes"
            env["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str(usage_dir)
            env["REPO_AUTOMATION_HERMES_SANDBOX_IMAGE"] = image

            result = subprocess.run(
                [
                    str(script_path),
                    "--repo-root",
                    str(repo_root),
                    "--prompt-file",
                    str(prompt_path),
                    "--context-file",
                    str(context_path),
                    "--handoff-file",
                    str(temp_path / "handoff.json"),
                    "--slice-id",
                    "slice-cleanroom",
                ],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)

            home = Path((temp_path / "capture.txt").read_text(encoding="utf-8").strip().removeprefix("home:"))
            effective = (home / "config.yaml").read_text(encoding="utf-8")
            # The deployed posture, still first, with the sandbox appended rather than
            # substituted: a cleanroom cell and a local cell differ in one block.
            profile = (self.repo_root / "automation/supervisor/hermes-benchmark.yaml").read_text(encoding="utf-8")
            self.assertTrue(effective.startswith(profile))
            self.assertIn("backend: docker", effective)
            self.assertIn("docker_network: false", effective)
            self.assertIn("docker_mount_cwd_to_workspace: true", effective)
            self.assertIn(f'docker_image: "{image}"', effective)
            # `docker_mount_cwd_to_workspace` above only binds the workspace for the CLI
            # parent's "default" backend; the pinned revision refuses to derive that mount
            # for a session-scoped container, which is what every tool call inside an agent
            # turn resolves to. The explicit binding applies to every task id.
            self.assertIn(f'docker_volumes: ["{repo_root}:/workspace"]', effective)
            # The key that keeps NVIDIA_API_KEY in the Hermes process and out of the
            # container the model's commands run in.
            self.assertIn("docker_forward_env: []", effective)
            # Reuse across Hermes processes is keyed by task/profile/egress labels and the
            # network mode - never by image and never by bind mount - and a CLI session's
            # task id stays "default", so container_persistent alone leaves a cell free to
            # inherit the previous cell's container, image and workspace mount.
            self.assertIn("docker_persist_across_processes: false", effective)

            controls = json.loads(sorted(usage_dir.glob("*.controls.json"))[0].read_text(encoding="utf-8"))
            self.assertEqual("docker", controls["sandbox"]["backend"])
            self.assertEqual(image, controls["sandbox"]["image"])
            self.assertEqual("sha256:c0ffee", controls["sandbox"]["image_id"])
            self.assertEqual("none", controls["sandbox"]["network"])
            self.assertEqual("/workspace", controls["sandbox"]["workspace"])
            self.assertEqual([], controls["sandbox"]["credentials_forwarded"])
            # The hash has to describe the file Hermes loaded. Hashing the template would
            # report the local posture for a run that was not local.
            self.assertEqual(
                hashlib.sha256((home / "config.yaml").read_bytes()).hexdigest(),
                controls["config_sha256"],
            )
            self.assertNotEqual(hashlib.sha256(profile.encode()).hexdigest(), controls["config_sha256"])

    # The documentation names ProgramBench actually ships, for an image whose docs are a
    # FAQ, an extensionless README and a man page - and no `README.md` anywhere.
    FIGLET_WORKSPACE_ENTRIES = ("FAQ", "LICENSE", "README", "figlet.6")

    def test_the_sandbox_probe_accepts_a_cleanroom_whose_docs_are_not_named_readme_md(self) -> None:
        """The probe asks whether Hermes got the workspace, not what the docs are called.

        `cmatsuoka__figlet.202a0a8` ships FAQ, README and figlet.6. The probe used to run
        `test -f README.md`, so it refused that cell - and because a CleanroomError aborts
        the whole run rather than the cell, run 36724292484 lost every instance after it,
        35 sound cells in, over a filename. cleanroom.py already refuses an image with no
        documentation at all, by type and convention rather than by one name.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workspace = temp_path / "workspace"
            (workspace / ".git").mkdir(parents=True)
            self.write_fake_executable(workspace / "executable", "#!/usr/bin/env bash\nprintf 'figlet\\n'\n")
            for name in self.FIGLET_WORKSPACE_ENTRIES:
                (workspace / name).write_text(f"{name} contents\n", encoding="utf-8")
            self.assertFalse((workspace / "README.md").exists())

            bin_dir = temp_path / "bin"
            bin_dir.mkdir()
            self.write_fake_executable(bin_dir / "hermes", "#!/usr/bin/env bash\nexit 1\n")
            self.write_fake_executable(bin_dir / "docker", "#!/usr/bin/env bash\nprintf 'sha256:c0ffee\\n'\n")
            # Stands in for Hermes' interpreter running terminal_tool twice: it discards the
            # probe program on stdin and runs the composed command where the container would
            # run it, so what is under test is the command itself against real file shapes.
            self.write_fake_executable(
                bin_dir / "python",
                "#!/usr/bin/env bash\ncat > /dev/null\nstatus=0\n"
                'for task in default "probe-$$"; do\n'
                '  if output="$(sh -c "$REPO_AUTOMATION_SANDBOX_PROBE_COMMAND" 2>&1)"; then code=0; else code=$?; status=1; fi\n'
                '  printf \'{"task_id": "%s", "result": {"output": "%s", "exit_code": %s}}\\n\' "$task" "$output" "$code"\n'
                "done\nexit $status\n",
            )

            usage_dir = temp_path / "sessions"
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["REPO_AUTOMATION_AGENT_RUNNER"] = "hermes"
            env["REPO_AUTOMATION_HERMES_PYTHON"] = str(bin_dir / "python")
            env["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str(usage_dir)
            env["REPO_AUTOMATION_HERMES_SANDBOX_IMAGE"] = "programbench/cmatsuoka_1776_figlet.202a0a8:task_cleanroom_v6"

            result = subprocess.run(
                [str(self.repo_root / "automation/supervisor/run_agent.sh"), "--repo-root", str(workspace), "--sandbox-probe"],
                # Where a cleanroom cell runs it, and where `docker -w /workspace` puts the
                # command: relative paths in the probe resolve against the workspace.
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            probe_log = (usage_dir / "sandbox-probe.log").read_text(encoding="utf-8")
            self.assertEqual(2, probe_log.count('"exit_code": 0'))
            self.assertNotIn('"exit_code": 1', probe_log)
            # The fix is the absence of a second documentation contract, so assert the
            # absence: a future edit that reintroduces a filename fails here.
            self.assertNotIn("README", probe_log)

    def fake_hermes_importables(self, root: Path) -> Path:
        """Enough of Hermes to run the probe program itself: the config bridge and one tool.

        The other sandbox-probe tests stand in for Hermes' interpreter with a shell script,
        which means the probe program on its stdin is discarded. These two are about what
        that program composes, so it has to actually run - against a `terminal_tool` that
        records the command it was handed and executes it where a container would.
        """
        (root / "hermes_cli").mkdir(parents=True)
        (root / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (root / "hermes_cli" / "config.py").write_text("def apply_terminal_config_to_env():\n    return None\n", encoding="utf-8")
        (root / "tools").mkdir()
        (root / "tools" / "__init__.py").write_text("", encoding="utf-8")
        (root / "tools" / "terminal_tool.py").write_text(
            "import json\nimport os\nimport subprocess\n\n\n"
            "def terminal_tool(command, task_id=None):\n"
            "    with open(os.environ['FAKE_TERMINAL_LOG'], 'a', encoding='utf-8') as log:\n"
            "        log.write(json.dumps({'task_id': task_id, 'command': command}) + '\\n')\n"
            "    done = subprocess.run(['sh', '-c', command], capture_output=True, text=True)\n"
            "    return json.dumps({'output': (done.stdout + done.stderr).strip(), 'exit_code': done.returncode})\n",
            encoding="utf-8",
        )
        return root

    def sandbox_probe_barrier_run(self, temp_path: Path, *, released: tuple[int, ...], seconds: str) -> tuple[Any, Path, Path]:
        """Run `--sandbox-probe` for real, with only the witness's side of it faked."""
        workspace = temp_path / "workspace"
        (workspace / ".git").mkdir(parents=True)
        self.write_fake_executable(workspace / "executable", "#!/usr/bin/env bash\nexit 0\n")
        barrier = workspace / ".sandbox-probe-barrier"
        barrier.mkdir()
        for release in released:
            (barrier / f"released-{release}").write_text("", encoding="utf-8")

        bin_dir = temp_path / "bin"
        bin_dir.mkdir()
        self.write_fake_executable(bin_dir / "hermes", "#!/usr/bin/env bash\nexit 1\n")
        self.write_fake_executable(bin_dir / "docker", "#!/usr/bin/env bash\nprintf 'sha256:c0ffee\\n'\n")
        usage_dir = temp_path / "sessions"
        terminal_log = temp_path / "terminal.jsonl"

        # Everything this harness measures itself with, dropped: these two tests are the
        # only ones that hand `run_agent.sh` a real interpreter, and pytest-cov's
        # subprocess hook would have it write statement-coverage data into a run
        # configured for branch coverage - which `coverage combine` refuses outright,
        # failing the whole job after every test has passed. The child stands in for
        # Hermes inside a cleanroom; it has no business inheriting our instrumentation.
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("COV_CORE_") and not name.startswith("COVERAGE_")
        }
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        env["PYTHONPATH"] = str(self.fake_hermes_importables(temp_path / "fake-hermes"))
        env["REPO_AUTOMATION_AGENT_RUNNER"] = "hermes"
        env["REPO_AUTOMATION_HERMES_PYTHON"] = sys.executable
        env["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str(usage_dir)
        env["REPO_AUTOMATION_HERMES_SANDBOX_IMAGE"] = "programbench/x.1:task_cleanroom_v6"
        # The path the waiting command sees. A cell passes the container's view,
        # /workspace/...; with no container here, the command runs where the bind mount
        # would have put it.
        env["REPO_AUTOMATION_SANDBOX_PROBE_BARRIER"] = str(barrier)
        env["REPO_AUTOMATION_SANDBOX_PROBE_BARRIER_SECONDS"] = seconds
        env["FAKE_TERMINAL_LOG"] = str(terminal_log)

        result = subprocess.run(
            [str(self.repo_root / "automation/supervisor/run_agent.sh"), "--repo-root", str(workspace), "--sandbox-probe"],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )
        return result, terminal_log, usage_dir

    def test_the_sandbox_probe_holds_each_container_open_until_it_is_released(self) -> None:
        """Both backends wait on their own marker, and neither exits before it appears.

        A probe container lives for exactly one `docker exec`, and in run 37275443406 the
        session-scoped one lived 267ms against SandboxWitness's 500ms poll - so a cleanroom
        whose own probe reported both backends sound was refused for the one nobody saw.
        The wait is what orders the inspection before the teardown.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            result, terminal_log, _ = self.sandbox_probe_barrier_run(Path(temp_dir), released=(1, 2), seconds="15")

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = [json.loads(line) for line in terminal_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(2, len(calls))
            # One marker per call, in the order the witness records the containers: the
            # witness knows the label it saw, only this side knows which call it is in.
            self.assertIn("released-1", calls[0]["command"])
            self.assertIn("released-2", calls[1]["command"])
            # The default backend is task_id=None, the session-scoped one its own id.
            self.assertIsNone(calls[0]["task_id"])
            self.assertTrue(str(calls[1]["task_id"]).startswith("probe-"))
            # The cleanroom contract is still the whole of what is being checked; the wait
            # is appended to it and prints nothing of its own.
            for call in calls:
                self.assertIn("test -x ./executable", call["command"])
            self.assertEqual(2, result.stdout.count('"exit_code": 0'))

    def test_a_sandbox_probe_no_witness_releases_fails_in_bounded_time(self) -> None:
        """A witness that never records the container must cost seconds, not the budget.

        The failure has to stay loud and cheap: if waiting to be inspected could hang, the
        protection against an unproved backend would cost more than the thing it protects.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            result, _, usage_dir = self.sandbox_probe_barrier_run(Path(temp_dir), released=(), seconds="1")

            self.assertNotEqual(0, result.returncode)
            probe_log = (usage_dir / "sandbox-probe.log").read_text(encoding="utf-8")
            self.assertEqual(2, probe_log.count('"exit_code": 75'))
            self.assertIn("no witness recorded this container within 1s", probe_log)

    def test_agent_wrapper_keeps_the_session_output_beside_its_reports(self) -> None:
        """Why a session failed is only ever printed; the usage report never says.

        A provider that refuses to serve and an agent that does badly both end as a failed
        turn with no model, and the benchmark has to tell those apart to keep a rate limit
        out of a lane delta.
        """
        script_path = self.repo_root / "automation/supervisor/run_agent.sh"
        refusal = "\u274c Rate limited after 3 retries \u2014 HTTP 429"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            (repo_root / ".git").mkdir(parents=True)
            prompt_path = temp_path / "prompt.md"
            prompt_path.write_text("rebuild it", encoding="utf-8")
            context_path = temp_path / "context.json"
            context_path.write_text("{}", encoding="utf-8")
            bin_dir = temp_path / "bin"
            bin_dir.mkdir()
            self.write_fake_executable(
                bin_dir / "hermes",
                f"""#!/usr/bin/env bash
printf '%s\\n' "{refusal}"
printf 'on stderr\\n' >&2
exit 7
""",
            )
            usage_dir = temp_path / "sessions"
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["REPO_AUTOMATION_AGENT_RUNNER"] = "hermes"
            env["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str(usage_dir)

            result = subprocess.run(
                [
                    str(script_path),
                    "--repo-root",
                    str(repo_root),
                    "--prompt-file",
                    str(prompt_path),
                    "--context-file",
                    str(context_path),
                    "--handoff-file",
                    str(temp_path / "handoff.json"),
                    "--slice-id",
                    "slice-c:implement",
                ],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
            )

            # The session's own exit status, not the tee's: a logging failure is not a phase
            # failure, and a phase failure must not be swallowed into a clean exit.
            self.assertEqual(7, result.returncode, result.stderr)
            self.assertIn(refusal, result.stdout)  # the job log still streams it live
            logs = sorted(usage_dir.glob("*.log"))
            self.assertEqual(1, len(logs), f"expected one session log, got {logs}")
            recorded = logs[0].read_text(encoding="utf-8")
            self.assertIn(refusal, recorded)
            self.assertIn("on stderr", recorded)  # stderr carries the failure, so it is kept
            controls = sorted(usage_dir.glob("*.controls.json"))
            self.assertEqual([logs[0].name.removesuffix(".log")], [c.name.removesuffix(".controls.json") for c in controls])

    def _run_hermes_wrapper(self, temp_path: Path, hermes_body: str, extra_env: dict) -> subprocess.CompletedProcess:
        """Launch the wrapper on the hermes runner with a fake CLI, and hand back its exit."""
        repo_root = temp_path / "repo"
        (repo_root / ".git").mkdir(parents=True)
        prompt_path = temp_path / "prompt.md"
        prompt_path.write_text("rebuild it", encoding="utf-8")
        context_path = temp_path / "context.json"
        context_path.write_text("{}", encoding="utf-8")
        bin_dir = temp_path / "bin"
        bin_dir.mkdir()
        self.write_fake_executable(bin_dir / "hermes", hermes_body)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        env["REPO_AUTOMATION_AGENT_RUNNER"] = "hermes"
        env["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str(temp_path / "sessions")
        env.update(extra_env)

        return subprocess.run(
            [
                str(self.repo_root / "automation/supervisor/run_agent.sh"),
                "--repo-root",
                str(repo_root),
                "--prompt-file",
                str(prompt_path),
                "--context-file",
                str(context_path),
                "--handoff-file",
                str(temp_path / "handoff.json"),
                "--slice-id",
                "slice-c:research",
            ],
            cwd=self.repo_root,
            env=env,
            capture_output=True,
            text=True,
        )

    ARG_ECHO = '#!/usr/bin/env bash\nfor arg in "$@"; do printf \'arg:%s\\n\' "$arg"; done\ncat "$HERMES_HOME/config.yaml"\n'

    def test_agent_wrapper_caps_the_turn_on_the_only_transport_that_consumes_a_cap(self) -> None:
        """The cap reaches the session as the `--max-turns` flag on the chat path.

        _CHAT_PASSTHROUGH carries it into the CLI, which hands it to AIAgent as
        max_iterations. Config is not an alternative here: the same value in config.yaml is
        read by the same chat-path resolver, so writing it there would cap nothing that the
        flag does not already cap - and would cap it through the file every lane shares.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            result = self._run_hermes_wrapper(
                temp_path,
                self.ARG_ECHO,
                {"REPO_AUTOMATION_HERMES_MAX_TURNS": "1", "REPO_AUTOMATION_HERMES_TRANSPORT": "stream-json"},
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("arg:--max-turns", result.stdout)
            self.assertIn("arg:1", result.stdout)
            controls = json.loads(sorted((temp_path / "sessions").glob("*.controls.json"))[0].read_text(encoding="utf-8"))
            self.assertEqual(1, controls["max_turns"])
            session_config = Path(controls["hermes_home"]) / "config.yaml"
            self.assertNotIn("max_turns", session_config.read_text(encoding="utf-8"))
            shared = (self.repo_root / "automation/supervisor/hermes-benchmark.yaml").read_text(encoding="utf-8")
            self.assertNotIn("max_turns", shared)

    def test_agent_wrapper_pins_the_model_on_the_transport_that_ignores_the_env_var(self) -> None:
        """HERMES_INFERENCE_MODEL reaches --oneshot and the TUI and nothing else. On the chat
        path cli_init_mixin resolves `model or config.model.default or ""` and says the
        environment is deliberately not consulted, so run 36822804022 asked for z-ai/glm-5.3
        here and was billed nvidia/nemotron-3-ultra-550b-a55b. The flag is the seam the pinned
        CLI already offers: cmd_chat passes args.model into cli_main, and _CHAT_PASSTHROUGH
        carries provider. Config is not used, so `-f model=...` stays the experiment's control.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            result = self._run_hermes_wrapper(
                temp_path,
                self.ARG_ECHO,
                {
                    "REPO_AUTOMATION_HERMES_TRANSPORT": "stream-json",
                    "HERMES_INFERENCE_MODEL": "z-ai/glm-5.3",
                    "HERMES_INFERENCE_PROVIDER": "nvidia",
                },
            )

            self.assertEqual(0, result.returncode, result.stderr)
            args = [line.removeprefix("arg:") for line in result.stdout.splitlines() if line.startswith("arg:")]
            self.assertEqual("z-ai/glm-5.3", args[args.index("--model") + 1])
            self.assertEqual("nvidia", args[args.index("--provider") + 1])
            session_config = (
                Path(
                    json.loads(sorted((temp_path / "sessions").glob("*.controls.json"))[0].read_text(encoding="utf-8"))[
                        "hermes_home"
                    ]
                )
                / "config.yaml"
            )
            self.assertNotIn("model:", session_config.read_text(encoding="utf-8"))

    def test_agent_wrapper_sends_no_model_flag_when_no_model_was_asked_for(self) -> None:
        """An empty pin must not become `--model ""`, which the CLI would take as a request."""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_hermes_wrapper(
                Path(temp_dir),
                self.ARG_ECHO,
                {
                    "REPO_AUTOMATION_HERMES_TRANSPORT": "stream-json",
                    "HERMES_INFERENCE_MODEL": "",
                    "HERMES_INFERENCE_PROVIDER": "",
                },
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("arg:--model", result.stdout)
            self.assertNotIn("arg:--provider", result.stdout)

    def test_the_lane_transport_argv_is_unchanged_by_the_diagnostic_pin(self) -> None:
        """A-E is measured on --oneshot, which reads the env var itself. Adding the flags there
        would change the argv of every lane to fix a transport no lane uses."""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_hermes_wrapper(
                Path(temp_dir),
                self.ARG_ECHO,
                {
                    "REPO_AUTOMATION_HERMES_TRANSPORT": "oneshot",
                    "HERMES_INFERENCE_MODEL": "z-ai/glm-5.3",
                    "HERMES_INFERENCE_PROVIDER": "nvidia",
                },
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("arg:--oneshot", result.stdout)
            self.assertNotIn("arg:--model", result.stdout)
            self.assertNotIn("arg:--provider", result.stdout)

    def test_agent_wrapper_refuses_a_turn_cap_the_oneshot_path_would_ignore(self) -> None:
        """Ignoring it is the failure that matters. The pinned --oneshot builds its AIAgent
        without max_iterations, so sys.maxsize stands and the flag, agent.max_turns and
        HERMES_MAX_ITERATIONS are all read on the chat path it bypasses. A session that ran to
        the wall clock under a receipt saying max_turns=1 would be a mislabelled measurement.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_hermes_wrapper(
                Path(temp_dir),
                "#!/usr/bin/env bash\necho launched\n",
                {"REPO_AUTOMATION_HERMES_MAX_TURNS": "1", "REPO_AUTOMATION_HERMES_TRANSPORT": "oneshot"},
            )

            self.assertEqual(64, result.returncode, result.stdout)
            self.assertIn("only honoured on the stream-json transport", result.stderr)
            self.assertNotIn("launched", result.stdout)

    def test_agent_wrapper_refuses_a_turn_cap_that_is_not_a_positive_integer(self) -> None:
        """`--max-turns one` would be rejected by the agent's own parser after the session had
        already been set up, and `--max-turns 0` is not a cap the diagnostic can read."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            for bad in ("one", "0", "-1", "1.5"):
                with self.subTest(max_turns=bad):
                    result = self._run_hermes_wrapper(
                        (temp_path / bad.replace(".", "_")),
                        "#!/usr/bin/env bash\necho launched\n",
                        {
                            "REPO_AUTOMATION_HERMES_MAX_TURNS": bad,
                            "REPO_AUTOMATION_HERMES_TRANSPORT": "stream-json",
                        },
                    )

                    self.assertEqual(64, result.returncode, result.stdout)
                    self.assertIn("must be a positive integer", result.stderr)
                    self.assertNotIn("launched", result.stdout)

    def test_agent_wrapper_leaves_the_turn_budget_alone_when_no_cap_is_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            result = self._run_hermes_wrapper(temp_path, self.ARG_ECHO, {"REPO_AUTOMATION_HERMES_TRANSPORT": "stream-json"})

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("max-turns", result.stdout)
            self.assertNotIn("max_turns", result.stdout)
            controls = sorted((temp_path / "sessions").glob("*.controls.json"))
            self.assertIsNone(json.loads(controls[0].read_text(encoding="utf-8"))["max_turns"])

    def test_format_agent_command_shell_quotes_placeholder_values(self) -> None:
        formatted = format_agent_command(
            command_template=(
                "runner --repo {repo_root} --prompt {prompt_file} "
                "--context {context_file} --handoff {handoff_file} --slice {slice_id}"
            ),
            repo_root=Path("/tmp/Repo With Space"),
            prompt_path=Path("/tmp/prompt file.md"),
            context_path=Path("/tmp/context file.json"),
            handoff_path=Path("/tmp/handoff file.json"),
            slice_id="today slice",
        )

        self.assertIn("--repo '/tmp/Repo With Space'", formatted)
        self.assertIn("--prompt '/tmp/prompt file.md'", formatted)
        self.assertIn("--context '/tmp/context file.json'", formatted)
        self.assertIn("--handoff '/tmp/handoff file.json'", formatted)
        self.assertIn("--slice 'today slice'", formatted)

    def test_replay_validation_commands_replays_only_exact_allowlist(self) -> None:
        calls: list[list[str]] = []

        def runner(_repo_root: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        replay_results = policy.replay_validation_commands(
            repo_root=self.repo_root,
            required_validations=["make architecture", "make test-domain DOMAIN=today", "git diff --check"],
            validations_passed=["make architecture", "make test-domain DOMAIN=today", "git diff --check"],
            runner=runner,
        )

        self.assertEqual([["make", "architecture"], ["git", "diff", "--check"]], calls)
        self.assertEqual(["make architecture", "git diff --check"], [replay.command for replay in replay_results])
        self.assertTrue(all(replay.success for replay in replay_results))

    def test_validation_ownership_classifier_marks_replayable_report_only_and_never_owned(self) -> None:
        ownership = policy.classify_validation_ownerships(
            ["make architecture", "make test-domain DOMAIN=today", "open -a Simulator"]
        )

        self.assertEqual(
            ["supervisor_replayable", "run_report_only", "never_supervisor_owned"], [item.tier for item in ownership]
        )

    def test_select_next_slice_respects_priority_and_dependencies(self) -> None:
        queue_data = {
            "version": 1,
            "policy": {
                "consecutive_autonomous_limit": 2,
                "handoff_timeout_seconds": 30,
                "agent_command_template": "",
                "supervisor_owned_paths": ["automation/queue/slices.json", "automation/handoffs/"],
            },
            "slices": [
                {
                    "slice_id": "a",
                    "title": "A",
                    "status": "done",
                    "priority": 20,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": [],
                    "max_files_changed": 2,
                    "notes": "",
                },
                {
                    "slice_id": "b",
                    "title": "B",
                    "status": "queued",
                    "priority": 30,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": ["a"],
                    "max_files_changed": 2,
                    "notes": "",
                },
                {
                    "slice_id": "c",
                    "title": "C",
                    "status": "queued",
                    "priority": 10,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": ["missing"],
                    "max_files_changed": 2,
                    "notes": "",
                },
            ],
        }

        self.assertEqual("b", self.require_selected_slice(queue_data)["slice_id"])

    def test_scope_check_ignores_supervisor_owned_paths(self) -> None:
        dirty_paths = [
            "automation/queue/slices.json",
            "automation/handoffs/20260421T153000Z-slice.json",
            "docs/product/domains/today.md",
        ]
        unexpected = policy.out_of_scope_paths(
            dirty_paths=dirty_paths,
            allowed_paths=["docs/product/domains/today.md"],
            supervisor_owned_paths=["automation/queue/slices.json", "automation/handoffs/"],
        )
        self.assertEqual([], unexpected)

    def test_completion_decision_stops_on_required_validation_failure(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)
        handoff["validations_passed"] = [
            validation for validation in slice_record["required_validations"] if validation != "git diff --check"
        ]
        handoff["recommended_next_slice"] = ""
        handoff["recommended_next_reason"] = ""

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=2,
        )

        self.assertEqual("failed", decision.queue_status)
        self.assertEqual("stop_failed", decision.decision)
        self.assertFalse(decision.should_continue)
        self.assertEqual(["git diff --check"], decision.required_validation_failures)

    def test_completion_decision_rejects_unknown_recommended_next_slice(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)
        handoff["recommended_next_slice"] = "brand-new-slice"
        handoff["recommended_next_reason"] = "The agent guessed at unqueued work."

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("done", decision.queue_status)
        self.assertEqual("stop_for_review", decision.decision)
        self.assertFalse(decision.should_continue)
        self.assertIn("not present in automation/queue/slices.json", decision.stop_reason)

    def test_completion_decision_rejects_recommendation_that_skips_higher_priority_slice(self) -> None:
        queue_data = {
            "version": 1,
            "policy": {
                "consecutive_autonomous_limit": 3,
                "handoff_timeout_seconds": 30,
                "agent_command_template": "",
                "supervisor_owned_paths": ["automation/queue/slices.json", "automation/handoffs/"],
            },
            "slices": [
                {
                    "slice_id": "bootstrap",
                    "title": "Bootstrap",
                    "status": "in_progress",
                    "priority": 10,
                    "domain": "repo-tooling",
                    "allowed_paths": ["automation/"],
                    "required_validations": ["make architecture"],
                    "depends_on": [],
                    "max_files_changed": 10,
                    "notes": "",
                },
                {
                    "slice_id": "higher-priority",
                    "title": "Higher priority",
                    "status": "queued",
                    "priority": 20,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": ["bootstrap"],
                    "max_files_changed": 4,
                    "notes": "",
                },
                {
                    "slice_id": "lower-priority",
                    "title": "Lower priority",
                    "status": "queued",
                    "priority": 30,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": ["bootstrap"],
                    "max_files_changed": 4,
                    "notes": "",
                },
            ],
        }
        slice_record = self.require_slice_record(queue_data, "bootstrap")
        handoff = {
            "slice_id": "bootstrap",
            "status": "done",
            "summary": "Bootstrap finished.",
            "files_touched": ["automation/README.md"],
            "validations_passed": ["make architecture"],
            "validations_failed": [],
            "risks": [],
            "recommended_next_slice": "lower-priority",
            "recommended_next_reason": "Skip directly to the lower-priority slice.",
            "dirty_paths_outside_scope": [],
            "timestamp": "2026-04-21T15:46:00Z",
        }

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=["automation/README.md"],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("done", decision.queue_status)
        self.assertEqual("stop_for_review", decision.decision)
        self.assertFalse(decision.should_continue)
        self.assertIn("highest-priority eligible queued slice", decision.stop_reason)

    def test_completion_decision_fails_when_files_touched_leave_scope(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        allowed_file = self.example_allowed_file(slice_record)
        handoff = policy.load_json(self.example_handoff_path)
        handoff["files_touched"] = [allowed_file, "README.md"]

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=[allowed_file],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("failed", decision.queue_status)
        self.assertEqual("stop_failed", decision.decision)
        self.assertEqual(["README.md"], decision.files_touched_outside_scope)

    def test_completion_decision_fails_when_diff_budget_is_exceeded(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)
        handoff["files_touched"] = [
            self.example_allowed_file(slice_record, f"file-{index}.swift")
            for index in range(slice_record["max_files_changed"] + 1)
        ]
        handoff["recommended_next_slice"] = ""
        handoff["recommended_next_reason"] = ""

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("failed", decision.queue_status)
        self.assertEqual("stop_failed", decision.decision)
        self.assertIn("max_files_changed", decision.stop_reason)

    def test_completion_decision_fails_when_supervisor_replay_fails(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)
        validation_replays = [
            policy.ValidationReplayResult(
                command="git diff --check", success=False, exit_code=1, reason="Supervisor replay exited with code 1."
            )
        ]

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=3,
            validation_replays=validation_replays,
        )

        self.assertEqual("failed", decision.queue_status)
        self.assertEqual("stop_failed", decision.decision)
        self.assertEqual(["git diff --check"], decision.required_validation_failures)
        self.assertEqual(1, len(decision.supervisor_validation_replays))

    def test_completion_decision_stops_for_review_at_autonomous_limit(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=2,
            run_limit=2,
        )

        self.assertEqual("done", decision.queue_status)
        self.assertEqual("stop_for_review", decision.decision)
        self.assertFalse(decision.should_continue)
        self.assertIn("human review", decision.stop_reason)

    def test_completion_decision_continues_when_recommended_next_is_known_and_eligible(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice_id = self.example_slice_id_at(1)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("done", decision.queue_status)
        self.assertEqual("continue", decision.decision)
        self.assertTrue(decision.should_continue)
        self.assertEqual(second_slice_id, decision.next_slice_id)

    def test_first_proof_runs_two_adjacent_slices_then_stops_for_review(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice_id = self.example_slice_id_at(1)
        third_slice_id = self.example_slice_id_at(2)
        first_queue = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        first_slice = self.require_slice_record(first_queue, first_slice_id)
        first_handoff = policy.load_json(self.example_handoff_path)
        first_replays = self.successful_replays(["make architecture", "git diff --check"])

        first_decision = policy.evaluate_completion(
            queue_data=first_queue,
            slice_record=first_slice,
            handoff=first_handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=first_handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=2,
            validation_replays=first_replays,
        )

        self.assertEqual("continue", first_decision.decision)
        self.assertEqual(second_slice_id, first_decision.next_slice_id)

        second_queue = policy.set_slice_status(first_queue, first_slice_id, "done")
        second_queue = policy.set_slice_status(second_queue, second_slice_id, "in_progress")
        second_slice = self.require_slice_record(second_queue, second_slice_id)
        queue_if_second_done = policy.set_slice_status(second_queue, second_slice_id, "done")
        self.assertEqual(third_slice_id, self.require_selected_slice(queue_if_second_done)["slice_id"])

        second_handoff = {
            "slice_id": second_slice_id,
            "status": "done",
            "summary": "Added targeted regression coverage for the next example interaction.",
            "files_touched": [
                self.example_allowed_file(second_slice, "RegressionTests.swift"),
                self.example_markdown_path(second_slice),
            ],
            "validations_passed": second_slice["required_validations"],
            "validations_failed": [],
            "risks": ["No manual simulator pass for the regression flow"],
            "recommended_next_slice": third_slice_id,
            "recommended_next_reason": "Adjacent proofread slice after the regression coverage pass.",
            "dirty_paths_outside_scope": [],
            "timestamp": "2026-04-21T16:00:00Z",
        }
        second_replays = self.successful_replays(["make architecture", "git diff --check"])

        second_decision = policy.evaluate_completion(
            queue_data=second_queue,
            slice_record=second_slice,
            handoff=second_handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=second_handoff["files_touched"],
            completed_autonomous_runs=2,
            run_limit=2,
            validation_replays=second_replays,
        )

        self.assertEqual("done", second_decision.queue_status)
        self.assertEqual("stop_for_review", second_decision.decision)
        self.assertFalse(second_decision.should_continue)
        self.assertEqual(third_slice_id, second_decision.recommended_next_slice)
        self.assertIn("human review", second_decision.stop_reason)

    def test_build_context_includes_compact_previous_handoff_and_relevant_docs(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice = self.example_slice_at(1)
        second_slice_id = second_slice["slice_id"]
        third_slice_id = self.example_slice_id_at(2)
        example_handoff = policy.load_json(self.example_handoff_path)
        with tempfile.TemporaryDirectory() as temp_dir:
            handoff_dir = Path(temp_dir)
            shutil.copy(self.example_handoff_path, handoff_dir / self.example_handoff_filename())

            bundle = build_context_bundle(
                repo_root=self.repo_root,
                queue_path=self.example_queue_path,
                handoff_dir=handoff_dir,
                slice_id=second_slice_id,
                max_doc_chars=1200,
            )

        document_paths = [document["path"] for document in bundle["documents"]]
        expected_doc = self.example_markdown_path(second_slice)
        if (self.repo_root / expected_doc).exists():
            self.assertIn(expected_doc, document_paths)
        self.assertEqual(first_slice_id, bundle["previous_handoff"]["slice_id"])
        self.assertEqual("domain-tested", bundle["previous_handoff"]["proof_level"])
        self.assertIn("running-app-smoke", bundle["previous_handoff"]["missing_proof_levels"])
        self.assertIn(
            example_handoff["contract_status_changes"][0]["contract"],
            bundle["previous_handoff"]["contract_status_changes"][0]["contract"],
        )
        self.assertEqual("clean", bundle["previous_handoff"]["repo_clean_status"])
        self.assertEqual("not-checked", bundle["previous_handoff"]["git_mirror_status"])
        self.assertIn(example_handoff["summary"].split(".")[0], bundle["previous_handoff_summary"])
        self.assertIn("Proof level: `domain-tested`", bundle["previous_handoff_summary"])
        self.assertIn(
            f"Contract status changes: {example_handoff['contract_status_changes'][0]['contract']}",
            bundle["previous_handoff_summary"],
        )
        self.assertIn("Residual risks: No manual simulator pass", bundle["previous_handoff_summary"])
        self.assertIn("Repo clean status: `clean`", bundle["previous_handoff_summary"])
        self.assertIn("Git mirror status: `not-checked`", bundle["previous_handoff_summary"])
        self.assertEqual(
            ["supervisor_replayable", "run_report_only", "supervisor_replayable"],
            [item["tier"] for item in bundle["validation_ownership"]],
        )
        self.assertEqual(third_slice_id, bundle["queue"]["adjacent_queued_slices"][0]["slice_id"])

    def test_build_context_preserves_legacy_previous_handoff_as_read_only_context(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice_id = self.example_slice_id_at(1)
        example_handoff = policy.load_json(self.example_handoff_path)
        with tempfile.TemporaryDirectory() as temp_dir:
            handoff_dir = Path(temp_dir)
            legacy_handoff = json.loads(json.dumps(example_handoff))
            legacy_handoff.pop("proof_level")
            legacy_handoff.pop("missing_proof_levels")
            legacy_handoff["risks"] = legacy_handoff.pop("residual_risks")
            legacy_handoff.pop("contract_status_changes")
            legacy_handoff.pop("repo_clean_status")
            legacy_handoff.pop("git_mirror_status")
            legacy_path = handoff_dir / self.example_handoff_filename()
            legacy_path.write_text(json.dumps(legacy_handoff), encoding="utf-8")

            bundle = build_context_bundle(
                repo_root=self.repo_root,
                queue_path=self.example_queue_path,
                handoff_dir=handoff_dir,
                slice_id=second_slice_id,
                max_doc_chars=1200,
            )

        self.assertEqual(first_slice_id, bundle["previous_handoff"]["slice_id"])
        self.assertEqual("legacy-unknown", bundle["previous_handoff"]["proof_level"])
        self.assertEqual(example_handoff["residual_risks"], bundle["previous_handoff"]["residual_risks"])
        self.assertEqual("legacy-unknown", bundle["previous_handoff"]["repo_clean_status"])
        self.assertEqual("legacy-unknown", bundle["previous_handoff"]["git_mirror_status"])
        self.assertIn("Proof level: `legacy-unknown`", bundle["previous_handoff_summary"])

    def test_render_prompt_includes_slice_fields_previous_handoff_and_template(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice = self.example_slice_at(1)
        second_slice_id = second_slice["slice_id"]
        third_slice_id = self.example_slice_id_at(2)
        with tempfile.TemporaryDirectory() as temp_dir:
            handoff_dir = Path(temp_dir)
            shutil.copy(self.example_handoff_path, handoff_dir / self.example_handoff_filename())
            queue_data = policy.load_queue(self.example_queue_path)
            slice_record = self.require_slice_record(queue_data, second_slice_id)
            context_bundle = build_context_bundle(
                repo_root=self.repo_root,
                queue_path=self.example_queue_path,
                handoff_dir=handoff_dir,
                slice_id=second_slice_id,
                max_doc_chars=1200,
            )
            prompt_text = render_prompt(
                repo_root=self.repo_root,
                slice_record=slice_record,
                context_bundle=context_bundle,
                handoff_path=Path("/tmp/handoff.json"),
            )

        self.assertIn(second_slice_id, prompt_text)
        self.assertIn(second_slice["title"], prompt_text)
        self.assertIn(f"`{second_slice['domain']}`", prompt_text)
        self.assertIn("`git diff --check`", prompt_text)
        self.assertIn("`supervisor_replayable`", prompt_text)
        self.assertIn("`run_report_only`", prompt_text)
        self.assertIn(f"`{second_slice['max_files_changed']}`", prompt_text)
        self.assertIn(first_slice_id, prompt_text)
        self.assertIn(third_slice_id, prompt_text)
        self.assertIn("/tmp/handoff.json", prompt_text)
        self.assertIn("<describe what changed", prompt_text)
        self.assertIn('"proof_level"', prompt_text)
        self.assertIn('"missing_proof_levels"', prompt_text)
        self.assertIn('"contract_status_changes"', prompt_text)
        self.assertIn('"residual_risks"', prompt_text)
        self.assertIn('"repo_clean_status"', prompt_text)
        self.assertIn('"git_mirror_status"', prompt_text)
        self.assertIn("Execution constraints for this slice:", prompt_text)
        self.assertIn("Stay within allowed_paths", prompt_text)
        # A renamed placeholder that the fragment still spells the old way would render
        # literally rather than fail, so assert none survives substitution.
        self.assertNotRegex(prompt_text, r"__[A-Z_]+__")

    def test_render_prompt_rejects_unknown_placeholder_in_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_root = Path(temp_dir)
            prompts_dir = fake_root / "automation/prompts"
            prompts_dir.mkdir(parents=True)
            shutil.copy(self.repo_root / "automation/prompts/base.md", prompts_dir / "base.md")
            stale_template = (self.repo_root / "automation/prompts/slice.md").read_text(encoding="utf-8")
            stale_template = stale_template.replace("__EXECUTION_CONSTRAINTS__", "__ACCEPTANCE_CHECKS__")
            (prompts_dir / "slice.md").write_text(stale_template, encoding="utf-8")

            handoff_dir = fake_root / "handoffs"
            handoff_dir.mkdir()
            slice_id = self.example_slice_id_at(1)
            queue_data = policy.load_queue(self.example_queue_path)
            slice_record = self.require_slice_record(queue_data, slice_id)
            context_bundle = build_context_bundle(
                repo_root=self.repo_root,
                queue_path=self.example_queue_path,
                handoff_dir=handoff_dir,
                slice_id=slice_id,
                max_doc_chars=1200,
            )
            with self.assertRaises(policy.ConfigError) as raised:
                render_prompt(
                    repo_root=fake_root,
                    slice_record=slice_record,
                    context_bundle=context_bundle,
                    handoff_path=Path("/tmp/handoff.json"),
                )
        self.assertEqual("unknown_prompt_placeholder: __ACCEPTANCE_CHECKS__", str(raised.exception))

    def test_render_prompt_allows_placeholder_shaped_text_in_injected_values(self) -> None:
        slice_id = self.example_slice_id_at(1)
        with tempfile.TemporaryDirectory() as temp_dir:
            handoff_dir = Path(temp_dir)
            queue_data = policy.load_queue(self.example_queue_path)
            slice_record = dict(self.require_slice_record(queue_data, slice_id))
            slice_record["notes"] = "Leave the __LEGACY_TOKEN__ marker in the fixture alone."
            context_bundle = build_context_bundle(
                repo_root=self.repo_root,
                queue_path=self.example_queue_path,
                handoff_dir=handoff_dir,
                slice_id=slice_id,
                max_doc_chars=1200,
            )
            prompt_text = render_prompt(
                repo_root=self.repo_root,
                slice_record=slice_record,
                context_bundle=context_bundle,
                handoff_path=Path("/tmp/handoff.json"),
            )
        self.assertIn("__LEGACY_TOKEN__", prompt_text)

    def test_decision_report_includes_supervisor_validation_replays(self) -> None:
        decision = policy.CompletionDecision(
            queue_status="done",
            decision="continue",
            should_continue=True,
            stop_reason="Continuation allowed into the recommended eligible next slice.",
            next_slice_id="today-continue-ui-regression-coverage",
            recommended_next_slice="today-continue-ui-regression-coverage",
            required_validation_failures=[],
            dirty_paths_outside_scope=[],
            files_touched_outside_scope=[],
            changed_file_count=2,
            supervisor_validation_replays=[
                policy.ValidationReplayResult(
                    command="make architecture", success=True, exit_code=0, reason="Supervisor replay passed."
                )
            ],
        )

        report = make_decision_report(
            slice_record={"slice_id": "today-nonfocus-add-to-focus"}, decision=decision, completed_runs=1, autonomous_limit=2
        )

        self.assertEqual(
            [{"command": "make architecture", "success": True, "exit_code": 0, "reason": "Supervisor replay passed."}],
            report["supervisor_validation_replays"],
        )

    def example_queue_with_slice_status(self, slice_id: str, status: str) -> dict[str, Any]:
        queue_data = policy.load_queue(self.example_queue_path)
        cloned = json.loads(json.dumps(queue_data))
        for slice_record in cloned["slices"]:
            if slice_record["slice_id"] == slice_id:
                slice_record["status"] = status
        return cloned

    def successful_replays(self, commands: list[str]) -> list[policy.ValidationReplayResult]:
        return [
            policy.ValidationReplayResult(command=command, success=True, exit_code=0, reason="Supervisor replay passed.")
            for command in commands
        ]

    def write_fake_executable(self, path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
