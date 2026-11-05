"""TASK-P1-012 —— Budget Manager。

验证超限显式失败、并发扣减不超限、币种可配置。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.policy.budget import (
    Budget,
    BudgetExhausted,
    BudgetManager,
    BudgetMetric,
)


def _fixed_clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


class TestBudgetConfiguration:
    """币种与指标类型可配置。"""

    def test_currency_is_configurable(self) -> None:
        assert Budget(currency="cny").currency == "CNY"
        assert Budget(currency=" eur ").currency == "EUR"

    def test_currency_is_not_hardcoded_to_usd(self) -> None:
        budget = Budget(currency="CNY", max_daily_cost=Decimal("100"))
        assert budget.currency == "CNY"

    def test_limits_are_readable_per_metric(self) -> None:
        budget = Budget(
            currency="JPY",
            max_daily_cost=Decimal("5000"),
            max_model_calls=100,
            max_wall_clock_minutes=240,
        )
        assert budget.limit_for(BudgetMetric.DAILY_COST) == Decimal("5000")
        assert budget.limit_for(BudgetMetric.MODEL_CALLS) == Decimal("100")
        assert budget.limit_for(BudgetMetric.WALL_CLOCK_MINUTES) == Decimal("240")
        assert budget.limit_for(BudgetMetric.TOKEN_USAGE) is None

    def test_unset_metric_has_no_limit(self) -> None:
        assert Budget().limit_for(BudgetMetric.TOTAL_COST) is None


class TestConsume:
    """扣减行为。"""

    async def test_consume_records_spend(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        record = await manager.consume(BudgetMetric.DAILY_COST, Decimal("10"))
        assert record.amount == Decimal("10")
        assert record.currency == "USD"
        assert manager.spent(BudgetMetric.DAILY_COST) == Decimal("10")

    async def test_exceeding_daily_cost_raises(self) -> None:
        """扣减超过 max_daily_cost 抛 BudgetExhausted —— 不得静默截断。"""
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("40"))

        with pytest.raises(BudgetExhausted) as exc_info:
            await manager.consume(BudgetMetric.DAILY_COST, Decimal("11"))

        assert exc_info.value.metric is BudgetMetric.DAILY_COST
        assert exc_info.value.limit == Decimal("50")
        assert exc_info.value.requested == Decimal("11")

    async def test_rejected_consume_is_not_recorded(self) -> None:
        """被拒绝的调用没有真正发生，不能记账。"""
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("40"))

        with pytest.raises(BudgetExhausted):
            await manager.consume(BudgetMetric.DAILY_COST, Decimal("100"))

        assert manager.spent(BudgetMetric.DAILY_COST) == Decimal("40")
        assert len(manager.blocked_attempts()) == 1

    async def test_exact_limit_is_allowed(self) -> None:
        """刚好用满上限是合法的，不是超限。"""
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("30"))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("20"))
        assert manager.spent(BudgetMetric.DAILY_COST) == Decimal("50")

    async def test_negative_amount_is_rejected(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        with pytest.raises(ValueError, match="不能为负"):
            await manager.consume(BudgetMetric.DAILY_COST, Decimal("-1"))

    async def test_unlimited_metric_never_raises(self) -> None:
        manager = BudgetManager(Budget())
        await manager.consume(BudgetMetric.TOKEN_USAGE, Decimal("999999"))
        assert manager.remaining(BudgetMetric.TOKEN_USAGE) is None


class TestConcurrency:
    """并发扣减不得超限。"""

    async def test_concurrent_consumes_do_not_exceed_limit(self) -> None:
        """10 次并发扣减，总额不得超过上限。

        竞态的根源是"检查余额"与"扣减"之间让出了控制权。这里扣减全程持锁，
        十个任务串行完成检查与写入。
        """
        manager = BudgetManager(Budget(max_daily_cost=Decimal("100")))
        amounts = [Decimal("15")] * 10

        async def worker(amount: Decimal) -> bool:
            try:
                await manager.consume(BudgetMetric.DAILY_COST, amount)
            except BudgetExhausted:
                return False
            return True

        results = await asyncio.gather(*(worker(amount) for amount in amounts))

        succeeded = sum(1 for ok in results if ok)
        assert manager.spent(BudgetMetric.DAILY_COST) <= Decimal("100")
        assert succeeded == 6  # 100 / 15 向下取整
        assert manager.spent(BudgetMetric.DAILY_COST) == Decimal("15") * succeeded


class TestPeriods:
    """按日重置。"""

    async def test_daily_cost_resets_next_day(self) -> None:
        start = datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
        manager = BudgetManager(
            Budget(max_daily_cost=Decimal("50")),
            clock=_fixed_clock(start),
        )
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("50"))

        assert manager.spent(BudgetMetric.DAILY_COST) == Decimal("50")

        manager.use_clock(_fixed_clock(start + timedelta(days=1)))
        assert manager.spent(BudgetMetric.DAILY_COST) == Decimal("0")

    async def test_total_cost_does_not_reset(self) -> None:
        start = datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
        manager = BudgetManager(
            Budget(max_total_cost=Decimal("500")),
            clock=_fixed_clock(start),
        )
        await manager.consume(BudgetMetric.TOTAL_COST, Decimal("100"))

        manager.use_clock(_fixed_clock(start + timedelta(days=1)))
        assert manager.spent(BudgetMetric.TOTAL_COST) == Decimal("100")

    async def test_reset_daily_clears_today_only(self) -> None:
        start = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        manager = BudgetManager(
            Budget(max_total_cost=Decimal("500")),
            clock=_fixed_clock(start),
        )
        await manager.consume(BudgetMetric.TOTAL_COST, Decimal("100"))
        manager.reset_daily()
        # reset_daily 只清当日记账；total_cost 是生命周期累计，也落在当天
        assert manager.spent(BudgetMetric.TOTAL_COST) == Decimal("0")


class TestQueries:
    """查询辅助。"""

    async def test_remaining_reports_left(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("20"))
        assert manager.remaining(BudgetMetric.DAILY_COST) == Decimal("30")

    async def test_would_exceed_is_side_effect_free(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        assert manager.would_exceed(BudgetMetric.DAILY_COST, Decimal("60")) is True
        assert manager.spent(BudgetMetric.DAILY_COST) == Decimal("0")

    async def test_summary_reports_spent_limit_remaining(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("20"))
        summary = manager.summary()
        assert summary["daily_cost.spent"] == Decimal("20")
        assert summary["daily_cost.limit"] == Decimal("50")
        assert summary["daily_cost.remaining"] == Decimal("30")
