"""Run the A-E effectiveness experiment and compare the lanes.

One instance is attempted once per (lane, repeat). Layout:

    <run-dir>/<lane>/r<repeat>/<instance>/submission.tar.gz   graded artefact
    <run-dir>/<lane>/r<repeat>/<instance>/run.json            provenance + process metrics
    <run-dir>/<lane>/r<repeat>/<instance>/rpi/                phase artifacts (C-E)
    <run-dir>/<lane>/r<repeat>/effectiveness-report.json      ProgramBench score for that cell
    <run-dir>/lane-comparison.json|md                         the comparison

Each ``<lane>/r<repeat>`` directory is exactly the shape ``programbench eval`` and
``score_run_dir`` already expect, so scoring is reused unchanged and the primary metric
stays ProgramBench correctness.

Lanes are interleaved per instance rather than run lane-by-lane: running all of A then
all of B would let provider load or time of day masquerade as a lane effect.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .adapter import AGENT_SESSIONS_DIR, AgentAdapter, SupervisorAgentAdapter
from .evalrunner import EvalRunner
from .instances import task_spec
from .jspace import JSpaceUnavailable
from .run import REPORT_FILENAME, score_and_write
from .scoring import EffectivenessReport
from .strategies import ALL_LANES, build_strategy

PROVENANCE_FILENAME = "run.json"
COMPARISON_FILENAME = "lane-comparison.json"
COMPARISON_MARKDOWN = "lane-comparison.md"

# Recorded so a result can be reproduced. Anything key-like is redacted, never stored.
PROVENANCE_ENV_KEYS = (
    "REPO_AUTOMATION_AGENT_RUNNER",
    "REPO_AUTOMATION_CLAUDE_PERMISSION_MODE",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_BASE_URL",
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_INFERENCE_MODEL",
    "HERMES_REVISION",
)
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")

# Whether a cell's outcome is comparable at all. A provider that refused to serve produces a
# low score for a reason that has nothing to do with the lane's treatment, and subtracting one
# such cell from another reports an outage as an effect: run 35701448042 rendered a rate limit
# as "D - C = -74.8% mean pass". Non-valid cells keep their score as a diagnostic and leave the
# deltas.
VALID = "valid"
PROVIDER_DEGRADED = "provider_degraded"
PROVIDER_UNAVAILABLE = "provider_unavailable"
# Worst-first: one refused cell decides the lane.
_VALIDITY_ORDER = (PROVIDER_UNAVAILABLE, PROVIDER_DEGRADED, VALID)

# Hermes' two terminal lines for a call it gave up on, from agent/turn_recovery.py: the first
# is the 429 path, the second the exhausted-transport path (5xx, connect/read timeouts). Both
# mean the provider declined to answer.
#
# Read from the session's own log, never inferred from a missing usage report: a session killed
# at its phase ceiling also writes no usage, and returncode 124 is an experiment outcome, not a
# provider failure. Only an explicit line counts.
_PROVIDER_FAILURE_MARKERS = (
    ("rate_limit", "Rate limited after "),
    ("unavailable", "API failed after "),
)
SESSION_LOG_SUFFIX = ".log"
_EVIDENCE_CHARS = 200

AdapterFactory = Callable[[str], AgentAdapter]


@dataclass(frozen=True)
class Cell:
    """One (lane, repeat, instance) attempt."""

    lane: str
    repeat: int
    instance_id: str

    def directory(self, run_dir: Path) -> Path:
        return Path(run_dir) / self.lane / f"r{self.repeat}" / self.instance_id


def interleave(instances: Sequence[str], lanes: Sequence[str], repeats: int) -> list[Cell]:
    """Deterministically rotate lane order per instance and repeat.

    Rotation rather than shuffling keeps the schedule reproducible from the arguments
    alone, while still preventing any lane from systematically occupying the same slot.
    """
    cells: list[Cell] = []
    for repeat in range(1, repeats + 1):
        for index, instance_id in enumerate(instances):
            offset = (index + repeat - 1) % len(lanes)
            rotated = list(lanes[offset:]) + list(lanes[:offset])
            cells.extend(Cell(lane, repeat, instance_id) for lane in rotated)
    return cells


def _repo_sha(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, OSError):  # pragma: no cover - non-git checkout.
        return "unknown"


def _environment_fingerprint() -> dict[str, str]:
    fingerprint = {}
    for key in PROVENANCE_ENV_KEYS:
        value = os.environ.get(key)
        if value is None:
            continue
        fingerprint[key] = "<redacted>" if any(marker in key.upper() for marker in _SECRET_MARKERS) else value
    return fingerprint


def default_adapter_factory(
    repo_root: Path,
    agent_command_template: Optional[str] = None,
    timeout_seconds: int = 1800,
) -> AdapterFactory:
    """Build one adapter per lane; everything except the strategy is held fixed."""

    def factory(lane: str) -> AgentAdapter:
        return SupervisorAgentAdapter(
            repo_root=repo_root,
            agent_command_template=agent_command_template,
            timeout_seconds=timeout_seconds,
            strategy=build_strategy(lane),
        )

    return factory


def skip_cell(cell: Cell, run_dir: Path, repo_root: Path, reason: str) -> dict[str, Any]:
    """Record a cell that could not be administered, without writing a submission.

    A lane whose treatment cannot be resolved is reported as unrun. Substituting a
    different treatment would silently rename one lane into another.
    """
    instance_dir = cell.directory(run_dir)
    instance_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    provenance: dict[str, Any] = {
        "lane": cell.lane,
        "instance_id": cell.instance_id,
        "repeat": cell.repeat,
        "repo_automation_sha": _repo_sha(repo_root),
        "environment": _environment_fingerprint(),
        "started_at": now,
        "finished_at": now,
        "returncode": None,
        "skipped": True,
        "skip_reason": reason,
        "strategy": None,
    }
    (instance_dir / PROVENANCE_FILENAME).write_text(json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return provenance


def _agent_sessions(instance_dir: Path) -> list[dict[str, Any]]:
    """What each agent session spent, and what it was allowed to do while spending it.

    Declared settings say what was asked for; the usage report says which model actually
    answered and how many calls it took. Both are kept, because they can disagree.

    Keyed on either file, not on the usage report: a session killed at its budget never
    writes one, and keying on usage alone deleted those sessions from the record. Lane D
    of run 35650966066 reported no agent sessions at all while having run two.
    """
    sessions_dir = instance_dir / AGENT_SESSIONS_DIR
    reports = {"usage": ".usage.json", "controls": ".controls.json"}
    stems = sorted({path.name[: -len(suffix)] for suffix in reports.values() for path in sessions_dir.glob(f"*{suffix}")})
    sessions: list[dict[str, Any]] = []
    for stem in stems:
        session: dict[str, Any] = {"session": stem}
        for key, suffix in reports.items():
            try:
                session[key] = json.loads((sessions_dir / f"{stem}{suffix}").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                session[key] = None
        session["provider_failure"] = _provider_failure(sessions_dir / f"{stem}{SESSION_LOG_SUFFIX}")
        sessions.append(session)
    return sessions


def _provider_failure(log_path: Path) -> Optional[dict[str, str]]:
    """The line where the provider refused this session, or None.

    The log is kept on the runner rather than uploaded, so carry the sentence itself: a
    classification nobody can check against its evidence is just an assertion.
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for kind, marker in _PROVIDER_FAILURE_MARKERS:
        index = text.find(marker)
        if index >= 0:
            return {"kind": kind, "evidence": text[index:].splitlines()[0][:_EVIDENCE_CHARS]}
    return None


def classify_provider_validity(sessions: Sequence[dict[str, Any]]) -> str:
    """Is this cell's outcome a treatment result, or a provider one?

    ``valid``                every session was either served or ended for an experiment-local
                             reason (its phase ceiling, a failed build, a bad submission).
    ``provider_degraded``    some session was served, another was refused - the lane ran on
                             part of its treatment.
    ``provider_unavailable`` nothing was ever served and the provider is on record refusing.
    """
    if not any(session.get("provider_failure") for session in sessions):
        return VALID
    served = any((session.get("usage") or {}).get("model") for session in sessions)
    return PROVIDER_DEGRADED if served else PROVIDER_UNAVAILABLE


def run_cell(cell: Cell, run_dir: Path, adapter: AgentAdapter, repo_root: Path) -> dict[str, Any]:
    """Attempt one cell and write its provenance record."""
    instance_dir = cell.directory(run_dir)
    instance_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()

    result = adapter.produce_submission(task_spec(cell.instance_id), instance_dir / "submission.tar.gz")

    sessions = _agent_sessions(instance_dir)
    provenance: dict[str, Any] = {
        "lane": cell.lane,
        "instance_id": cell.instance_id,
        "repeat": cell.repeat,
        "repo_automation_sha": _repo_sha(repo_root),
        "environment": _environment_fingerprint(),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "returncode": result.returncode,
        "agent_sessions": sessions,
        "provider_validity": classify_provider_validity(sessions),
        "strategy": result.strategy.to_dict() if result.strategy is not None else None,
    }
    (instance_dir / PROVENANCE_FILENAME).write_text(json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return provenance


def cell_dirs(run_dir: Path, lanes: Sequence[str], repeats: int) -> list[Path]:
    """Every ``<lane>/r<repeat>`` directory that exists under ``run_dir``."""
    dirs = []
    for lane in lanes:
        for repeat in range(1, repeats + 1):
            candidate = Path(run_dir) / lane / f"r{repeat}"
            if candidate.is_dir():
                dirs.append(candidate)
    return dirs


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def _load_provenance(cell_dir: Path) -> list[dict[str, Any]]:
    records = []
    for instance_dir in sorted(p for p in cell_dir.iterdir() if p.is_dir()):
        record = instance_dir / PROVENANCE_FILENAME
        if record.is_file():
            records.append(json.loads(record.read_text(encoding="utf-8")))
    return records


def summarize_lane(run_dir: Path, lane: str, repeats: int, reports: Sequence[EffectivenessReport]) -> dict[str, Any]:
    """Aggregate a lane's repeats: primary outcome, efficiency, process, stability."""
    resolve_rates = [report.resolve_rate for report in reports]
    near_rates = [report.near_resolve_rate for report in reports]
    pass_fractions = [report.mean_pass_fraction for report in reports]

    provenance: list[dict[str, Any]] = []
    for repeat in range(1, repeats + 1):
        cell_dir = Path(run_dir) / lane / f"r{repeat}"
        if cell_dir.is_dir():
            provenance.extend(_load_provenance(cell_dir))

    strategies = [record["strategy"] for record in provenance if record.get("strategy")]
    skipped = [record for record in provenance if record.get("skipped")]
    # A lane whose treatment never resolved has no outcome to compare; carry the count so a
    # skipped lane cannot be read as a treatment that simply performed badly.
    jspace = next((strategy["jspace"] for strategy in strategies if strategy.get("jspace")), None)
    compaction_ratios = [
        value
        for strategy in strategies
        for key, value in (strategy.get("compaction") or {}).items()
        if key.endswith("_compression_ratio") and value is not None
    ]

    return {
        "lane": lane,
        "repeats": len(reports),
        "attempts": len(provenance) - len(skipped),
        "skipped": len(skipped),
        # Worst cell decides: one repeat the provider refused is enough to stop the lane from
        # standing in a comparison, even if another repeat ran clean.
        "provider_validity": min(
            (record.get("provider_validity", VALID) for record in provenance),
            key=_VALIDITY_ORDER.index,
            default=VALID,
        ),
        "jspace": jspace,
        "primary": {
            "resolve_rate": round(_mean(resolve_rates), 4),
            "near_resolve_rate": round(_mean(near_rates), 4),
            "mean_pass_fraction": round(_mean(pass_fractions), 4),
        },
        "efficiency": {
            "wall_clock_seconds": round(sum(strategy["seconds"] for strategy in strategies), 3),
            "agent_invocations": sum(strategy["agent_invocations"] for strategy in strategies),
            "mean_seconds_per_attempt": round(_mean(strategy["seconds"] for strategy in strategies), 3),
        },
        "process": {
            "phase_failures": sum(strategy["phase_failures"] for strategy in strategies),
            "nonzero_returncodes": sum(1 for record in provenance if record.get("returncode")),
            "mean_compression_ratio": round(_mean(compaction_ratios), 4) if compaction_ratios else None,
        },
        "stability": {
            "resolve_rate_stdev": round(_stdev(resolve_rates), 4),
            "mean_pass_fraction_stdev": round(_stdev(pass_fractions), 4),
        },
    }


def lane_deltas(summaries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Marginal contribution of each treatment: B-A, C-B, D-C, E-D.

    Only adjacent lanes are compared, because that adjacency is what isolates a single
    treatment. Comparing E to A would measure four changes at once.
    """
    by_lane = {summary["lane"]: summary for summary in summaries}
    ordered = [lane for lane in ALL_LANES if lane in by_lane]
    deltas = []
    for previous, current in zip(ordered, ordered[1:]):
        before, after = by_lane[previous], by_lane[current]
        # Both sides have to be outcomes the provider actually produced. Subtracting a refused
        # cell from a served one measures the outage, and does it in the units of the treatment.
        if VALID != before.get("provider_validity", VALID) or VALID != after.get("provider_validity", VALID):
            deltas.append(
                {
                    "comparison": f"{current} - {previous}",
                    "treatment": LANE_TREATMENTS.get(current, ""),
                    "status": "unavailable",
                    "reason": "no provider-valid pair",
                }
            )
            continue
        deltas.append(
            {
                "comparison": f"{current} - {previous}",
                "treatment": LANE_TREATMENTS.get(current, ""),
                "status": VALID,
                "resolve_rate": round(after["primary"]["resolve_rate"] - before["primary"]["resolve_rate"], 4),
                "near_resolve_rate": round(after["primary"]["near_resolve_rate"] - before["primary"]["near_resolve_rate"], 4),
                "mean_pass_fraction": round(after["primary"]["mean_pass_fraction"] - before["primary"]["mean_pass_fraction"], 4),
                "agent_invocations": after["efficiency"]["agent_invocations"] - before["efficiency"]["agent_invocations"],
                "wall_clock_seconds": round(
                    after["efficiency"]["wall_clock_seconds"] - before["efficiency"]["wall_clock_seconds"], 3
                ),
            }
        )
    return deltas


LANE_TREATMENTS = {
    "A": "one session, raw objective",
    "B": "bounded slice context (shipped harness)",
    "C": "Research -> Plan -> Implement",
    "D": "RPI + intentional compaction",
    "E": "RPI + compaction + J-Space",
}


def build_comparison(run_dir: Path, lanes: Sequence[str], repeats: int) -> dict[str, Any]:
    """Score every cell that has eval output and reduce it to one comparison."""
    run_dir = Path(run_dir)
    summaries = []
    for lane in lanes:
        reports = []
        for repeat in range(1, repeats + 1):
            cell_dir = run_dir / lane / f"r{repeat}"
            if not cell_dir.is_dir():
                continue
            reports.append(score_and_write(cell_dir))
        if reports or (run_dir / lane).is_dir():
            summaries.append(summarize_lane(run_dir, lane, repeats, reports))
    return {
        "lanes": summaries,
        "deltas": lane_deltas(summaries),
        "note": ("Primary metric is ProgramBench correctness. A lane that saves context but loses resolve rate is worse."),
    }


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    lines = ["# Lane comparison", "", "## Primary outcome (ProgramBench)", ""]
    lines.append("| lane | treatment | validity | resolve | near | mean pass | stdev(resolve) | agent calls | wall s |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
    for summary in comparison["lanes"]:
        primary, efficiency, stability = summary["primary"], summary["efficiency"], summary["stability"]
        lines.append(
            f"| {summary['lane']} | {LANE_TREATMENTS.get(summary['lane'], '')} "
            f"| {summary.get('provider_validity', VALID)} "
            f"| {primary['resolve_rate']:.1%} | {primary['near_resolve_rate']:.1%} "
            f"| {primary['mean_pass_fraction']:.1%} | {stability['resolve_rate_stdev']:.3f} "
            f"| {efficiency['agent_invocations']} | {efficiency['wall_clock_seconds']:.0f} |"
        )

    invalid = [summary for summary in comparison["lanes"] if summary.get("provider_validity", VALID) != VALID]
    if invalid:
        lines += [
            "",
            "## Provider validity",
            "",
            "The provider refused to serve part or all of these lanes. Their scores are kept as",
            "diagnostics and excluded from the deltas below: a rate limit is not a treatment.",
            "",
        ]
        lines += [
            f"- {summary['lane']}: {summary['provider_validity']} "
            f"- diagnostic score {summary['primary']['mean_pass_fraction']:.1%}"
            for summary in invalid
        ]

    lines += ["", "## Marginal contribution of each treatment", ""]
    comparable = [delta for delta in comparison["deltas"] if delta.get("status", VALID) == VALID]
    if comparable:
        lines.append("| comparison | treatment added | resolve | near | mean pass | agent calls | wall s |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
    else:
        lines.append("No comparison had a provider-valid pair; this run measures nothing.")
    for delta in comparable:
        lines.append(
            f"| {delta['comparison']} | {delta['treatment']} | {delta['resolve_rate']:+.1%} "
            f"| {delta['near_resolve_rate']:+.1%} | {delta['mean_pass_fraction']:+.1%} "
            f"| {delta['agent_invocations']:+d} | {delta['wall_clock_seconds']:+.0f} |"
        )

    excluded = [delta for delta in comparison["deltas"] if delta.get("status", VALID) != VALID]
    if excluded:
        lines += ["", "Comparisons with no provider-valid pair:", ""]
        lines += [f"- {delta['comparison']}: {delta['status']} - reason: {delta['reason']}" for delta in excluded]

    failures = [
        (summary["lane"], summary["process"]["phase_failures"], summary["process"]["nonzero_returncodes"])
        for summary in comparison["lanes"]
    ]
    if any(phase or rc for _, phase, rc in failures):
        lines += ["", "## Process failures", "", "| lane | phase failures | nonzero exits |", "|---|---:|---:|"]
        lines += [f"| {lane} | {phase} | {rc} |" for lane, phase, rc in failures]

    skips = [(summary["lane"], summary["skipped"]) for summary in comparison["lanes"] if summary.get("skipped")]
    if skips:
        lines += [
            "",
            "## Unrun cells",
            "",
            "A skipped cell was never administered, so its lane's rates are computed over fewer",
            "attempts. Read a skipped lane as unrun, not as a treatment that underperformed.",
            "",
            "| lane | skipped cells |",
            "|---|---:|",
        ]
        lines += [f"| {lane} | {count} |" for lane, count in skips]

    administered = [(s["lane"], s["jspace"]) for s in comparison["lanes"] if s.get("jspace")]
    if administered:
        lines += ["", "## J-Space artifact administered", ""]
        lines += [
            f"- lane {lane}: `{record['source']}` at `{record['revision']}`, `{record['artifact']}` sha256 `{record['sha256']}`"
            for lane, record in administered
        ]

    lines += ["", comparison["note"], ""]
    return "\n".join(lines)


def write_comparison(comparison: dict[str, Any], run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / COMPARISON_FILENAME).write_text(json.dumps(comparison, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path = run_dir / COMPARISON_MARKDOWN
    path.write_text(render_comparison_markdown(comparison), encoding="utf-8")
    return path


def run_experiment(
    run_dir: Path,
    instances: Sequence[str],
    adapter_factory: AdapterFactory,
    repo_root: Path,
    lanes: Sequence[str] = ALL_LANES,
    repeats: int = 1,
    eval_runner: Optional[EvalRunner] = None,
    score: bool = True,
) -> Optional[dict[str, Any]]:
    """Run the full matrix, optionally evaluate and score it."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    for cell in interleave(instances, lanes, repeats):
        try:
            adapter = adapter_factory(cell.lane)
        except JSpaceUnavailable as error:
            skip_cell(cell, run_dir, repo_root, str(error))
            continue
        run_cell(cell, run_dir, adapter, repo_root)

    if eval_runner is not None:
        for cell_dir in cell_dirs(run_dir, lanes, repeats):
            eval_runner.evaluate(cell_dir)

    if not score:
        return None
    comparison = build_comparison(run_dir, lanes, repeats)
    write_comparison(comparison, run_dir)
    return comparison


__all__ = [
    "COMPARISON_FILENAME",
    "COMPARISON_MARKDOWN",
    "PROVIDER_DEGRADED",
    "PROVIDER_UNAVAILABLE",
    "VALID",
    "Cell",
    "LANE_TREATMENTS",
    "PROVENANCE_FILENAME",
    "REPORT_FILENAME",
    "build_comparison",
    "classify_provider_validity",
    "cell_dirs",
    "default_adapter_factory",
    "interleave",
    "lane_deltas",
    "render_comparison_markdown",
    "run_cell",
    "skip_cell",
    "run_experiment",
    "summarize_lane",
    "write_comparison",
]
