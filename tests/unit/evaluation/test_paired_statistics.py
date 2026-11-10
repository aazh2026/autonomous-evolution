"""TASK-P4-040 —— 配对比较的精确检验。

验证（T1）：McNemar 精确检验与配对符号翻转置换检验的结论是对的。

**为什么这两个检验是这一篇的前提。** 自适应调度要证明自己"比静态分配强"，
比的必然是**同一批任务**上跑出来的两条策略线 —— 任务的难易在两条线之间是共
有的。不配对地比两个成功率，等于把"这批任务难"算成"这个策略差"；而配对检验
只看**不一致的那几对**，正是这里需要的口径。

**期望值都是手算得出来的**：McNemar 的 p 值由二项分布一笔笔数出来；符号翻转
的 p 值由 2ⁿ 种翻法逐个列出来。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from evaluation.executors.statistics import (
    Alternative,
    StatisticsOptions,
    mcnemar_exact,
    paired_permutation_test,
)


class TestTheMcNemarExactTest:
    """McNemar 精确检验 —— 配对二值结果。"""

    def test_two_identical_policies_are_not_significant(self) -> None:
        """两个策略每个任务上都一样 —— p 值就是 1。"""
        result = mcnemar_exact([True, False, True, True], [True, False, True, True])

        assert result.p_value == Decimal(1)
        assert result.significant is False

    def test_no_discordant_pair_is_reported_as_a_note(self) -> None:
        """**"完全一样"与"测不动"是两件事** —— 前者 p=1，后者根本没有 p。"""
        result = mcnemar_exact([True, True], [True, True])

        assert result.p_value == Decimal(1)
        assert result.detail["discordant"] == 0
        assert "完全一样" in str(result.detail["note"])

    def test_the_p_value_is_the_hand_counted_binomial_share(self) -> None:
        """**一笔笔数得出来的精确值。**

        十对任务里，甲对 9 次、乙对 1 次。不一致的有 10 对，甲在其中赢了 9
        次 —— 零假设下"谁赢是等可能的"，赢 9 次以上的概率是
        (C(10,9) + C(10,10)) / 2¹⁰ = 11/1024。
        """
        first = [True] * 9 + [False]
        second = [False] * 9 + [True]

        result = mcnemar_exact(first, second)

        assert result.detail["discordant"] == 10
        assert result.detail["only_first"] == 9
        assert result.detail["only_second"] == 1
        assert result.p_value == Decimal(22) / Decimal(1024)

    def test_a_one_sided_test_looks_at_one_tail_only(self) -> None:
        """**方向是事先定好的** —— 单侧只看上尾，比双侧更敏感。"""
        first = [True] * 9 + [False]
        second = [False] * 9 + [True]

        two_sided = mcnemar_exact(first, second)
        greater = mcnemar_exact(first, second, StatisticsOptions(alternative=Alternative.GREATER))

        assert greater.p_value < two_sided.p_value
        assert greater.p_value == Decimal(11) / Decimal(1024)

    def test_the_other_tail_reverses_the_conclusion(self) -> None:
        """方向反过来，结论也反过来 —— 单侧检验不是"总显著"。"""
        first = [True] * 9 + [False]
        second = [False] * 9 + [True]

        greater = mcnemar_exact(first, second, StatisticsOptions(alternative=Alternative.GREATER))
        less = mcnemar_exact(first, second, StatisticsOptions(alternative=Alternative.LESS))

        assert less.p_value > Decimal("0.5")
        assert greater.p_value < Decimal("0.05")

    def test_the_statistic_is_the_gap_of_the_two_success_rates(self) -> None:
        """统计量是两组成功率之差 —— 前者减后者。

        四对任务里甲对三次、乙对两次 —— 成功率 3/4 与 2/4，差 1/4。
        """
        first = [True, True, True, False]
        second = [False, False, True, True]

        result = mcnemar_exact(first, second)

        assert result.statistic == Decimal("0.25")

    def test_agreeing_pairs_do_not_dilute_the_signal(self) -> None:
        """**两边都对的那些对不进检验** —— 它们对"谁更好"一句话也说不出来。

        十对任务里只有两对不一致，两对都是甲赢 —— 这已经够罕见了。而那八对
        都一样的任务不该把 p 值稀释掉。
        """
        first = [True] * 10
        second = [False, False] + [True] * 8

        result = mcnemar_exact(first, second)

        assert result.detail["discordant"] == 2
        assert result.p_value == pytest.approx(Decimal("0.5"))

    def test_mismatched_lengths_are_refused(self) -> None:
        """配对检验要的是**同样长**的两组 —— 长度不齐说明口径错了。"""
        with pytest.raises(ValueError, match="一样长"):
            mcnemar_exact([True], [True, False])

    def test_an_empty_pair_set_is_refused(self) -> None:
        with pytest.raises(ValueError, match="一个样本都没有"):
            mcnemar_exact([], [])


class TestThePairedPermutationTest:
    """配对符号翻转置换检验 —— 配对连续量（成本、耗时）。"""

    def test_identical_pairs_are_not_significant(self) -> None:
        """两组的数一样 —— 怎么翻都还是一样。"""
        first = [Decimal(1), Decimal(2), Decimal(3)]
        second = [Decimal(1), Decimal(2), Decimal(3)]

        result = paired_permutation_test(first, second)

        assert result.p_value == Decimal(1)
        assert result.significant is False

    def test_the_p_value_is_the_hand_counted_share_of_the_flips(self) -> None:
        """**八种翻法，一种一种数。**

        差是 9、18、27（后一组减前一组），观测到的平均差是 18。翻法共 2³ = 8
        种，其中平均差绝对值 ≥ 18 的只有两种（一个都不翻、三个全翻），所以
        p = 2/8 = 0.25。
        """
        first = [Decimal(1), Decimal(2), Decimal(3)]
        second = [Decimal(10), Decimal(20), Decimal(30)]

        result = paired_permutation_test(first, second)

        assert result.method == "paired_permutation_exact"
        assert result.p_value == Decimal("0.25")

    def test_the_statistic_is_the_mean_gap(self) -> None:
        """统计量是配对差的平均 —— 后一组减前一组。"""
        first = [Decimal(1), Decimal(2), Decimal(3)]
        second = [Decimal(10), Decimal(20), Decimal(30)]

        result = paired_permutation_test(first, second)

        assert result.statistic == Decimal(18)

    def test_a_consistently_cheaper_second_group_is_significant(self) -> None:
        """每一对都是后者便宜 —— 这种一致的方向不该被判成噪声。"""
        first = [Decimal(10)] * 8 + [Decimal(11)] * 4
        second = [Decimal(1)] * 12

        result = paired_permutation_test(first, second)

        assert result.p_value < Decimal("0.05")
        assert result.significant is True

    def test_the_detail_reports_the_paired_totals(self) -> None:
        """配对检验的附加上下文 —— 样本量、差值总和、稳健的中位数，都得能核对。"""
        first = [Decimal(1), Decimal(2)]
        second = [Decimal(4), Decimal(5)]

        result = paired_permutation_test(first, second)

        assert result.detail["pairs"] == 2
        assert result.detail["sum_difference"] == Decimal(6)
        assert result.detail["mean_difference"] == Decimal(3)
        assert result.detail["median_difference"] == Decimal(3)

    def test_a_large_sample_falls_back_to_monte_carlo(self) -> None:
        """样本大 —— 翻法太多，改抽样，并**说清是抽样**。"""
        first = [Decimal(index) for index in range(20)]
        second = [Decimal(index + 100) for index in range(20)]

        result = paired_permutation_test(first, second)

        assert result.method == "paired_permutation_monte_carlo"
        assert result.detail["exact"] is False

    def test_the_monte_carlo_fallback_is_reproducible(self) -> None:
        """**同样的种子，同样的结论** —— 否则这条检验没法复核。"""
        first = [Decimal(index) for index in range(20)]
        second = [Decimal(index + 100) for index in range(20)]

        first_run = paired_permutation_test(first, second)
        second_run = paired_permutation_test(first, second)

        assert first_run.p_value == second_run.p_value

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(ValueError, match="一样长"):
            paired_permutation_test([Decimal(1)], [Decimal(1), Decimal(2)])

    def test_an_empty_pair_set_is_refused(self) -> None:
        with pytest.raises(ValueError, match="不能是空的"):
            paired_permutation_test([], [])
