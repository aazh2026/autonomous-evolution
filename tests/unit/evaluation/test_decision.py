"""TASK-P1-071 —— 决策质量层指标。

验证（T1）：决策采纳率、误报率各有**已知输入的期望值断言**。

**两个指标量的是不同的事，不能互相替代。** 采纳率问"系统的建议有多少被
接受"；误报率问"被接受去做的那些，选对了吗"。

**误报率为什么限定在"被采纳去做的决策"上。** 一个无效候选被 reject 掉，
没有任何损失 —— 它甚至说明系统判断对了。把它算进误报率，等于因为系统
**没有**浪费资源而惩罚它。只有那些真的被派了任务的无效候选才构成浪费。
"""

from __future__ import annotations

from decimal import Decimal

from evaluation.common import MetricState, Verdict
from evaluation.evolution_evaluation.decision import (
    DecisionRecord,
    decision_adoption_rate,
    false_positive_rate,
)


def _decision(
    decision_id: str,
    *,
    decision: str = "record",
    overridden_by: str | None = None,
    label: Verdict | None = None,
) -> DecisionRecord:
    return DecisionRecord(
        decision_id=decision_id,
        candidate_id=f"C-{decision_id}",
        decision=decision,
        overridden_by=overridden_by,
        candidate_label=label,
    )


class TestDecisionAdoptionRate:
    def test_a_known_set_of_decisions_gives_a_known_rate(self) -> None:
        decisions = [
            _decision("D-1"),
            _decision("D-2", overridden_by="critic"),
            _decision("D-3"),
        ]

        result = decision_adoption_rate(decisions)

        assert result.metric_id == "decision_adoption_rate"
        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal(2)
        assert result.denominator == Decimal(3)
        assert result.value == Decimal("0.6666666666666666666666666667")
        assert result.detail["total_decisions"] == 3
        assert result.detail["proposals"] == 3
        assert result.detail["overridden"] == 1

    def test_deferred_and_rejected_decisions_are_not_proposals(self) -> None:
        """defer / reject 本来就不是"要执行它"的提议。

        把它们算进分母，指标就变成了"系统动作的激进程度" —— 而那与决策
        **质量**无关：一个什么都 reject 的系统在这里会拿满分。
        """
        decisions = [
            _decision("D-1"),
            _decision("D-2", decision="defer"),
            _decision("D-3", decision="reject"),
        ]

        result = decision_adoption_rate(decisions)

        assert result.numerator == Decimal(1)
        assert result.denominator == Decimal(1)
        assert result.value == Decimal(1)
        assert result.detail["total_decisions"] == 3
        assert result.detail["proposals"] == 1

    def test_an_overridden_proposal_is_not_adopted(self) -> None:
        """被 Critic 否决或人工推翻的建议不算被采纳。"""
        decisions = [
            _decision("D-1", overridden_by="human"),
            _decision("D-2", overridden_by="critic"),
        ]

        result = decision_adoption_rate(decisions)

        assert result.numerator == Decimal(0)
        assert result.value == Decimal(0)
        assert result.state is MetricState.COMPUTED

    def test_without_any_proposal_there_are_no_samples(self) -> None:
        decisions = [
            _decision("D-1", decision="defer"),
            _decision("D-2", decision="reject"),
        ]

        result = decision_adoption_rate(decisions)

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None


class TestFalsePositiveRate:
    def test_a_known_set_of_labels_gives_a_known_rate(self) -> None:
        decisions = [
            _decision("D-1", label=Verdict.INVALID),
            _decision("D-2", label=Verdict.VALID),
            _decision("D-3", label=Verdict.VALID),
        ]

        result = false_positive_rate(decisions)

        assert result.metric_id == "false_positive_rate"
        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal(1)
        assert result.denominator == Decimal(3)
        assert result.detail["labelled_proposals"] == 3

    def test_a_rejected_invalid_candidate_costs_nothing(self) -> None:
        """无效候选被 reject 掉 —— 不该算成误报。

        它甚至说明系统判断对了。把它算进误报率，等于因为系统**没有**浪费
        资源而惩罚它。
        """
        decisions = [
            _decision("D-1", decision="record", label=Verdict.INVALID),
            _decision("D-2", decision="reject", label=Verdict.INVALID),
            _decision("D-3", decision="defer", label=Verdict.INVALID),
        ]

        result = false_positive_rate(decisions)

        assert result.numerator == Decimal(1)
        assert result.denominator == Decimal(1)
        assert result.value == Decimal(1)
        assert result.detail["proposals"] == 1
        assert result.detail["total_decisions"] == 3

    def test_unlabelled_proposals_stay_out_of_the_denominator(self) -> None:
        decisions = [
            _decision("D-1", label=Verdict.INVALID),
            _decision("D-2"),
            _decision("D-3"),
        ]

        result = false_positive_rate(decisions)

        assert result.numerator == Decimal(1)
        assert result.denominator == Decimal(1)
        assert result.detail["proposals"] == 3
        assert result.detail["labelled_proposals"] == 1

    def test_without_any_label_the_rate_is_uncomputable_not_zero(self) -> None:
        """没有标注时报"基线未建立" —— 报 0 会让看板显示"误报率 0%"。

        读的人会以为系统很准，而实际上一个都还没判过。
        """
        decisions = [
            _decision("D-1"),
            _decision("D-2"),
        ]

        result = false_positive_rate(decisions)

        assert result.state is MetricState.BASELINE_MISSING
        assert result.value is None
        assert result.is_computed is False

    def test_with_no_decisions_at_all_there_are_no_samples(self) -> None:
        result = false_positive_rate([])

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None

    def test_a_rate_of_zero_is_a_computed_result(self) -> None:
        """全都判为有效 —— 这是"试过了，确实是零"。"""
        decisions = [
            _decision("D-1", label=Verdict.VALID),
            _decision("D-2", label=Verdict.VALID),
        ]

        result = false_positive_rate(decisions)

        assert result.state is MetricState.COMPUTED
        assert result.value == Decimal(0)
        assert result.is_computed is True
