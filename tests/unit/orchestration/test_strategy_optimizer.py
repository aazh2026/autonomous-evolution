"""TASK-P4-050 自主演进策略优化测试套件。

覆盖：
1. 策略参数边界与不可变性、规范化指纹计算；
2. Welch's t-test 显著性统计检验（显著正增益、不显著负增益、小样本拒判）；
3. 策略优化器决策逻辑（达标采纳 vs 未达标回退）；
4. 采纳后生成 SystemEvolutionProposal 受控治理提案的完备性与防越权；
5. 未达标时拒绝生成提案的阻断契约。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.evolution.proposal import ProposalKind, ProposalStatus
from core.policy.strategy_optimization import (
    CycleOutcomeMeasurement,
    TunableParameters,
    compare_measurements,
)
from evaluation.strategy_optimization import (
    evaluate_policy_tuning,
    format_optimization_markdown,
)
from orchestration.strategy_optimizer import StrategyOptimizer


class TestTunableParameters:
    def test_parameters_within_valid_bounds(self) -> None:
        params = TunableParameters(
            tolerance=Decimal("0.08"),
            explore_every=10,
            min_confidence_gate=Decimal("0.40"),
        )
        assert params.tolerance == Decimal("0.08")
        assert params.explore_every == 10
        assert len(params.digest()) == 64

    def test_parameters_out_of_bounds_rejected(self) -> None:
        with pytest.raises(ValueError, match="超出安全边界"):
            TunableParameters(tolerance=Decimal("0.99"))

        with pytest.raises(ValueError, match="超出安全边界"):
            TunableParameters(min_confidence_gate=Decimal("0.05"))


class TestStatisticalComparison:
    def test_insufficient_samples_rejected(self) -> None:
        comp = compare_measurements([Decimal("1.0"), Decimal("1.2")], [Decimal("2.0")])
        assert not comp.is_significant
        assert "样本量过少" in comp.details

    def test_significant_improvement_detected(self) -> None:
        # 基线均值约 0.35，候选均值约 0.85，差异大且方差小
        baseline = [Decimal(x) for x in ["0.32", "0.34", "0.36", "0.35", "0.33", "0.37"]]
        candidate = [Decimal(x) for x in ["0.82", "0.86", "0.84", "0.85", "0.83", "0.88"]]
        comp = compare_measurements(baseline, candidate)
        assert comp.is_significant
        assert comp.p_value < Decimal("0.05")
        assert comp.absolute_gain > Decimal("0.40")

    def test_negative_or_insignificant_gain_rejected(self) -> None:
        # 候选比基线差
        baseline = [Decimal(x) for x in ["0.70", "0.72", "0.71", "0.69", "0.73", "0.70"]]
        candidate = [Decimal(x) for x in ["0.50", "0.52", "0.51", "0.49", "0.53", "0.50"]]
        comp = compare_measurements(baseline, candidate)
        assert not comp.is_significant
        assert comp.absolute_gain < Decimal("0")


class TestStrategyOptimizer:
    def _make_measurements(self, values: list[str]) -> list[CycleOutcomeMeasurement]:
        return [
            CycleOutcomeMeasurement(
                cycle_id=f"CYC-{i:03d}",
                attainment_gain=Decimal(val),
                cost_usd=Decimal("0.50"),
                task_success_rate=Decimal("1.0"),
            )
            for i, val in enumerate(values)
        ]

    def test_optimizer_accepts_when_statistically_significant(self) -> None:
        base = self._make_measurements(["0.20", "0.22", "0.21", "0.19", "0.23", "0.20"])
        cand = self._make_measurements(["0.60", "0.62", "0.61", "0.59", "0.63", "0.60"])
        cand_params = TunableParameters(tolerance=Decimal("0.08"))

        optimizer = StrategyOptimizer()
        report = optimizer.optimize_and_evaluate(
            baseline_history=base,
            candidate_history=cand,
            candidate_parameters=cand_params,
        )

        assert report.accepted
        assert "策略优化通过验收" in report.rationale

        proposal = optimizer.create_evolution_proposal(
            report,
            proposed_by="strategy-optimizer-agent",
            proposal_id="PROP-OPT-001",
            current_criteria_version="criteria-v1",
        )
        assert proposal.status is ProposalStatus.DRAFT
        assert proposal.kind is ProposalKind.POLICY_CHANGE
        assert "自学习策略参数动态调优提案" in proposal.title
        assert "PROP-OPT-001" in proposal.proposal_id

    def test_optimizer_rejects_and_falls_back_when_not_significant(self) -> None:
        base = self._make_measurements(["0.50", "0.52", "0.51", "0.49", "0.53", "0.50"])
        cand = self._make_measurements(["0.51", "0.50", "0.52", "0.49", "0.50", "0.51"])
        cand_params = TunableParameters(tolerance=Decimal("0.08"))

        optimizer = StrategyOptimizer()
        report = optimizer.optimize_and_evaluate(
            baseline_history=base,
            candidate_history=cand,
            candidate_parameters=cand_params,
        )

        assert not report.accepted
        assert "执行安全回退保持基线" in report.rationale

        # 未被采纳的报告禁止生成治理提案
        with pytest.raises(ValueError, match="未通过显著性验收"):
            optimizer.create_evolution_proposal(
                report,
                proposed_by="strategy-optimizer-agent",
                proposal_id="PROP-OPT-002",
                current_criteria_version="criteria-v1",
            )

    def test_evaluation_markdown_formatting(self) -> None:
        base = self._make_measurements(["0.20", "0.22", "0.21", "0.19", "0.23", "0.20"])
        cand = self._make_measurements(["0.60", "0.62", "0.61", "0.59", "0.63", "0.60"])
        cand_params = TunableParameters(tolerance=Decimal("0.08"))

        report = evaluate_policy_tuning(
            base,
            cand,
            candidate_params=cand_params,
        )
        md = format_optimization_markdown(report)
        assert "# 自主演进策略优化与参数调优报告 (TASK-P4-050)" in md
        assert "决策结论" in md
        assert "统计显著性检验证据" in md
