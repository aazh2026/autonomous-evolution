"""连续运行 —— 一轮接一轮，预算撞墙就优雅停止。

设计依据：docs/tasks.md TASK-P3-015（验收条件"连续运行期间每日成本不超过
``max_daily_cost``；超限时优雅停止而非崩溃"；交付物：跨周期预算、成本归集、
并发限流）、TASK-P3-018（连续 10 周期长跑的前置件）、R-009（目标达成度）、
R-013（Budget 含日成本与并发上限，可配）。

**这一层是"怎么把闸门用起来"的那一小段。** 闸门（:mod:`core.policy.resource`）
只回答"能不能开跑"，**谁来一轮一轮地跑、跑完了怎么记、撞墙了怎么停**，在这
一层。

**停止是结论，不是异常。** 撞墙时抛异常会把正在跑的东西掀掉 —— 跑完的成果
没了，没跑完的也没了。**正确的做法是停下来，把原因写进结论里**，让调用方决
定是明天再来、还是改预算、还是先看看哪一轮花超了。

**每一轮的成本由这一轮自己报上来。** 驱动方只管干活，**记账在收尾处统一
做** —— 成本是"这一轮花了多少"的**结果**，事前谁也算不准，而事前估出来的一
个数只会让人以为算得准。

**撞墙的那一笔不算花出去，但账上要留痕。** 被拦下的钱没有记进累计额（累计额
只增不减），所以"今天还剩多少"不会虚高；但那条"被拒绝"的流水留着，它是
"系统想花超了"的证据。

**一轮撞墙了，这一轮仍然算跑过了。** 它确实跑了，也确实花了钱（只是没记进
账）—— **把它从记录里抹掉，等于把一次超支尝试从历史里删掉。**
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from core.policy.resource import ResourceGate, ResourceUsage

#: 轮次 id 的前缀。**轮次 id 本身带信息** —— 第几轮，从记录里一眼看得出来。
CYCLE_ID_PREFIX = "cycle-"


@dataclass(frozen=True, slots=True)
class ContinuousRun:
    """连续运行的结论。

    :param cycles: 跑过的周期 id，**按跑的次序**。
    :param stopped: 是不是**因为预算**停下来的。``False`` 表示跑满了轮数上
        限 —— 那不是被拦，是跑完了。
    :param reason: 为什么停。**说给人听的一句话。**
    :param total: 这一趟记在账上的钱。**撞墙的那一笔不在里面** —— 它没记进
        账；想知道它多大，看 :attr:`blocked`。
    :param blocked: 有没有哪一轮被预算拦下过。

    **"跑满了轮数"与"被预算拦下"是两件事。** 前者是任务做完，后者是资源到
    头 —— 补救动作完全不同：一个是收工，一个是要么改预算要么改优先级。
    """

    cycles: tuple[str, ...] = ()
    stopped: bool = False
    reason: str = ""
    total: Decimal = Decimal(0)
    blocked: bool = False

    @property
    def count(self) -> int:
        """跑了几轮。"""
        return len(self.cycles)


@dataclass(frozen=True, slots=True)
class ContinuousRunner:
    """连续跑若干轮周期 —— **预算撞墙就优雅停止**。

    :param gate: 资源闸门。**所有轮次记的是同一本账** —— 这就是"跨周期"。
    :param max_cycles: 最多跑几轮。**这是"连续"的另一个尽头** —— 没有它，
        预算充足时就会一直跑下去。

    **轮次 id 是编出来的，但编得有规矩。** 第几轮从 id 里直接看得出来
    （``cycle-1``、``cycle-2``……）—— 事后读记录时，"这是第几轮"是最常问的
    一个问题。
    """

    gate: ResourceGate
    max_cycles: int = field(default=10)
    cycle_ids: Sequence[str] = field(default_factory=tuple)

    async def run(self, cycle: Callable[[str], Awaitable[ResourceUsage]]) -> ContinuousRun:
        """一轮接一轮地跑。

        :param cycle: 跑一轮的活。**收尾时把这一轮的用量报上来** —— 它报什
            么，账上就记什么；**驱动方不要自己往闸门里记**，否则同一笔钱会
            被记两遍。
        :returns: 这一趟的结论 —— 跑了几轮、为什么停、记了多少。
        """
        cycles: list[str] = []
        total = Decimal(0)
        blocked = False
        for cycle_id in self._ids():
            admission = await self.gate.admit()
            if not admission.allowed:
                return ContinuousRun(
                    cycles=tuple(cycles),
                    stopped=True,
                    reason=admission.reason,
                    total=total,
                    blocked=blocked,
                )
            usage = await cycle(cycle_id)
            outcome = await self.gate.charge(usage, cycle_id=cycle_id)
            cycles.append(cycle_id)
            total += await self.gate.cycle_spend(cycle_id)
            blocked = blocked or outcome.blocked
        return ContinuousRun(
            cycles=tuple(cycles),
            stopped=False,
            reason=f"跑满了 {self.max_cycles} 轮",
            total=total,
            blocked=blocked,
        )

    def _ids(self) -> tuple[str, ...]:
        """这一趟的轮次 id。"""
        if self.cycle_ids:
            return tuple(self.cycle_ids)
        return tuple(f"{CYCLE_ID_PREFIX}{index + 1}" for index in range(self.max_cycles))


__all__ = [
    "CYCLE_ID_PREFIX",
    "ContinuousRun",
    "ContinuousRunner",
]
