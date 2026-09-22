"""Orchestrate and drive the ProgramBench effectiveness benchmark.

Subcommands:
  run    Produce ``submission.tar.gz`` per instance by driving the harness (no Docker).
  eval   Run ProgramBench's authoritative test suites over the submissions (Docker).
  score  Reduce eval outputs to an effectiveness report (no Docker).
  all    run -> eval -> score.
  lanes  Run the A-E effectiveness experiment (lane x repeat x instance matrix).
  compare  Reduce an existing lane matrix to lane-comparison.json/md (no Docker).

Pick the agent and the model it talks to with the runner env, e.g. NVIDIA via Hermes:
REPO_AUTOMATION_AGENT_RUNNER=hermes, HERMES_INFERENCE_PROVIDER=nvidia,
HERMES_INFERENCE_MODEL=moonshotai/kimi-k3, NVIDIA_API_KEY=...
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

from .adapter import AgentAdapter, SupervisorAgentAdapter
from .evalrunner import MAX_DOCKER_CPUS, EvalRunner, ProgramBenchEvalRunner
from .instances import resolve_instances, task_spec
from .scoring import EffectivenessReport, score_run_dir, write_report
from .strategies import ALL_LANES

REPORT_FILENAME = "effectiveness-report.json"
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def resolve_agent_env(names: Optional[Sequence[str]]) -> dict[str, str]:
    """Copy exactly the named launcher variables through to the agent session.

    The session inherits only what the runner reads (``adapter.INHERITED_ENV_NAMES``), which
    is deliberately too little to reach a provider. This is how a run says which of its own
    variables it authorizes on top of that - a name at a time.

    Names only. ``--agent-env NAME=value`` would put credential material in argv, where every
    process listing on the host can read it, so an argument carrying a value is refused and
    only the part before the ``=`` is ever echoed back. A name that is not set is refused too:
    a run that reached the provider unauthenticated would score the outage as a lane effect,
    and it is cheaper to fail now than after the first cell has spent its budget.
    """
    selected: dict[str, str] = {}
    for name in names or ():
        if not _ENV_NAME.match(name):
            raise SystemExit(
                f"--agent-env takes a variable name, not {name.split('=', 1)[0]!r}: "
                "pass the name and set the variable in the environment."
            )
        if name not in os.environ:
            raise SystemExit(f"--agent-env {name} is not set in this environment; the agent was not launched.")
        selected[name] = os.environ[name]
    return selected


def produce_submissions(run_dir: Path, instances: Sequence[str], adapter: AgentAdapter) -> None:
    run_dir = Path(run_dir)
    for instance_id in instances:
        instance_dir = run_dir / instance_id
        instance_dir.mkdir(parents=True, exist_ok=True)
        adapter.produce_submission(task_spec(instance_id), instance_dir / "submission.tar.gz")


def score_and_write(run_dir: Path) -> EffectivenessReport:
    report = score_run_dir(run_dir)
    write_report(report, Path(run_dir) / REPORT_FILENAME)
    return report


def run_benchmark(
    run_dir: Path,
    instances: Sequence[str],
    adapter: AgentAdapter,
    eval_runner: Optional[EvalRunner] = None,
    score: bool = True,
) -> Optional[EffectivenessReport]:
    """End-to-end orchestration used by both the CLI and tests."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    produce_submissions(run_dir, instances, adapter)
    if eval_runner is not None:
        eval_runner.evaluate(run_dir)
    if score:
        return score_and_write(run_dir)
    return None


def _build_adapter(args: argparse.Namespace) -> SupervisorAgentAdapter:
    return SupervisorAgentAdapter(
        repo_root=Path(args.repo_root),
        agent_command_template=args.agent_cmd,
        timeout_seconds=args.timeout,
        env=resolve_agent_env(args.agent_env),
    )


def _instances_from_args(args: argparse.Namespace) -> list[str]:
    explicit = list(args.instances) if args.instances else None
    return resolve_instances(explicit, use_all=args.all)


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True, type=Path, help="Directory holding per-instance submissions/results.")
    parser.add_argument("--instances", nargs="*", help="Explicit instance ids (default: smoke set).")
    parser.add_argument("--all", action="store_true", help="Run the full ProgramBench instance set.")


def _add_adapter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, default=_DEFAULT_REPO_ROOT, help="Harness repo root.")
    parser.add_argument("--agent-cmd", help="Override the agent command template (default: run_agent.sh).")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-instance agent timeout (seconds).")
    parser.add_argument(
        "--agent-env",
        action="append",
        metavar="VARIABLE_NAME",
        default=[],
        help="Forward this launcher variable to the agent session. Repeatable. Name only, never NAME=value.",
    )


def _add_eval_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workers", type=int, help="ProgramBench eval workers.")
    parser.add_argument(
        "--docker-cpus",
        type=int,
        help=f"CPUs per eval container (default: host CPUs, capped at {MAX_DOCKER_CPUS}).",
    )
    parser.add_argument(
        "--programbench-cmd",
        nargs="+",
        default=["uvx", "programbench"],
        help="Command used to invoke the ProgramBench CLI.",
    )


def _add_lane_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lanes", nargs="+", default=list(ALL_LANES), help=f"Lanes to run (default: {' '.join(ALL_LANES)}).")
    parser.add_argument("--repeats", type=int, default=1, help="Attempts per lane/instance. Use >=2; one run is noisy.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m automation.benchmark", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Produce submissions (no Docker).")
    _add_selection_args(run_parser)
    _add_adapter_args(run_parser)

    eval_parser = subparsers.add_parser("eval", help="Run ProgramBench eval (Docker).")
    eval_parser.add_argument("--run-dir", required=True, type=Path)
    _add_eval_args(eval_parser)

    score_parser = subparsers.add_parser("score", help="Score eval outputs (no Docker).")
    score_parser.add_argument("--run-dir", required=True, type=Path)

    all_parser = subparsers.add_parser("all", help="run -> eval -> score.")
    _add_selection_args(all_parser)
    _add_adapter_args(all_parser)
    _add_eval_args(all_parser)

    lanes_parser = subparsers.add_parser("lanes", help="Run the A-E effectiveness experiment.")
    _add_selection_args(lanes_parser)
    _add_adapter_args(lanes_parser)
    _add_lane_args(lanes_parser)
    _add_eval_args(lanes_parser)
    lanes_parser.add_argument("--eval", action="store_true", help="Also run ProgramBench eval (Docker) per cell.")

    compare_parser = subparsers.add_parser("compare", help="Compare an existing lane matrix (no Docker).")
    compare_parser.add_argument("--run-dir", required=True, type=Path)
    _add_lane_args(compare_parser)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "run":
        produce_submissions(args.run_dir, _instances_from_args(args), _build_adapter(args))
        print(f"Wrote submissions to {args.run_dir}")
        return 0

    if args.command == "eval":
        ProgramBenchEvalRunner(args.programbench_cmd, workers=args.workers, docker_cpus=args.docker_cpus).evaluate(args.run_dir)
        return 0

    if args.command == "score":
        report = score_and_write(args.run_dir)
        print(report.summary_table())
        return 0

    if args.command in {"lanes", "compare"}:
        from . import lanes as lanes_module

        if args.command == "compare":
            comparison = lanes_module.build_comparison(args.run_dir, args.lanes, args.repeats)
        else:
            comparison = lanes_module.run_experiment(
                run_dir=args.run_dir,
                instances=_instances_from_args(args),
                adapter_factory=lanes_module.default_adapter_factory(
                    repo_root=Path(args.repo_root),
                    agent_command_template=args.agent_cmd,
                    timeout_seconds=args.timeout,
                    env=resolve_agent_env(args.agent_env),
                ),
                repo_root=Path(args.repo_root),
                lanes=args.lanes,
                repeats=args.repeats,
                eval_runner=(
                    ProgramBenchEvalRunner(args.programbench_cmd, workers=args.workers, docker_cpus=args.docker_cpus)
                    if args.eval
                    else None
                ),
            )
        assert comparison is not None
        path = lanes_module.write_comparison(comparison, args.run_dir)
        print(lanes_module.render_comparison_markdown(comparison))
        print(f"Wrote {path}")
        return 0

    if args.command == "all":
        report = run_benchmark(
            run_dir=args.run_dir,
            instances=_instances_from_args(args),
            adapter=_build_adapter(args),
            eval_runner=ProgramBenchEvalRunner(args.programbench_cmd, workers=args.workers, docker_cpus=args.docker_cpus),
        )
        assert report is not None
        print(report.summary_table())
        return 0

    return 1  # pragma: no cover - argparse enforces a valid subcommand.


if __name__ == "__main__":
    sys.exit(main())
