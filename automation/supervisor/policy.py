from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Optional, Sequence

from pydantic import BaseModel, ValidationError

from automation.schemas.models import Handoff, SliceQueue


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str]


@dataclass
class ValidationReplayResult:
    command: str
    success: bool
    exit_code: Optional[int]
    reason: str


@dataclass
class ValidationOwnership:
    command: str
    tier: str
    reason: str


@dataclass
class BlockedSliceReport:
    slice_id: str
    status: str
    entry_condition: str
    recommended_unblocker: str
    notes: str


@dataclass
class CompletionDecision:
    queue_status: str
    decision: str
    should_continue: bool
    stop_reason: str
    next_slice_id: Optional[str]
    recommended_next_slice: str
    required_validation_failures: list[str]
    dirty_paths_outside_scope: list[str]
    files_touched_outside_scope: list[str]
    changed_file_count: int
    supervisor_validation_replays: list[ValidationReplayResult] = field(default_factory=list)
    missing_manual_proofs: list[str] = field(default_factory=list)
    missing_proof_level: bool = False


PROOF_LADDER = {
    "doc-only": 0,
    "domain-tested": 1,
    "build-tested": 2,
    "running-app-smoke": 3,
    "flow-verified": 4,
    "screenshot-verified": 5,
    "device-verified": 6,
    "testflight-verified": 7,
}


def meets_proof_level(achieved: str, required: str) -> bool:
    if not required:
        return True
    if not achieved:
        return False
    return PROOF_LADDER.get(achieved, -1) >= PROOF_LADDER.get(required, 0)


def verify_manual_proofs(
    slice_record: dict[str, Any], handoff: dict[str, Any], post_run_commit_sha: str
) -> tuple[list[str], bool]:
    required_proofs = slice_record.get("manual_proof", [])
    required_level = slice_record.get("required_proof_level", "")

    provided_proofs = handoff.get("provided_proofs", [])
    verified_sha = handoff.get("verified_commit_sha", "")

    missing_proofs = []

    if required_proofs:
        if not verified_sha:
            missing_proofs.append("Missing verified_commit_sha (required when the slice requires manual proof)")
        elif verified_sha != post_run_commit_sha:
            missing_proofs.append("Invalid verified_commit_sha (does not match supervisor-observed HEAD)")

    provided_by_type = {}
    for p in provided_proofs:
        if p.get("slice_id") != slice_record.get("slice_id"):
            continue
        if p.get("commit_sha") != post_run_commit_sha:
            continue
        provided_by_type[p.get("proof_type")] = p

    repo_root = Path.cwd().resolve()
    slice_id = slice_record.get("slice_id", "")

    for req in required_proofs:
        if req not in provided_by_type:
            missing_proofs.append(req)
        else:
            p = provided_by_type[req]
            path_str = p.get("path", "")
            try:
                proof_path = Path(path_str).resolve()
            except Exception:
                missing_proofs.append(f"{req} (invalid path)")
                continue

            expected_dir = repo_root / "automation" / "proofs" / slice_id
            try:
                proof_path.relative_to(expected_dir)
            except ValueError:
                missing_proofs.append(f"{req} (outside approved proof root)")
                continue

            if not proof_path.exists():
                missing_proofs.append(f"{req} (missing file)")
                continue

            expected_sha256 = p.get("sha256", "")
            if expected_sha256:
                try:
                    actual_sha256 = hashlib.sha256(proof_path.read_bytes()).hexdigest()
                    if actual_sha256 != expected_sha256:
                        missing_proofs.append(f"{req} (sha256 mismatch)")
                except Exception:
                    missing_proofs.append(f"{req} (could not read file for sha256)")
            else:
                missing_proofs.append(f"{req} (missing sha256 in metadata)")

    achieved_level = handoff.get("proof_level", "")
    missing_level = False
    if required_level and not meets_proof_level(achieved_level, required_level):
        missing_level = True

    return missing_proofs, missing_level


SUPERVISOR_VALIDATION_REPLAY_COMMANDS: dict[str, list[str]] = {
    "make architecture": ["make", "architecture"],
    "git diff --check": ["git", "diff", "--check"],
}

RUN_REPORT_ONLY_VALIDATION_PREFIX_REASONS: tuple[tuple[str, str], ...] = (
    ("make test-domain ", "Domain-targeted tests are currently run-reported and not replayed by the supervisor."),
)

NEVER_SUPERVISOR_OWNED_VALIDATION_PREFIX_REASONS: tuple[tuple[str, str], ...] = (
    ("manual ", "Manual validation steps are never supervisor-owned and should not be replayed."),
    ("open ", "UI-launch steps are never supervisor-owned and should not be replayed."),
)


class ConfigError(Exception):
    """Raised when supervisor or context input is missing or malformed in a user-fixable way.

    Catch this at CLI entry points to print a friendly message and exit non-zero instead of
    surfacing a Python traceback to a consumer who is bootstrapping the reusable automation.
    """


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as error:
        raise ConfigError(f"missing file: {path}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"invalid JSON in {path}: {error}") from error


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def validate_document(document: Any, model: type[BaseModel]) -> ValidationResult:
    """Validate a raw document against its canonical contract model.

    The document itself is never rewritten: the harness stays dict-based, so validation
    is a gate at the door rather than a parsing layer the rest of the code depends on.
    """
    try:
        model.model_validate(document)
    except ValidationError as error:
        return ValidationResult(is_valid=False, errors=[_format_error(item) for item in error.errors()])
    return ValidationResult(is_valid=True, errors=[])


def _location(location: Sequence[Any]) -> str:
    parts = ["$"]
    for item in location:
        parts.append(f"[{item}]" if isinstance(item, int) else f".{item}")
    return "".join(parts)


def _format_error(error: Mapping[str, Any]) -> str:
    """Render a Pydantic error the way the supervisor and its agents already read them."""
    location = tuple(error["loc"])
    kind = error["type"]
    context = error.get("ctx", {})

    if kind == "missing":
        return f"{_location(location[:-1])}: missing required property {str(location[-1])!r}"

    path = _location(location)
    if kind == "extra_forbidden":
        return f"{path}: additional property not allowed"
    if kind == "too_short":
        return f"{path}: expected at least {context.get('min_length')} items, found {context.get('actual_length')}"
    if kind == "string_too_short":
        return f"{path}: expected string length >= {context.get('min_length')}, found {len(error['input'])}"
    return f"{path}: {error['msg']}, found {error['input']!r}"


def load_queue(queue_path: Path) -> dict[str, Any]:
    if not queue_path.exists():
        raise ConfigError(
            f"queue file not found: {queue_path}\n"
            "hint: copy automation/examples/example-slices.json to that path "
            "and edit it for this repository's slices."
        )
    queue_data = load_json(queue_path)
    validation = validate_document(queue_data, SliceQueue)
    if not validation.is_valid:
        raise ValueError("Invalid queue file:\n- " + "\n- ".join(validation.errors))

    integrity_errors = validate_queue_integrity(queue_data)
    if integrity_errors:
        raise ValueError("Queue integrity failure:\n- " + "\n- ".join(integrity_errors))

    return queue_data


def load_handoff(handoff_path: Path) -> dict[str, Any]:
    handoff = load_json(handoff_path)
    validation = validate_document(handoff, Handoff)
    if not validation.is_valid:
        raise ValueError("Invalid handoff file:\n- " + "\n- ".join(validation.errors))
    return handoff


def validate_queue_integrity(queue_data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    slices = queue_data.get("slices", [])
    slice_ids: list[str] = [slice_record["slice_id"] for slice_record in slices]

    if len(slice_ids) != len(set(slice_ids)):
        errors.append("slice_id values must be unique")

    active_count = sum(1 for slice_record in slices if slice_record["status"] == "in_progress")
    if active_count > 1:
        errors.append("at most one slice may be in_progress")

    known_ids = set(slice_ids)
    for slice_record in slices:
        missing = [dep for dep in slice_record["depends_on"] if dep not in known_ids]
        if missing:
            errors.append(f"slice {slice_record['slice_id']!r} depends on unknown slice IDs: {missing}")
        if slice_record["status"] in {"blocked", "deferred"}:
            entry_condition = slice_record.get("entry_condition", "").strip()
            if not entry_condition:
                errors.append(
                    f"slice {slice_record['slice_id']!r} is {slice_record['status']!r} but is missing an explicit entry_condition"
                )
            recommended_unblocker = slice_record.get("recommended_unblocker", "").strip()
            if recommended_unblocker and recommended_unblocker not in known_ids:
                errors.append(f"slice {slice_record['slice_id']!r} recommends unknown unblocker {recommended_unblocker!r}")

    return errors


def find_slice(queue_data: dict[str, Any], slice_id: str) -> Optional[dict[str, Any]]:
    for slice_record in queue_data["slices"]:
        if slice_record["slice_id"] == slice_id:
            return slice_record
    return None


def active_slice(queue_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    for slice_record in queue_data["slices"]:
        if slice_record["status"] == "in_progress":
            return slice_record
    return None


def dependencies_satisfied(slice_record: dict[str, Any], queue_data: dict[str, Any]) -> bool:
    for dependency in slice_record["depends_on"]:
        dependency_record = find_slice(queue_data, dependency)
        if dependency_record is None or dependency_record["status"] != "done":
            return False
    return True


def select_next_slice(queue_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, slice_record in enumerate(queue_data["slices"]):
        if slice_record["status"] != "queued":
            continue
        if not dependencies_satisfied(slice_record, queue_data):
            continue
        candidates.append((slice_record["priority"], index, slice_record))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def blocked_slice_reports(queue_data: dict[str, Any]) -> list[BlockedSliceReport]:
    reports: list[BlockedSliceReport] = []
    for slice_record in queue_data["slices"]:
        if slice_record["status"] not in {"blocked", "deferred"}:
            continue
        reports.append(
            BlockedSliceReport(
                slice_id=slice_record["slice_id"],
                status=slice_record["status"],
                entry_condition=slice_record.get("entry_condition", "").strip(),
                recommended_unblocker=slice_record.get("recommended_unblocker", "").strip(),
                notes=slice_record.get("notes", "").strip(),
            )
        )
    return reports


def set_slice_status(queue_data: dict[str, Any], slice_id: str, status: str) -> dict[str, Any]:
    updated = json.loads(json.dumps(queue_data))
    for slice_record in updated["slices"]:
        if slice_record["slice_id"] == slice_id:
            slice_record["status"] = status
            return updated
    raise KeyError(f"Unknown slice_id: {slice_id}")


def git_dirty_paths(repo_root: Path) -> list[str]:
    commands = [
        ["git", "diff", "--name-only", "--relative"],
        ["git", "diff", "--name-only", "--relative", "--cached"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    dirty_paths: list[str] = []

    for command in commands:
        try:
            result = subprocess.run(command, cwd=repo_root, check=True, capture_output=True, text=True)
        except FileNotFoundError as error:
            raise ConfigError(
                f"git executable not found on PATH: {error}\nhint: install Git, or ensure git is on PATH for this process."
            ) from error
        except subprocess.CalledProcessError as error:
            if not (repo_root / ".git").exists():
                raise ConfigError(
                    f"not a Git repository: {repo_root}\n"
                    "hint: run 'git init -b main' in this directory, "
                    "commit the bootstrap, then re-run."
                ) from error
            stderr_text = (error.stderr or "").strip() or "(empty)"
            raise ConfigError(f"git command failed in {repo_root}: {' '.join(command)}\nstderr: {stderr_text}") from error
        for raw_line in result.stdout.splitlines():
            if not raw_line:
                continue
            dirty_paths.append(normalize_repo_path(raw_line))

    return sorted(set(dirty_paths))


def normalize_repo_path(path: str) -> str:
    return PurePosixPath(path.strip()).as_posix()


def path_matches_prefix(path: str, prefix: str) -> bool:
    normalized_path = normalize_repo_path(path)
    normalized_prefix = normalize_repo_path(prefix)
    if normalized_prefix == ".":
        return True
    return normalized_path == normalized_prefix or normalized_path.startswith(f"{normalized_prefix}/")


def out_of_scope_paths(dirty_paths: list[str], allowed_paths: list[str], supervisor_owned_paths: list[str]) -> list[str]:
    unexpected: list[str] = []
    for path in dirty_paths:
        if any(path_matches_prefix(path, prefix) for prefix in supervisor_owned_paths):
            continue
        if any(path_matches_prefix(path, prefix) for prefix in allowed_paths):
            continue
        unexpected.append(path)
    return sorted(set(unexpected))


def introduced_paths(before_paths: list[str], after_paths: list[str]) -> list[str]:
    before = {normalize_repo_path(path) for path in before_paths}
    after = {normalize_repo_path(path) for path in after_paths}
    return sorted(after - before)


def count_paths_within_scope(dirty_paths: list[str], allowed_paths: list[str], supervisor_owned_paths: list[str]) -> int:
    count = 0
    for path in dirty_paths:
        if any(path_matches_prefix(path, prefix) for prefix in supervisor_owned_paths):
            continue
        if any(path_matches_prefix(path, prefix) for prefix in allowed_paths):
            count += 1
    return count


def normalize_paths(paths: list[str]) -> list[str]:
    return sorted({normalize_repo_path(path) for path in paths if path.strip()})


def required_validation_failures(
    required_validations: list[str], validations_passed: list[str], validations_failed: list[str]
) -> list[str]:
    passed = set(validations_passed)
    failed = set(validations_failed)
    missing: list[str] = []
    for command in required_validations:
        if command in failed or command not in passed:
            missing.append(command)
    return missing


def classify_validation_ownership(command: str) -> ValidationOwnership:
    normalized = command.strip()
    if normalized in SUPERVISOR_VALIDATION_REPLAY_COMMANDS:
        return ValidationOwnership(
            command=normalized,
            tier="supervisor_replayable",
            reason="The supervisor replays this exact allowlisted command before continuation.",
        )

    for prefix, reason in NEVER_SUPERVISOR_OWNED_VALIDATION_PREFIX_REASONS:
        if normalized.startswith(prefix):
            return ValidationOwnership(command=normalized, tier="never_supervisor_owned", reason=reason)

    for prefix, reason in RUN_REPORT_ONLY_VALIDATION_PREFIX_REASONS:
        if normalized.startswith(prefix):
            return ValidationOwnership(command=normalized, tier="run_report_only", reason=reason)

    return ValidationOwnership(
        command=normalized,
        tier="run_report_only",
        reason="This command is currently run-reported only and not replayed by the supervisor.",
    )


def classify_validation_ownerships(commands: list[str]) -> list[ValidationOwnership]:
    classifications: list[ValidationOwnership] = []
    seen_commands: set[str] = set()
    for command in commands:
        normalized = command.strip()
        if not normalized or normalized in seen_commands:
            continue
        seen_commands.add(normalized)
        classifications.append(classify_validation_ownership(normalized))
    return classifications


def supervisor_replayable_validations(required_validations: list[str]) -> list[str]:
    replayable: list[str] = []
    for command in required_validations:
        if command not in SUPERVISOR_VALIDATION_REPLAY_COMMANDS:
            continue
        if command in replayable:
            continue
        replayable.append(command)
    return replayable


def default_validation_replay_runner(repo_root: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=repo_root, check=False, capture_output=True, text=True)


def replay_validation_commands(
    repo_root: Path,
    required_validations: list[str],
    validations_passed: list[str],
    runner: Optional[Callable[[Path, list[str]], subprocess.CompletedProcess[str]]] = None,
) -> list[ValidationReplayResult]:
    execute = runner or default_validation_replay_runner
    passed = set(validations_passed)
    results: list[ValidationReplayResult] = []

    for command in supervisor_replayable_validations(required_validations):
        if command not in passed:
            continue
        argv = SUPERVISOR_VALIDATION_REPLAY_COMMANDS[command]
        try:
            result = execute(repo_root, argv)
        except OSError as error:
            results.append(
                ValidationReplayResult(
                    command=command, success=False, exit_code=None, reason=f"Supervisor replay could not execute: {error}"
                )
            )
            continue

        if result.returncode == 0:
            results.append(ValidationReplayResult(command=command, success=True, exit_code=0, reason="Supervisor replay passed."))
            continue

        results.append(
            ValidationReplayResult(
                command=command,
                success=False,
                exit_code=result.returncode,
                reason=f"Supervisor replay exited with code {result.returncode}.",
            )
        )

    return results


def validation_replay_failures(replay_results: list[ValidationReplayResult]) -> list[str]:
    failures: list[str] = []
    for replay in replay_results:
        if replay.success:
            continue
        failures.append(replay.command)
    return sorted(set(failures))


def next_slice_eligibility(
    queue_data: dict[str, Any], current_slice_id: str, recommended_next_slice: str, completed_autonomous_runs: int, run_limit: int
) -> CompletionDecision:
    queue_if_current_done = set_slice_status(queue_data, current_slice_id, "done")
    highest_priority_next = select_next_slice(queue_if_current_done)
    normalized_recommendation = recommended_next_slice.strip()

    if completed_autonomous_runs >= run_limit:
        return CompletionDecision(
            queue_status="done",
            decision="stop_for_review",
            should_continue=False,
            stop_reason="Consecutive autonomous run limit reached; stop for human review.",
            next_slice_id=None,
            recommended_next_slice=normalized_recommendation,
            required_validation_failures=[],
            dirty_paths_outside_scope=[],
            files_touched_outside_scope=[],
            changed_file_count=0,
            missing_manual_proofs=[],
            missing_proof_level=False,
        )

    if normalized_recommendation:
        if normalized_recommendation == current_slice_id:
            return CompletionDecision(
                queue_status="done",
                decision="stop_for_review",
                should_continue=False,
                stop_reason="The handoff recommended the current slice again instead of a queued successor.",
                next_slice_id=None,
                recommended_next_slice=normalized_recommendation,
                required_validation_failures=[],
                dirty_paths_outside_scope=[],
                files_touched_outside_scope=[],
                changed_file_count=0,
            )

        recommended_record = find_slice(queue_if_current_done, normalized_recommendation)
        if recommended_record is None:
            return CompletionDecision(
                queue_status="done",
                decision="stop_for_review",
                should_continue=False,
                stop_reason=("The handoff recommended a next slice that is not present in automation/queue/slices.json."),
                next_slice_id=None,
                recommended_next_slice=normalized_recommendation,
                required_validation_failures=[],
                dirty_paths_outside_scope=[],
                files_touched_outside_scope=[],
                changed_file_count=0,
            )

        if recommended_record["status"] != "queued":
            return CompletionDecision(
                queue_status="done",
                decision="stop_for_review",
                should_continue=False,
                stop_reason=(
                    f"The handoff recommended slice {normalized_recommendation!r}, "
                    f"but its queue status is {recommended_record['status']!r} instead of 'queued'."
                ),
                next_slice_id=None,
                recommended_next_slice=normalized_recommendation,
                required_validation_failures=[],
                dirty_paths_outside_scope=[],
                files_touched_outside_scope=[],
                changed_file_count=0,
            )

        if not dependencies_satisfied(recommended_record, queue_if_current_done):
            return CompletionDecision(
                queue_status="done",
                decision="stop_for_review",
                should_continue=False,
                stop_reason=(
                    f"The handoff recommended slice {normalized_recommendation!r}, but its dependencies are not yet satisfied."
                ),
                next_slice_id=None,
                recommended_next_slice=normalized_recommendation,
                required_validation_failures=[],
                dirty_paths_outside_scope=[],
                files_touched_outside_scope=[],
                changed_file_count=0,
            )

        if highest_priority_next is None:
            return CompletionDecision(
                queue_status="done",
                decision="stop_for_review",
                should_continue=False,
                stop_reason=(
                    f"The handoff recommended slice {normalized_recommendation!r}, "
                    "but there is no eligible queued next slice after marking the current one done."
                ),
                next_slice_id=None,
                recommended_next_slice=normalized_recommendation,
                required_validation_failures=[],
                dirty_paths_outside_scope=[],
                files_touched_outside_scope=[],
                changed_file_count=0,
            )

        if highest_priority_next["slice_id"] != normalized_recommendation:
            return CompletionDecision(
                queue_status="done",
                decision="stop_for_review",
                should_continue=False,
                stop_reason=(
                    f"The handoff recommended slice {normalized_recommendation!r}, but the "
                    f"highest-priority eligible queued slice is {highest_priority_next['slice_id']!r}."
                ),
                next_slice_id=None,
                recommended_next_slice=normalized_recommendation,
                required_validation_failures=[],
                dirty_paths_outside_scope=[],
                files_touched_outside_scope=[],
                changed_file_count=0,
            )

        return CompletionDecision(
            queue_status="done",
            decision="continue",
            should_continue=True,
            stop_reason="Continuation allowed into the recommended eligible next slice.",
            next_slice_id=normalized_recommendation,
            recommended_next_slice=normalized_recommendation,
            required_validation_failures=[],
            dirty_paths_outside_scope=[],
            files_touched_outside_scope=[],
            changed_file_count=0,
            missing_manual_proofs=[],
            missing_proof_level=False,
        )

    if highest_priority_next is None:
        return CompletionDecision(
            queue_status="done",
            decision="stop",
            should_continue=False,
            stop_reason="No eligible queued next slice remains after the current slice completed.",
            next_slice_id=None,
            recommended_next_slice="",
            required_validation_failures=[],
            dirty_paths_outside_scope=[],
            files_touched_outside_scope=[],
            changed_file_count=0,
            missing_manual_proofs=[],
            missing_proof_level=False,
        )

    return CompletionDecision(
        queue_status="done",
        decision="continue",
        should_continue=True,
        stop_reason="Continuation allowed into the highest-priority eligible queued slice.",
        next_slice_id=highest_priority_next["slice_id"],
        recommended_next_slice="",
        required_validation_failures=[],
        dirty_paths_outside_scope=[],
        files_touched_outside_scope=[],
        changed_file_count=0,
    )


def latest_handoff_for_slice(handoff_dir: Path, slice_id: str) -> Optional[dict[str, Any]]:
    latest_match: Optional[dict[str, Any]] = None
    latest_key: Optional[str] = None

    for candidate in sorted(handoff_dir.glob("*.json")):
        try:
            payload = load_handoff(candidate)
        except (ValueError, json.JSONDecodeError):
            payload = load_legacy_handoff_for_context(candidate)
            if payload is None:
                continue
        if payload["slice_id"] != slice_id:
            continue

        sort_key = f"{payload.get('timestamp', '')}:{candidate.name}"
        if latest_key is None or sort_key > latest_key:
            latest_key = sort_key
            latest_match = payload

    return latest_match


def load_legacy_handoff_for_context(handoff_path: Path) -> Optional[dict[str, Any]]:
    try:
        payload = load_json(handoff_path)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    required_legacy_keys = {
        "slice_id",
        "status",
        "summary",
        "files_touched",
        "validations_passed",
        "validations_failed",
        "recommended_next_slice",
        "recommended_next_reason",
        "timestamp",
    }
    if not required_legacy_keys.issubset(payload):
        return None
    if "residual_risks" not in payload and "risks" not in payload:
        return None

    return payload


def make_handoff_filename(slice_id: str, timestamp_token: str) -> str:
    safe_slice_id = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in slice_id)
    return f"{timestamp_token}-{safe_slice_id}.json"


def evaluate_completion(
    queue_data: dict[str, Any],
    slice_record: dict[str, Any],
    handoff: dict[str, Any],
    dirty_paths_before_run: list[str],
    dirty_paths_after_run: list[str],
    completed_autonomous_runs: int,
    run_limit: int,
    validation_replays: list[ValidationReplayResult] = [],
    post_run_commit_sha: str = "",
) -> CompletionDecision:
    validation_replays = validation_replays or []
    supervisor_owned_paths = queue_data["policy"]["supervisor_owned_paths"]
    introduced_dirty_paths = introduced_paths(dirty_paths_before_run, dirty_paths_after_run)
    validation_failures = sorted(
        set(
            required_validation_failures(
                slice_record["required_validations"], handoff["validations_passed"], handoff["validations_failed"]
            )
            + validation_replay_failures(validation_replays)
        )
    )
    unexpected_dirty_paths = out_of_scope_paths(introduced_dirty_paths, slice_record["allowed_paths"], supervisor_owned_paths)
    files_touched = normalize_paths(handoff["files_touched"])
    files_touched_outside_scope = out_of_scope_paths(files_touched, slice_record["allowed_paths"], supervisor_owned_paths)
    reported_out_of_scope = sorted(set(handoff["dirty_paths_outside_scope"]))
    changed_file_count = max(
        count_paths_within_scope(introduced_dirty_paths, slice_record["allowed_paths"], supervisor_owned_paths),
        len(set(files_touched)),
    )

    if handoff["status"] == "blocked":
        return CompletionDecision(
            queue_status="blocked",
            decision="stop_blocked",
            should_continue=False,
            stop_reason="Slice reported blocked status.",
            next_slice_id=None,
            recommended_next_slice=handoff["recommended_next_slice"].strip(),
            required_validation_failures=validation_failures,
            dirty_paths_outside_scope=unexpected_dirty_paths or reported_out_of_scope,
            files_touched_outside_scope=files_touched_outside_scope,
            changed_file_count=changed_file_count,
            supervisor_validation_replays=validation_replays,
            missing_manual_proofs=[],
            missing_proof_level=False,
        )

    if handoff["status"] == "failed":
        return CompletionDecision(
            queue_status="failed",
            decision="stop_failed",
            should_continue=False,
            stop_reason="Slice reported failed status.",
            next_slice_id=None,
            recommended_next_slice=handoff["recommended_next_slice"].strip(),
            required_validation_failures=validation_failures,
            dirty_paths_outside_scope=unexpected_dirty_paths or reported_out_of_scope,
            files_touched_outside_scope=files_touched_outside_scope,
            changed_file_count=changed_file_count,
            supervisor_validation_replays=validation_replays,
            missing_manual_proofs=[],
            missing_proof_level=False,
        )

    if validation_failures:
        return CompletionDecision(
            queue_status="failed",
            decision="stop_failed",
            should_continue=False,
            stop_reason="Required validations were missing, failed, or did not pass supervisor replay.",
            next_slice_id=None,
            recommended_next_slice=handoff["recommended_next_slice"].strip(),
            required_validation_failures=validation_failures,
            dirty_paths_outside_scope=unexpected_dirty_paths or reported_out_of_scope,
            files_touched_outside_scope=files_touched_outside_scope,
            changed_file_count=changed_file_count,
            supervisor_validation_replays=validation_replays,
            missing_manual_proofs=[],
            missing_proof_level=False,
        )

    if unexpected_dirty_paths or reported_out_of_scope or files_touched_outside_scope:
        return CompletionDecision(
            queue_status="failed",
            decision="stop_failed",
            should_continue=False,
            stop_reason="Files changed outside the slice's allowed_paths were detected.",
            next_slice_id=None,
            recommended_next_slice=handoff["recommended_next_slice"].strip(),
            required_validation_failures=validation_failures,
            dirty_paths_outside_scope=sorted(set(unexpected_dirty_paths + reported_out_of_scope)),
            files_touched_outside_scope=files_touched_outside_scope,
            changed_file_count=changed_file_count,
            supervisor_validation_replays=validation_replays,
            missing_manual_proofs=[],
            missing_proof_level=False,
        )

    if changed_file_count > slice_record["max_files_changed"]:
        return CompletionDecision(
            queue_status="failed",
            decision="stop_failed",
            should_continue=False,
            stop_reason=(f"Slice exceeded max_files_changed ({changed_file_count} > {slice_record['max_files_changed']})."),
            next_slice_id=None,
            recommended_next_slice=handoff["recommended_next_slice"].strip(),
            required_validation_failures=validation_failures,
            dirty_paths_outside_scope=[],
            files_touched_outside_scope=[],
            changed_file_count=changed_file_count,
            supervisor_validation_replays=validation_replays,
            missing_manual_proofs=[],
            missing_proof_level=False,
        )

    if handoff["status"] == "done":
        missing_proofs, missing_level = verify_manual_proofs(slice_record, handoff, post_run_commit_sha)
        if missing_proofs or missing_level:
            return CompletionDecision(
                queue_status="failed",
                decision="stop_failed",
                should_continue=False,
                stop_reason="Slice reported done, but required manual proof or proof level was missing.",
                next_slice_id=None,
                recommended_next_slice=handoff["recommended_next_slice"].strip(),
                required_validation_failures=validation_failures,
                dirty_paths_outside_scope=sorted(set(unexpected_dirty_paths + reported_out_of_scope)),
                files_touched_outside_scope=files_touched_outside_scope,
                changed_file_count=changed_file_count,
                supervisor_validation_replays=validation_replays,
                missing_manual_proofs=missing_proofs,
                missing_proof_level=missing_level,
            )

    next_decision = next_slice_eligibility(
        queue_data=queue_data,
        current_slice_id=slice_record["slice_id"],
        recommended_next_slice=handoff["recommended_next_slice"],
        completed_autonomous_runs=completed_autonomous_runs,
        run_limit=run_limit,
    )
    next_decision.required_validation_failures = []
    next_decision.dirty_paths_outside_scope = []
    next_decision.files_touched_outside_scope = []
    next_decision.changed_file_count = changed_file_count
    next_decision.supervisor_validation_replays = validation_replays
    next_decision.missing_manual_proofs = []
    next_decision.missing_proof_level = False
    return next_decision
