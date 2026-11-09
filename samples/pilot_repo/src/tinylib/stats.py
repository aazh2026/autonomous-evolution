"""统计小工具。

这一模块里有几处**故意留下的可改点** —— 它们是试点仓库的"真实缺陷"来源，
不是笔误。
"""

from __future__ import annotations

from collections.abc import Sequence


def mean(values: Sequence[float]) -> float:
    """算术平均。空序列没有平均值。"""
    if not values:
        raise ValueError("空序列没有平均值")
    return sum(values) / len(values)


def median(values: Sequence[float]) -> float:
    """中位数。

    偶数个样本取中间两个的平均 —— 取下中位数会让一半样本落在"中位数"之上，
    那样"一半的样本比它小"这句话就不成立了。
    """
    if not values:
        raise ValueError("空序列没有中位数")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def percentile(values: Sequence[float], p: float) -> float:
    """百分位数 —— ``p`` 取 0~100。

    用**线性插值**（与 numpy 的默认口径一致）：位置 ``p/100 * (n-1)``，
    两端之间的样本按小数部分插值。
    """
    if not values:
        raise ValueError("空序列没有百分位数")
    if not 0 <= p <= 100:
        raise ValueError(f"百分位必须在 0 到 100 之间：{p}")
    ordered = sorted(values)
    if p == 0:
        return ordered[0]
    position = p / 100 * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def moving_average(values: Sequence[float], window: int) -> list[float]:
    """滑动平均。

    窗口超出序列长度时返回空序列 —— 没有完整的窗口就没有平均值。
    """
    if window <= 0:
        raise ValueError(f"窗口必须是正数：{window}")
    if window > len(values):
        return []
    return [mean(values[index : index + window]) for index in range(len(values) - window + 1)]


def variance(values: Sequence[float]) -> float:
    """样本方差（除以 ``n-1``）。"""
    if len(values) < 2:
        raise ValueError("样本方差至少要两个样本")
    average = mean(values)
    return sum((value - average) ** 2 for value in values) / (len(values) - 1)


__all__ = ["mean", "median", "moving_average", "percentile", "variance"]
