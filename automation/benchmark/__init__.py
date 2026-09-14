"""ProgramBench effectiveness benchmark for the repo-automation harness.

Measures how effective the supervisor harness is at producing working code by having
it rebuild ProgramBench programs and scoring the results with ProgramBench's own
black-box test suites. See ``automation/benchmark/run.py`` for the CLI.
"""

from .adapter import AgentAdapter, SubmissionResult, SupervisorAgentAdapter
from .evalrunner import EvalRunner, ProgramBenchEvalRunner, RecordedEvalRunner
from .instances import TaskSpec, all_instances, resolve_instances, task_spec
from .lanes import Cell, build_comparison, interleave, run_experiment
from .strategies import (
    ALL_LANES,
    ExecutionContext,
    ExecutionStrategy,
    OneSessionStrategy,
    RpiStrategy,
    SliceContextStrategy,
    StrategyResult,
    build_strategy,
)
from .scoring import EffectivenessReport, InstanceScore, score_run_dir, write_report

__all__ = [
    "AgentAdapter",
    "SubmissionResult",
    "SupervisorAgentAdapter",
    "EvalRunner",
    "ProgramBenchEvalRunner",
    "RecordedEvalRunner",
    "TaskSpec",
    "ALL_LANES",
    "Cell",
    "ExecutionContext",
    "ExecutionStrategy",
    "OneSessionStrategy",
    "RpiStrategy",
    "SliceContextStrategy",
    "StrategyResult",
    "build_comparison",
    "build_strategy",
    "interleave",
    "run_experiment",
    "all_instances",
    "resolve_instances",
    "task_spec",
    "EffectivenessReport",
    "InstanceScore",
    "score_run_dir",
    "write_report",
]
