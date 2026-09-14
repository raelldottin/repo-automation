"""Adapters that turn one ProgramBench task into a ``submission.tar.gz``.

The production adapter, ``SupervisorAgentAdapter``, exercises the *real* harness: it
models the rebuild as a single supervisor slice, renders the harness prompt with the
existing ``run_next.render_prompt`` (base + slice fragments), and launches the harness
agent runner (``run_agent.sh``) in a fresh local workspace. Nothing here uses a
container; the agent rebuilds on the local filesystem and the workspace is archived as
the submission. ProgramBench's cleanroom is only used later, by the eval step.

The agent invocation is a single injectable seam (``runner``) so tests drive the whole
adapter without an LLM or any external process. *How* the agent is driven is a second
seam (``strategy``): the default is ``SliceContextStrategy`` - the shipped harness
behaviour, and lane B of the effectiveness experiment - while other lanes vary context
and workflow only. Workspace setup, archiving and budget stay here so they are identical
across lanes.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

from automation.context import build_context

from .instances import TaskSpec
from .strategies import RPI_DIR, ExecutionContext, ExecutionStrategy, SliceContextStrategy, StrategyResult

DEFAULT_TIMEOUT_SECONDS = 1800
# Allow the agent to touch the whole rebuild workspace.
WORKSPACE_ALLOWED_PATH = "./"
# Effectively unbounded diff budget: a from-scratch rebuild is not a bounded slice.
REBUILD_DIFF_BUDGET = 1_000_000

# (formatted_command, workspace, env, timeout_seconds) -> return code
CommandRunner = Callable[[str, Path, Mapping[str, str], int], int]


@dataclass
class SubmissionResult:
    instance_id: str
    tar_path: Path
    workspace: Path
    returncode: int
    strategy: Optional[StrategyResult] = None


class AgentAdapter(Protocol):
    def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult: ...


def build_slice_record(task: TaskSpec) -> dict:
    """Model a ProgramBench rebuild as a schema-valid supervisor slice."""
    return {
        "slice_id": task.instance_id,
        "title": f"Rebuild {task.repository} from scratch",
        "status": "queued",
        "priority": 0,
        "domain": "programbench",
        "allowed_paths": [WORKSPACE_ALLOWED_PATH],
        "required_validations": ["sh compile.sh"],
        "depends_on": [],
        "max_files_changed": REBUILD_DIFF_BUDGET,
        "notes": task.objective,
    }


def build_queue_data(slice_record: dict, agent_command_template: str, timeout_seconds: int) -> dict:
    return {
        "version": 1,
        "policy": {
            "consecutive_autonomous_limit": 1,
            "handoff_timeout_seconds": timeout_seconds,
            "agent_command_template": agent_command_template,
            "supervisor_owned_paths": ["automation/"],
        },
        "slices": [slice_record],
    }


def build_context_bundle(queue_data: dict, slice_record: dict) -> dict:
    """Assemble the minimal bundle ``run_next.render_prompt`` consumes, reusing helpers."""
    return {
        "policy_sentence": build_context.POLICY_SENTENCE,
        "slice": build_context.summarize_slice(slice_record),
        "queue": build_context.build_queue_metadata(queue_data, slice_record),
        "validation_ownership": build_context.summarize_validation_ownership(slice_record),
        "previous_handoff": None,
        "previous_handoff_summary": build_context.render_previous_handoff_summary(None),
        "acceptance_checks": build_context.build_acceptance_checks(slice_record),
        "handoff_template": build_context.build_handoff_template(slice_record),
        "documents": [],
    }


def _default_command_template(repo_root: Path) -> str:
    """run_agent.sh referenced by absolute path: the agent's cwd is the workspace."""
    script = repo_root / "automation/supervisor/run_agent.sh"
    return (
        f"{shlex.quote(str(script))} --repo-root {{repo_root}} --prompt-file {{prompt_file}} "
        "--context-file {context_file} --handoff-file {handoff_file} --slice-id {slice_id}"
    )


def _subprocess_runner(command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
    result = subprocess.run(command, cwd=workspace, shell=True, env=dict(env), timeout=timeout)
    return result.returncode


# Never graded: VCS metadata and the lane's own phase artifacts.
EXCLUDED_FROM_SUBMISSION = frozenset({".git", RPI_DIR})


def _archive_workspace(workspace: Path, out_tar: Path) -> None:
    out_tar.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_tar, "w:gz") as tar:
        for entry in sorted(workspace.iterdir()):
            if entry.name in EXCLUDED_FROM_SUBMISSION:
                continue
            tar.add(entry, arcname=entry.name)


def _save_phase_artifacts(workspace: Path, out_dir: Path) -> None:
    """Keep phase artifacts next to the submission so a result can be reproduced."""
    source = workspace / RPI_DIR
    if not source.is_dir():
        return
    destination = Path(out_dir) / "rpi"
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination)


class SupervisorAgentAdapter:
    """Drive the repo-automation supervisor loop to produce a submission, locally."""

    def __init__(
        self,
        repo_root: Path,
        agent_command_template: Optional[str] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        env: Optional[Mapping[str, str]] = None,
        runner: Optional[CommandRunner] = None,
        strategy: Optional[ExecutionStrategy] = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._template = agent_command_template or _default_command_template(self._repo_root)
        self._timeout = timeout_seconds
        self._env = dict(env) if env is not None else None
        self._runner = runner or _subprocess_runner
        self._strategy = strategy or SliceContextStrategy()

    @property
    def lane(self) -> str:
        return self._strategy.name

    def _run_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        if self._env:
            environment.update(self._env)
        return environment

    def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
        out_tar = Path(out_tar)

        workspace = Path(tempfile.mkdtemp(prefix=f"pb-{task.instance_id}-"))
        # run_agent.sh refuses a non-git repo root; the workspace is the agent's repo.
        subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
        control_dir = Path(tempfile.mkdtemp(prefix=f"pb-control-{task.instance_id}-"))

        strategy_result = self._strategy.execute(
            ExecutionContext(
                repo_root=self._repo_root,
                task=task,
                workspace=workspace,
                control_dir=control_dir,
                command_template=self._template,
                env=self._run_environment(),
                timeout_seconds=self._timeout,
                runner=self._runner,
            )
        )

        _save_phase_artifacts(workspace, out_tar.parent)
        _archive_workspace(workspace, out_tar)
        return SubmissionResult(
            instance_id=task.instance_id,
            tar_path=out_tar,
            workspace=workspace,
            returncode=strategy_result.returncode,
            strategy=strategy_result,
        )
