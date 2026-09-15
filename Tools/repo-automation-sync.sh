#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

python3 - "$ROOT" "$@" <<'PY'
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


FORBIDDEN_SOURCE_PREFIXES = (
    ".githooks/",
    "SecondBrain/",
    "automation/handoffs/",
    "automation/proofs/",
    "automation/queue/",
    "automation/smoke/",
    "docs/product/",
    "docs/runtime/",
    "localization/",
    "owlory_xcode/",
)

FORBIDDEN_SOURCE_FILES = {
    ".githooks/pre-push",
    "Makefile",
    "Tools/bump-version.sh",
    "Tools/generate-build-info.sh",
    "Tools/release-preflight.sh",
    "Tools/set-build-number.sh",
    "Tools/verify-build-provenance.sh",
}

# Destinations a consumer owns outright. The canonical repository supplies both the file
# list and this tool, so without a destination check an upstream entry could aim reusable
# content at the consumer's hook, Makefile or live queue - and delete_stale could remove
# them. Unlike the source check there is no manifest opt-out: these are never importable.
#
# The lock file is here because the consumer's Makefile reads it and passes the values to
# this tool: whoever writes the lock chooses what a consumer's `make` executes. It is a
# statement about the canonical repo, written by the consumer at import time, never
# imported.
FORBIDDEN_DESTINATION_PREFIXES = FORBIDDEN_SOURCE_PREFIXES
FORBIDDEN_DESTINATION_FILES = FORBIDDEN_SOURCE_FILES | {"automation/repo-automation.lock"}

# Compared casefolded: on APFS and NTFS a destination of "makefile" or ".GitHooks/pre-push"
# is the same file as the one being protected, and an exact-match check would wave it past.
FOLDED_DESTINATION_FILES = {path.casefold() for path in FORBIDDEN_DESTINATION_FILES}
FOLDED_DESTINATION_PREFIXES = tuple(prefix.casefold() for prefix in FORBIDDEN_DESTINATION_PREFIXES)

SKIP_NAMES = {"__pycache__", ".DS_Store"}


@dataclass(frozen=True)
class Entry:
    source: str
    destination: str
    kind: str
    preserve_executable: bool
    delete_stale: bool
    template: bool
    allow_owlory_specific: bool


class SyncError(Exception):
    pass


def fail(message: str) -> int:
    print(f"repo-automation-sync: error: {message}", file=sys.stderr)
    return 2


def normalize_relative_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SyncError(f"{field_name} must be a non-empty string")

    path = PurePosixPath(value)
    if path.is_absolute():
        raise SyncError(f"{field_name} must be repo-relative, got absolute path {value!r}")

    parts = path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SyncError(f"{field_name} must not contain '.', '..', or empty path parts: {value!r}")

    return path.as_posix()


def bool_field(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise SyncError(f"manifest entry {raw.get('source', '<unknown>')!r} has non-boolean {key}")
    return value


def parse_entry(raw: object) -> Entry:
    if not isinstance(raw, dict):
        raise SyncError("manifest entries must be objects")

    source = normalize_relative_path(raw.get("source"), "source")
    destination = normalize_relative_path(raw.get("destination"), "destination")

    kind = raw.get("kind")
    if kind not in {"file", "directory"}:
        raise SyncError(f"manifest entry {source!r} has unsupported kind {kind!r}")

    allow_owlory_specific = bool(raw.get("allow_owlory_specific", False))
    if not allow_owlory_specific:
        if source in FORBIDDEN_SOURCE_FILES:
            raise SyncError(f"forbidden Owlory-specific source requires explicit approval: {source}")
        for prefix in FORBIDDEN_SOURCE_PREFIXES:
            if source == prefix.rstrip("/") or source.startswith(prefix):
                raise SyncError(f"forbidden Owlory-specific source requires explicit approval: {source}")

    folded_destination = destination.casefold()
    if folded_destination in FOLDED_DESTINATION_FILES:
        raise SyncError(f"forbidden import destination is owned by the consumer: {destination}")
    for prefix in FOLDED_DESTINATION_PREFIXES:
        if folded_destination == prefix.rstrip("/") or folded_destination.startswith(prefix):
            raise SyncError(f"forbidden import destination is owned by the consumer: {destination}")

    # An ancestor is just as dangerous: a directory entry rooted at "automation" with
    # delete_stale set would sweep automation/queue, automation/handoffs and automation/proofs
    # without ever naming them.
    owned = sorted(FOLDED_DESTINATION_FILES) + [prefix.rstrip("/") for prefix in FOLDED_DESTINATION_PREFIXES]
    for consumer_path in owned:
        if consumer_path.startswith(folded_destination + "/"):
            raise SyncError(
                f"forbidden import destination {destination!r} contains consumer-owned {consumer_path!r}"
            )

    return Entry(
        source=source,
        destination=destination,
        kind=kind,
        preserve_executable=bool_field(raw, "preserve_executable"),
        delete_stale=bool_field(raw, "delete_stale"),
        template=bool_field(raw, "template"),
        allow_owlory_specific=allow_owlory_specific,
    )


def load_manifest(path: Path) -> list[Entry]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SyncError(f"manifest not found at {path}") from error
    except json.JSONDecodeError as error:
        raise SyncError(f"manifest is not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise SyncError("manifest root must be an object")
    if data.get("version") != 1:
        raise SyncError("manifest version must be 1")

    # A tool whose whole job is writing files has no safe default destination. The field
    # this replaces named the canonical repository itself - correct before the ownership
    # flip, and afterwards a --sync away from writing outward. Rejecting it rather than
    # ignoring it keeps it from drifting back in from an upstream manifest.
    if "default_target" in data:
        raise SyncError(
            "manifest must not carry default_target: the destination is the caller's to name. "
            "Pass --target explicitly."
        )

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SyncError("manifest entries must be a non-empty list")

    return [parse_entry(raw) for raw in raw_entries]


def resolve_under(base: Path, relative: str, *, must_exist: bool) -> Path:
    base_resolved = base.resolve(strict=False)
    candidate = base.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as error:
        raise SyncError(f"source path does not exist: {relative}") from error

    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise SyncError(f"path escapes root: {relative}")
    return resolved


def should_skip(path: Path) -> bool:
    return any(part in SKIP_NAMES for part in path.parts)


def source_files(source_root: Path, target_root: Path, entry: Entry) -> list[tuple[Path, Path, str]]:
    source = resolve_under(source_root, entry.source, must_exist=True)
    destination = resolve_under(target_root, entry.destination, must_exist=False)

    if source.is_symlink():
        raise SyncError(f"symlink sources are not supported: {entry.source}")

    if entry.kind == "file":
        if not source.is_file():
            raise SyncError(f"manifest entry {entry.source} is not a file")
        return [(source, destination, entry.destination)]

    if not source.is_dir():
        raise SyncError(f"manifest entry {entry.source} is not a directory")

    pairs: list[tuple[Path, Path, str]] = []
    for child in sorted(source.rglob("*")):
        if should_skip(child):
            continue
        if child.is_symlink():
            raise SyncError(f"symlink sources are not supported: {child.relative_to(source_root)}")
        if child.is_dir():
            continue
        relative = child.relative_to(source)
        target = destination / relative
        display = PurePosixPath(entry.destination, relative.as_posix()).as_posix()
        pairs.append((child, target, display))
    return pairs


def executable_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode) & 0o111


def set_target_mode(source: Path, target: Path, *, preserve_executable: bool) -> None:
    if preserve_executable:
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
    else:
        os.chmod(target, 0o644)


def file_drift(source: Path, target: Path, *, preserve_executable: bool) -> str | None:
    if not target.exists():
        return "missing"
    if not target.is_file():
        return "type"
    if source.read_bytes() != target.read_bytes():
        return "changed"
    if preserve_executable and executable_bits(source) != executable_bits(target):
        return "mode"
    return None


def stale_files(target_root: Path, entry: Entry, expected: set[Path]) -> list[tuple[Path, str]]:
    if entry.kind != "directory" or not entry.delete_stale or entry.template:
        return []

    destination = resolve_under(target_root, entry.destination, must_exist=False)
    if not destination.exists():
        return []

    stale: list[tuple[Path, str]] = []
    for child in sorted(destination.rglob("*")):
        if should_skip(child):
            continue
        if child.is_dir():
            continue
        if child not in expected:
            display = child.relative_to(target_root).as_posix()
            stale.append((child, display))
    return stale


def remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for child in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        try:
            child.rmdir()
        except OSError:
            pass


def locally_modified_targets(target_root: Path) -> set[Path]:
    """Tracked files the consumer has edited but not committed.

    Overwriting one of these destroys work that exists nowhere else - not in the consumer's
    history and not upstream. Untracked files are excluded: a brand new file in a vendored
    directory is stale, which is a different decision handled by delete_stale.
    """
    toplevel_result = git_result(target_root, "rev-parse", "--show-toplevel")
    if toplevel_result.returncode != 0:
        # A consumer that is not under version control has no uncommitted work to lose, so
        # there is nothing for this protection to do. Every other failure - dubious
        # ownership, a missing git, an unreadable index - must not silently switch it off.
        if "not a git repository" in toplevel_result.stderr.lower():
            return set()
        raise SyncError(
            f"could not determine whether {target_root} has uncommitted changes, so the import "
            f"cannot promise not to overwrite them: {toplevel_result.stderr.strip() or 'git failed'}"
        )

    # Status paths are relative to the repository root, not to -C, so a consumer vendored in
    # a subdirectory needs the toplevel to rebuild them.
    toplevel = Path(toplevel_result.stdout.strip())

    status = git_result(target_root, "status", "--porcelain", "-z", "--untracked-files=no")
    if status.returncode != 0:
        raise SyncError(f"could not read consumer status: {status.stderr.strip() or 'git failed'}")

    # -z emits "XY <path>\0", and for a rename or copy a second "\0<source path>" follows.
    # It is the one format that does not quote or escape unusual bytes in a filename.
    fields = status.stdout.split("\0")
    modified: set[Path] = set()
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        states, entry_path = field[:2], field[3:]
        if "R" in states or "C" in states:
            index += 1
        modified.add((toplevel / entry_path).resolve(strict=False))
    return modified


def sync_entries(
    source_root: Path,
    target_root: Path,
    entries: list[Entry],
    *,
    check: bool,
    force_templates: bool = False
) -> int:
    target_root_resolved = target_root.resolve(strict=False)
    issues: list[str] = []

    # Plan the whole import before touching anything, so a refusal leaves the consumer
    # byte-for-byte unchanged rather than half-imported.
    writes: list[tuple[Path, Path, str, Entry]] = []
    deletions: list[tuple[Path, str]] = []

    for entry in entries:
        pairs = source_files(source_root, target_root_resolved, entry)
        expected = {target for _, target, _ in pairs}

        for source, target, display in pairs:
            if entry.template and target.exists() and not force_templates:
                continue
            drift = file_drift(source, target, preserve_executable=entry.preserve_executable)
            if drift is None:
                continue
            if check:
                issues.append(f"{drift}: {display}")
                continue
            writes.append((source, target, display, entry))

        for stale, display in stale_files(target_root_resolved, entry, expected):
            if check:
                issues.append(f"stale: {display}")
                continue
            deletions.append((stale, display))

    if check:
        if issues:
            for issue in sorted(issues):
                print(issue)
            print(f"result: drift found ({len(issues)} issue(s))")
            return 1
        print("result: target is current")
        return 0

    modified = locally_modified_targets(target_root_resolved)
    clobbered = sorted(
        display
        for path, display in [(target, display) for _, target, display, _ in writes] + deletions
        if path.resolve(strict=False) in modified
    )
    if clobbered:
        raise SyncError(
            "refusing to import over locally modified files: "
            + ", ".join(clobbered)
            + ". Commit, stash or revert them first - an import overwrites them silently otherwise."
        )

    changes = 0
    for source, target, display, entry in writes:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        set_target_mode(source, target, preserve_executable=entry.preserve_executable)
        print(f"synced: {display}")
        changes += 1

    for stale, display in deletions:
        stale.unlink()
        print(f"removed stale: {display}")
        changes += 1

    for entry in entries:
        if entry.kind == "directory" and entry.delete_stale:
            remove_empty_dirs(resolve_under(target_root_resolved, entry.destination, must_exist=False))

    if changes:
        print(f"result: synced {changes} change(s)")
    else:
        print("result: already current")
    return 0


def git_result(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git without raising. Callers that must tell one failure from another read stderr."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def git_output(repo: Path, *args: str) -> tuple[int, str]:
    result = git_result(repo, *args)
    return result.returncode, result.stdout.strip()


def ensure_target_is_not_canonical(source_root: Path, target_root: Path, expect_remote: str | None) -> None:
    """Protection 1: an import has no write path back into the canonical repository.

    The old --auto-update mode wrote outward and once resolved to deleting 1937 lines of
    committed canonical work. There is no flag to re-enable that: files move canonical ->
    consumer only.

    Path containment alone does not establish that. Two checkouts of the canonical
    repository sitting side by side contain neither the other, so the destructive case
    looks like an ordinary import right up until delete_stale runs. What makes a
    destination unsafe is its identity, not its location.
    """
    source = source_root.resolve(strict=False)
    target = target_root.resolve(strict=False)

    if source == target:
        raise SyncError(f"refusing to import into the canonical source itself: {target}")
    if source in target.parents:
        raise SyncError(f"refusing to import into {target}, which lives inside the canonical source {source}")
    if target in source.parents:
        raise SyncError(f"refusing to import into {target}, which contains the canonical source {source}")

    # Worktrees of one repository share a common git dir, and that holds even when neither
    # has a remote configured. This is the sibling-checkout case.
    source_code, source_common = git_output(source, "rev-parse", "--git-common-dir")
    target_code, target_common = git_output(target, "rev-parse", "--git-common-dir")
    if source_code == 0 and target_code == 0:
        if (source / source_common).resolve(strict=False) == (target / target_common).resolve(strict=False):
            raise SyncError(
                f"refusing to import into {target}: it is a checkout of the canonical source {source}"
            )

    # Independent clones do not share a git dir, so fall back to what the repository says it
    # is. Protection 3 already applies this to the source; the destination needs it more,
    # because that is the side that gets written to.
    target_remote_code, target_remote = git_output(target, "remote", "get-url", "origin")
    if target_remote_code != 0 or not target_remote:
        return

    source_remote_code, source_remote = git_output(source, "remote", "get-url", "origin")
    canonical_remotes = {remote for remote in (expect_remote, source_remote if source_remote_code == 0 else None) if remote}
    if target_remote in canonical_remotes:
        raise SyncError(
            f"refusing to import into {target}: its 'origin' is the canonical repository {target_remote}"
        )


def verify_source_provenance(source_root: Path, pin: str | None, expect_remote: str | None) -> None:
    """Protections 2, 3 and 4: the snapshot must be traceable to a published commit.

    Cleanliness is not provenance. The destructive 2026-09-14 run was against a clean
    worktree on the correct branch; what it lacked was any statement of which commit it
    was meant to be. A checkout that merely resembles the canonical repository is refused.
    """
    if not pin:
        raise SyncError(
            "refusing to import without --pin: the canonical source commit must be explicit. "
            "Pass --pin <full-sha> (Owlory reads it from automation/repo-automation.lock), "
            "or --allow-unverified-source for local development only."
        )

    is_repo, _ = git_output(source_root, "rev-parse", "--show-toplevel")
    if is_repo != 0:
        raise SyncError(f"source is not a Git checkout, so its identity cannot be verified: {source_root}")

    # Protection 3. An absent origin is a failure, not a pass - a source that cannot state
    # what repository it is, is exactly the case this check exists for.
    remote_code, remote_url = git_output(source_root, "remote", "get-url", "origin")
    if remote_code != 0 or not remote_url:
        raise SyncError(
            f"source repository identity cannot be established: {source_root} has no 'origin' remote"
        )
    if expect_remote and remote_url != expect_remote:
        raise SyncError(
            f"source repository identity mismatch: expected {expect_remote}, found {remote_url}"
        )

    # Protection 4. Uncommitted content is unpublished by definition.
    status_code, status = git_output(source_root, "status", "--porcelain", "--untracked-files=all")
    if status_code != 0:
        raise SyncError(f"could not read source status: {source_root}")
    if status:
        raise SyncError(
            f"canonical source has uncommitted changes, so the import would vendor unpublished content:\n{status}"
        )

    head_code, head = git_output(source_root, "rev-parse", "HEAD")
    if head_code != 0:
        raise SyncError(f"could not resolve source HEAD: {source_root}")
    if head != pin:
        raise SyncError(
            f"source is not at the pinned commit: pinned {pin}, source HEAD {head}"
        )


def main(argv: list[str]) -> int:
    repo_root = Path(argv[0]).resolve()

    parser = argparse.ArgumentParser(description="Import reusable automation from the canonical repo-automation checkout into a consumer.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Report drift without changing files.")
    mode.add_argument("--sync", action="store_true", help="Update the target to match the reusable manifest.")
    parser.add_argument("--target", required=True, help="Destination consumer folder. There is no default.")
    parser.add_argument("--source", default=str(repo_root), help="Canonical source repository root. Defaults to this checkout.")
    parser.add_argument("--manifest", help="Manifest path. Defaults to <source>/automation/reusable-manifest.json.")
    parser.add_argument("--pin", help="Full commit SHA the canonical source must be checked out at.")
    parser.add_argument("--expect-remote", help="Remote URL the canonical source's 'origin' must match.")
    parser.add_argument(
        "--allow-unverified-source",
        action="store_true",
        help="Development only. Skip the pin, identity and cleanliness checks on the source."
    )
    parser.add_argument(
        "--force-templates",
        action="store_true",
        help="Re-baseline template entries to source content even when destination files already exist. "
        "Consumer-added files in template directories still survive."
    )
    args = parser.parse_args(argv[1:])

    source_root = Path(args.source).expanduser().resolve(strict=True)
    manifest = Path(args.manifest).expanduser() if args.manifest else source_root / "automation/reusable-manifest.json"
    entries = load_manifest(manifest)
    target_root = Path(args.target).expanduser()

    ensure_target_is_not_canonical(source_root, target_root, args.expect_remote)

    # --check answers "does the snapshot match the commit it is pinned to", so it needs the
    # same provenance as --sync whenever a pin is supplied. Without a pin it stays a plain
    # drift report, which is what the development fixtures use.
    if args.sync or args.pin:
        if args.allow_unverified_source:
            print(
                "repo-automation-sync: warning: reading from an unverified source "
                "(--allow-unverified-source); the result is not traceable to a published commit.",
                file=sys.stderr,
            )
        else:
            verify_source_provenance(source_root, args.pin, args.expect_remote)

    if args.sync:
        target_root.mkdir(parents=True, exist_ok=True)
    elif not target_root.exists():
        print(f"missing target: {target_root}")
        print("result: drift found (target missing)")
        return 1

    return sync_entries(
        source_root,
        target_root,
        entries,
        check=args.check,
        force_templates=args.force_templates
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SyncError as error:
        raise SystemExit(fail(str(error)))
PY
