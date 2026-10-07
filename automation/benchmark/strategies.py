"""Execution strategies (lanes) for the effectiveness experiment.

Every lane solves the *same* ProgramBench task, in the same workspace, with the same
agent command, tool authority, and total time budget. Lanes differ only in how context
and workflow are treated:

* ``A`` one-session      - raw objective, no harness context at all.
* ``B`` slice-context    - the shipped harness behaviour (``base.md`` + ``slice.md``).
* ``C`` rpi              - fresh Research -> Plan -> Implement sessions, typed artifacts.
* ``D`` rpi+compaction   - C, with each artifact intentionally compacted before the next phase.
* ``E`` rpi+compaction+j - D, with the canonical J-Space skill administered each phase.

That containment is the whole point: ``E - D`` is the marginal value of J-Space, ``D - C``
of compaction, ``C - B`` of RPI, ``B - A`` of the harness's bounded context. Anything that
differs between lanes other than context/workflow treatment invalidates the decomposition,
so the agent command, environment, workspace and budget all live in ``ExecutionContext``
and are handed to every lane unchanged.

Phase artifacts are written under ``<workspace>/.rpi/`` and excluded from the submission
archive, so what ProgramBench grades is identical in kind across lanes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from automation.supervisor import run_next

from . import jspace as jspace_module
from .cleanroom import WORKSPACE_DIR
from .instances import TaskSpec
from .jspace import JSpaceArtifact, JSpaceUnavailable

# Phase artifacts live here, inside the workspace so the agent sandbox can write them,
# and are stripped from the graded submission. Host-side; what the *agent* is told to write
# is ``artifact_path`` below, which is not always the same string.
RPI_DIR = ".rpi"

# What timeout(1) reports, so the code means the same thing here as it does in a shell.
AGENT_TIMEOUT_RETURNCODE = 124

# Strict output budget for intentional compaction. Compaction that cannot lose
# information is not compaction, so lane D must be able to fail to preserve something.
COMPACT_BUDGET_CHARS = 4000

# The lane ceiling these phase budgets are written against; a different instance budget
# scales them proportionally, so the profile below always describes a whole lane.
LANE_CEILING_SECONDS = 3300
# Fixed ceilings, not one fungible remainder. A single "remaining" counter let the first
# phase eat the lane: in run 35650966066, lane D spent 1799 of 1800 seconds researching
# and implement got one, so no multi-phase lane submitted anything. Ceilings also keep the
# decomposition honest - C never reclaims the compaction slots D and E spend, so D - C is
# the cost of compaction rather than compaction plus whatever C did with the spare time.
#
# Sized from run 35715428932, where the previous profile censored the treatments it was
# meant to measure: research hit its 420s ceiling in all three multi-phase lanes, implement
# hit its 1260s ceiling in two of three, and lane E was killed before it wrote compile.sh.
# The phases that finished topped out near 229s (plan) and 202s (compaction), so those keep
# a 300s ceiling with headroom; research and implement get ~43% more than the ceilings they
# repeatedly hit. A phase kill stays a legitimate outcome - the point is that it should
# report a treatment that ran out of road, not a budget that was never wide enough.
PHASE_CEILING_SECONDS: dict[str, int] = {
    "research": 600,
    "research_compact": 300,
    "plan": 300,
    "plan_compact": 300,
    "implement": 1800,
}

RESEARCH_ARTIFACT = "research.json"
PLAN_ARTIFACT = "plan.json"
RESEARCH_COMPACT_ARTIFACT = "research.compact.json"
PLAN_COMPACT_ARTIFACT = "plan.compact.json"

# How a phase ended, with administration held apart from validity. Being killed at a
# ceiling is administrative: the treatment ran and ran out of road, which is a legitimate
# outcome the benchmark has to be able to report. A required artifact that never arrived
# is not: the next phase was never handed what this one owed it, so whatever ran after it
# was not the treatment. Run 35736569601 could not tell the two apart and reported six
# cells that never produced research.json as merely censored.
PHASE_COMPLETED = "completed"
PHASE_CENSORED = "censored"
PHASE_TREATMENT_INVALID = "treatment_invalid"
PHASE_FAILED = "failed"

TREATMENT_VALID = "valid"
TREATMENT_INVALID = "invalid"
REQUIRED_ARTIFACT_MISSING = "required_phase_artifact_missing"


def phase_state(returncode: Optional[int], artifact_valid: Optional[bool]) -> str:
    """Classify one phase from how it ended and what it left behind.

    ``artifact_valid`` is ``None`` where the phase owed no artifact (implement, and the
    single-session lanes) and ``False`` only where one was required and did not arrive.
    An invalid artifact outranks the return code: a phase that exits 0 having written
    nothing has still not administered its treatment.
    """
    if artifact_valid is False:
        return PHASE_TREATMENT_INVALID
    if returncode == AGENT_TIMEOUT_RETURNCODE:
        return PHASE_CENSORED
    return PHASE_COMPLETED if not returncode else PHASE_FAILED


@dataclass
class PhaseResult:
    """One agent session inside a lane."""

    phase: str
    returncode: int
    seconds: float
    prompt_chars: int
    budget_seconds: int = 0
    artifact_chars: int = 0
    artifact_valid: Optional[bool] = None
    artifact_errors: tuple[str, ...] = ()
    # Time after the budget kill, spent flushing and shutting down rather than working.
    # Kept out of ``seconds`` so the phase is still charged exactly what it was granted.
    grace_seconds: float = 0.0

    @property
    def state(self) -> str:
        return phase_state(self.returncode, self.artifact_valid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "state": self.state,
            "returncode": self.returncode,
            "seconds": round(self.seconds, 3),
            "grace_seconds": round(self.grace_seconds, 3),
            "budget_seconds": self.budget_seconds,
            "prompt_chars": self.prompt_chars,
            "artifact_chars": self.artifact_chars,
            "artifact_valid": self.artifact_valid,
            "artifact_errors": list(self.artifact_errors),
        }


@dataclass
class StrategyResult:
    """What a lane did, beyond the submission itself."""

    lane: str
    returncode: int
    phases: tuple[PhaseResult, ...] = ()
    compaction: dict[str, Any] = field(default_factory=dict)
    jspace: Optional[dict[str, Any]] = None
    # The ceilings this lane ran under. Part of the treatment, not an implementation
    # detail: change them and D - C stops meaning what it meant in the previous run.
    phase_budgets: dict[str, int] = field(default_factory=dict)
    # Set where a lane stopped because a required handoff never arrived. Left unset, the
    # verdict is read off the phases themselves.
    treatment_invalid_reason: Optional[str] = None
    treatment_invalid_phase: Optional[str] = None

    @property
    def treatment_validity(self) -> str:
        """Did this lane administer its treatment, whatever its score says?

        A cell that never produced a required artifact is not a lane that did badly; it is
        a lane that did not run. Its score stays as a diagnostic and leaves the deltas.
        """
        invalid = self.treatment_invalid_reason or any(phase.state == PHASE_TREATMENT_INVALID for phase in self.phases)
        return TREATMENT_INVALID if invalid else TREATMENT_VALID

    @property
    def agent_invocations(self) -> int:
        return len(self.phases)

    @property
    def seconds(self) -> float:
        return sum(phase.seconds for phase in self.phases)

    @property
    def phase_failures(self) -> int:
        return sum(1 for phase in self.phases if phase.returncode != 0 or phase.artifact_valid is False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "returncode": self.returncode,
            "agent_invocations": self.agent_invocations,
            "seconds": round(self.seconds, 3),
            "phase_failures": self.phase_failures,
            "phases": [phase.to_dict() for phase in self.phases],
            "compaction": self.compaction,
            "jspace": self.jspace,
            "phase_budgets": self.phase_budgets,
            "treatment_validity": self.treatment_validity,
            "treatment_invalid_reason": self._invalid_reason(),
            "treatment_invalid_phase": self._invalid_phase(),
        }

    def _first_invalid_phase(self) -> Optional[PhaseResult]:
        return next((phase for phase in self.phases if phase.state == PHASE_TREATMENT_INVALID), None)

    def _invalid_reason(self) -> Optional[str]:
        if self.treatment_invalid_reason:
            return self.treatment_invalid_reason
        return REQUIRED_ARTIFACT_MISSING if self._first_invalid_phase() else None

    def _invalid_phase(self) -> Optional[str]:
        if self.treatment_invalid_reason:
            return self.treatment_invalid_phase
        phase = self._first_invalid_phase()
        return phase.phase if phase else None


@dataclass
class ExecutionContext:
    """Everything a lane is allowed to vary is *not* in here; everything fixed is.

    ``timeout_seconds`` is the budget for the whole instance, not per session: a
    three-session lane must not get three times the wall clock of a one-session lane.
    """

    repo_root: Path
    task: TaskSpec
    workspace: Path
    control_dir: Path
    command_template: str
    env: Mapping[str, str]
    timeout_seconds: int
    runner: Any  # CommandRunner; typed in adapter.py, kept loose to avoid a cycle.
    # The agent's own view of the workspace root, which is not always ``workspace``: under
    # the cleanroom the model's tools live in a container that binds ``workspace`` at
    # /workspace. Empty means local execution, where the agent stands in the workspace
    # itself and nothing needs translating. Set from ``agent_workspace_for``.
    agent_workspace: str = ""


class ExecutionStrategy(Protocol):
    name: str

    def execute(self, ctx: ExecutionContext) -> StrategyResult: ...


# --------------------------------------------------------------------------------------
# Typed phase artifacts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactSpec:
    """Required keys of a phase artifact, and their expected container type."""

    filename: str
    list_keys: tuple[str, ...]
    text_keys: tuple[str, ...]

    def template(self) -> dict[str, Any]:
        template: dict[str, Any] = {key: "..." for key in self.text_keys}
        template.update({key: [] for key in self.list_keys})
        return template

    def validate(self, data: Any) -> list[str]:
        if not isinstance(data, dict):
            return ["artifact is not a JSON object"]
        errors = []
        for key in self.text_keys:
            if not isinstance(data.get(key), str) or not data.get(key):
                errors.append(f"missing or empty text field: {key}")
        for key in self.list_keys:
            if not isinstance(data.get(key), list):
                errors.append(f"missing or non-list field: {key}")
        return errors


RESEARCH_SPEC = ArtifactSpec(
    filename=RESEARCH_ARTIFACT,
    text_keys=("task",),
    list_keys=("relevant_files", "findings", "constraints", "unknowns", "risks", "evidence"),
)

PLAN_SPEC = ArtifactSpec(
    filename=PLAN_ARTIFACT,
    text_keys=("goal",),
    list_keys=("implementation_steps", "files_expected", "validation_plan", "risks", "research_refs"),
)


def agent_workspace_for(cleanroom: bool) -> str:
    """The workspace root as the *agent's* tools resolve it, or "" when that is just its cwd.

    One function so the diagnostic probe and a real cell cannot disagree about it: a probe
    that resolves paths differently from lane C is measuring a different treatment.
    """
    return WORKSPACE_DIR if cleanroom else ""


def artifact_path(filename: str, agent_workspace: str = "") -> str:
    """Where to tell the agent to write ``filename``, in the agent's own terms.

    Absolute under a sandbox, because a relative path there has two answers. Hermes remaps
    the terminal tool's cwd to the container's /workspace, while the file tool resolves
    relative paths against ``TERMINAL_CWD`` - the *host* workspace path, which inside the
    container is not the mount but an empty directory in the container's own layer. Run
    37440878348 wrote eight schema-valid checkpoints to ``.rpi/research.json``, every one
    reported ``verified`` with a growing byte count, and the host workspace never saw one of
    them: the probe collected nothing and the phase read as artifact-absent. The bytes were
    never lost by the model, only addressed to a directory that dies with the container.
    """
    directory = f"{agent_workspace.rstrip('/')}/{RPI_DIR}" if agent_workspace else RPI_DIR
    return f"{directory}/{filename}"


def read_artifact(workspace: Path, filename: str) -> tuple[Optional[Any], str]:
    """Return ``(parsed_or_None, raw_text)`` for a phase artifact the agent should have written.

    Host-side, and deliberately so: the sandbox binds this directory at /workspace, so
    ``artifact_path``'s absolute string and this path are two views of the same bytes, and
    collection stays outside the container that is about to be removed.
    """
    path = Path(workspace) / RPI_DIR / filename
    if not path.is_file():
        return None, ""
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        return None, raw


# --------------------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------------------

_ENVELOPE = """## Objective (immutable)

{objective}

- Task: `{public_id}`
- {workspace_root}
- A working `compile.sh` at the workspace root that builds `./executable` is mandatory.

This objective is fixed. Do not reinterpret, narrow, or soften it in any later phase.
"""

_ARTIFACT_INSTRUCTION = """## Required output artifact

Write your result as JSON to `{path}` (create the directory if needed).
It must be a single JSON object with exactly these keys:

```json
{template}
```

Create `{path}` immediately, as a schema-valid initial checkpoint: every key
present, lists empty where you have nothing yet. Update it as you work, and keep every
update schema-valid. The artifact is this phase's durable state; your chat output is not,
and nothing else you produce here is read by the next phase.

This session can be stopped at any moment without warning. Whatever is in the file at that
moment is what this phase produced, so never hold results back for one final write.
"""

_RESEARCH_BODY = """## Phase: Research

Gather the evidence needed to rebuild this program correctly. Do **not** implement it.

Establish the program's observable contract: its interface, arguments/flags, input and
output behaviour, edge cases, and anything its black-box tests would plausibly exercise.
Inspect whatever material is present in the workspace. Record what you do not know rather
than guessing.
"""

# Run 37420620542 wrote research.json once, 116s in, then made 21 more tool calls without
# writing again: the flag surface, exit codes and terminfo dependency it discovered died with
# the transcript. "Update it as you work" was already in the artifact instruction, so what was
# missing is not the intent but the cadence - a bound on how far exploration may run ahead of
# the durable record. Scoped to research deliberately: the same three-call bound in implement
# would interrupt every third edit to rewrite a summary.
_RESEARCH_CHECKPOINT = """## Checkpoint protocol

Create `{path}` as your first tool action, before any investigative command.
Treat the artifact as the durable research record, not a final report.

Whenever a tool result changes any finding, constraint, unknown, risk, or piece of evidence:

1. update `{path}` before issuing another investigative tool call;
2. keep the file schema-valid at every update.

You may batch closely related observations, but never make more than three investigative tool
calls without writing an updated artifact.

Before starting a command that may block, wait, invoke an interactive program, or consume
substantial time, checkpoint everything you have learned so far. If a command fails or times out
in a way that changes what you know, checkpoint that failure or uncertainty before trying
something else.

Your chat response does not count as a checkpoint. Information that exists only in the
conversation is lost at the end of this phase.
"""

_PLAN_BODY = """## Phase: Plan

Turn the research below into an implementation plan. Do **not** implement it.

State the files you expect to create, the order of work, and how each step will be
validated. If the research left an unknown that the plan must resolve, say how.

## Research

```json
{research}
```
"""

_IMPLEMENT_BODY = """## Phase: Implement

Execute the plan below. Build the program in the workspace, write `compile.sh`, and make
sure it produces `./executable`.

You are given the plan, not the research conversation. If the plan is wrong where it
touches the immutable objective, follow the objective.

## Plan

```json
{plan}
```
"""

_COMPACT_BODY = """## Phase: Compact ({label})

Compress the artifact below to at most {budget} characters of JSON, keeping only what the
next phase needs:

- the goal
- load-bearing findings
- where the evidence is
- constraints
- unresolved questions
- risks
- inputs the next phase requires

Drop everything else: narration, rejected hypotheses, restated context, detail the next
phase cannot act on. Losing information is the point; losing a constraint is not.

Write the compacted JSON to `{out_path}`, keeping the same top-level keys as
the input. Do not add commentary.

## Artifact to compact

```json
{artifact}
```
"""


_LOCAL_WORKSPACE_ROOT = "Workspace root: the current working directory. Only files here are graded."


def workspace_instruction(agent_workspace: str) -> str:
    """The environment fact every lane needs, in one place so no lane can be told less.

    Named rather than implied, for the same reason the artifact path is absolute: a phase
    artifact has a harness-chosen path to hand over, but `compile.sh` and the sources the
    implement phase writes do not, and a relative path for those lands in the container layer
    exactly as `.rpi/research.json` did. This is a fact about the ProgramBench sandbox rather
    than a treatment, so it reaches all five lanes identically: the envelope carries it to A
    and C/D/E, and ``SliceContextStrategy`` appends it to B's execution constraints, because
    B's prompt is the shipped renderer and never sees the envelope. If only the envelope
    lanes knew where to write, ``B - A`` would partly measure that knowledge.
    """
    if not agent_workspace:
        return ""
    return (
        f"Workspace root is `{agent_workspace}`. Write graded files by absolute path under it: "
        "the file tool and the shell do not resolve relative paths to the same place."
    )


def envelope(task: TaskSpec, agent_workspace: str = "") -> str:
    instruction = workspace_instruction(agent_workspace)
    root = f"{instruction} Only files here are graded." if instruction else _LOCAL_WORKSPACE_ROOT
    return _ENVELOPE.format(objective=task.objective, public_id=task.public_id, workspace_root=root)


def artifact_instruction(spec: ArtifactSpec, filename: Optional[str] = None, agent_workspace: str = "") -> str:
    return _ARTIFACT_INSTRUCTION.format(
        path=artifact_path(filename or spec.filename, agent_workspace),
        template=json.dumps(spec.template(), indent=2),
    )


def compose_prompt(*sections: str, jspace: Optional[JSpaceArtifact] = None) -> str:
    """Assemble one phase prompt; ``jspace`` is the canonical skill, never a paraphrase."""
    blocks = [section.strip() for section in sections if section and section.strip()]
    if jspace is not None:
        blocks.insert(1, jspace.prompt_block())
    return "\n\n".join(blocks) + "\n"


def research_prompt(task: TaskSpec, jspace: Optional[JSpaceArtifact] = None, agent_workspace: str = "") -> str:
    """The research phase's prompt, byte for byte.

    A diagnostic that asks what the research session did has to ask it the same question a
    lane does; composing a near-copy somewhere else would measure the near-copy. That now
    includes the artifact's path: the checkpoint protocol is handed the path the artifact
    instruction resolved, so the two sections cannot name different files.
    """
    path = artifact_path(RESEARCH_ARTIFACT, agent_workspace)
    return compose_prompt(
        f"# Research: {task.public_id}",
        envelope(task, agent_workspace),
        _RESEARCH_BODY,
        artifact_instruction(RESEARCH_SPEC, agent_workspace=agent_workspace),
        _RESEARCH_CHECKPOINT.format(path=path),
        jspace=jspace,
    )


# --------------------------------------------------------------------------------------
# Session plumbing shared by every lane
# --------------------------------------------------------------------------------------


def run_phase(
    ctx: ExecutionContext,
    phase: str,
    prompt_text: str,
    context_data: dict[str, Any],
    remaining_seconds: int,
) -> PhaseResult:
    """Invoke one agent session through the harness runner seam.

    Every lane routes through here, so tool availability, sandbox authority and the
    command template are identical across lanes by construction.
    """
    control = Path(ctx.control_dir) / phase
    control.mkdir(parents=True, exist_ok=True)
    prompt_path = control / "prompt.md"
    context_path = control / "context.json"
    handoff_path = control / "handoff.json"  # must not pre-exist for run_agent.sh

    prompt_path.write_text(prompt_text, encoding="utf-8")
    context_path.write_text(json.dumps(context_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    command = run_next.format_agent_command(
        command_template=ctx.command_template,
        repo_root=ctx.workspace,
        prompt_path=prompt_path,
        context_path=context_path,
        handoff_path=handoff_path,
        # The public id here too. run_agent.sh exports this as
        # REPO_AUTOMATION_SUPERVISOR_SLICE_ID and OWLORY_SUPERVISOR_SLICE_ID and writes it
        # to the controls receipt; the pinned Hermes path does not render either variable
        # into a prompt, but there is no reason to hand the worker process the real name
        # after removing it from everywhere the model can read.
        slice_id=f"{ctx.task.public_id}:{phase}",
    )

    budget = max(remaining_seconds, 1)
    started = time.monotonic()
    returncode = ctx.runner(command, ctx.workspace, ctx.env, budget)
    elapsed = time.monotonic() - started
    # A killed session is cut off at its budget; anything past that is the shutdown grace
    # the runner allows it to flush in, which the treatment did not get to spend.
    worked = min(elapsed, budget) if returncode == AGENT_TIMEOUT_RETURNCODE else elapsed
    return PhaseResult(
        phase=phase,
        returncode=returncode,
        seconds=worked,
        grace_seconds=elapsed - worked,
        budget_seconds=budget,
        prompt_chars=len(prompt_text),
    )


class _BudgetedRun:
    """Spend one instance-wide time budget across however many sessions a lane uses.

    A lane with per-phase ceilings spends each phase's own allowance, so an overrunning
    research phase is cut off at its ceiling instead of taking implement's time with it.
    A lane without them (A, B) is one session and gets the whole instance budget.
    """

    def __init__(self, ctx: ExecutionContext, ceilings: Optional[Mapping[str, int]] = None) -> None:
        self._ctx = ctx
        self._started = time.monotonic()
        self.phases: list[PhaseResult] = []
        self.budgets = _scaled_ceilings(ceilings, ctx.timeout_seconds) if ceilings else {}

    @property
    def remaining(self) -> int:
        spent = time.monotonic() - self._started
        return int(self._ctx.timeout_seconds - spent)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def phase(self, name: str, prompt_text: str, context_data: dict[str, Any]) -> PhaseResult:
        # The instance budget stays the backstop: ceilings sum to it, but a lane must not
        # outlive it if a phase overruns its own kill.
        allowed = min(self.budgets.get(name, self.remaining), self.remaining)
        result = run_phase(self._ctx, name, prompt_text, context_data, allowed)
        self.phases.append(result)
        return result


def _scaled_ceilings(ceilings: Mapping[str, int], timeout_seconds: int) -> dict[str, int]:
    """Hold the profile's shape when the instance budget is not the ceiling it was written for."""
    scale = timeout_seconds / LANE_CEILING_SECONDS
    # Floor, not round: the ceilings sum to the whole lane, so rounding each one up can hand
    # out a second more than the instance budget allows.
    return {phase: max(int(seconds * scale), 1) for phase, seconds in ceilings.items()}


# --------------------------------------------------------------------------------------
# Lanes
# --------------------------------------------------------------------------------------


class OneSessionStrategy:
    """Lane A: one agent, the raw objective, no harness context."""

    name = "A"

    def execute(self, ctx: ExecutionContext) -> StrategyResult:
        prompt = compose_prompt(
            f"# Rebuild {ctx.task.public_id}",
            envelope(ctx.task, ctx.agent_workspace),
            "## Phase: Implement\n\nBuild the program now. Finish with a working `compile.sh`.",
        )
        run = _BudgetedRun(ctx)
        result = run.phase("implement", prompt, {"objective": ctx.task.objective})
        return StrategyResult(
            lane=self.name,
            returncode=result.returncode,
            phases=tuple(run.phases),
            phase_budgets={"implement": ctx.timeout_seconds},
        )


class SliceContextStrategy:
    """Lane B: the shipped bounded-context rendering, plus the environment contract every lane gets.

    This is the control for the existing harness, so its context is exactly what
    ``SupervisorAgentAdapter`` rendered before lanes existed: one schema-valid slice, the
    normal bounded context bundle, and the real ``base.md`` + ``slice.md`` prompt. Under a
    sandbox, ``workspace_instruction`` follows that rendering once - the same sentence, the
    same number of times, that A and C/D/E receive through the envelope.
    """

    name = "B"

    def execute(self, ctx: ExecutionContext) -> StrategyResult:
        from .adapter import build_context_bundle, build_queue_data, build_slice_record

        slice_record = build_slice_record(ctx.task)
        queue_data = build_queue_data(slice_record, ctx.command_template, ctx.timeout_seconds)
        context_bundle = build_context_bundle(queue_data, slice_record)

        control = Path(ctx.control_dir) / "implement"
        control.mkdir(parents=True, exist_ok=True)
        prompt_text = run_next.render_prompt(
            repo_root=ctx.repo_root,
            slice_record=slice_record,
            context_bundle=context_bundle,
            handoff_path=control / "handoff.json",
        )
        # After the rendering, not inside the bundle. The renderer prints the bundle twice -
        # once as the "Execution constraints" list, once inside the compact context JSON - so a
        # sentence added there reached B's agent twice while every other lane got it once, and
        # the extra salience for a fact meant to be held constant is itself a difference
        # between lanes. Appended here, B's bundle stays byte-for-byte the shipped one and the
        # environment contract arrives exactly once. `base.md`/`slice.md` stay generic: this
        # is a ProgramBench sandbox fact, not harness policy.
        instruction = workspace_instruction(ctx.agent_workspace)
        if instruction:
            prompt_text = f"{prompt_text.rstrip()}\n\n{instruction}\n"
        run = _BudgetedRun(ctx)
        result = run.phase("implement", prompt_text, context_bundle)
        return StrategyResult(
            lane=self.name,
            returncode=result.returncode,
            phases=tuple(run.phases),
            phase_budgets={"implement": ctx.timeout_seconds},
        )


class RpiStrategy:
    """Lanes C/D/E: Research -> Plan -> Implement, optionally compacted, optionally J-Space.

    Each phase is a fresh session that receives the immutable envelope again plus the
    previous phase's *artifact* - never the previous phase's conversation. That is what
    makes this a test of RPI rather than one long session with section headings.
    """

    def __init__(
        self,
        compaction: bool = False,
        jspace: Optional[JSpaceArtifact] = None,
        name: Optional[str] = None,
    ) -> None:
        self.compaction = compaction
        self.jspace = jspace
        self.name = name or ("E" if jspace is not None else ("D" if compaction else "C"))

    def _validate(self, result: PhaseResult, spec: ArtifactSpec, workspace: Path, filename: str) -> Any:
        data, raw = read_artifact(workspace, filename)
        errors = ["artifact missing or unparseable"] if data is None else spec.validate(data)
        result.artifact_chars = len(raw)
        result.artifact_valid = not errors
        result.artifact_errors = tuple(errors)
        return data

    def _compact(
        self,
        run: _BudgetedRun,
        ctx: ExecutionContext,
        label: str,
        spec: ArtifactSpec,
        data: Any,
        out_filename: str,
    ) -> tuple[Any, dict[str, Any]]:
        """Run a compaction session; fall back to the raw artifact if it fails.

        The fallback keeps the lane running, but the failed phase still reports itself as
        treatment_invalid: a D cell whose compaction never landed is lane C under D's name.
        """
        raw_json = json.dumps(data, indent=2, ensure_ascii=False)
        prompt = compose_prompt(
            f"# Compact the {label} artifact",
            envelope(ctx.task, ctx.agent_workspace),
            _COMPACT_BODY.format(
                label=label,
                budget=COMPACT_BUDGET_CHARS,
                out_path=artifact_path(out_filename, ctx.agent_workspace),
                artifact=raw_json,
            ),
            jspace=self.jspace,
        )
        result = run.phase(f"{label}_compact", prompt, {"budget_chars": COMPACT_BUDGET_CHARS})
        compacted = self._validate(result, spec, ctx.workspace, out_filename)
        raw_chars = len(raw_json)
        compact_chars = result.artifact_chars
        stats = {
            f"{label}_raw_chars": raw_chars,
            f"{label}_compacted_chars": compact_chars,
            f"{label}_compression_ratio": round(compact_chars / raw_chars, 4) if raw_chars else None,
        }
        return (compacted if compacted is not None else data), stats

    def _result(self, run: _BudgetedRun, compaction_stats: dict[str, Any], **extra: Any) -> StrategyResult:
        return StrategyResult(
            lane=self.name,
            phases=tuple(run.phases),
            compaction=compaction_stats,
            jspace=self.jspace.provenance() if self.jspace is not None else None,
            phase_budgets=dict(run.budgets),
            **extra,
        )

    def _not_administered(
        self, run: _BudgetedRun, compaction_stats: dict[str, Any], phase: Optional[str] = None
    ) -> StrategyResult:
        """Stop the lane rather than invent the handoff the phase owed the next one.

        Every multi-phase cell of runs 35715428932 and 35736569601 lost research.json and
        carried on against a stub the harness wrote for itself, so what got scored was some
        other treatment wearing this lane's name. There is nothing to salvage here: an
        implement phase given a fabricated plan measures the fabrication.
        """
        last = run.phases[-1] if run.phases else None
        return self._result(
            run,
            compaction_stats,
            returncode=last.returncode if last is not None else AGENT_TIMEOUT_RETURNCODE,
            treatment_invalid_reason=REQUIRED_ARTIFACT_MISSING,
            treatment_invalid_phase=phase or (last.phase if last is not None else None),
        )

    def execute(self, ctx: ExecutionContext) -> StrategyResult:
        run = _BudgetedRun(ctx, ceilings=PHASE_CEILING_SECONDS)
        compaction_stats: dict[str, Any] = {}

        prompt = research_prompt(ctx.task, self.jspace, ctx.agent_workspace)
        research_result = run.phase("research", prompt, {"objective": ctx.task.objective})
        research = self._validate(research_result, RESEARCH_SPEC, ctx.workspace, RESEARCH_ARTIFACT)
        if not research_result.artifact_valid:
            return self._not_administered(run, compaction_stats)

        if self.compaction and not run.exhausted:
            research, stats = self._compact(run, ctx, "research", RESEARCH_SPEC, research, RESEARCH_COMPACT_ARTIFACT)
            compaction_stats.update(stats)

        if run.exhausted:
            return self._not_administered(run, compaction_stats, phase="plan")

        plan_prompt = compose_prompt(
            f"# Plan: {ctx.task.public_id}",
            envelope(ctx.task, ctx.agent_workspace),
            _PLAN_BODY.format(research=json.dumps(research, indent=2, ensure_ascii=False)),
            artifact_instruction(PLAN_SPEC, agent_workspace=ctx.agent_workspace),
            jspace=self.jspace,
        )
        plan_result = run.phase("plan", plan_prompt, {"research": research})
        plan = self._validate(plan_result, PLAN_SPEC, ctx.workspace, PLAN_ARTIFACT)
        if not plan_result.artifact_valid:
            return self._not_administered(run, compaction_stats)

        if self.compaction and not run.exhausted:
            plan, stats = self._compact(run, ctx, "plan", PLAN_SPEC, plan, PLAN_COMPACT_ARTIFACT)
            compaction_stats.update(stats)

        implement_prompt = compose_prompt(
            f"# Implement: {ctx.task.public_id}",
            envelope(ctx.task, ctx.agent_workspace),
            _IMPLEMENT_BODY.format(plan=json.dumps(plan, indent=2, ensure_ascii=False)),
            jspace=self.jspace,
        )
        implement_result = run.phase("implement", implement_prompt, {"plan": plan})
        return self._result(run, compaction_stats, returncode=implement_result.returncode)


LANE_FACTORIES = {
    "A": OneSessionStrategy,
    "B": SliceContextStrategy,
    "C": lambda: RpiStrategy(compaction=False, jspace=None),
    "D": lambda: RpiStrategy(compaction=True, jspace=None),
    # Resolution happens here so an unresolvable J-Space stops lane E at construction,
    # before any budget is spent, and never degrades into running lane D under E's name.
    "E": lambda: RpiStrategy(compaction=True, jspace=jspace_module.resolve()),
}

ALL_LANES: tuple[str, ...] = ("A", "B", "C", "D", "E")


def build_strategy(lane: str) -> ExecutionStrategy:
    try:
        return LANE_FACTORIES[lane]()
    except KeyError:
        raise ValueError(f"Unknown lane {lane!r}; expected one of {', '.join(ALL_LANES)}") from None


__all__ = [
    "AGENT_TIMEOUT_RETURNCODE",
    "ALL_LANES",
    "LANE_CEILING_SECONDS",
    "PHASE_CEILING_SECONDS",
    "ArtifactSpec",
    "ExecutionContext",
    "ExecutionStrategy",
    "JSpaceArtifact",
    "JSpaceUnavailable",
    "OneSessionStrategy",
    "PHASE_CENSORED",
    "PHASE_COMPLETED",
    "PHASE_FAILED",
    "PHASE_TREATMENT_INVALID",
    "PLAN_SPEC",
    "PhaseResult",
    "RESEARCH_SPEC",
    "RPI_DIR",
    "REQUIRED_ARTIFACT_MISSING",
    "RpiStrategy",
    "SliceContextStrategy",
    "StrategyResult",
    "TREATMENT_INVALID",
    "TREATMENT_VALID",
    "agent_workspace_for",
    "artifact_path",
    "build_strategy",
    "envelope",
    "phase_state",
    "read_artifact",
    "workspace_instruction",
]
