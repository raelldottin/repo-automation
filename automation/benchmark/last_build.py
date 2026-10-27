"""Keep the last tree the agent saw build, and grade that one when implement is killed.

Run 40399303297's implement phase was killed at its 5400s budget mid-edit: ``cmatrix.c``
referenced an identifier it had not declared yet, and the cell was scored ``compile_failed``.
Whether that session ever had a building tree is unknown - ``--oneshot`` archived no tool
transcript - but the build-first protocol asks for one, and a kill lands between an edit and
the rebuild that would have checked it, discarding whatever built before.

The harness cannot see the agent's builds from the host - ``--oneshot`` leaves no tool
transcript, and compiling host-side would test a toolchain the grader does not use. So the
snapshot is taken inside the build itself: the implement prompt's build command is
``SCRIPT_NAME``, which runs ``compile.sh`` in the agent's own environment and, only when it
exits 0, records the tree it just built. A session that is killed is graded on that record.
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path
from typing import Any

SCRIPT_NAME = "build.sh"
SNAPSHOT_NAME = "last-build.tar"
# Extracted beside the snapshot, inside the phase-artifact directory, so the swap into the
# workspace is a rename on one filesystem rather than a copy that can die halfway.
_STAGING_NAME = "last-build.restore"

# POSIX sh and tar, because it runs in whatever image the instance ships. The temp-then-mv is
# what keeps the record whole: a kill during tar leaves the previous snapshot in place.
# COPYFILE_DISABLE stops macOS bsdtar storing extended attributes as `._*` entries, which a
# restore would put into a local cell's submission; GNU tar ignores it.
_SCRIPT = """#!/bin/sh
# Written by the benchmark harness before implement. Runs compile.sh and, only if it
# succeeds, records the tree it built: a session stopped at its budget is graded on the
# last tree recorded here, not on an edit no build ever checked.
set -e
cd "$(dirname "$0")/.."
./compile.sh
COPYFILE_DISABLE=1 tar {excludes} -cf {rpi_dir}/{snapshot}.part .
mv -f {rpi_dir}/{snapshot}.part {rpi_dir}/{snapshot}
"""


def install(workspace: Path, rpi_dir: str, excluded: frozenset[str]) -> Path:
    """Write the checkpointing build script into the workspace; return its host path.

    ``excluded`` is what the submission leaves out, so the record holds exactly what would
    be submitted and a restore never touches anything else.
    """
    script = Path(workspace) / rpi_dir / SCRIPT_NAME
    script.parent.mkdir(parents=True, exist_ok=True)
    excludes = " ".join(f"--exclude=./{name}" for name in sorted(excluded))
    script.write_text(_SCRIPT.format(excludes=excludes, rpi_dir=rpi_dir, snapshot=SNAPSHOT_NAME), encoding="utf-8")
    script.chmod(0o755)
    return script


def restore(workspace: Path, rpi_dir: str, excluded: frozenset[str]) -> dict[str, Any]:
    """Replace the workspace's submittable contents with the last recorded build.

    Everything is extracted and checked before anything in the workspace is removed, so a
    record that cannot be read leaves the workspace as the agent left it.
    """
    workspace = Path(workspace)
    snapshot = workspace / rpi_dir / SNAPSHOT_NAME
    if not snapshot.is_file():
        return {"restored": False, "reason": "no successful build was recorded"}

    staging = workspace / rpi_dir / _STAGING_NAME
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        with tarfile.open(snapshot) as tar:
            members = [member for member in tar.getmembers() if _top_level(member.name) not in excluded | {""}]
            # "data" refuses absolute paths, ``..``, links out of the tree and device files: the
            # record was written from inside the agent's sandbox, so it is not trusted input.
            tar.extractall(staging, members=members, filter="data")
    except (tarfile.TarError, OSError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        return {"restored": False, "reason": f"recorded build unreadable: {error}"}

    for entry in workspace.iterdir():
        if entry.name in excluded:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    restored = sorted(entry.name for entry in staging.iterdir())
    for name in restored:
        (staging / name).rename(workspace / name)
    staging.rmdir()
    return {"restored": True, "entries": restored}


def _top_level(name: str) -> str:
    parts = [part for part in name.split("/") if part not in ("", ".")]
    return parts[0] if parts else ""


def checkpointed_build_command(agent_workspace: str, rpi_dir: str) -> str:
    """The implement prompt's build command: ``compile.sh``, recorded when it succeeds."""
    script = f"./{rpi_dir}/{SCRIPT_NAME}"
    return f"cd {agent_workspace.rstrip('/')} && {script}" if agent_workspace else script


__all__ = ["SCRIPT_NAME", "SNAPSHOT_NAME", "checkpointed_build_command", "install", "restore"]
