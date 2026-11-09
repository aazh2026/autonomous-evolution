"""TASK-P2-018 —— 精确统计检验。

验证（T1）：Fisher 精确检验、置换检验与多重比较校正的结论是对的。

**为什么这些数要一个一个钉死。** 统计口径写错一个地方，整块看板就在说假话
—— 而看板是用来做选型决策的。所以这里的期望值都是**手算得出来的**：要么是
教科书里的经典例子，要么是能一笔笔数出来的计数。

**为什么不用 scipy 来对答案。** scipy 是要验的那一方，不是标准答案。这里用
"把每一种分法都列一遍"的定义式算法算一遍 —— 定义本身就是答案。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from evaluation.executors.statistics import (
    Alternative,
    StatisticsOptions,
    compare_success_rates,
    fisher_exact,
    holm_adjust,
    permutation_test,
)


class TestTheFisherExactTest:
    """Fisher 精确检验 —— 计数型指标的检验。"""

    def test_a_table_with_no_difference_is_not_significant(self) -> None:
        """两组一模一样 —— p 值应该是 1。"""
        result = fisher_exact(((5, 5), (5, 5)))

        assert result.p_value == Decimal(1)
        assert result.significant is False

    def test_a_wide_gap_is_significant(self) -> None:
        """差得足够远 —— p 值很小。"""
        result = fisher_exact(((9, 1), (1, 9)))

        assert result.p_value < Decimal("0.01")
        assert result.significant is True

    def test_the_p_value_matches_the_hand_counted_value(self) -> None:
        """**教科书里的经典例子。** 表是 [[1, 9], [11, 3]]，p 值是 0.0027594561…

        固定边际下只有 12 张可能的表，把它们的概率全加起来（精确有理数），p
        值就是这张表以及所有不比它更极端的表的概率之和。
        """
        result = fisher_exact(((1, 9), (11, 3)))

        assert result.p_value == pytest.approx(Decimal("0.002759456185220"), abs=Decimal("1e-15"))

    def test_the_statistic_is_the_gap_of_the_two_rates(self) -> None:
        """统计量是两个成功率之差 —— 前者减后者。"""
        result = fisher_exact(((9, 1), (1, 9)))

        assert result.statistic == pytest.approx(Decimal("0.8"))

    def test_the_one_sided_test_only_looks_at_one_tail(self) -> None:
        """单侧检验看的是单边 —— 单侧 p 值不大于双侧。

        表是 [[9, 1], [3, 7]]：甲的成功率明显更高。所以"甲更大"这个方向的单
        侧 p 值很小，而双侧 p 值要把两端都算上，更大。
        """
        table = ((9, 1), (3, 7))
        two_sided = fisher_exact(table)
        greater = fisher_exact(table, StatisticsOptions(alternative=Alternative.GREATER))

        assert greater.p_value <= two_sided.p_value
        assert greater.p_value < Decimal("0.05")

    def test_the_other_tail_looks_the_other_way(self) -> None:
        """方向反过来，结论也反过来 —— 单侧检验不是"总显著"。"""
        table = ((3, 7), (9, 1))
        greater = fisher_exact(table, StatisticsOptions(alternative=Alternative.GREATER))
        less = fisher_exact(table, StatisticsOptions(alternative=Alternative.LESS))

        assert less.p_value < Decimal("0.05")
        assert greater.p_value > Decimal("0.5")

    def test_an_empty_table_is_refused(self) -> None:
        """一张表里一个样本都没有 —— 检验无从做起。"""
        with pytest.raises(ValueError, match="一个样本都没有"):
            fisher_exact(((0, 0), (0, 0)))

    def test_a_table_of_the_wrong_shape_is_refused(self) -> None:
        with pytest.raises(ValueError, match="2×2"):
            fisher_exact(((1, 2, 3), (4, 5, 6)))

    def test_negative_counts_are_refused(self) -> None:
        with pytest.raises(ValueError, match="不能为负"):
            fisher_exact(((-1, 2), (3, 4)))


class TestTheSuccessRateComparison:
    """成功率比较 —— 直接喂计数的那个入口。"""

    def test_the_counts_become_a_two_by_two_table(self) -> None:
        result = compare_success_rates(9, 10, 1, 10)

        assert result.detail["table"] == {"a": 9, "b": 1, "c": 1, "d": 9}

    def test_more_successes_than_attempts_is_refused(self) -> None:
        with pytest.raises(ValueError, match="不能超过总数"):
            compare_success_rates(11, 10, 1, 10)

    def test_the_sample_size_is_the_total(self) -> None:
        result = compare_success_rates(3, 4, 2, 6)

        assert result.sample_size == 10


class TestThePermutationTest:
    """置换检验 —— 连续量（成本、耗时）的检验。"""

    def test_identical_groups_are_not_significant(self) -> None:
        """两组数一样 —— 换多少次分法都还是一样。"""
        first = [Decimal(1), Decimal(2), Decimal(3)]
        second = [Decimal(1), Decimal(2), Decimal(3)]

        result = permutation_test(first, second)

        assert result.p_value == Decimal(1)
        assert result.significant is False

    def test_the_p_value_is_the_share_of_the_extreme_orderings(self) -> None:
        """**手工数得出来的精确值。**

        两组：一组 [1]，另一组 [2, 3]。混起来共 3 个数，取 1 个给第一组 —— 一共
        3 种分法。观测到的中位数差是 2.5 − 1 = 1.5。三种分法里，第一组拿到 1
        或拿到 3 时差距都是 1.5（算极端），拿到 2 时差距是 0 —— 不算极端。
        所以 p = 2/3。
        """
        result = permutation_test([Decimal(1)], [Decimal(2), Decimal(3)])

        assert result.method == "permutation_exact"
        assert result.p_value == Decimal(2) / Decimal(3)

    def test_completely_separated_groups_are_significant(self) -> None:
        """两组完全不重叠 —— 换法怎么换都对不上。"""
        first = [Decimal(1), Decimal(2), Decimal(3)]
        second = [Decimal(90), Decimal(91), Decimal(92)]

        result = permutation_test(first, second)

        assert result.p_value <= Decimal("0.25")
        assert result.statistic > Decimal(80)

    def test_the_statistic_is_the_median_gap(self) -> None:
        """统计量是两组中位数之差 —— 后一组减前一组。"""
        first = [Decimal(1), Decimal(2), Decimal(3)]
        second = [Decimal(10), Decimal(20), Decimal(30)]

        result = permutation_test(first, second)

        assert result.statistic == Decimal(18)

    def test_a_small_sample_is_enumerated_exactly(self) -> None:
        """样本小 —— 每一种分法都列一遍，不抽样。"""
        first = [Decimal(1), Decimal(2)]
        second = [Decimal(8), Decimal(9)]

        result = permutation_test(first, second)

        assert result.method == "permutation_exact"
        assert result.detail["exact"] is True

    def test_a_large_sample_falls_back_to_monte_carlo(self) -> None:
        """样本大 —— 组合数太多，改抽样，并**说清是抽样**。"""
        first = [Decimal(index) for index in range(12)]
        second = [Decimal(index + 100) for index in range(12)]

        result = permutation_test(first, second)

        assert result.method == "permutation_monte_carlo"
        assert result.detail["exact"] is False

    def test_the_monte_carlo_fallback_is_reproducible(self) -> None:
        """**同样的种子，同样的结论** —— 否则这条检验没法复核。"""
        first = [Decimal(index) for index in range(12)]
        second = [Decimal(index + 100) for index in range(12)]

        first_run = permutation_test(first, second)
        second_run = permutation_test(first, second)

        assert first_run.p_value == second_run.p_value

    def test_an_empty_group_is_refused(self) -> None:
        with pytest.raises(ValueError, match="不能是空的"):
            permutation_test([], [Decimal(1)])


class TestTheHolmAdjustment:
    """多重比较校正。"""

    def test_a_single_test_is_left_alone(self) -> None:
        adjusted = holm_adjust([Decimal("0.03")])

        assert adjusted == (Decimal("0.03"),)

    def test_the_smallest_p_value_is_multiplied_by_the_family_size(self) -> None:
        """最小的那一个按族的大小乘一遍。"""
        adjusted = holm_adjust([Decimal("0.01"), Decimal("0.02"), Decimal("0.03")])

        assert adjusted[0] == Decimal("0.03")

    def test_the_adjusted_values_stay_monotone(self) -> None:
        """**校正后的 p 值按原始 p 值的大小单调不减。**

        原始 p 值是 [0.04, 0.01, 0.02] —— 注意 0.01 排在第二个。校正之后，
        原始 p 值越小的，校正后也越小 —— 不改变结论的方向。
        """
        adjusted = holm_adjust([Decimal("0.04"), Decimal("0.01"), Decimal("0.02")])

        assert adjusted[1] <= adjusted[2] <= adjusted[0]

    def test_the_adjusted_value_never_exceeds_one(self) -> None:
        adjusted = holm_adjust([Decimal("0.9"), Decimal("0.95")])

        assert all(value <= Decimal(1) for value in adjusted)

    def test_an_empty_family_is_allowed(self) -> None:
        assert holm_adjust([]) == ()

    def test_a_correction_can_turn_a_result_non_significant(self) -> None:
        """**校正会推翻结论** —— 这正是它存在的理由。"""
        raw = Decimal("0.04")
        adjusted = holm_adjust([raw] * 6)

        assert adjusted[0] > Decimal("0.05")
