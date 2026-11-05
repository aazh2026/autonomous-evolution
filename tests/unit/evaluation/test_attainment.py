"""TASK-P1-071 —— 项目演进层指标（阶段一只做达成度差值）。

验证（T1）：达成度差值有**已知输入的期望值断言**。

**阶段一为什么只做差值。** §10 的项目演进层有三项：目标达成度、质量趋势、
单位成本收益。质量趋势需要跨周期的质量基线（TASK-P1-042 / TASK-P1-072
才产出）；单位成本收益需要每轮的成本与收益配对，而收益归因（这一轮的改善
有多少来自这一轮）在阶段一没有可辩护的做法 —— **硬算出来的是噪声**。

**为什么取平均而不是求和。** 求和会随周期数量线性增长，看不出"每一轮平均
好了多少"；而"每一轮平均好了多少"才是"这个系统是不是在让项目变好"的答案。
"""

from __future__ import annotations

from decimal import Decimal

from evaluation.common import MetricState
from evaluation.project_evaluation.attainment import (
    CycleAttainment,
    attainment_delta,
)


def _cycle(
    cycle_id: str,
    before: str | None,
    after: str | None,
    outcome: str = "",
) -> CycleAttainment:
    return CycleAttainment(
        cycle_id=cycle_id,
        attainment_before=None if before is None else Decimal(before),
        attainment_after=None if after is None else Decimal(after),
        outcome=outcome,
    )


#: 四轮前后快照都齐的周期，差值分别是 +0.10 / +0.20 / -0.10 / +0.20，
#: 合计 +0.40，平均 +0.10。
_KNOWN_CYCLES = (
    _cycle("cycle-1", "0.40", "0.50", "improved"),
    _cycle("cycle-2", "0.50", "0.70", "improved"),
    _cycle("cycle-3", "0.70", "0.60", "regressed"),
    _cycle("cycle-4", "0.60", "0.80", "improved"),
)


class TestCycleAttainmentDelta:
    def test_a_missing_snapshot_yields_no_delta(self) -> None:
        """缺数据不是"没变化" —— 差值是 ``None``，不是 0。"""
        assert _cycle("c", None, "0.90").delta is None
        assert _cycle("c", "0.40", None).delta is None
        assert _cycle("c", None, None).delta is None

    def test_a_complete_snapshot_yields_the_difference(self) -> None:
        assert _cycle("c", "0.40", "0.65").delta == Decimal("0.25")
        assert _cycle("c", "0.65", "0.40").delta == Decimal("-0.25")


class TestAttainmentDelta:
    def test_a_known_set_of_cycles_gives_a_known_mean(self) -> None:
        result = attainment_delta(_KNOWN_CYCLES)

        assert result.metric_id == "attainment_delta"
        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal("0.40")
        assert result.denominator == Decimal(4)
        assert result.value == Decimal("0.10")
        assert result.detail["total_cycles"] == 4
        assert result.detail["comparable_cycles"] == 4

    def test_cycles_without_a_baseline_are_excluded_from_the_mean(self) -> None:
        """缺基线的轮次差值算不出来 —— 把它们当成 0 参与平均，
        等于宣称"那一轮没有任何改善"。"""
        cycles = (*_KNOWN_CYCLES, _cycle("cycle-5", None, "0.90", "improved"))

        result = attainment_delta(cycles)

        assert result.denominator == Decimal(4)
        assert result.numerator == Decimal("0.40")
        assert result.value == Decimal("0.10")
        assert result.detail["total_cycles"] == 5
        assert result.detail["comparable_cycles"] == 4

    def test_the_outcome_breakdown_is_reported(self) -> None:
        """均值会掩盖"一半变好一半变差"这种形状 —— 分布要一起给。"""
        result = attainment_delta(_KNOWN_CYCLES)

        assert result.detail["outcomes"] == {"improved": 3, "regressed": 1}

    def test_without_any_comparable_cycle_there_are_no_samples(self) -> None:
        cycles = [
            _cycle("cycle-1", None, "0.90", "improved"),
            _cycle("cycle-2", "0.40", None, "improved"),
        ]

        result = attainment_delta(cycles)

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None
        assert result.detail["comparable_cycles"] == 0
        assert result.detail["total_cycles"] == 2

    def test_with_no_cycles_at_all_there_are_no_samples(self) -> None:
        result = attainment_delta([])

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None

    def test_a_mean_of_zero_is_a_computed_result(self) -> None:
        """改进与倒退相抵 —— 这是"试过了，确实是零"，与"算不出来"必须区分。"""
        cycles = (
            _cycle("cycle-1", "0.40", "0.50", "improved"),
            _cycle("cycle-2", "0.50", "0.40", "regressed"),
        )

        result = attainment_delta(cycles)

        assert result.state is MetricState.COMPUTED
        assert result.value == Decimal(0)
        assert result.is_computed is True

    def test_a_negative_mean_is_reported_as_such(self) -> None:
        """倒退不能被截断成 0 —— 那会把"系统在变差"显示成"没变化"。"""
        cycles = (
            _cycle("cycle-1", "0.60", "0.50", "regressed"),
            _cycle("cycle-2", "0.50", "0.40", "regressed"),
        )

        result = attainment_delta(cycles)

        assert result.value == Decimal("-0.10")
        assert result.state is MetricState.COMPUTED
