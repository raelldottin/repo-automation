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
FORBIDDEN_DESTINATION_PREFIXES = FORBIDDEN_SOURCE_PREFIXES
FORBIDDEN_DESTINATION_FILES = FORBIDDEN_SOURCE_FILES

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

    if destination in FORBIDDEN_DESTINATION_FILES:
        raise SyncError(f"forbidden import destination is owned by the consumer: {destination}")
    for prefix in FORBIDDEN_DESTINATION_PREFIXES:
        if destination == prefix.rstrip("/") or destination.startswith(prefix):
            raise SyncError(f"forbidden import destination is owned by the consumer: {destination}")

    # An ancestor is just as dangerous: a directory entry rooted at "automation" with
    # delete_stale set would sweep automation/queue, automation/handoffs and automation/proofs
    # without ever naming them.
    owned = sorted(FORBIDDEN_DESTINATION_FILES) + [prefix.rstrip("/") for prefix in FORBIDDEN_DESTINATION_PREFIXES]
    for consumer_path in owned:
        if consumer_path.startswith(destination + "/"):
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


def load_manifest(path: Path) -> tuple[Path, list[Entry]]:
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

    default_target_raw = data.get("default_target")
    if not isinstance(default_target_raw, str) or not default_target_raw:
        raise SyncError("manifest default_target must be a non-empty string")
    default_target = Path(default_target_raw).expanduser()

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SyncError("manifest entries must be a non-empty list")

    return default_target, [parse_entry(raw) for raw in raw_entries]


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
    status = subprocess.run(
        ["git", "-C", str(target_root), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        return set()

    modified: set[Path] = set()
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        entry_path = line[3:]
        # Renames report "old -> new"; the new path is the one on disk.
        if " -> " in entry_path:
            entry_path = entry_path.split(" -> ", 1)[1]
        modified.add((target_root / entry_path.strip().strip('"')).resolve(strict=False))
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


def git_output(repo: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout.strip()


def ensure_target_is_not_the_source(source_root: Path, target_root: Path) -> None:
    """Protection 1: an import has no write path back into the canonical repository.

    The old --auto-update mode wrote outward and once resolved to deleting 1937 lines of
    committed canonical work. There is no flag to re-enable that: files move canonical ->
    consumer only, so writing into the source, or into anything containing it, is refused.
    """
    source = source_root.resolve(strict=False)
    target = target_root.resolve(strict=False)

    if source == target:
        raise SyncError(f"refusing to import into the canonical source itself: {target}")
    if source in target.parents:
        raise SyncError(f"refusing to import into {target}, which lives inside the canonical source {source}")
    if target in source.parents:
        raise SyncError(f"refusing to import into {target}, which contains the canonical source {source}")


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
    parser.add_argument("--target", help="Destination consumer folder. Defaults to manifest default_target.")
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
    default_target, entries = load_manifest(manifest)
    target_root = Path(args.target).expanduser() if args.target else default_target

    ensure_target_is_not_the_source(source_root, target_root)

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
