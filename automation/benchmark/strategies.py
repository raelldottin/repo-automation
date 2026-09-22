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
from .instances import TaskSpec
from .jspace import JSpaceArtifact, JSpaceUnavailable

# Phase artifacts live here, inside the workspace so the agent sandbox can write them,
# and are stripped from the graded submission.
RPI_DIR = ".rpi"

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "returncode": self.returncode,
            "seconds": round(self.seconds, 3),
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
        }


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


def read_artifact(workspace: Path, filename: str) -> tuple[Optional[Any], str]:
    """Return ``(parsed_or_None, raw_text)`` for a phase artifact the agent should have written."""
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

- Instance: `{instance_id}`
- Program: `{repository}` (language: {language})
- Workspace root: the current working directory. Only files here are graded.
- A working `compile.sh` at the workspace root that builds `./executable` is mandatory.

This objective is fixed. Do not reinterpret, narrow, or soften it in any later phase.
"""

_ARTIFACT_INSTRUCTION = """## Required output artifact

Write your result as JSON to `{RPI_DIR}/{filename}` (create the directory if needed).
It must be a single JSON object with exactly these keys:

```json
{template}
```

Write the file before you finish. Nothing else you produce in this phase is read by the
next one.
"""

_RESEARCH_BODY = """## Phase: Research

Gather the evidence needed to rebuild this program correctly. Do **not** implement it.

Establish the program's observable contract: its interface, arguments/flags, input and
output behaviour, edge cases, and anything its black-box tests would plausibly exercise.
Inspect whatever material is present in the workspace. Record what you do not know rather
than guessing.
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

Write the compacted JSON to `{RPI_DIR}/{out_filename}`, keeping the same top-level keys as
the input. Do not add commentary.

## Artifact to compact

```json
{artifact}
```
"""


def envelope(task: TaskSpec) -> str:
    return _ENVELOPE.format(
        objective=task.objective,
        instance_id=task.instance_id,
        repository=task.repository,
        language=task.language,
    )


def artifact_instruction(spec: ArtifactSpec, filename: Optional[str] = None) -> str:
    return _ARTIFACT_INSTRUCTION.format(
        RPI_DIR=RPI_DIR,
        filename=filename or spec.filename,
        template=json.dumps(spec.template(), indent=2),
    )


def compose_prompt(*sections: str, jspace: Optional[JSpaceArtifact] = None) -> str:
    """Assemble one phase prompt; ``jspace`` is the canonical skill, never a paraphrase."""
    blocks = [section.strip() for section in sections if section and section.strip()]
    if jspace is not None:
        blocks.insert(1, jspace.prompt_block())
    return "\n\n".join(blocks) + "\n"


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
        slice_id=f"{ctx.task.instance_id}:{phase}",
    )

    budget = max(remaining_seconds, 1)
    started = time.monotonic()
    returncode = ctx.runner(command, ctx.workspace, ctx.env, budget)
    elapsed = time.monotonic() - started
    return PhaseResult(phase=phase, returncode=returncode, seconds=elapsed, budget_seconds=budget, prompt_chars=len(prompt_text))


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
            f"# Rebuild {ctx.task.repository}",
            envelope(ctx.task),
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
    """Lane B: the shipped harness behaviour, unchanged.

    This is the control for the existing harness, so it renders exactly what
    ``SupervisorAgentAdapter`` rendered before lanes existed: one schema-valid slice, the
    normal bounded context bundle, and the real ``base.md`` + ``slice.md`` prompt.
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
        """Run a compaction session; fall back to the raw artifact if it fails."""
        raw_json = json.dumps(data, indent=2, ensure_ascii=False)
        prompt = compose_prompt(
            f"# Compact the {label} artifact",
            envelope(ctx.task),
            _COMPACT_BODY.format(
                label=label,
                budget=COMPACT_BUDGET_CHARS,
                RPI_DIR=RPI_DIR,
                out_filename=out_filename,
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

    def execute(self, ctx: ExecutionContext) -> StrategyResult:
        run = _BudgetedRun(ctx, ceilings=PHASE_CEILING_SECONDS)
        compaction_stats: dict[str, Any] = {}

        research_prompt = compose_prompt(
            f"# Research: {ctx.task.repository}",
            envelope(ctx.task),
            _RESEARCH_BODY,
            artifact_instruction(RESEARCH_SPEC),
            jspace=self.jspace,
        )
        research_result = run.phase("research", research_prompt, {"objective": ctx.task.objective})
        research = self._validate(research_result, RESEARCH_SPEC, ctx.workspace, RESEARCH_ARTIFACT)
        if research is None:
            research = {"task": ctx.task.objective, "findings": [], "unknowns": ["research phase produced no artifact"]}

        if self.compaction and not run.exhausted:
            research, stats = self._compact(run, ctx, "research", RESEARCH_SPEC, research, RESEARCH_COMPACT_ARTIFACT)
            compaction_stats.update(stats)

        if not run.exhausted:
            plan_prompt = compose_prompt(
                f"# Plan: {ctx.task.repository}",
                envelope(ctx.task),
                _PLAN_BODY.format(research=json.dumps(research, indent=2, ensure_ascii=False)),
                artifact_instruction(PLAN_SPEC),
                jspace=self.jspace,
            )
            plan_result = run.phase("plan", plan_prompt, {"research": research})
            plan = self._validate(plan_result, PLAN_SPEC, ctx.workspace, PLAN_ARTIFACT)
            if plan is None:
                plan = {"goal": ctx.task.objective, "implementation_steps": [], "risks": ["plan phase produced no artifact"]}

            if self.compaction and not run.exhausted:
                plan, stats = self._compact(run, ctx, "plan", PLAN_SPEC, plan, PLAN_COMPACT_ARTIFACT)
                compaction_stats.update(stats)
        else:
            plan = {"goal": ctx.task.objective, "implementation_steps": [], "risks": ["budget exhausted before planning"]}

        implement_prompt = compose_prompt(
            f"# Implement: {ctx.task.repository}",
            envelope(ctx.task),
            _IMPLEMENT_BODY.format(plan=json.dumps(plan, indent=2, ensure_ascii=False)),
            jspace=self.jspace,
        )
        implement_result = run.phase("implement", implement_prompt, {"plan": plan})
        return StrategyResult(
            lane=self.name,
            returncode=implement_result.returncode,
            phases=tuple(run.phases),
            compaction=compaction_stats,
            jspace=self.jspace.provenance() if self.jspace is not None else None,
            phase_budgets=dict(run.budgets),
        )


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
    "ALL_LANES",
    "LANE_CEILING_SECONDS",
    "PHASE_CEILING_SECONDS",
    "ArtifactSpec",
    "ExecutionContext",
    "ExecutionStrategy",
    "JSpaceArtifact",
    "JSpaceUnavailable",
    "OneSessionStrategy",
    "PLAN_SPEC",
    "PhaseResult",
    "RESEARCH_SPEC",
    "RPI_DIR",
    "RpiStrategy",
    "SliceContextStrategy",
    "StrategyResult",
    "build_strategy",
    "envelope",
    "read_artifact",
]
