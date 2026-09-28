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

import json
import os
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from automation.context import build_context

from .cleanroom import CleanroomError, CleanroomReceipt
from .cleanroom import prepare as prepare_cleanroom
from .witness import SandboxWitness
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
    cleanroom: Optional[CleanroomReceipt] = None


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


SUPERVISOR_SCRIPT = "automation/supervisor/run_agent.sh"
# The no-model default-backend probe: its witness receipt and its log are named for it
# rather than for a session, because it is not one.
SANDBOX_PROBE_STEM = "sandbox-probe"
# One container start and one `docker exec`. Generous for that, and short enough that a
# wedged daemon does not eat the cell it is protecting.
SANDBOX_PROBE_TIMEOUT_SECONDS = 300


def _observed_task_ids(observation: Mapping[str, Any]) -> str:
    containers = observation.get("containers") or []
    return ", ".join(str(record.get("task_id")) for record in containers) or "none"


def _default_command_template(repo_root: Path) -> str:
    """run_agent.sh referenced by absolute path: the agent's cwd is the workspace."""
    script = repo_root / SUPERVISOR_SCRIPT
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


# Never graded: VCS metadata, the lane's own phase artifacts, and whatever binary sits at
# the graded path. The evaluator deletes ``./executable`` before running compile.sh either
# way, but under a cleanroom workspace that file is ProgramBench's *reference* binary, and a
# submission shipping it reads to the disqualification judge as wrapping the reference.
EXCLUDED_FROM_SUBMISSION = frozenset({".git", RPI_DIR, "executable"})
# Per-session agent reports, written next to the submission rather than into it.
AGENT_SESSIONS_DIR = "agent-sessions"
# What the sandbox was proved to be, written next to the submission it produced.
CLEANROOM_RECEIPT_FILENAME = "cleanroom.json"
# Read by run_agent.sh: the image the model-facing tools run inside, or nothing at all.
SANDBOX_IMAGE_ENV = "REPO_AUTOMATION_HERMES_SANDBOX_IMAGE"
# What was actually graded, kept when the tarball itself is not.
SUBMISSION_MANIFEST_FILENAME = "submission.files.txt"


def _archive_workspace(workspace: Path, out_tar: Path) -> None:
    out_tar.parent.mkdir(parents=True, exist_ok=True)
    manifest = out_tar.with_name(SUBMISSION_MANIFEST_FILENAME)
    # touch(exist_ok=False), for the reason _save_phase_artifacts uses mkdir(exist_ok=False)
    # below: creating the path is the single operation that decides who owns it. These two
    # writes used to truncate unconditionally, so a rerun into a run directory holding a
    # finished cell overwrote the graded submission and the record of what was graded - the
    # same destruction the .rpi copy beside them refuses. Both paths are claimed before either
    # is written, so refusing the second does not strand a submission from the first.
    claimed: list[Path] = []
    try:
        for path in (out_tar, manifest):
            try:
                path.touch(exist_ok=False)
            except FileExistsError as collision:
                raise FileExistsError(f"refusing to overwrite pre-existing submission output: {path}") from collision
            claimed.append(path)
        with tarfile.open(out_tar, "w:gz") as tar:
            for entry in sorted(workspace.iterdir()):
                if entry.name in EXCLUDED_FROM_SUBMISSION:
                    continue
                tar.add(entry, arcname=entry.name)
            members = tar.getnames()
        # The tarball itself is too large to keep, so the graded contents are unprovable after
        # the job ends: whether the cell submitted anything, and whether a lane's own phase
        # artifacts leaked into what was scored. List what went in, next to what came out.
        # ponytail: one name per line, so a workspace filename containing a newline reads as two
        # entries. Nothing decides anything from this file - two tests split() it and CI uploads
        # it as evidence - so it stays plainly readable rather than escaped. Escape it when
        # something starts parsing it.
        manifest.write_text("".join(f"{name}\n" for name in members), encoding="utf-8")
    except BaseException:
        # Only ever the paths this call created, and only because it created them: a
        # half-written submission reads as a complete one.
        for path in claimed:
            path.unlink(missing_ok=True)
        raise


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
        cleanroom: bool = False,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._template = agent_command_template or _default_command_template(self._repo_root)
        self._timeout = timeout_seconds
        self._env = dict(env) if env is not None else None
        self._runner = runner or _subprocess_runner
        self._strategy = strategy or SliceContextStrategy()
        self._cleanroom = cleanroom

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

    def _probe_sandbox(
        self, workspace: Path, environment: Mapping[str, str], sessions_dir: Path, receipt: CleanroomReceipt
    ) -> None:
        """Prove the containers the model's tools would get, before spending the budget.

        The preflight proves the image, and the witness proves whichever containers a
        session happens to create - but the containers that serve terminal, file and
        code_execution are made by the model's first tool call, and a cell that times out
        before that one leaves the question open. This asks it directly, with no model in
        the loop: one terminal call on each of the two backends an agent turn uses, through
        the same composed config and the same sandbox image the session will get.

        Refusing here costs two container starts. Refusing after the session costs the
        budget, which is how run 36485906813 spent 300 seconds to discover a tmpfs.
        """
        witness = SandboxWitness(sessions_dir, workspace, receipt.image, receipt.image_id, stem=SANDBOX_PROBE_STEM)
        script = shlex.quote(str(self._repo_root / SUPERVISOR_SCRIPT))
        command = f"{script} --repo-root {shlex.quote(str(workspace))} --sandbox-probe"
        with witness.watching():
            returncode = self._runner(command, workspace, environment, SANDBOX_PROBE_TIMEOUT_SECONDS)
        observation = witness.receipt
        failures = list(witness.violations)
        if returncode != 0:
            failures.append(f"the sandbox probe exited {returncode}; see {SANDBOX_PROBE_STEM}.log")
        for backend, verified in (
            ("default", observation["default_backend_verified"]),
            ("session-scoped", observation["session_backend_verified"]),
        ):
            if not verified:
                failures.append(
                    f"no sound {backend} container was observed, so the backend the model's tools "
                    f"would have used is unproved (task ids seen: {_observed_task_ids(observation)})"
                )
        # `docker_persist_across_processes: false` read off the daemon rather than off the
        # config file: a container the daemon still lists is one the next process attaches
        # to by label, carrying this cell's mounts into the next.
        outlived = [record["container_id"] for record in observation["containers"] if not record["removed_after_exit"]]
        if outlived:
            failures.append(f"{len(outlived)} probe container(s) outlived the process that made them: {outlived}")
        if failures:
            raise CleanroomError("the agent's sandbox was not the cleanroom it was given:\n  - " + "\n  - ".join(failures))

    def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
        out_tar = Path(out_tar)

        workspace = Path(tempfile.mkdtemp(prefix=f"pb-{task.instance_id}-"))
        if not self._cleanroom:
            # run_agent.sh refuses a non-git repo root; the workspace is the agent's repo.
            # A cleanroom cell gets its worktree from the image instead - see below.
            subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
        control_dir = Path(tempfile.mkdtemp(prefix=f"pb-control-{task.instance_id}-"))

        environment = self._run_environment()
        # The inference environment, before the first agent turn and proved rather than
        # configured: the reference ``./executable`` and the image's bundled documentation
        # land in the workspace, and the image the tools run inside is named to run_agent.sh.
        # A cleanroom that fails its own preflight raises - an agent that can reach the
        # upstream source is not attempting this benchmark, whatever it scores.
        #
        # The workspace is the image's ``/workspace`` and nothing else: it must be empty
        # when the copy starts, and the Git worktree the harness needs is the one-commit
        # repository the image already ships. Creating either here would make the cell's
        # environment partly the harness's invention rather than ProgramBench's.
        # Beside the submission, never inside it: one usage + controls report per agent
        # session, so a result says which model answered and what it was allowed to do.
        # Absolute, because the agent runs with the workspace as its working directory and
        # --run-dir is usually relative: a relative path here wrote the reports into the
        # workspace, which archived them into the submission and left run.json with none.
        sessions_dir = (out_tar.parent / AGENT_SESSIONS_DIR).resolve()
        environment["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str(sessions_dir)

        receipt = None
        if self._cleanroom:
            receipt = prepare_cleanroom(task.instance_id, workspace, task.repository)
            environment[SANDBOX_IMAGE_ENV] = receipt.image
            out_tar.parent.mkdir(parents=True, exist_ok=True)
            (out_tar.parent / CLEANROOM_RECEIPT_FILENAME).write_text(
                json.dumps(receipt.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
            self._probe_sandbox(workspace, environment, sessions_dir, receipt)

        # The preflight proves the image; this proves the container the agent's tools were
        # actually given, from outside, while the session still holds it. Reuse of a
        # container from an earlier process is the failure it exists for: nothing in the
        # receipt, the config or the session's own output would show it.
        runner = self._runner
        witness = None
        if self._cleanroom and receipt is not None:
            witness = SandboxWitness(sessions_dir, workspace, receipt.image, receipt.image_id)
            runner = witness.wrap(self._runner)

        strategy_result = self._strategy.execute(
            ExecutionContext(
                repo_root=self._repo_root,
                task=task,
                workspace=workspace,
                control_dir=control_dir,
                command_template=self._template,
                env=environment,
                timeout_seconds=self._timeout,
                runner=runner,
            )
        )
        if witness is not None and witness.violations:
            # Same refusal as a failed preflight, for the same reason: what ran was not a
            # ProgramBench cleanroom, so its score is not a ProgramBench result. The
            # witness receipts are already on disk beside the submission path.
            raise CleanroomError(
                "the agent's sandbox was not the cleanroom it was given:\n  - " + "\n  - ".join(witness.violations)
            )

        _save_phase_artifacts(workspace, out_tar.parent)
        _archive_workspace(workspace, out_tar)
        return SubmissionResult(
            instance_id=task.instance_id,
            tar_path=out_tar,
            workspace=workspace,
            returncode=strategy_result.returncode,
            strategy=strategy_result,
            cleanroom=receipt,
        )
