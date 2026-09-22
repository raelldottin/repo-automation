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
import signal
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

from automation.context import build_context

from .instances import TaskSpec
from .strategies import (
    AGENT_TIMEOUT_RETURNCODE,
    RPI_DIR,
    ExecutionContext,
    ExecutionStrategy,
    SliceContextStrategy,
    StrategyResult,
)

DEFAULT_TIMEOUT_SECONDS = 1800
# How long a session gets to shut down after its budget kill, before the process group is
# destroyed. A SIGKILL takes the session's buffered stdout with it: every phase killed at
# its ceiling in runs 35715428932 and 35736569601 archived a 0-byte log, which is also the
# evidence the provider-failure classifier reads. Short, because this is flush-and-exit
# time, not working time - it is recorded separately and never charged to the phase.
AGENT_TERMINATION_GRACE_SECONDS = 5
# Allow the agent to touch the whole rebuild workspace.
WORKSPACE_ALLOWED_PATH = "./"
# Effectively unbounded diff budget: a from-scratch rebuild is not a bounded slice.
REBUILD_DIFF_BUDGET = 1_000_000

# (formatted_command, workspace, env, timeout_seconds) -> return code
CommandRunner = Callable[[str, Path, Mapping[str, str], int], int]

# Exactly what ``run_agent.sh`` reads, by literal name, and nothing else.
#
# A cell's whole job is to run model-authored commands, and it used to be handed
# ``dict(os.environ)``: the operator's provider keys, GitHub token, cloud credentials and
# SSH agent socket, none of which rebuilds a C repository. The list is names, not patterns,
# because a pattern fails open - ``REPO_AUTOMATION_*`` ships the next variable somebody adds
# under that prefix, whatever ends up in it, and the property worth having is that a variable
# nobody considered is absent. Anything else a session genuinely needs (a provider key, a base
# URL) is authorized deliberately through ``env=`` / ``--agent-env``.
#
# ``TERMINAL_CWD`` and ``HERMES_HOME`` are deliberately absent: the runner sets both itself,
# per invocation, and inheriting either would point a session at the previous cell's state.
INHERITED_ENV_NAMES = (
    "PATH",  # find any agent CLI at all
    "HOME",  # the CLI's own config and credential store
    "TMPDIR",  # run_agent.sh mktemp -d's the throwaway HERMES_HOME under it
    "LANG",
    "LC_ALL",
    "REPO_AUTOMATION_AGENT_RUNNER",
    "REPO_AUTOMATION_CODEX_BIN",
    "REPO_AUTOMATION_CLAUDE_BIN",
    "REPO_AUTOMATION_CLAUDE_PERMISSION_MODE",
    "REPO_AUTOMATION_HERMES_BIN",
    "OWLORY_CODEX_BIN",  # legacy alias run_agent.sh still honours
    "HERMES_REVISION",
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_INFERENCE_MODEL",
    "CLAUDECODE",  # the three markers run_agent.sh auto-detects Claude Code by
    "CLAUDE_CODE",
    "CLAUDE_CODE_ENTRYPOINT",
)


def audited_inherited_environment() -> dict[str, str]:
    """The launcher variables an agent session inherits, selected by exact name."""
    return {name: os.environ[name] for name in INHERITED_ENV_NAMES if name in os.environ}


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
        "execution_constraints": build_context.build_execution_constraints(slice_record),
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
    """Run one agent session, and treat exhausting its budget as a result, not a crash.

    The budget exists to bound a cell. If running it out raised, one slow cell would take
    the rest of the matrix with it and the run would report nothing at all - including for
    the lanes that finished. A timed-out session is a failed phase like any other.
    """
    # Its own process group, so the timeout can collect the whole session: the agent spawns
    # tool subprocesses, and one left behind would spend the next cell's wall clock too.
    with subprocess.Popen(command, cwd=workspace, shell=True, env=dict(env), start_new_session=True) as agent:
        try:
            return agent.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Ask before compelling. SIGTERM lets the session flush what it has written and
            # the tee drain the pipe, so a censored phase still leaves its checkpointed
            # artifact and a readable log; SIGKILL after the grace so a session that ignores
            # the signal still cannot outlive its budget.
            os.killpg(agent.pid, signal.SIGTERM)
            try:
                agent.wait(timeout=AGENT_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(agent.pid, signal.SIGKILL)
            return AGENT_TIMEOUT_RETURNCODE


# Never graded: VCS metadata and the lane's own phase artifacts.
EXCLUDED_FROM_SUBMISSION = frozenset({".git", RPI_DIR})
# Per-session agent reports, written next to the submission rather than into it.
AGENT_SESSIONS_DIR = "agent-sessions"
# What was actually graded, kept when the tarball itself is not.
SUBMISSION_MANIFEST_FILENAME = "submission.files.txt"


def _archive_workspace(workspace: Path, out_tar: Path) -> None:
    out_tar.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_tar, "w:gz") as tar:
        for entry in sorted(workspace.iterdir()):
            if entry.name in EXCLUDED_FROM_SUBMISSION:
                continue
            tar.add(entry, arcname=entry.name)
        members = tar.getnames()
    # The tarball itself is too large to keep, so the graded contents are unprovable after
    # the job ends: whether the cell submitted anything, and whether a lane's own phase
    # artifacts leaked into what was scored. List what went in, next to what came out.
    out_tar.with_name(SUBMISSION_MANIFEST_FILENAME).write_text("".join(f"{name}\n" for name in members), encoding="utf-8")


def _save_phase_artifacts(workspace: Path, out_dir: Path) -> None:
    """Keep phase artifacts next to the submission so a result can be reproduced."""
    source = workspace / RPI_DIR
    if not source.is_dir():
        return
    destination = Path(out_dir) / "rpi"
    # mkdir, not exists()-then-copy: claiming the directory is how ownership is decided, so it
    # has to be the single operation that decides it. The copy used to open with an
    # unconditional rmtree, which meant a rerun into a run directory holding a finished cell's
    # phase artifacts deleted them - the evidence of the attempt being investigated, destroyed
    # by the attempt investigating it. A retry does not get to decide it owns someone's output.
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as collision:
        raise FileExistsError(f"refusing to overwrite pre-existing phase artifact directory: {destination}") from collision
    try:
        shutil.copytree(source, destination, dirs_exist_ok=True)
    except BaseException:
        # Only ever the directory this call created, and only because it created it: a
        # half-copied artifact set reads as a complete one.
        shutil.rmtree(destination, ignore_errors=True)
        raise


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
        environment = audited_inherited_environment()
        if self._env:
            # Explicit configuration wins, and is never filtered through the inherited list:
            # an env= mapping is the caller's authorization, not an accident of the launcher.
            environment.update(self._env)
        return environment

    def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
        out_tar = Path(out_tar)

        workspace = Path(tempfile.mkdtemp(prefix=f"pb-{task.instance_id}-"))
        # run_agent.sh refuses a non-git repo root; the workspace is the agent's repo.
        subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
        control_dir = Path(tempfile.mkdtemp(prefix=f"pb-control-{task.instance_id}-"))

        environment = self._run_environment()
        # Beside the submission, never inside it: one usage + controls report per agent
        # session, so a result says which model answered and what it was allowed to do.
        # Absolute, because the agent runs with the workspace as its working directory and
        # --run-dir is usually relative: a relative path here wrote the reports into the
        # workspace, which archived them into the submission and left run.json with none.
        environment["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str((out_tar.parent / AGENT_SESSIONS_DIR).resolve())

        strategy_result = self._strategy.execute(
            ExecutionContext(
                repo_root=self._repo_root,
                task=task,
                workspace=workspace,
                control_dir=control_dir,
                command_template=self._template,
                env=environment,
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
