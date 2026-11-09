"""执行成本的记账与观测 —— **一次换算，三处同源**。

设计依据：docs/tasks.md TASK-P1-035（T1 ① 同一次执行在 Adapter 报告、预算
账本、OTel span 属性三处的成本数值相等；T1 ② 换算单价变更后，三处同步变化）、
I-13（三处成本必须同源同币种）。

**它只共享，不换算。** 换算发生在适配器那边 —— 只有那里知道这次花了多少、
是什么币种。换出来的那一个 :class:`~core.metrics.cost.CostAmount` 原样进了
账本与观测，于是"三处相等"不是三处碰巧算出了同一个数，而是三处拿的是同一个
数。**三处各算一遍的话，换算规则改一次就要改三处，而漏掉的那一处不会报错**
—— 它只会安静地记下另一个数，而两个数都是"看起来正常的钱"。

**观测先记，账本后记。** 钱已经花了 —— 预算超了不该让这次花费从观测里
消失。账本那一步会自己留下"被拒绝"的流水（金额与币种照旧），所以三处的
数字仍然一样。

**超预算不抛异常。** 这次执行的活已经干完了，把它变成失败等于丢掉已经做好的
工作，而重做一遍更贵。该拦的是"下一轮要不要继续"，那件事由预算检查去做；
账本里那条"被拒绝"的流水就是超预算的证据。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.metrics.cost import CostAmount, CostObserver
from core.policy.budget import BudgetExhausted, BudgetManager, BudgetMetric


@dataclass(frozen=True, slots=True)
class CostAccounting:
    """一次成本记账的结果。"""

    #: 记下去的那一笔。
    cost: CostAmount
    #: 账本有没有真的记下这笔钱。``False`` 表示被预算拦下了。
    recorded: bool


@dataclass(frozen=True, slots=True)
class ExecutionCostAccountant:
    """把一次执行的成本记到账本与观测里。

    **它只共享，不换算。** 见模块文档。

    :param budget: 预算账本。**它的币种就是记账币种** —— 账本那一侧会核
        币种，对不上就报错。
    :param observer: 观测口（OTel span / Langfuse 都实现了同一个端口）。
    :param metric: 记到哪个预算维度。默认是生命周期累计的成本。
    """

    budget: BudgetManager
    observer: CostObserver
    metric: BudgetMetric = BudgetMetric.TOTAL_COST

    async def record(
        self,
        cost: CostAmount,
        *,
        task_id: str,
        session_id: str,
    ) -> CostAccounting:
        """记一次执行的成本。

        :param cost: **已经换算好的**金额与币种。
        :param task_id: 这次执行属于哪个任务。
        :param session_id: 这次执行属于哪一次会话。
        """
        await self.observer.observe(cost, task_id=task_id, session_id=session_id)
        try:
            await self.budget.consume_cost(self.metric, cost)
        except BudgetExhausted:
            # 账本自己已经留下了一条"被拒绝"的流水 —— 那条流水就是超预算的
            # 证据。这里不把它当成失败：钱已经花了。
            return CostAccounting(cost=cost, recorded=False)
        return CostAccounting(cost=cost, recorded=True)


__all__ = [
    "CostAccounting",
    "ExecutionCostAccountant",
]
