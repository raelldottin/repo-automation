from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.context.build_context import build_context_bundle
from automation.supervisor import kanban_projection as kp
from automation.supervisor import kanban_writer, policy


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the next eligible queued slice under the automation supervisor.")
    parser.add_argument("--queue", default="automation/queue/slices.json", help="Path to the live queue JSON file.")
    parser.add_argument("--handoff-dir", default="automation/handoffs", help="Directory where handoff artifacts are expected.")
    parser.add_argument("--agent-cmd", help="Shell command template for the fresh agent run.")
    parser.add_argument("--autonomous-limit", type=int, help="Override the queue policy limit for consecutive autonomous slices.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Select and package the next slice without launching the agent or mutating the queue.",
    )
    parser.add_argument(
        "--include-blocked",
        action="store_true",
        help="When no queued slice is eligible, report blocked/deferred slices and their recommended unblockers.",
    )
    parser.add_argument(
        "--kanban-board",
        type=Path,
        help="Take slices from this Hermes Kanban board (~/.hermes/kanban/boards/<slug>) instead of --queue. "
        "Opt-in; see docs/architecture/kanban-cutover.md.",
    )
    parser.add_argument("--kanban-overlay", type=Path, help="Approval overlay for --kanban-board; required with it.")
    parser.add_argument("--hermes", default="hermes", help="The hermes command that writes to the board.")
    args = parser.parse_args(argv)
    if (args.kanban_board is None) != (args.kanban_overlay is None):
        parser.error("--kanban-board and --kanban-overlay go together")
    return args


class JsonQueueIntake:
    """The operational authority: the JSON queue file, which the supervisor itself updates."""

    def __init__(self, queue_path: Path) -> None:
        self.queue_path = queue_path

    def load(self) -> dict:
        return policy.load_queue(self.queue_path)

    def start(self, slice_record: dict) -> None:
        queue_data = policy.set_slice_status(self.load(), slice_record["slice_id"], "in_progress")
        policy.write_json(self.queue_path, queue_data)

    def finish(self, slice_record: dict, status: str, reason: str, handoff_path: Path) -> None:
        queue_data = policy.set_slice_status(self.load(), slice_record["slice_id"], status)
        policy.write_json(self.queue_path, queue_data)


class KanbanIntake:
    """One Kanban board, projected read-only each time and written only through ``hermes kanban``.

    The projected queue is kept outside the repository as a working copy for the context
    builder; the board stays the authority for it, and the JSON queue is never touched.
    """

    def __init__(self, board_dir: Path, overlay_path: Path, work_dir: Path, hermes: str) -> None:
        self.board_dir, self.overlay_path, self.hermes = board_dir, overlay_path, shlex.split(hermes)
        self.queue_path = work_dir / "kanban-queue.json"
        self.revision = ""
        self.held: Optional[kanban_writer.Claim] = None

    def load(self) -> dict:
        projection = kp.project(kp.read_board(self.board_dir), kp.load_overlay(self.overlay_path))
        self.revision = projection.revision
        policy.write_json(self.queue_path, projection.queue)
        return projection.queue

    def start(self, slice_record: dict) -> None:
        traced = kp.parse_slice_id(slice_record["slice_id"])
        assert traced is not None, slice_record["slice_id"]
        self.held = kanban_writer.claim(self.board_dir, traced[1], self.revision, self.hermes)
        self.load()

    def finish(self, slice_record: dict, status: str, reason: str, handoff_path: Path) -> None:
        assert self.held is not None
        kanban_writer.record(self.board_dir, self.held, status, reason, handoff_path, self.hermes)
        self.held = None


def main(argv: Optional[list[str]] = None, repo_root: Path = REPO_ROOT) -> int:
    args = parse_args(argv)
    handoff_dir = repo_root / args.handoff_dir
    handoff_dir.mkdir(parents=True, exist_ok=True)
    if args.kanban_board is None:
        return supervise(args, repo_root, handoff_dir, JsonQueueIntake(repo_root / args.queue))
    with tempfile.TemporaryDirectory(prefix="kanban-intake-") as work_dir:
        intake = KanbanIntake(args.kanban_board, args.kanban_overlay, Path(work_dir), args.hermes)
        try:
            return supervise(args, repo_root, handoff_dir, intake)
        except kp.ProjectionError as error:
            # Claim and record fail closed; the board keeps whatever Hermes last accepted.
            print(f"stop: kanban: {error}", file=sys.stderr)
            return 2


def supervise(args: argparse.Namespace, repo_root: Path, handoff_dir: Path, intake: Any) -> int:
    queue_data = intake.load()
    active = policy.active_slice(queue_data)
    if active is not None:
        print(f"stop: queue already has in_progress slice {active['slice_id']}", file=sys.stderr)
        return 2

    agent_command_template = args.agent_cmd or queue_data["policy"]["agent_command_template"]
    if not args.dry_run and not agent_command_template.strip():
        print(
            "stop: no agent command template configured. Pass --agent-cmd or set policy.agent_command_template.", file=sys.stderr
        )
        return 2

    autonomous_limit = args.autonomous_limit or queue_data["policy"]["consecutive_autonomous_limit"]
    completed_runs = 0

    def fail(slice_record: dict, reason: str, handoff_path: Path) -> None:
        intake.finish(slice_record, "failed", reason, handoff_path)
        print(
            json.dumps(
                make_failure_report(
                    slice_record=slice_record,
                    reason=reason,
                    completed_runs=completed_runs,
                    autonomous_limit=autonomous_limit,
                ),
                indent=2,
            )
        )

    while True:
        queue_data = intake.load()
        next_slice = policy.select_next_slice(queue_data)
        if next_slice is None:
            print("stop: no eligible queued slice found.")
            if args.include_blocked:
                reports = policy.blocked_slice_reports(queue_data)
                if reports:
                    print()
                    print("blocked/deferred slices:")
                    for report in reports:
                        print(f"- {report.status}: {report.slice_id}")
                        print(f"  missing entry condition: {report.entry_condition}")
                        if report.recommended_unblocker:
                            print(f"  recommended unblocker: {report.recommended_unblocker}")
                        else:
                            print("  recommended unblocker: <none recorded>")
            return 0

        dirty_paths_before_run = policy.git_dirty_paths(repo_root)
        unexpected_dirty = policy.out_of_scope_paths(
            dirty_paths_before_run, next_slice["allowed_paths"], queue_data["policy"]["supervisor_owned_paths"]
        )
        if unexpected_dirty:
            print("stop: repo is dirty outside the next slice scope:\n- " + "\n- ".join(unexpected_dirty), file=sys.stderr)
            return 2

        timestamp_token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        handoff_filename = policy.make_handoff_filename(next_slice["slice_id"], timestamp_token)
        handoff_path = handoff_dir / handoff_filename
        context_bundle = build_context_bundle(
            repo_root=repo_root, queue_path=intake.queue_path, handoff_dir=handoff_dir, slice_id=next_slice["slice_id"]
        )

        if args.dry_run:
            report = {
                "selected_slice": next_slice["slice_id"],
                "handoff_path": str(handoff_path),
                "required_validations": next_slice["required_validations"],
                "supervisor_replayable_validations": policy.supervisor_replayable_validations(next_slice["required_validations"]),
                "validation_ownership": context_bundle["validation_ownership"],
                "allowed_paths": next_slice["allowed_paths"],
                "context_documents": [doc["path"] for doc in context_bundle["documents"]],
            }
            if isinstance(intake, KanbanIntake):
                report["kanban_revision"] = intake.revision
            if args.include_blocked:
                report["blocked_slices"] = [
                    {
                        "slice_id": blocked.slice_id,
                        "status": blocked.status,
                        "entry_condition": blocked.entry_condition,
                        "recommended_unblocker": blocked.recommended_unblocker,
                    }
                    for blocked in policy.blocked_slice_reports(queue_data)
                ]
            print(json.dumps(report, indent=2))
            return 0

        intake.start(next_slice)
        queue_data = intake.load()

        try:
            exit_code = run_agent(
                repo_root=repo_root,
                queue_data=queue_data,
                slice_record=next_slice,
                context_bundle=context_bundle,
                handoff_path=handoff_path,
                command_template=agent_command_template,
            )
        except Exception as error:  # pragma: no cover - last-resort queue recovery
            fail(next_slice, f"Supervisor error while running slice: {error}", handoff_path)
            return 2

        if exit_code != 0:
            fail(next_slice, f"Agent command exited with code {exit_code} for slice {next_slice['slice_id']}.", handoff_path)
            return exit_code

        if not handoff_path.exists():
            fail(next_slice, f"Missing handoff artifact for slice {next_slice['slice_id']} at {handoff_path}", handoff_path)
            return 2

        try:
            handoff = policy.load_handoff(handoff_path)
        except ValueError as error:
            fail(next_slice, f"Invalid handoff artifact: {error}", handoff_path)
            return 2

        if handoff["slice_id"] != next_slice["slice_id"]:
            fail(next_slice, "Handoff slice_id does not match the in-progress slice.", handoff_path)
            return 2

        dirty_paths_after_run = policy.git_dirty_paths(repo_root)

        post_run_commit_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, cwd=repo_root
        ).stdout.strip()

        validation_replays: list[policy.ValidationReplayResult] = []
        if handoff["status"] == "done":
            validation_replays = policy.replay_validation_commands(
                repo_root=repo_root,
                required_validations=next_slice["required_validations"],
                validations_passed=handoff["validations_passed"],
            )
        completed_runs += 1
        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=next_slice,
            handoff=handoff,
            dirty_paths_before_run=dirty_paths_before_run,
            dirty_paths_after_run=dirty_paths_after_run,
            completed_autonomous_runs=completed_runs,
            run_limit=autonomous_limit,
            validation_replays=validation_replays,
            post_run_commit_sha=post_run_commit_sha,
        )

        intake.finish(next_slice, decision.queue_status, decision.stop_reason or decision.decision, handoff_path)

        print(
            json.dumps(
                make_decision_report(
                    slice_record=next_slice, decision=decision, completed_runs=completed_runs, autonomous_limit=autonomous_limit
                ),
                indent=2,
            )
        )

        if not decision.should_continue:
            return 0


def run_agent(
    repo_root: Path, queue_data: dict, slice_record: dict, context_bundle: dict, handoff_path: Path, command_template: str
) -> int:
    timeout_seconds = queue_data["policy"]["handoff_timeout_seconds"]
    prompt_text = render_prompt(
        repo_root=repo_root, slice_record=slice_record, context_bundle=context_bundle, handoff_path=handoff_path
    )

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as prompt_handle:
        prompt_handle.write(prompt_text)
        prompt_handle.write("\n")
        prompt_path = Path(prompt_handle.name)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as context_handle:
        json.dump(context_bundle, context_handle, indent=2, ensure_ascii=False)
        context_handle.write("\n")
        context_path = Path(context_handle.name)

    formatted_command = format_agent_command(
        command_template=command_template,
        repo_root=repo_root,
        prompt_path=prompt_path,
        context_path=context_path,
        handoff_path=handoff_path,
        slice_id=slice_record["slice_id"],
    )

    try:
        result = subprocess.run(formatted_command, cwd=repo_root, shell=True, timeout=timeout_seconds)
        return result.returncode
    finally:
        prompt_path.unlink(missing_ok=True)
        context_path.unlink(missing_ok=True)


def format_agent_command(
    command_template: str, repo_root: Path, prompt_path: Path, context_path: Path, handoff_path: Path, slice_id: str
) -> str:
    """Render the shell command with quoted placeholder values."""
    return command_template.format(
        repo_root=shlex.quote(str(repo_root)),
        prompt_file=shlex.quote(str(prompt_path)),
        context_file=shlex.quote(str(context_path)),
        handoff_file=shlex.quote(str(handoff_path)),
        slice_id=shlex.quote(slice_id),
    )


# Scanned before substitution: injected values (notes, context JSON, handoff data) may
# legitimately contain __FOO__-shaped text, so only the raw template is checked.
PROMPT_PLACEHOLDER_PATTERN = re.compile(r"__[A-Z0-9_]+__")


def render_prompt(repo_root: Path, slice_record: dict, context_bundle: dict, handoff_path: Path) -> str:
    base_prompt = (repo_root / "automation/prompts/base.md").read_text(encoding="utf-8")
    slice_prompt = (repo_root / "automation/prompts/slice.md").read_text(encoding="utf-8")
    replacements = {
        "__SLICE_ID__": slice_record["slice_id"],
        "__SLICE_TITLE__": slice_record["title"],
        "__SLICE_DOMAIN__": slice_record["domain"],
        "__SLICE_PRIORITY__": str(slice_record["priority"]),
        "__ALLOWED_PATHS__": format_value_list(slice_record["allowed_paths"]),
        "__REQUIRED_VALIDATIONS__": format_value_list(slice_record["required_validations"]),
        "__VALIDATION_OWNERSHIP__": format_validation_ownership(context_bundle["validation_ownership"]),
        "__DIFF_BUDGET__": str(slice_record["max_files_changed"]),
        "__DEPENDENCIES__": format_value_list(slice_record["depends_on"], empty_message="- none"),
        "__SLICE_NOTES__": slice_record["notes"] or "None.",
        "__POLICY_SENTENCE__": context_bundle["policy_sentence"],
        "__ADJACENT_SLICE_CANDIDATES__": format_adjacent_slices(context_bundle["queue"]["adjacent_queued_slices"]),
        "__PREVIOUS_HANDOFF_SUMMARY__": context_bundle["previous_handoff_summary"],
        "__QUEUE_METADATA_JSON__": json.dumps(context_bundle["queue"], indent=2, ensure_ascii=False),
        "__EXECUTION_CONSTRAINTS__": format_plain_list(context_bundle["execution_constraints"]),
        "__CONTEXT_DOCUMENT_INDEX__": format_documents(context_bundle["documents"]),
        "__CONTEXT_JSON__": json.dumps(context_bundle, indent=2, ensure_ascii=False),
        "__HANDOFF_PATH__": str(handoff_path),
        "__HANDOFF_TEMPLATE_JSON__": json.dumps(context_bundle["handoff_template"], indent=2, ensure_ascii=False),
    }
    unknown = set(PROMPT_PLACEHOLDER_PATTERN.findall(slice_prompt)) - replacements.keys()
    if unknown:
        raise policy.ConfigError("unknown_prompt_placeholder: " + ", ".join(sorted(unknown)))
    rendered_slice_prompt = slice_prompt
    for token, value in replacements.items():
        rendered_slice_prompt = rendered_slice_prompt.replace(token, value)
    return base_prompt.strip() + "\n\n" + rendered_slice_prompt.strip() + "\n"


def format_value_list(values: list[str], empty_message: str = "- none") -> str:
    if not values:
        return empty_message
    return "\n".join(f"- `{value}`" for value in values)


def format_plain_list(values: list[str], empty_message: str = "- none") -> str:
    if not values:
        return empty_message
    return "\n".join(f"- {value}" for value in values)


def format_adjacent_slices(adjacent_slices: list[dict]) -> str:
    if not adjacent_slices:
        return "- none"
    return "\n".join(
        (
            f"- `{slice_info['slice_id']}`: {slice_info['title']} "
            f"(priority {slice_info['priority']}, domain `{slice_info['domain']}`)"
        )
        for slice_info in adjacent_slices
    )


def format_documents(documents: list[dict]) -> str:
    if not documents:
        return "- none"
    return "\n".join(f"- `{document['path']}`: {document['reason']}" for document in documents)


def format_validation_ownership(validation_ownership: list[dict]) -> str:
    if not validation_ownership:
        return "- none"
    return "\n".join((f"- `{item['command']}` -> `{item['tier']}`: {item['reason']}") for item in validation_ownership)


def make_decision_report(
    slice_record: dict, decision: policy.CompletionDecision, completed_runs: int, autonomous_limit: int
) -> dict:
    return {
        "slice_id": slice_record["slice_id"],
        "queue_status": decision.queue_status,
        "decision": decision.decision,
        "reason": decision.stop_reason,
        "recommended_next_slice": decision.recommended_next_slice,
        "next_slice_id": decision.next_slice_id,
        "changed_file_count": decision.changed_file_count,
        "required_validation_failures": decision.required_validation_failures,
        "missing_manual_proofs": decision.missing_manual_proofs,
        "missing_proof_level": decision.missing_proof_level,
        "supervisor_validation_replays": serialize_validation_replays(decision.supervisor_validation_replays),
        "dirty_paths_outside_scope": decision.dirty_paths_outside_scope,
        "files_touched_outside_scope": decision.files_touched_outside_scope,
        "completed_autonomous_runs": completed_runs,
        "autonomous_run_limit": autonomous_limit,
    }


def make_failure_report(slice_record: dict, reason: str, completed_runs: int, autonomous_limit: int) -> dict:
    return {
        "slice_id": slice_record["slice_id"],
        "queue_status": "failed",
        "decision": "stop_failed",
        "reason": reason,
        "recommended_next_slice": "",
        "next_slice_id": None,
        "changed_file_count": 0,
        "required_validation_failures": [],
        "missing_manual_proofs": [],
        "missing_proof_level": False,
        "supervisor_validation_replays": [],
        "dirty_paths_outside_scope": [],
        "files_touched_outside_scope": [],
        "completed_autonomous_runs": completed_runs,
        "autonomous_run_limit": autonomous_limit,
    }


def serialize_validation_replays(replay_results: list[policy.ValidationReplayResult]) -> list[dict]:
    return [
        {"command": replay.command, "success": replay.success, "exit_code": replay.exit_code, "reason": replay.reason}
        for replay in replay_results
    ]


def _cli_entry() -> int:
    try:
        return main()
    except policy.ConfigError as error:
        print(f"stop: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli_entry())
