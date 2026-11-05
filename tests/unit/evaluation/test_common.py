"""TASK-P1-071 —— 指标层的公共词汇。

验证（T1）：``rate()`` 在分母为 0 时**不返回 0**。

这一条是整个指标层的地基。"一个样本都没有"和"比例恰好是 0"在报表上长得
一模一样，而含义完全相反 —— 后者意味着"试过了，确实是零"。所以这里把
"算不出来"做成独立状态，并让 ``value`` 在该状态下为 ``None``。

**TASK-P1-014 立下的"不可判定 ≠ 未触发"在这里是同一条规矩。**
"""

from __future__ import annotations

from decimal import Decimal

from evaluation.common import (
    MetricResult,
    MetricState,
    Verdict,
    rate,
)


class TestRateComputesTheRatio:
    """已知输入 → 已知期望值。"""

    def test_a_simple_ratio(self) -> None:
        result = rate("some_rate", 3, 4)

        assert result.metric_id == "some_rate"
        assert result.state is MetricState.COMPUTED
        assert result.value == Decimal("0.75")
        assert result.numerator == Decimal(3)
        assert result.denominator == Decimal(4)
        assert result.sample_size == 4
        assert result.is_computed is True

    def test_a_ratio_of_zero_is_computed_not_missing(self) -> None:
        """比例恰好为 0 是**算出来了** —— 与"算不出来"必须区分开。"""
        result = rate("some_rate", 0, 5)

        assert result.state is MetricState.COMPUTED
        assert result.value == Decimal(0)
        assert result.is_computed is True

    def test_the_detail_is_carried_through(self) -> None:
        result = rate("some_rate", 1, 2, detail={"note": "备注"})

        assert result.detail == {"note": "备注"}


class TestRateRefusesToInventANumber:
    """分母为 0 时报"算不出来"，不报 0。"""

    def test_an_empty_denominator_yields_no_value(self) -> None:
        result = rate("some_rate", 0, 0)

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None
        assert result.is_computed is False

    def test_the_empty_state_can_be_overridden(self) -> None:
        """有样本但缺基线 —— 与"连样本都没有"要分开报。"""
        result = rate("some_rate", 0, 0, empty_state=MetricState.BASELINE_MISSING)

        assert result.state is MetricState.BASELINE_MISSING
        assert result.value is None

    def test_the_numerator_is_still_reported(self) -> None:
        """算不出来不等于信息全丢 —— 分母为 0 这个事实要留着。"""
        result = rate("some_rate", 0, 0)

        assert result.numerator == Decimal(0)
        assert result.denominator == Decimal(0)
        assert result.sample_size == 0


class TestMetricResultInvariants:
    def test_a_result_without_a_value_is_never_computed(self) -> None:
        """``is_computed`` 同时看状态与值 —— 只看其中一个会漏掉构造错误。"""
        inconsistent = MetricResult(
            metric_id="x",
            state=MetricState.COMPUTED,
            value=None,
        )

        assert inconsistent.is_computed is False

    def test_a_result_without_a_denominator_has_no_sample_size(self) -> None:
        result = MetricResult(metric_id="x", state=MetricState.NO_SAMPLES, value=None)

        assert result.sample_size is None


class TestVerdictVocabulary:
    def test_the_two_verdicts_are_the_ones_humans_can_choose(self) -> None:
        """标注只有两种结论：有效 / 无效。"待定"是**没标**，不是第三种结论。"""
        assert {item.value for item in Verdict} == {"valid", "invalid"}
