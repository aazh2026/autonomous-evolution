"""自主演进策略优化器引擎 —— 闭环调优、显著性门禁与治理提案联动。

设计依据：
- docs/tasks.md ``TASK-P4-050``；
- R-033（价值模型校准）；
- R-121（策略调整证据与统计显著性）；
- TASK-P4-070（修改决策逻辑走 SystemEvolutionProposal 提案与审批）。

工作流程：
1. **收集历史周期表现**（达成度增益 Δ、单位成本效率）；
2. **搜索候选参数空间**（容差、探索频率、因子权重指数）；
3. **统计显著性假设检验**（Welch's t-test，要求 p < 0.05 且效果量 > 0）；
4. **决策三态**：
   - 显著正向改善 -> 生成 `SystemEvolutionProposal` 策略变更提案并输出 `OptimizationReport`；
   - 未达显著或负向 -> 坚决拒纳，回退基线并留痕审计；
5. **提供与编排层 Loop B 的直接联动接口**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from core.evolution.proposal import (
    ProposalKind,
    SystemEvolutionProposal,
)
from core.evolution.risk import ReviewerScope
from core.evolution.taxonomy import RiskLevel
from core.policy.strategy_optimization import (
    MINIMUM_EVALUATION_CYCLES,
    SIGNIFICANCE_ALPHA,
    CycleOutcomeMeasurement,
    OptimizationReport,
    TunableParameters,
    compare_measurements,
)


@dataclass(frozen=True, slots=True)
class StrategyOptimizer:
    """策略优化引擎。

    :param baseline_parameters: 当前运行的基线参数。
    :param alpha: 显著性水平阈值（默认 0.05）。
    """

    baseline_parameters: TunableParameters = field(default_factory=TunableParameters)
    alpha: Decimal = SIGNIFICANCE_ALPHA

    def optimize_and_evaluate(
        self,
        *,
        baseline_history: Sequence[CycleOutcomeMeasurement],
        candidate_history: Sequence[CycleOutcomeMeasurement],
        candidate_parameters: TunableParameters,
        metric: str = "attainment_gain",
    ) -> OptimizationReport:
        """评估候选策略与基线策略的表现，产出决策报告。"""
        is_sample_deficient = (
            len(baseline_history) < MINIMUM_EVALUATION_CYCLES
            or len(candidate_history) < MINIMUM_EVALUATION_CYCLES
        )
        if is_sample_deficient:
            comparison = compare_measurements((), (), alpha=self.alpha)
            return OptimizationReport(
                baseline_parameters=self.baseline_parameters,
                candidate_parameters=candidate_parameters,
                accepted=False,
                comparison=comparison,
                rationale=(
                    f"样本量不足（基线={len(baseline_history)}, 候选={len(candidate_history)}, "
                    f"要求 ≥ {MINIMUM_EVALUATION_CYCLES}），拒绝策略变更并保持基线。"
                ),
            )

        if metric == "cost_efficiency":
            base_values = [m.cost_efficiency for m in baseline_history]
            cand_values = [m.cost_efficiency for m in candidate_history]
            metric_label = "单位成本效率"
        else:
            base_values = [m.attainment_gain for m in baseline_history]
            cand_values = [m.attainment_gain for m in candidate_history]
            metric_label = "目标达成度增益"

        comparison = compare_measurements(base_values, cand_values, alpha=self.alpha)

        if comparison.is_significant:
            accepted = True
            rationale = (
                f"策略优化通过验收：在 {metric_label} 上，调优参数相对基线呈现统计显著改善 "
                f"(p={comparison.p_value} < {self.alpha}, "
                f"相对提升 {comparison.relative_gain}%)。"
            )
        else:
            accepted = False
            rationale = (
                f"策略优化未通过验收：在 {metric_label} 上未达到统计显著性正向收益 "
                f"(p={comparison.p_value}, Δ={comparison.absolute_gain})，"
                "执行安全回退保持基线。"
            )

        return OptimizationReport(
            baseline_parameters=self.baseline_parameters,
            candidate_parameters=candidate_parameters,
            accepted=accepted,
            comparison=comparison,
            rationale=rationale,
        )

    def create_evolution_proposal(
        self,
        report: OptimizationReport,
        *,
        proposed_by: str,
        proposal_id: str,
        current_criteria_version: str,
    ) -> SystemEvolutionProposal:
        """为被采纳的策略调优生成受控治理提案（TASK-P4-070 契约）。"""
        if not report.accepted:
            raise ValueError(f"未通过显著性验收的优化报告不得创建提案：{report.rationale}")

        diff_summary = (
            f"自主演进策略调优: tolerance {report.baseline_parameters.tolerance} -> "
            f"{report.candidate_parameters.tolerance}, "
            f"explore_every {report.baseline_parameters.explore_every} -> "
            f"{report.candidate_parameters.explore_every}, "
            f"min_confidence_gate {report.baseline_parameters.min_confidence_gate} -> "
            f"{report.candidate_parameters.min_confidence_gate}"
        )

        rationale = (
            f"{report.rationale}\n"
            f"统计证据: t={report.comparison.t_statistic}, p={report.comparison.p_value}, "
            f"提升比例={report.comparison.relative_gain}%"
        )

        return SystemEvolutionProposal(
            proposal_id=proposal_id,
            project_id="system",
            kind=ProposalKind.POLICY_CHANGE,
            title="自学习策略参数动态调优提案",
            target="core.policy.strategy_optimization.TunableParameters",
            change_summary=diff_summary,
            proposed_by=proposed_by,
            criteria_version=current_criteria_version,
            risk=RiskLevel.HIGH,
            required_approvals=2,
            details=rationale,
            required_scopes=(ReviewerScope.GOVERNANCE,),
        )


__all__ = ["StrategyOptimizer"]
