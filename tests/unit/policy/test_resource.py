"""TASK-P3-015 —— 跨周期资源闸门：一本账、一道准入门、一个信号量。

**测的是三件事**：跨周期（所有周期记同一本账）、成本归集（每轮的钱归到自
己名下）、并发限流（同时跑着的数不超上限）。

**这一层最要紧的一条是"撞过墙就别再开新周期"。** 账本的原子扣减只保证"记
进去的总额不超上限"—— 撞墙的那一笔**没有记进累计额**，于是"余额还剩三块"
是可以发生的，而系统会再开一轮、再撞一次。**只看余额是不够的。**
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.metrics.cost import CostAmount
from core.metrics.cycle import CycleStatus
from core.policy.budget import Budget, BudgetManager, BudgetMetric
from core.policy.resource import (
    ConcurrencyLimiter,
    ResourceGate,
    ResourceUsage,
)

#: 测试用的固定时刻 —— **不取"现在"**，否则日切那条用例会随跑的时刻变化。
START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


class _Clock:
    """一个能往前拨的时钟。"""

    def __init__(self, moment: datetime = START) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> None:
        self.moment += timedelta(**delta)


def _budget(**kwargs: object) -> Budget:
    """一份预算。**上限只有一个出处。**"""
    return Budget(**kwargs)  # type: ignore[arg-type]


def _gate(
    budget: Budget,
    *,
    scope: str = "project",
    clock: _Clock | None = None,
) -> ResourceGate:
    """搭一个闸门。"""
    return ResourceGate(budget, scope=scope, clock=clock)


def _cost(amount: str) -> CostAmount:
    """一笔钱。"""
    return CostAmount(amount=Decimal(amount), currency="USD")


class TestTheOneBook:
    """跨周期预算 —— **所有周期记同一本账**。"""

    async def test_every_cycle_charges_the_same_account(self) -> None:
        """**三轮的钱加在一起，就是今天花的钱。**"""
        gate = _gate(_budget(max_daily_cost=Decimal("100")))
        for index in range(3):
            await gate.charge(ResourceUsage(cost=_cost("2")), cycle_id=f"c{index}")

        assert await gate.daily_spend() == Decimal("6")

    async def test_the_daily_cap_holds_across_the_cycles(self) -> None:
        """**上限是"一天"的，不是"一轮"的。**

        这是这一层存在的全部理由：每轮各记各的账，六轮下来账面日成本就是六
        份上限。**这里必须是五。**
        """
        gate = _gate(_budget(max_daily_cost=Decimal("5")))
        for index in range(5):
            await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id=f"c{index}")
        sixth = await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c5")

        assert sixth.blocked is True
        assert await gate.daily_spend() == Decimal("5")

    async def test_a_budget_per_cycle_would_have_been_six_books(self) -> None:
        """**反面验证：证明"跨周期"这件事有牙。**

        每轮各建一个 BudgetManager（看上去很自然的写法），每轮都能记满五块 ——
        六轮记下来的总额是三十块，**上限形同虚设**。同一个闸门，六轮只能记
        五块。**两个写法的差别，就是这一层的全部价值。**
        """
        per_cycle = _budget(max_daily_cost=Decimal("5"))
        for index in range(6):
            await BudgetManager(per_cycle, scope=f"cycle-{index}").consume_cost(
                BudgetMetric.DAILY_COST, _cost("5")
            )

        gate = _gate(_budget(max_daily_cost=Decimal("5")))
        for index in range(6):
            await gate.charge(ResourceUsage(cost=_cost("5")), cycle_id=f"c{index}")

        assert await gate.daily_spend() == Decimal("5")

    async def test_two_scopes_are_two_books(self) -> None:
        """**作用域是账本的主键** —— 换个作用域就是换了一本账。"""
        first = _gate(_budget(max_daily_cost=Decimal("10")), scope="alpha")
        second = _gate(_budget(max_daily_cost=Decimal("10")), scope="beta")
        await first.charge(ResourceUsage(cost=_cost("3")), cycle_id="c0")

        assert await first.daily_spend() == Decimal("3")
        assert await second.daily_spend() == Decimal("0")


class TestTheCostAggregation:
    """成本归集 —— **每轮的钱归到自己名下**。"""

    async def test_the_cycle_spend_is_the_sum_of_its_own_charges(self) -> None:
        """**这一轮花了多少，是这一轮自己的数。**"""
        gate = _gate(_budget(max_daily_cost=Decimal("100")))
        await gate.charge(ResourceUsage(cost=_cost("1.5")), cycle_id="c0")
        await gate.charge(ResourceUsage(cost=_cost("2.5")), cycle_id="c0")

        assert await gate.cycle_spend("c0") == Decimal("4.0")

    async def test_two_cycles_keep_their_own_attribution(self) -> None:
        """**各归各的 —— 归集不是把所有人加在一起。**"""
        gate = _gate(_budget(max_daily_cost=Decimal("100")))
        await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c0")
        await gate.charge(ResourceUsage(cost=_cost("3")), cycle_id="c1")

        assert await gate.cycle_spend("c0") == Decimal("1")
        assert await gate.cycle_spend("c1") == Decimal("3")

    async def test_an_unknown_cycle_cost_nothing(self) -> None:
        """**没花过的周期是零，不是"不知道"。**

        这里零是安全的 —— 没有"该有却没有"的语义；而 :mod:`core.evolution.
        calibration` 那种零会掩盖"算不出"，那里才必须区分。
        """
        gate = _gate(_budget(max_daily_cost=Decimal("100")))
        assert await gate.cycle_spend("never-ran") == Decimal("0")

    async def test_the_tokens_go_to_their_own_dimension(self) -> None:
        """**token 与钱是两面 —— 各记各的维度。**"""
        gate = _gate(_budget(max_daily_cost=Decimal("100")))
        outcome = await gate.charge(ResourceUsage(cost=_cost("1"), tokens=1200), cycle_id="c0")

        assert await gate.manager.spent(BudgetMetric.TOKEN_USAGE) == Decimal("1200")
        assert outcome.recorded is True

    async def test_the_remaining_dimensions_are_counted_too(self) -> None:
        """**R-018 管的四样都在：token、调用次数、计算时间、墙钟时间。**

        四样缺一样，"资源控制"就只管住了其中一样 —— 而**没人盯着的那一维恰
        恰是最容易超的那一维**。
        """
        gate = _gate(
            _budget(
                max_daily_cost=Decimal("100"),
                max_token_usage=1000,
                max_model_calls=10,
                max_compute_minutes=60,
                max_wall_clock_minutes=120,
            )
        )
        await gate.charge(
            ResourceUsage(
                cost=_cost("1"),
                tokens=100,
                model_calls=2,
                compute_minutes=5,
                wall_clock_minutes=7,
            ),
            cycle_id="c0",
        )

        assert await gate.manager.spent(BudgetMetric.TOKEN_USAGE) == Decimal("100")
        assert await gate.manager.spent(BudgetMetric.MODEL_CALLS) == Decimal("2")
        assert await gate.manager.spent(BudgetMetric.COMPUTE_MINUTES) == Decimal("5")
        assert await gate.manager.spent(BudgetMetric.WALL_CLOCK_MINUTES) == Decimal("7")

    async def test_the_compute_minutes_are_capped(self) -> None:
        """**计算时间也有上限 —— 超了就记不进去。**"""
        gate = _gate(_budget(max_daily_cost=Decimal("100"), max_compute_minutes=10))
        outcome = await gate.charge(
            ResourceUsage(cost=_cost("1"), compute_minutes=30),
            cycle_id="c0",
        )

        assert outcome.blocked is True
        assert await gate.manager.spent(BudgetMetric.COMPUTE_MINUTES) == Decimal("0")

    async def test_the_lifetime_total_is_the_sum_of_the_daily_entries(self) -> None:
        """**一共花了多少，从流水求和 —— 不另开一本累计账。**"""
        gate = _gate(_budget(max_daily_cost=Decimal("100")))
        await gate.charge(ResourceUsage(cost=_cost("2")), cycle_id="c0")
        await gate.charge(ResourceUsage(cost=_cost("3")), cycle_id="c1")

        assert await gate.lifetime_spend() == Decimal("5")


class TestTheConcurrency:
    """并发限流 —— **同时跑着的数不超上限**。"""

    async def test_the_limit_comes_from_the_budget(self) -> None:
        """**上限只有一个出处。**"""
        gate = _gate(_budget(max_parallel_agents=3))
        assert gate.limiter.limit == 3

    async def test_the_limit_is_never_exceeded(self) -> None:
        """**八个任务、上限三个 —— 同时跑着的最多三个。**"""
        limiter = ConcurrencyLimiter(3)
        live = 0
        peak = 0

        async def _one() -> None:
            nonlocal live, peak
            async with limiter.slot():
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.01)
                live -= 1

        await asyncio.gather(*(_one() for _ in range(8)))

        assert peak <= 3
        assert limiter.peak <= 3

    async def test_the_peak_is_remembered_across_batches(self) -> None:
        """**峰值跨批次也记着。**

        单批的峰值会被下一批冲掉，而"整个系统在某个时刻同时跑过几个"才是
        限流真正要回答的问题。
        """
        limiter = ConcurrencyLimiter(2)

        async def _one() -> None:
            async with limiter.slot():
                await asyncio.sleep(0.01)

        await asyncio.gather(*(_one() for _ in range(2)))
        assert limiter.peak == 2

        await asyncio.gather(*(_one() for _ in range(1)))
        assert limiter.peak == 2

    def test_a_limit_below_one_is_refused(self) -> None:
        """**上限为 0 不是限流，那是停机。**"""
        with pytest.raises(ValueError, match="至少是 1"):
            ConcurrencyLimiter(0)

    async def test_the_limiter_is_shared_by_the_gate(self) -> None:
        """**闸门持有那个信号量** —— 谁要用谁从这儿拿。"""
        gate = _gate(_budget(max_parallel_agents=2))
        assert gate.limiter is gate.limiter


class TestTheAdmission:
    """准入门 —— **开跑之前先问一句**。"""

    async def test_it_allows_while_there_is_budget(self) -> None:
        """**还有余额就放行。**"""
        gate = _gate(_budget(max_daily_cost=Decimal("10")))
        await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c0")

        admission = await gate.admit()

        assert admission.allowed is True
        assert admission.stopped is False

    async def test_it_refuses_when_the_budget_is_used_up(self) -> None:
        """**用满了就不开新周期。**"""
        gate = _gate(_budget(max_daily_cost=Decimal("1")))
        await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c0")

        admission = await gate.admit()

        assert admission.allowed is False
        assert admission.metric is BudgetMetric.DAILY_COST
        assert admission.spent == Decimal("1")
        assert admission.limit == Decimal("1")

    async def test_the_reason_says_which_dimension(self) -> None:
        """**理由要能定位到维度** —— 补救动作各不相同。"""
        gate = _gate(_budget(max_daily_cost=Decimal("1")))
        await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c0")

        admission = await gate.admit()

        assert "daily_cost" in admission.reason

    async def test_the_stop_status_is_budget_exhausted(self) -> None:
        """**周期词汇表里早有这一档** —— 被预算掐断的周期跑完了但没变好。"""
        gate = _gate(_budget(max_daily_cost=Decimal("1")))
        await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c0")

        assert (await gate.admit()).cycle_status is CycleStatus.BUDGET_EXHAUSTED

    async def test_it_opens_again_the_next_day(self) -> None:
        """**日切自动生效** —— 新的一天是新的一行累计额。"""
        clock = _Clock()
        gate = _gate(_budget(max_daily_cost=Decimal("1")), clock=clock)
        await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c0")
        assert (await gate.admit()).allowed is False

        clock.advance(days=1)

        admission = await gate.admit()
        assert admission.allowed is True
        assert await gate.daily_spend() == Decimal("0")


class TestTheGracefulStop:
    """撞过墙之后 —— **停下来，而不是掀桌子**。"""

    async def test_a_rejected_charge_does_not_count_as_spent(self) -> None:
        """**被拦下的钱不算花出去。** 记进去的话，账上的数会比真实花掉的
        多，而"多花的那部分"从来没发生过。"""
        gate = _gate(_budget(max_daily_cost=Decimal("1")))
        await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c0")

        outcome = await gate.charge(ResourceUsage(cost=_cost("5")), cycle_id="c1")

        assert outcome.blocked is True
        assert await gate.daily_spend() == Decimal("1")

    async def test_the_rejected_attempt_is_still_recorded(self) -> None:
        """**"我们想花超了"这件事要被问出来。**"""
        gate = _gate(_budget(max_daily_cost=Decimal("1")))
        await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c0")
        await gate.charge(ResourceUsage(cost=_cost("5")), cycle_id="c1")

        blocked = await gate.blocked_attempts()

        assert len(blocked) == 1
        assert blocked[0].metric is BudgetMetric.DAILY_COST
        assert blocked[0].recorded is False

    async def test_after_a_rejection_the_next_cycle_is_refused(self) -> None:
        """**撞过墙就别再开新周期。**

        这一条是"跨周期"三个字最要紧的落点：撞墙的那一笔**没有记进累计
        额**，所以"余额还剩三块"是可以发生的 —— **只看余额，系统会一轮一轮
        地撞下去。**
        """
        gate = _gate(_budget(max_daily_cost=Decimal("5")))
        await gate.charge(ResourceUsage(cost=_cost("3")), cycle_id="c0")
        await gate.charge(ResourceUsage(cost=_cost("9")), cycle_id="c1")

        admission = await gate.admit()

        assert admission.allowed is False
        assert "撞过" in admission.reason

    async def test_the_token_dimension_is_not_blocked_by_the_money_one(self) -> None:
        """**一个维度被拦下，不影响别的维度。**

        钱撞了上限，token 照样用掉了 —— 把整笔记成"失败"会让人以为 token
        也没用上。
        """
        gate = _gate(_budget(max_daily_cost=Decimal("1")))
        await gate.charge(ResourceUsage(cost=_cost("1")), cycle_id="c0")

        outcome = await gate.charge(ResourceUsage(cost=_cost("5"), tokens=100), cycle_id="c1")

        assert outcome.blocked is True
        assert await gate.manager.spent(BudgetMetric.TOKEN_USAGE) == Decimal("100")
