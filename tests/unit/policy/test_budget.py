"""TASK-P1-012 —— Budget Manager。

验证超限显式失败、并发扣减不超限、币种可配置。

这些是**内存账本**下的断言。跨进程的真实并发（写库）由
``tests/integration/test_budget_concurrency.py`` 验 —— 内存在一把锁，
数据库靠行级锁，两者要分开验。
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
    BudgetLedger,
    BudgetManager,
    BudgetMetric,
    InMemoryBudgetLedger,
    SpendRecord,
    SpendRequest,
    SpendStatus,
    period_key,
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
        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("10")

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
        """被拒绝的调用没有真正发生，不能计入累计额。"""
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("40"))

        with pytest.raises(BudgetExhausted):
            await manager.consume(BudgetMetric.DAILY_COST, Decimal("100"))

        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("40")

    async def test_rejected_attempt_is_still_auditable(self) -> None:
        """被拒绝的尝试要留痕 —— "系统试图超预算"本身就是需要被看见的事实。"""
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("40"))

        with pytest.raises(BudgetExhausted):
            await manager.consume(BudgetMetric.DAILY_COST, Decimal("100"))

        blocked = await manager.blocked_attempts()
        assert len(blocked) == 1
        assert blocked[0].status is SpendStatus.BLOCKED
        assert blocked[0].amount == Decimal("100")

    async def test_exact_limit_is_allowed(self) -> None:
        """刚好用满上限是合法的，不是超限。"""
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("30"))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("20"))
        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("50")

    async def test_negative_amount_is_rejected(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        with pytest.raises(ValueError, match="不能为负"):
            await manager.consume(BudgetMetric.DAILY_COST, Decimal("-1"))

    async def test_unlimited_metric_never_raises(self) -> None:
        manager = BudgetManager(Budget())
        await manager.consume(BudgetMetric.TOKEN_USAGE, Decimal("999999"))
        assert await manager.remaining(BudgetMetric.TOKEN_USAGE) is None

    async def test_consume_uses_the_configured_currency(self) -> None:
        """币种可配 —— 流水里记的是配置的币种，不是硬编码的 USD。"""
        manager = BudgetManager(Budget(currency="CNY", max_daily_cost=Decimal("100")))
        record = await manager.consume(BudgetMetric.DAILY_COST, Decimal("10"))
        assert record.currency == "CNY"


class TestConcurrency:
    """并发扣减不得超限（内存账本）。"""

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
        assert await manager.spent(BudgetMetric.DAILY_COST) <= Decimal("100")
        assert succeeded == 6  # 100 / 15 向下取整
        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("15") * succeeded

    async def test_blocked_attempts_are_all_recorded(self) -> None:
        """并发下被拒绝的尝试一个都不能丢。"""
        manager = BudgetManager(Budget(max_daily_cost=Decimal("100")))
        amounts = [Decimal("15")] * 10

        async def worker(amount: Decimal) -> bool:
            try:
                await manager.consume(BudgetMetric.DAILY_COST, amount)
            except BudgetExhausted:
                return False
            return True

        await asyncio.gather(*(worker(amount) for amount in amounts))

        blocked = await manager.blocked_attempts()
        assert len(blocked) == 4  # 10 次尝试，6 次成功


class TestPeriods:
    """按日重置。"""

    def test_daily_cost_uses_a_day_scoped_period(self) -> None:
        moment = datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
        assert period_key(BudgetMetric.DAILY_COST, moment) == "day:2026-03-01"

    def test_lifetime_metrics_share_one_period(self) -> None:
        moment = datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
        assert period_key(BudgetMetric.TOTAL_COST, moment) == "lifetime"
        assert period_key(BudgetMetric.TOKEN_USAGE, moment) == "lifetime"

    async def test_daily_cost_resets_next_day(self) -> None:
        start = datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
        manager = BudgetManager(
            Budget(max_daily_cost=Decimal("50")),
            clock=_fixed_clock(start),
        )
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("50"))

        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("50")

        manager.use_clock(_fixed_clock(start + timedelta(days=1)))
        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("0")

    async def test_total_cost_does_not_reset(self) -> None:
        start = datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
        manager = BudgetManager(
            Budget(max_total_cost=Decimal("500")),
            clock=_fixed_clock(start),
        )
        await manager.consume(BudgetMetric.TOTAL_COST, Decimal("100"))

        manager.use_clock(_fixed_clock(start + timedelta(days=1)))
        assert await manager.spent(BudgetMetric.TOTAL_COST) == Decimal("100")

    async def test_daily_rollover_needs_no_deletion(self) -> None:
        """日切不靠删记录实现 —— 账本是 append-only 的。

        所以**不能**有 ``reset_daily`` 之类的操作：它要么删流水（破坏审计），
        要么把累计额调小（数据库触发器会拒绝）。新的一天是新的一行累计额，
        自然从零开始。
        """
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        assert not hasattr(manager, "reset_daily")
        assert not hasattr(manager, "reset")
        assert not hasattr(manager, "clear")

        start = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        manager.use_clock(_fixed_clock(start))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("30"))

        manager.use_clock(_fixed_clock(start + timedelta(days=1)))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("10"))

        # 两天的流水都在，一条没少
        entries = await manager.ledger_entries()
        assert len(entries) == 2
        assert {entry.period for entry in entries} == {"day:2026-03-01", "day:2026-03-02"}
        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("10")


class TestScopeIsolation:
    """不同归属的预算互不干扰。"""

    async def test_scopes_have_separate_budgets(self) -> None:
        ledger = InMemoryBudgetLedger()
        first = BudgetManager(
            Budget(max_daily_cost=Decimal("100")), scope="project-a", ledger=ledger
        )
        second = BudgetManager(
            Budget(max_daily_cost=Decimal("100")), scope="project-b", ledger=ledger
        )

        await first.consume(BudgetMetric.DAILY_COST, Decimal("100"))

        assert await first.spent(BudgetMetric.DAILY_COST) == Decimal("100")
        assert await second.spent(BudgetMetric.DAILY_COST) == Decimal("0")

        # A 用满了，B 还能用
        await second.consume(BudgetMetric.DAILY_COST, Decimal("100"))
        assert await second.spent(BudgetMetric.DAILY_COST) == Decimal("100")

    async def test_scope_is_visible_on_records(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("100")), scope="project-x")
        record = await manager.consume(BudgetMetric.DAILY_COST, Decimal("1"))
        assert record.scope == "project-x"


class TestLedgerPort:
    """账本是可替换的端口。"""

    def test_in_memory_ledger_implements_the_protocol(self) -> None:
        assert isinstance(InMemoryBudgetLedger(), BudgetLedger)

    async def test_manager_uses_the_injected_ledger(self) -> None:
        ledger = InMemoryBudgetLedger()
        manager = BudgetManager(Budget(max_daily_cost=Decimal("10")), ledger=ledger)
        assert manager.ledger is ledger

        await manager.consume(BudgetMetric.DAILY_COST, Decimal("4"))
        assert await ledger.spent("default", BudgetMetric.DAILY_COST, datetime.now(UTC)) == Decimal(
            "4"
        )

    async def test_manager_defaults_to_the_in_memory_ledger(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("10")))
        assert isinstance(manager.ledger, InMemoryBudgetLedger)

    async def test_custom_ledger_receives_the_request(self) -> None:
        """自定义账本拿到的是完整的扣减请求，而不是零散参数。"""

        class RecordingLedger:
            def __init__(self) -> None:
                self.requests: list[SpendRequest] = []

            async def consume(self, request: SpendRequest) -> SpendRecord:
                self.requests.append(request)
                return SpendRecord(
                    scope=request.scope,
                    metric=request.metric,
                    amount=request.amount,
                    currency=request.currency,
                    recorded_at=request.moment,
                    day=request.moment.date(),
                    period=request.period,
                )

            async def spent(self, _scope: str, _metric: BudgetMetric, _moment: datetime) -> Decimal:
                return Decimal("0")

            async def entries(
                self, _scope: str, _metric: BudgetMetric | None = None
            ) -> tuple[SpendRecord, ...]:
                return ()

        ledger = RecordingLedger()
        moment = datetime(2026, 5, 4, 8, 0, tzinfo=UTC)
        manager = BudgetManager(
            Budget(currency="EUR", max_daily_cost=Decimal("50")),
            scope="p-1",
            ledger=ledger,
            clock=_fixed_clock(moment),
        )

        await manager.consume(BudgetMetric.DAILY_COST, Decimal("10"))

        request = ledger.requests[0]
        assert request.scope == "p-1"
        assert request.metric is BudgetMetric.DAILY_COST
        assert request.amount == Decimal("10")
        assert request.currency == "EUR"
        assert request.limit == Decimal("50")
        assert request.period == "day:2026-05-04"


class TestQueries:
    """查询辅助。"""

    async def test_remaining_reports_left(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("20"))
        assert await manager.remaining(BudgetMetric.DAILY_COST) == Decimal("30")

    async def test_would_exceed_is_side_effect_free(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        assert await manager.would_exceed(BudgetMetric.DAILY_COST, Decimal("60")) is True
        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("0")

    async def test_summary_reports_spent_limit_remaining(self) -> None:
        manager = BudgetManager(Budget(max_daily_cost=Decimal("50")))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("20"))
        summary = await manager.summary()
        assert summary["daily_cost.spent"] == Decimal("20")
        assert summary["daily_cost.limit"] == Decimal("50")
        assert summary["daily_cost.remaining"] == Decimal("30")
