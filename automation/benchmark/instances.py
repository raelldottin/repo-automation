"""Resolve which ProgramBench instances to run and their task metadata.

The instance catalogue and per-task metadata ship inside the ``programbench`` package
(``programbench/data/tasks/<instance>/task.yaml``), so this module reads them locally
with no network or container. When ``programbench`` is not importable we fall back to a
small built-in smoke set so the CLI and tests still function.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

# Cheap, fast-building instances used when neither --instances nor --all is given.
DEFAULT_SMOKE_INSTANCES: tuple[str, ...] = ("abishekvashok__cmatrix.5c082c6",)

# ProgramBench reserves this prefix for its own test fixtures; never benchmark them.
_FIXTURE_PREFIX = "testorg__"

# How a task is named to the agent. An instance id is `<owner>__<repo>.<sha>`, so it names
# the upstream project outright, and a model that reads the name can recall the source
# instead of deriving it from the reference binary. A digest of it is stable across lanes
# and repeats, so two cells for the same task stay comparable in a transcript.
#
# Opaque in a prompt, not anonymous. ProgramBench's instance catalogue is public and
# small, so anyone holding it can hash every entry and invert this in a second. It is not
# an anti-memorization measure - that would need a run-scoped random or HMAC id, which
# would also cost cross-run comparability. What it is: the harness declining to put the
# name in front of the model.
_PUBLIC_ID_PREFIX = "task-"
_PUBLIC_ID_LENGTH = 12


@dataclass(frozen=True)
class TaskSpec:
    """What the harness needs to attempt one ProgramBench rebuild."""

    instance_id: str
    repository: str
    commit: str
    language: str
    difficulty: str

    @property
    def public_id(self) -> str:
        """What the harness calls this task in front of the agent."""
        return _PUBLIC_ID_PREFIX + hashlib.sha256(self.instance_id.encode("utf-8")).hexdigest()[:_PUBLIC_ID_LENGTH]

    @property
    def objective(self) -> str:
        """The task, with nothing in it that identifies the program being rebuilt.

        The language is gone along with the name. The submission only has to produce a
        `compile.sh` that builds an `./executable`, so the original implementation language
        was never a requirement - it was a hint about the upstream source, and an agent
        told "this is the Rust one" is part-way to recalling which Rust one.

        This says nothing about what the agent can work out from the workspace. ProgramBench
        bundles the program's own documentation, and cmatrix's `README.md` names the project
        and links its repository. The claim is about what the *harness* adds, not about what
        the cleanroom contains.
        """
        return (
            "Rebuild the program in this workspace from scratch so that its black-box test suite "
            "passes. Work only from the program's observable behaviour and interface. Produce a "
            "self-contained codebase plus an executable `compile.sh` at the workspace root that "
            "builds the program's `./executable`."
        )


def _tasks_dir() -> Optional[Path]:
    try:
        from programbench.constants import TASKS_DIR  # ty: ignore[unresolved-import]
    except Exception:  # pragma: no cover - programbench optional.
        return None
    tasks_dir = Path(TASKS_DIR)
    return tasks_dir if tasks_dir.is_dir() else None


def _read_task_yaml(path: Path) -> dict[str, str]:
    """Parse the flat scalar keys of a ProgramBench task.yaml without a YAML dependency.

    task.yaml is a flat ``key: value`` mapping plus one list (``eval_clean_hashes``); we
    only need the scalar fields, so a tiny line parser keeps this dependency-free.
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith(("#", "-")) or ":" not in line:
            continue
        if line[0].isspace():  # nested list/mapping value, not a top-level scalar.
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip("'\"")
        if value:
            values[key.strip()] = value
    return values


def task_spec(instance_id: str) -> TaskSpec:
    data: dict[str, str] = {}
    tasks_dir = _tasks_dir()
    if tasks_dir is not None:
        task_yaml = tasks_dir / instance_id / "task.yaml"
        if task_yaml.is_file():
            data = _read_task_yaml(task_yaml)
    repository = data.get("repository") or instance_id.rsplit(".", 1)[0].replace("__", "/")
    return TaskSpec(
        instance_id=instance_id,
        repository=repository,
        commit=data.get("commit", ""),
        language=data.get("language", "unknown"),
        difficulty=data.get("difficulty", "unknown"),
    )


def all_instances() -> list[str]:
    tasks_dir = _tasks_dir()
    if tasks_dir is None:
        return list(DEFAULT_SMOKE_INSTANCES)
    return sorted(
        entry.name
        for entry in tasks_dir.iterdir()
        if entry.is_dir() and (entry / "task.yaml").is_file() and not entry.name.startswith(_FIXTURE_PREFIX)
    )


def resolve_instances(explicit: Optional[Sequence[str]], use_all: bool) -> list[str]:
    if explicit:
        return list(explicit)
    if use_all:
        return all_instances()
    return list(DEFAULT_SMOKE_INSTANCES)
