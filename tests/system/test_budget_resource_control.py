"""TASK-P3-015 —— 连续运行期间每日成本不超上限，超限时优雅停止而非崩溃。

**这是验收测试。** 验收条件是一条：

    **连续运行期间每日成本不超过 ``max_daily_cost``；超限时优雅停止而非崩
    溃。**

**"连续运行"是真的连着跑。** 下面那几轮不是各自建一本账 —— 它们共用**同一
本账**，所以"今天花了多少"是一个数，而不是每轮一个数各说各话。

**"优雅停止"是这一条里最要紧的半句。** 撞墙的时候抛异常会把正在跑的东西
掀掉；**停下来并把原因写进结论里**，才是"停下来"。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from core.metrics.cost import CostAmount
from core.metrics.cycle import CycleStatus
from core.policy.budget import Budget, BudgetMetric
from core.policy.resource import ResourceGate, ResourceUsage
from orchestration.continuous import ContinuousRunner

#: 测试用的固定时刻 —— 不取"现在"，否则断言会随跑的时刻变化。
START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def _cost(amount: str) -> CostAmount:
    """一笔钱。"""
    return CostAmount(amount=Decimal(amount), currency="USD")


def _gate(limit: str) -> ResourceGate:
    """搭一个闸门 —— **上限只有一个出处**。"""
    return ResourceGate(
        Budget(max_daily_cost=Decimal(limit)),
        scope="pilot",
        clock=lambda: START,
    )


def _spending(
    amount: str,
    *,
    tokens: int = 100,
    seen: list[str] | None = None,
) -> Callable[[str], Awaitable[ResourceUsage]]:
    """一轮的活 —— 干完把这一轮的用量报上来。

    :param seen: 收到轮次 id 的地方。**轮次 id 得有人看** —— 否则"这一轮是第
        几轮"就只能靠猜。
    """

    async def _run(cycle_id: str) -> ResourceUsage:
        if seen is not None:
            seen.append(cycle_id)
        return ResourceUsage(cost=_cost(amount), tokens=tokens)

    return _run


class TestTheContinuousRun:
    """连续运行 —— **日成本不超上限**。"""

    async def test_the_daily_cost_never_exceeds_the_limit(self) -> None:
        """**二十轮下来，今天花的钱不超过上限。**"""
        gate = _gate("5")
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        await runner.run(_spending("1"))

        assert await gate.daily_spend() <= Decimal("5")

    async def test_the_run_stops_gracefully(self) -> None:
        """**停下来的是一个结论，不是一个异常。**

        抛异常的话，这一趟跑完的东西全没了 —— 而"我们花到上限了"这件事本
        身就该被记下来。
        """
        gate = _gate("5")
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        result = await runner.run(_spending("1"))

        assert result.stopped is True
        assert "daily_cost" in result.reason

    async def test_the_run_stops_at_the_limit(self) -> None:
        """**该停就停 —— 不是跑满二十轮，也不是一轮都不跑。**"""
        gate = _gate("5")
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        result = await runner.run(_spending("1"))

        assert result.count == 5

    async def test_the_cycles_are_numbered(self) -> None:
        """**轮次 id 自己带着序号** —— 事后读记录时最常问的一句话。"""
        seen: list[str] = []
        gate = _gate("5")
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        await runner.run(_spending("1", seen=seen))

        assert seen == ["cycle-1", "cycle-2", "cycle-3", "cycle-4", "cycle-5"]

    async def test_the_stop_status_is_budget_exhausted(self) -> None:
        """**周期词汇表里早有这一档** —— 被预算掐断的周期跑完了但没变好。"""
        gate = _gate("1")
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        await runner.run(_spending("1"))
        admission = await gate.admit()

        assert admission.cycle_status is CycleStatus.BUDGET_EXHAUSTED

    async def test_the_rejected_attempt_is_kept(self) -> None:
        """**"我们想花超了"这件事要被问出来。**"""
        gate = _gate("2")
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        await runner.run(_spending("3"))

        blocked = await gate.blocked_attempts()
        assert len(blocked) == 1
        assert blocked[0].amount == Decimal("3")


class TestTheCycleThatOvershoots:
    """一轮花超了 —— **上限还是没破**。"""

    async def test_the_recorded_cost_stays_under_the_limit(self) -> None:
        """**记在账上的钱不超过上限。**

        这一条要说清楚：**上限拦的是记账，不是时间机器。** 这一轮的钱已经花
        掉了，拦是拦不回来的 —— 能拦的是"不把它记成正常花销"，以及"不再开
        新周期"。**账上的数因此不会超，而真实的花费可能超** —— 那个差额就
        是那条"被拒绝"的流水。
        """
        gate = _gate("5")
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        await runner.run(_spending("9"))

        assert await gate.daily_spend() <= Decimal("5")

    async def test_the_next_cycle_does_not_start(self) -> None:
        """**撞过墙就别再开新周期。**

        撞墙的那一笔**没有记进累计额**，所以只看余额会以为"还有余额"——
        **只看余额，系统会一轮一轮地撞下去。**
        """
        gate = _gate("5")
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        result = await runner.run(_spending("9"))

        assert result.count == 1
        assert result.stopped is True
        assert result.blocked is True


class TestTheDayRollsOver:
    """日切 —— **新的一天是新的一行累计额**。"""

    async def test_the_gate_opens_again_the_next_day(self) -> None:
        """**日切自动生效** —— 账本一条记录都不用删。"""
        moments = [START]

        def _clock() -> datetime:
            return moments[0]

        gate = ResourceGate(
            Budget(max_daily_cost=Decimal("1")),
            scope="pilot",
            clock=_clock,
        )
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        await runner.run(_spending("1"))

        moments[0] = START.replace(day=2)
        admission = await gate.admit()

        assert admission.allowed is True
        assert await gate.daily_spend() == Decimal("0")


class TestTheOtherDimensions:
    """R-018 —— **Budget 管 Token / 调用 / 资源 / 时间**。"""

    async def test_the_token_limit_is_enforced_too(self) -> None:
        """**token 也归闸门管** —— 钱没超不代表 token 没超。"""
        gate = ResourceGate(
            Budget(max_daily_cost=Decimal("100"), max_token_usage=500),
            scope="pilot",
            clock=lambda: START,
        )
        runner = ContinuousRunner(gate=gate, max_cycles=20)

        result = await runner.run(_spending("1", tokens=400))

        assert await gate.manager.spent(BudgetMetric.TOKEN_USAGE) == Decimal("400")
        assert result.count == 2
        assert result.blocked is True
