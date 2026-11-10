"""精确统计检验 —— 样本很少时唯一站得住的那一套。

设计依据：docs/tasks.md TASK-P2-018（"按统一口径产出成功率、成本、耗时对比看
板，**验证统计显著性差异**"）；I-17（多执行器结果可比性口径需前置设计，否则
阶段二验收无法判定）。

**样本小到不能用渐近近似。** 每个执行器在标准任务集上跑三次，9 个任务就是 27
次运行 —— 但两两比较时每一组只有几十个计数。这种样本量下正态近似是**不可信
的**：而浮点数的舍入在这种量级上足以把 p 值从"显著"推到"不显著"，让结论随
机。所以中间全用 :class:`fractions.Fraction` 精确计算，只在最后展示的那一步
落到 :class:`~decimal.Decimal`。

**为什么不用 scipy。** 依赖越少，结论越不会被"某个库的版本变了"污染；而 Fisher
精确检验的数学只需要一行组合数。

**三条检验，各管一件事：**

- :func:`fisher_exact` —— **成功率**这类计数。两个执行器的成功/失败构成一张
  2×2 表，精确检验问的是"在'成功率与执行器无关'这个假设下，看到这么极端的表
  有多难"。
- :func:`permutation_test` —— **成本与耗时**这类连续量。把两组的数混在一起重
  新分组，看观测到的差距有多少种分法排得上。**这是唯一在 n 小时也不失效的
  方法** —— 它不假设分布形状。
- :func:`holm_adjust` —— **多重比较**。四个执行器两两比是 6 对，四项指标就是
  24 次检验；不校正的话，"至少有一次显著"几乎必然发生，那不是发现，是噪声。

**"样本不够"必须能被说出来。** :attr:`TestResult.method` 会说清这次检验是精确
枚举还是蒙特卡洛，:attr:`TestResult.detail` 带着分辨率上限 —— 所以"没测出差异"
与"测不动"在报表上是两件事。
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from itertools import combinations

#: 默认显著性水平。
ALPHA: Decimal = Decimal("0.05")

#: 精确枚举的上限 —— 超过这个组合数就改用蒙特卡洛。
MAX_EXACT_PERMUTATIONS: int = 200_000

#: 蒙特卡洛的抽样次数。
MONTE_CARLO_DRAWS: int = 10_000

#: 蒙特卡洛的随机种子。**固定它，结论才可复现** —— 换种子就换结论的评测没有
#: 意义。
DEFAULT_SEED: int = 20261110

#: Fisher 检验要的表的大小。
_TABLE_SIZE: int = 2


class Alternative(StrEnum):
    """备择假设的方向。

    **默认是双侧的。** "执行器 A 比 B 好"这种单侧结论只在**事先**就定好了
    方向时才成立；事后看数据再挑方向，那叫挑数据。
    """

    #: 有差异（不分方向）。
    TWO_SIDED = "two-sided"
    #: 前者更大。
    GREATER = "greater"
    #: 前者更小。
    LESS = "less"


@dataclass(frozen=True, slots=True)
class StatisticsOptions:
    """一次检验的设置。

    **打包成对象而不是散成一堆关键字参数**，是因为参数一多就没人记得住顺序
    —— 而这里每一个都影响结论。
    """

    #: 备择假设。
    alternative: Alternative = Alternative.TWO_SIDED
    #: 显著性水平。
    alpha: Decimal = ALPHA
    #: 报告里的标签。
    label: str = ""
    #: 蒙特卡洛的随机种子。
    seed: int = DEFAULT_SEED
    #: 精确枚举的组合数上限。
    max_exact: int = MAX_EXACT_PERMUTATIONS
    #: 蒙特卡洛的抽样次数。
    draws: int = MONTE_CARLO_DRAWS


#: 默认的检验设置 —— 模块级单例，因为 :class:`StatisticsOptions` 是不可变的。
DEFAULT_OPTIONS: StatisticsOptions = StatisticsOptions()


@dataclass(frozen=True, slots=True)
class TestResult:
    """一次检验的结论。

    **不只是一个 p 值。** p 值不说"差多少"、不说"怎么算的"、也不说"样本有
    多小"。所以统计量、方法名、样本量与分辨率都在这儿 —— 一个只写"p=0.03"
    的看板，谁也复核不了。
    """

    #: 检验的名字（"success_rate" / "cost_median" 之类）。
    name: str
    #: 统计量的值。
    statistic: Decimal
    #: p 值。
    p_value: Decimal
    #: 怎么算的 —— "fisher_exact" / "permutation_exact" / "permutation_monte_carlo"。
    method: str
    #: 备择假设。
    alternative: Alternative
    #: 显著性水平。
    alpha: Decimal
    #: 样本量。
    sample_size: int
    #: 附加上下文 —— 分组计数、分辨率上限等。
    detail: Mapping[str, object] = field(default_factory=dict)

    @property
    def significant(self) -> bool:
        """在给定显著性水平下是否显著。"""
        return self.p_value <= self.alpha


def _to_decimal(value: Fraction) -> Decimal:
    """把精确有理数落成 :class:`~decimal.Decimal`。

    走 ``Decimal`` 而不是 ``float``：报表上的数要能被人核对，而浮点数打印出来
    是一串十六进制尾巴。
    """
    return Decimal(value.numerator) / Decimal(value.denominator)


def _median(values: Sequence[Fraction]) -> Fraction:
    """中位数 —— 偶数个取中间两个的平均。

    **用精确有理数算。** 中间两个数的平均可能除不尽，而一个差一点点中位数
    会把"两组没差别"变成"两组有差别" —— 那正是这份报表要回答的问题。
    """
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _hypergeometric(col_success: int, col_failure: int, row_total: int, x: int) -> Fraction:
    """超几何分布的概率 —— 固定边际下的表出现概率。

    ``x`` 是"A 组成功"的计数。整行整列的边际固定时，任何一张表的概率就是
    这么多个组合数之比。
    """
    total = col_success + col_failure
    numerator = math.comb(col_success, x) * math.comb(col_failure, row_total - x)
    return Fraction(numerator, math.comb(total, row_total))


def fisher_exact(
    table: Sequence[Sequence[int]],
    options: StatisticsOptions = DEFAULT_OPTIONS,
) -> TestResult:
    """2×2 表的 Fisher 精确检验。

    表的读法::

        ┌──────────┬──────┐
        │          │ 成功 │ 失败
        ├──────────┼──────┤
        │ A（前）  │  a   │  b
        │ B（后）  │  c   │  d
        └──────────┴──────┘

    **为什么是精确检验而不是卡方。** 卡方近似在期望频数小于 5 时失效得很难看 ——
    而"9 个任务跑 3 次"正好落在这个区间里。精确检验没有这个前提。

    **双侧 p 值怎么来的。** 不是"取双尾各一半"那种近似，而是把**所有**边际
    相同的表都算一遍，取概率不超过观测表的那几张的概率之和 —— 这才是 Fisher
    双侧检验的定义。
    """
    if len(table) != _TABLE_SIZE or any(len(row) != _TABLE_SIZE for row in table):
        raise ValueError("Fisher 检验要的是一张 2×2 表")
    (a, b), (c, d) = (tuple(table[0]), tuple(table[1]))
    if min(a, b, c, d) < 0:
        raise ValueError("计数不能为负")
    total = a + b + c + d
    if total == 0:
        raise ValueError("表里一个样本都没有，检验无从做起")

    row_first = a + b
    col_success = a + c
    col_failure = b + d
    low = max(0, row_first - col_failure)
    high = min(row_first, col_success)

    observed = _hypergeometric(col_success, col_failure, row_first, a)
    if options.alternative is Alternative.TWO_SIDED:
        p_value = Fraction(0)
        for x in range(low, high + 1):
            probability = _hypergeometric(col_success, col_failure, row_first, x)
            if probability <= observed:
                p_value += probability
    elif options.alternative is Alternative.GREATER:
        # "前者更大" —— 零假设下，第一行的成功数**不少于**观测值的概率。
        p_value = sum(
            (_hypergeometric(col_success, col_failure, row_first, x) for x in range(a, high + 1)),
            Fraction(0),
        )
    else:
        # "前者更小" —— 第一行的成功数**不多于**观测值的概率。
        p_value = sum(
            (_hypergeometric(col_success, col_failure, row_first, x) for x in range(low, a + 1)),
            Fraction(0),
        )

    statistic = _rate_gap(a, row_first, c, c + d)
    return TestResult(
        name=options.label or "fisher_exact",
        statistic=statistic,
        p_value=_to_decimal(p_value),
        method="fisher_exact",
        alternative=options.alternative,
        alpha=options.alpha,
        sample_size=total,
        detail={
            "table": {"a": a, "b": b, "c": c, "d": d},
            "row_first": row_first,
            "row_second": c + d,
            "col_success": col_success,
            "exact": True,
        },
    )


def _rate_gap(success: int, total: int, other_success: int, other_total: int) -> Decimal:
    """两个率的差 —— 前者减后者。

    分母为 0 时这一列没有率可言，差记作 0 并由调用方在 ``detail`` 里说明 ——
    **不能拿 0 当"两边一样"**。
    """
    if total == 0 or other_total == 0:
        return Decimal(0)
    return _to_decimal(Fraction(success, total) - Fraction(other_success, other_total))


def compare_success_rates(
    success_first: int,
    total_first: int,
    success_second: int,
    total_second: int,
    options: StatisticsOptions = DEFAULT_OPTIONS,
) -> TestResult:
    """两个成功率的可比性检验 —— 直接喂计数。

    **失败的运行不进分母，"没跑起来"的运行也不进。** 这一点由调用方保证：把
    "没跑起来"算成失败，等于把执行器没装好这件事算成它能力差。
    """
    if min(success_first, total_first, success_second, total_second) < 0:
        raise ValueError("计数不能为负")
    if success_first > total_first or success_second > total_second:
        raise ValueError("成功数不能超过总数")
    return fisher_exact(
        (
            (success_first, total_first - success_first),
            (success_second, total_second - success_second),
        ),
        options,
    )


def _extreme(difference: Fraction, observed: Fraction, alternative: Alternative) -> bool:
    """某个重分法的差距算不算"跟观测到的一样极端"。"""
    if alternative is Alternative.TWO_SIDED:
        return abs(difference) >= abs(observed)
    if alternative is Alternative.GREATER:
        return difference >= observed
    return difference <= observed


def _split_median_difference(pooled: Sequence[Fraction], chosen: Sequence[int]) -> Fraction:
    """按某种分法算两组的 median 差 —— 后一组减前一组。"""
    picked = set(chosen)
    first = [pooled[index] for index in chosen]
    second = [value for index, value in enumerate(pooled) if index not in picked]
    return _median(second) - _median(first)


def permutation_test(
    group_first: Sequence[Decimal],
    group_second: Sequence[Decimal],
    options: StatisticsOptions = DEFAULT_OPTIONS,
) -> TestResult:
    """中位数的精确置换检验。

    **为什么是置换检验而不是 t 检验。** 成本与耗时的分布**不是正态的** —— 一次
    卡死的运行能把均值拖走一大截，而中位数不受影响。而 t 检验的正态前提在这个
    样本量下根本没法验证。置换检验只问一个换问题：**如果两组其实没有差别，
    观测到的这个差距有多罕见？** 它对分布形状不作任何假设。

    **统计量取中位数之差**（后一组减前一组），与 :func:`fisher_exact` 的约定
    一致：正数意味着第二组更大。

    **样本够小就精确枚举，够大就蒙特卡洛。** 组合数不超过
    :attr:`StatisticsOptions.max_exact` 时把每一种分法都算一遍（精确）；否则抽
    :attr:`StatisticsOptions.draws` 次（蒙特卡洛，种子固定）。**方法名会写明是哪
    一种** —— 把蒙特卡洛说成"精确检验"就是在骗人。
    """
    if not group_first or not group_second:
        raise ValueError("两组都不能是空的")
    first = [Fraction(value) for value in group_first]
    second = [Fraction(value) for value in group_second]
    pooled = first + second
    size_first = len(first)
    observed = _median(second) - _median(first)

    total_combinations = math.comb(len(pooled), size_first)
    if total_combinations <= options.max_exact:
        extreme = 0
        examined = 0
        for chosen in combinations(range(len(pooled)), size_first):
            examined += 1
            if _extreme(_split_median_difference(pooled, chosen), observed, options.alternative):
                extreme += 1
        p_value = Fraction(extreme, examined)
        method = "permutation_exact"
    else:
        generator = random.Random(options.seed)
        extreme = 0
        examined = options.draws
        for _ in range(options.draws):
            drawn = generator.sample(range(len(pooled)), size_first)
            if _extreme(_split_median_difference(pooled, drawn), observed, options.alternative):
                extreme += 1
        # +1 修正：蒙特卡洛不该报出 p=0 —— 那是"没抽到"，不是"不可能"。
        p_value = Fraction(extreme + 1, examined + 1)
        method = "permutation_monte_carlo"

    resolution = Fraction(1, examined) if examined else Fraction(1)
    return TestResult(
        name=options.label or "permutation_test",
        statistic=_to_decimal(observed),
        p_value=_to_decimal(p_value),
        method=method,
        alternative=options.alternative,
        alpha=options.alpha,
        sample_size=len(pooled),
        detail={
            "size_first": size_first,
            "size_second": len(second),
            "combinations": total_combinations,
            "permutations": examined,
            "exact": method == "permutation_exact",
            "resolution": _to_decimal(resolution),
        },
    )


def holm_adjust(p_values: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """Holm 步降法校正多重比较。

    **为什么必须校正。** 24 次检验里即使全都不显著，按 5% 的水平也至少有
    一次"显著"的概率是 1 − 0.95²⁴ ≈ **70%**。不校正的看板会把噪声当成发现。

    步降法（Bonferroni 的逐步加强版）比 Bonferroni 更有功效：把 p 值排序后，
    第 i 个乘以 ``m - i``，并且**取累计最大值** —— 保证校正后的 p 值仍然单调，
    而且任何一个"显著"都要有它前面那些也显著。
    """
    count = len(p_values)
    if count == 0:
        return ()
    order = sorted(range(count), key=lambda index: p_values[index])
    adjusted = [Decimal(0)] * count
    running = Decimal(0)
    for rank, index in enumerate(order):
        scaled = p_values[index] * Decimal(count - rank)
        running = max(running, scaled)
        adjusted[index] = min(running, Decimal(1))
    return tuple(adjusted)


def _mcnemar_p_value(
    *,
    only_first: int,
    only_second: int,
    discordant: int,
    alternative: Alternative,
) -> Fraction:
    """McNemar 检验的 p 值 —— 只看不一致的那几对。

    **二项分布的精确算法。** 零假设下"谁对谁错是等可能的"，于是不一致的对里
    前者赢的次数服从 ``Binomial(discordant, 0.5)``。

    **双侧不是"取两半"。** 双侧的定义是"所有概率不超过观测值那一张表的概率之
    和" —— 与 Fisher 双侧检验同一套做法。

    **单侧要按方向算。** "前者更好"只看上尾，"后者更好"只看下尾 —— 事后挑一
    个方向再算 p 值，那叫挑数据。
    """
    if discordant == 0:
        return Fraction(1)
    if alternative is Alternative.GREATER:
        return sum(
            (
                Fraction(math.comb(discordant, count), 2**discordant)
                for count in range(only_first, discordant + 1)
            ),
            Fraction(0),
        )
    if alternative is Alternative.LESS:
        return sum(
            (
                Fraction(math.comb(discordant, count), 2**discordant)
                for count in range(only_first + 1)
            ),
            Fraction(0),
        )
    observed = min(only_first, only_second)
    observed_probability = Fraction(math.comb(discordant, observed), 2**discordant)
    return sum(
        (
            Fraction(math.comb(discordant, count), 2**discordant)
            for count in range(discordant + 1)
            if Fraction(math.comb(discordant, count), 2**discordant) <= observed_probability
        ),
        Fraction(0),
    )


def mcnemar_exact(
    first: Sequence[bool],
    second: Sequence[bool],
    options: StatisticsOptions = DEFAULT_OPTIONS,
) -> TestResult:
    """配对二值结果的 McNemar 精确检验。

    **为什么必须是配对的。** 两个策略跑的是**同一批任务** —— 任务的难易在两
    组之间是共有的。不配对地比两个成功率，等于把"这批任务难"算成"这个策略
    差"。配对检验只看**不一致的那几对**：一个对了另一个错了的那些。

    **只有不一致的对才进检验。** 两边都对、两边都错的对子，对"谁更好"这个
    问题一句话也说不出来 —— 把它们算进去只会稀释信号。

    **零假设**：在不一致的那些对里，谁对谁错是等可能的（也就是两个策略一样
    好）。**p 值**由二项分布算出："如果真是五五开，抽到这么极端的分法有多难"。

    **一个不一致的对都没有时，p = 1** —— 不是"没法做"，而是"**两个策略在这
    批任务上完全一样**"。这与"测不动"是两回事，所以 :attr:`TestResult.detail`
    里写着不一致的对数。

    :param first: 前一个策略在每个任务上的结果。
    :param second: 后一个策略在每个任务上的结果。
    """
    if len(first) != len(second):
        raise ValueError("配对检验要两组一样长的结果")
    total = len(first)
    if total == 0:
        raise ValueError("一个样本都没有，检验无从做起")
    only_first = sum(1 for left, right in zip(first, second, strict=True) if left and not right)
    only_second = sum(1 for left, right in zip(first, second, strict=True) if right and not left)
    discordant = only_first + only_second

    if discordant == 0:
        return TestResult(
            name=options.label or "mcnemar_exact",
            statistic=Decimal(0),
            p_value=Decimal(1),
            method="mcnemar_exact",
            alternative=options.alternative,
            alpha=options.alpha,
            sample_size=total,
            detail={
                "only_first": only_first,
                "only_second": only_second,
                "discordant": 0,
                "pairs": total,
                "exact": True,
                "note": "两个策略在这批任务上完全一样 —— 没有一个不一致的对",
            },
        )

    p_value = _mcnemar_p_value(
        only_first=only_first,
        only_second=only_second,
        discordant=discordant,
        alternative=options.alternative,
    )

    statistic = _to_decimal(
        Fraction(only_first - only_second, total),
    )
    return TestResult(
        name=options.label or "mcnemar_exact",
        statistic=statistic,
        p_value=_to_decimal(p_value),
        method="mcnemar_exact",
        alternative=options.alternative,
        alpha=options.alpha,
        sample_size=total,
        detail={
            "only_first": only_first,
            "only_second": only_second,
            "discordant": discordant,
            "pairs": total,
            "exact": True,
        },
    )


def paired_permutation_test(
    first: Sequence[Decimal],
    second: Sequence[Decimal],
    options: StatisticsOptions = DEFAULT_OPTIONS,
) -> TestResult:
    """配对连续量的符号翻转置换检验。

    **为什么是配对的。** 与 :func:`mcnemar_exact` 同理 —— 同一批任务的难易在
    两组之间是共有的，不配对地比，等于把"这批任务贵"算成"这个策略贵"。

    **怎么换。** 零假设是"每一对里谁大谁小是随机的"。于是把每一对的**差**随机
    翻正负，翻出来的平均差就是一次重新分组。**观测到的平均差有多罕见**，就是
    p 值。

    **统计量取平均差**（后一组减前一组），与 :func:`permutation_test` 同一套
    方向约定：正数意味着后一组更大。备择假设也因此按"后一组更大"读
    （:attr:`Alternative.GREATER`）。

    **为什么配对用平均、不配对用中位数。** 不配对的时候，两组的**水平**是比
    较的对象，而成本水平是重尾的 —— 一次卡死的运行能把均值拖走一大截，所以
    取中位数。配对的时候，任务的难易被**差**掉了，剩下的"每个任务上省了多
    少"就是要估的那个效应；中位数在这里会**失去分辨力**：若每一对的差都是同
    一个数（如每个任务都省 0.1 元），翻正负之后中位数不是 0 就是那个数本身，
    "极端"的翻法占了七成多，于是明明有差别却报出 p ≈ 0.77 —— 那不是检验，是
    噪声。

    **中位数差仍会记在 :attr:`TestResult.detail` 里** —— 描述性的数要稳健，
    检验的统计量要有分辨力，两者分开记。

    **样本够小就精确枚举**（2ⁿ 种翻法），**够大就蒙特卡洛**（种子固定）——
    方法名会写明是哪一种。
    """
    if len(first) != len(second):
        raise ValueError("配对检验要两组一样长的数")
    if not first:
        raise ValueError("两组都不能是空的")
    differences = [
        Fraction(right) - Fraction(left) for left, right in zip(first, second, strict=True)
    ]
    difference_sum = sum(differences, Fraction(0))
    observed = difference_sum / len(differences)

    total_flips = 2 ** len(differences)
    if total_flips <= options.max_exact:
        extreme = 0
        examined = total_flips
        for mask in range(total_flips):
            flipped = [
                value if mask >> index & 1 else -value for index, value in enumerate(differences)
            ]
            if _extreme(sum(flipped, Fraction(0)) / len(flipped), observed, options.alternative):
                extreme += 1
        p_value = Fraction(extreme, examined)
        method = "paired_permutation_exact"
    else:
        generator = random.Random(options.seed)
        extreme = 0
        examined = options.draws
        for _ in range(options.draws):
            flipped = [value if generator.getrandbits(1) else -value for value in differences]
            if _extreme(sum(flipped, Fraction(0)) / len(flipped), observed, options.alternative):
                extreme += 1
        # +1 修正：蒙特卡洛不该报出 p=0 —— 那是"没抽到"，不是"不可能"。
        p_value = Fraction(extreme + 1, examined + 1)
        method = "paired_permutation_monte_carlo"

    return TestResult(
        name=options.label or "paired_permutation_test",
        statistic=_to_decimal(observed),
        p_value=_to_decimal(p_value),
        method=method,
        alternative=options.alternative,
        alpha=options.alpha,
        sample_size=len(differences),
        detail={
            "pairs": len(differences),
            "sum_difference": _to_decimal(difference_sum),
            "mean_difference": _to_decimal(observed),
            "median_difference": _to_decimal(_median(differences)),
            "flips": total_flips,
            "permutations": examined,
            "exact": method == "paired_permutation_exact",
            "resolution": _to_decimal(Fraction(1, examined) if examined else Fraction(1)),
        },
    )


__all__ = [
    "ALPHA",
    "DEFAULT_OPTIONS",
    "DEFAULT_SEED",
    "MAX_EXACT_PERMUTATIONS",
    "MONTE_CARLO_DRAWS",
    "Alternative",
    "StatisticsOptions",
    "TestResult",
    "compare_success_rates",
    "fisher_exact",
    "holm_adjust",
    "mcnemar_exact",
    "paired_permutation_test",
    "permutation_test",
]
