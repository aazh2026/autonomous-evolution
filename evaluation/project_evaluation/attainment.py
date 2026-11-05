"""项目演进层 —— 项目是否真的变好。

设计依据：docs/design.md §10（项目演进层：目标达成度、质量趋势、单位成本
收益）；docs/tasks.md TASK-P1-071。

**阶段一这里只做达成度差值。** §10 的项目演进层有三项：目标达成度、质量
趋势、单位成本收益。质量趋势需要跨周期的质量基线（TASK-P1-072 才产出），
单位成本收益需要每轮的成本与收益配对 —— 而收益归因（这一轮的改善有多少
来自这一轮）在阶段一没有可辩护的做法，**硬算出来的是噪声**。所以阶段一
只做差值，并把另外两项显式推迟到阶段四（见 docs/tasks.md 阶段二~四骨架）。

差值本身由 ``core.metrics.cycle`` 判定（``EvolutionCycle.attainment_delta``）：
缺前值或后值时它是 ``None``，不是 0 —— 缺数据不是"没变化"。这一层只是
把若干轮次的差值汇总起来。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from evaluation.common import MetricResult, MetricState

#: 达成度差值的指标 id。
ATTAINMENT_DELTA = "attainment_delta"


@dataclass(frozen=True)
class CycleAttainment:
    """一轮演进的达成度快照 —— 项目演进层指标的唯一输入。"""

    cycle_id: str
    #: 周期开始前的达成度；``None`` 表示没有可比的前值。
    attainment_before: Decimal | None
    #: 周期结束后的达成度；``None`` 表示没有可比的后值。
    attainment_after: Decimal | None
    #: 这一轮的结局（improved / no_change / regressed / stalled）。
    outcome: str = ""

    @property
    def delta(self) -> Decimal | None:
        """前后差值。缺任一快照就是 ``None`` —— 缺数据不是 0。"""
        if self.attainment_before is None or self.attainment_after is None:
            return None
        return self.attainment_after - self.attainment_before


def attainment_delta(cycles: Sequence[CycleAttainment]) -> MetricResult:
    """目标达成度差值 = 各轮差值的**平均**。

    取平均而不是求和：求和会随周期数量线性增长，看不出"每一轮平均好了
    多少"；而"每一轮平均好了多少"才是"这个系统是不是在让项目变好"的
    答案。

    只有**前后快照都齐**的轮次进平均 —— 缺基线的轮次差值算不出来，把它们
    当成 0 参与平均，等于宣称"那一轮没有任何改善"。

    ``detail`` 里带每轮的差值与结局分布 —— 均值会掩盖"一半变好一半变差"
    这种形状。
    """
    deltas = [item.delta for item in cycles]
    usable = [delta for delta in deltas if delta is not None]
    if not usable:
        return MetricResult(
            metric_id=ATTAINMENT_DELTA,
            state=MetricState.NO_SAMPLES,
            value=None,
            detail={
                "reason": "没有可比的前后达成度快照",
                "total_cycles": len(cycles),
                "comparable_cycles": 0,
            },
        )
    total = sum(usable, Decimal(0))
    outcomes: dict[str, int] = {}
    for item in cycles:
        if item.outcome:
            outcomes[item.outcome] = outcomes.get(item.outcome, 0) + 1
    return MetricResult(
        metric_id=ATTAINMENT_DELTA,
        state=MetricState.COMPUTED,
        value=total / Decimal(len(usable)),
        numerator=total,
        denominator=Decimal(len(usable)),
        detail={
            "total_cycles": len(cycles),
            "comparable_cycles": len(usable),
            "total_delta": total,
            "outcomes": outcomes,
        },
    )


__all__ = [
    "ATTAINMENT_DELTA",
    "CycleAttainment",
    "attainment_delta",
]
