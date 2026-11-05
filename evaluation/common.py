"""指标层的公共词汇。

设计依据：docs/design.md §10（四级指标体系）；docs/tasks.md TASK-P1-071。

这一层只定义**说清楚话**所需要的东西：指标属于哪一层、数据够不够算、算出
的数是多少。具体的算法在各自的子包里。

**为什么"算不出来"必须是一种独立的状态。** 一个指标在数据不足时返回 0，
和"这个指标算出来是 0"在报表上是**一模一样**的 —— 而这两件事的含义完全
相反。前者是"还不知道"，后者是"确实为零"。所以这里有三种状态：

- ``computed`` —— 算出来了；
- ``no_samples`` —— 一个样本都没有，没有可说的；
- ``baseline_missing`` —— 有样本，但缺 ground truth 或基线，**算不出来**。

TASK-P1-014 立下的规矩在这里继续生效：**不可判定 ≠ 未触发**。缺基线时
报"基线未建立"，不报 0。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class MetricError(ValueError):
    """指标计算出错。"""


class MetricLayer(StrEnum):
    """指标层 —— design.md §10 的四级。

    层级不是分类学上的偏好，而是**评价目的**上的分工：每一层回答一个不同
    的问题。把它压平就会得出一个"综合分"，而综合分无法回答"到底哪一环
    出了问题"。
    """

    #: 自主发现 —— 能否找到真实问题。
    DISCOVERY = "discovery"
    #: 决策质量 —— 能否选对问题。
    DECISION = "decision"
    #: 工程交付 —— 能否正确交付。
    DELIVERY = "delivery"
    #: 项目演进 —— 项目是否真正变好。
    EVOLUTION = "evolution"


class MetricState(StrEnum):
    """一次计算的状态。

    三态而不是两态，是因为"有样本但缺基线"与"连样本都没有"要分开报 ——
    前者缺的是**标注**，后者缺的是**系统还没产出东西**，两者的处置方式
    完全不同（一个去补标注，一个去跑一轮）。
    """

    COMPUTED = "computed"
    NO_SAMPLES = "no_samples"
    BASELINE_MISSING = "baseline_missing"


class Verdict(StrEnum):
    """人工标注的结论 —— ground truth。

    标注是**人工**提供的（见 TASK-P1-083）：系统不能自己给自己打分。
    """

    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class MetricResult:
    """一次指标计算的结果。

    **不只是一个数。** 只给一个数，读者无从判断它可不可信 —— 分母是 3 还是
    3000，用的是哪一层的数据，有没有 ground truth。所以分子、分母、样本量
    与附加上下文都留着，让每个结论都可复核。

    ``value`` 在 ``computed`` 之外的状态下为 ``None`` —— **没有值**和**值是
    0** 是两回事，字段类型把它们区分开。
    """

    metric_id: str
    state: MetricState
    value: Decimal | None
    numerator: Decimal | None = None
    denominator: Decimal | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    @property
    def is_computed(self) -> bool:
        """是否真的算出了数。"""
        return self.state is MetricState.COMPUTED and self.value is not None

    @property
    def sample_size(self) -> int | None:
        """样本量 —— 分母。"""
        if self.denominator is None:
            return None
        return int(self.denominator)


def rate(
    metric_id: str,
    numerator: int,
    denominator: int,
    *,
    empty_state: MetricState = MetricState.NO_SAMPLES,
    detail: Mapping[str, object] | None = None,
) -> MetricResult:
    """算一个比率型指标。

    分母为 0 时**不返回 0** —— 返回 ``value=None`` 与 ``empty_state``。
    "一个样本都没有"和"比例恰好是 0"在报表上长得一样，但含义相反。
    """
    extra = dict(detail or {})
    if denominator == 0:
        return MetricResult(
            metric_id=metric_id,
            state=empty_state,
            value=None,
            numerator=Decimal(numerator),
            denominator=Decimal(0),
            detail=extra,
        )
    return MetricResult(
        metric_id=metric_id,
        state=MetricState.COMPUTED,
        value=Decimal(numerator) / Decimal(denominator),
        numerator=Decimal(numerator),
        denominator=Decimal(denominator),
        detail=extra,
    )


__all__ = [
    "MetricError",
    "MetricLayer",
    "MetricResult",
    "MetricState",
    "Verdict",
    "rate",
]
