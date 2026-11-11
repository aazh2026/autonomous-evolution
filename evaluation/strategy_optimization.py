"""TASK-P4-050 自主演进策略优化评估与基准导出。

设计依据：
- docs/tasks.md ``TASK-P4-050``（自主演进策略优化，验收要求：调优相对基线有显著改善）；
- 提供可复现的仿真评估驱动，支持批量导出对比看板与显著性证据。
"""

from __future__ import annotations

from collections.abc import Sequence

from core.policy.strategy_optimization import (
    CycleOutcomeMeasurement,
    OptimizationReport,
    TunableParameters,
)
from orchestration.strategy_optimizer import StrategyOptimizer


def evaluate_policy_tuning(
    baseline_samples: Sequence[CycleOutcomeMeasurement],
    candidate_samples: Sequence[CycleOutcomeMeasurement],
    *,
    candidate_params: TunableParameters,
    metric: str = "attainment_gain",
) -> OptimizationReport:
    """运行策略优化对比评估并产出正式报告。"""
    optimizer = StrategyOptimizer()
    return optimizer.optimize_and_evaluate(
        baseline_history=baseline_samples,
        candidate_history=candidate_samples,
        candidate_parameters=candidate_params,
        metric=metric,
    )


def format_optimization_markdown(report: OptimizationReport) -> str:
    """将优化报告序列化为清晰的 Markdown 看板。"""
    status_icon = "🟢 采纳 (Accepted)" if report.accepted else "🔴 拒绝/回退 (Rejected / Fallback)"
    comp = report.comparison
    sig_label = "通过 (p < 0.05)" if comp.is_significant else "未通过"
    row = (
        f"| 实测表现 | `{comp.baseline_mean}` | `{comp.candidate_mean}` | "
        f"`{comp.absolute_gain}` | `{comp.relative_gain}%` | "
        f"`{comp.t_statistic}` | `{comp.p_value}` | `{sig_label}` |"
    )
    lines = [
        "# 自主演进策略优化与参数调优报告 (TASK-P4-050)",
        "",
        f"**决策结论**：{status_icon}",
        f"**审计摘要**：`{report.candidate_digest}`",
        "",
        "## 一、 参数调整对比",
        "",
        "| 参数项 | 基线策略 (Baseline) | 调优候选 (Candidate) | 说明 |",
        "| :--- | :--- | :--- | :--- |",
        f"| **调度容差 (tolerance)** | `{report.baseline_parameters.tolerance}` | "
        f"`{report.candidate_parameters.tolerance}` | 调度质量与成本权衡 |",
        f"| **探索周期 (explore_every)** | `{report.baseline_parameters.explore_every}` | "
        f"`{report.candidate_parameters.explore_every}` | 冷门组合试探频次 |",
        f"| **置信度门槛 (min_confidence)** | "
        f"`{report.baseline_parameters.min_confidence_gate}` | "
        f"`{report.candidate_parameters.min_confidence_gate}` | 过滤低置信候选 |",
        "",
        "## 二、 统计显著性检验证据",
        "",
        "| 指标项 | 基线均值 | 调优均值 | 绝对增益 | 相对提升 (%) | t 统计量 | p 值 | 显著性判定 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        row,
        "",
        "## 三、 决策理由与留痕",
        "",
        f"> {report.rationale}",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "evaluate_policy_tuning",
    "format_optimization_markdown",
]
