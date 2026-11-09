"""执行器横向评测 —— 统一口径、对比看板、统计显著性。

设计依据：docs/tasks.md TASK-P2-018；I-17（多执行器结果可比性口径需前置设计）。

这一层只做**评测**：铺工作区、跑执行器、验验收、算指标、判显著性。它不调度
任务、不决定用哪个执行器、不碰任务状态 —— 那些是编排层的事。
"""

from __future__ import annotations

from evaluation.executors.benchmark import (
    KNOWN_ISSUES_FILE,
    METRIC_AVAILABILITY,
    METRIC_COST,
    METRIC_DURATION,
    METRIC_REGRESSION_RATE,
    METRIC_SUCCESS_RATE,
    SUITE_DIR,
    AdapterRunner,
    BenchmarkBoard,
    BenchmarkHarness,
    BenchmarkTask,
    CliRunner,
    CliSpec,
    ExecutionObservation,
    ExecutorReport,
    ExecutorRunner,
    MetricComparison,
    RunOutcome,
    RunRecord,
    available_executors,
    build_board,
    evaluate_executor,
    pilot_benchmark_tasks,
    temporary_root,
)
from evaluation.executors.statistics import (
    ALPHA,
    Alternative,
    StatisticsOptions,
    TestResult,
    compare_success_rates,
    fisher_exact,
    holm_adjust,
    permutation_test,
)

__all__ = [
    "ALPHA",
    "KNOWN_ISSUES_FILE",
    "METRIC_AVAILABILITY",
    "METRIC_COST",
    "METRIC_DURATION",
    "METRIC_REGRESSION_RATE",
    "METRIC_SUCCESS_RATE",
    "SUITE_DIR",
    "AdapterRunner",
    "Alternative",
    "BenchmarkBoard",
    "BenchmarkHarness",
    "BenchmarkTask",
    "CliRunner",
    "CliSpec",
    "ExecutionObservation",
    "ExecutorReport",
    "ExecutorRunner",
    "MetricComparison",
    "RunOutcome",
    "RunRecord",
    "StatisticsOptions",
    "TestResult",
    "available_executors",
    "build_board",
    "compare_success_rates",
    "evaluate_executor",
    "fisher_exact",
    "holm_adjust",
    "permutation_test",
    "pilot_benchmark_tasks",
    "temporary_root",
]
