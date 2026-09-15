"""Resolve the canonical J-Space skill lane E administers.

Lane E is lane D plus J-Space. That subtraction only means anything if E receives the
*canonical* skill rather than an in-house paraphrase of it, so this module resolves the
artifact from a pinned external checkout, verifies its identity and hash, and refuses to
substitute anything when it cannot. There is no fallback text: an unresolvable J-Space
makes lane E unrunnable, which is a reportable outcome, unlike a silent approximation.

The skill's content is never vendored into this repository. `jspace.lock.json` pins who it
is and what its bytes hash to; the operator supplies a checkout through ``JSPACE_ROOT``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

LOCK_FILENAME = "jspace.lock.json"
ROOT_ENV_VAR = "JSPACE_ROOT"


class JSpaceUnavailable(RuntimeError):
    """The canonical skill could not be resolved, so lane E must not run."""


@dataclass(frozen=True)
class JSpaceArtifact:
    """The exact bytes lane E administers, and where they came from."""

    source: str
    revision: str
    artifact: str
    sha256: str
    root: Path
    text: str

    def provenance(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "revision": self.revision,
            "artifact": self.artifact,
            "sha256": self.sha256,
            "root": str(self.root),
        }

    def prompt_block(self) -> str:
        """The canonical text, verbatim, with only the plumbing the skill leaves open.

        The skill addresses its own installation as ``<skill-root>`` and its interpreter as
        ``<python-command>``. Binding those two names is plumbing; editing anything else
        would make E measure our rewrite instead of the skill.
        """
        return f"<skill-root> is `{self.root}`. <python-command> is `python3`.\n\n{self.text.strip()}"


def load_lock(lock_path: Optional[Path] = None) -> dict[str, Any]:
    path = Path(lock_path) if lock_path else Path(__file__).resolve().parent / LOCK_FILENAME
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise JSpaceUnavailable(f"jspace_unavailable: lock file missing at {path}") from error
    except json.JSONDecodeError as error:
        raise JSpaceUnavailable(f"jspace_unavailable: lock file is not valid JSON: {error}") from error

    missing = [key for key in ("source", "revision", "artifact", "sha256") if not lock.get(key)]
    if missing:
        raise JSpaceUnavailable(f"jspace_unavailable: lock file is missing {', '.join(missing)}")
    return lock


def checkout_revision(root: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def resolve(root: Optional[Path] = None, lock_path: Optional[Path] = None) -> JSpaceArtifact:
    """Return the pinned artifact, or raise ``JSpaceUnavailable`` saying exactly what failed."""
    lock = load_lock(lock_path)

    root_value = root if root is not None else os.environ.get(ROOT_ENV_VAR)
    if not root_value:
        raise JSpaceUnavailable(f"jspace_unavailable: set {ROOT_ENV_VAR} to a checkout of {lock['source']} at {lock['revision']}")

    resolved_root = Path(root_value).expanduser()
    if not resolved_root.is_dir():
        raise JSpaceUnavailable(f"jspace_unavailable: {ROOT_ENV_VAR} is not a directory: {resolved_root}")

    # Identity before content: a directory holding the right bytes under the wrong history
    # is still not the revision the run claims to have administered.
    head = checkout_revision(resolved_root)
    if head is None:
        raise JSpaceUnavailable(f"jspace_unavailable: {resolved_root} is not a Git checkout, so its revision cannot be verified")
    if head != lock["revision"]:
        raise JSpaceUnavailable(f"jspace_unavailable: {resolved_root} is at {head}, not the pinned {lock['revision']}")

    artifact_path = resolved_root / Path(lock["artifact"])
    try:
        raw = artifact_path.read_bytes()
    except OSError as error:
        raise JSpaceUnavailable(f"jspace_unavailable: cannot read {artifact_path}: {error}") from error

    digest = hashlib.sha256(raw).hexdigest()
    if digest != lock["sha256"]:
        raise JSpaceUnavailable(f"jspace_unavailable: {lock['artifact']} hashes to {digest}, not the pinned {lock['sha256']}")

    return JSpaceArtifact(
        source=lock["source"],
        revision=lock["revision"],
        artifact=lock["artifact"],
        sha256=digest,
        root=resolved_root.resolve(),
        text=raw.decode("utf-8"),
    )


__all__ = ["JSpaceArtifact", "JSpaceUnavailable", "LOCK_FILENAME", "ROOT_ENV_VAR", "load_lock", "resolve"]
