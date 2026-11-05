"""TASK-P1-012 ② —— 真实并发写库下的预算不超限。

验证（T1）：

- 10 个**真实并发**的数据库扣减，总和不得超过上限（无竞态）；
- 并发下被拒绝的尝试一条都不丢；
- 累计额只增不减 —— 直接 UPDATE 把 ``spent`` 改小会被数据库拒绝；
- 流水不可改写 —— UPDATE / DELETE 被数据库拒绝；
- 币种不硬编码 —— 换个币种同样工作。

"真实并发"是这里的关键。``tests/unit/policy/test_budget.py`` 里也有并发测试，
但那是内存账本：一把 ``asyncio.Lock`` 就够了。内存账本证明不了跨进程的
正确性 —— 两个进程各有一份账本，互相看不见余额。这个测试用真正的
PostgreSQL 行级锁来验，所以才有意义。

判据是**不变量**而不是具体次数：无论 10 次扣减以什么顺序落地，
"累计额 ≤ 上限"和"累计额 = 成功次数 × 单次金额"都必须成立。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.policy.budget import (
    Budget,
    BudgetExhausted,
    BudgetManager,
    BudgetMetric,
    SpendRequest,
    SpendStatus,
)
from infrastructure.database.budget import CurrencyConflictError, PostgresBudgetLedger

pytestmark = pytest.mark.integration

#: 并发扣减的次数。任务书要求 10 次。
CONCURRENCY = 10

#: 单次扣减金额。100 / 15 向下取整 = 6 次能成功。
AMOUNT = Decimal("15")
LIMIT = Decimal("100")


@pytest_asyncio.fixture
async def budget_engine(engine: AsyncEngine, session: AsyncSession) -> AsyncIterator[AsyncEngine]:
    """干净数据库上的引擎。

    ``session`` 夹具在这里是为它的副作用：每个测试前把全部表清空。预算测试
    要断言精确的次数（"10 次里成功 6 次"），必须从干净状态开始 —— 集成测试
    库在两次 pytest 运行之间**不会**自动清空。

    清库要临时关掉 append-only 守卫（那是 conftest 的事），所以这里不能自己
    DELETE 预算表 —— 那些 DELETE 本来就该被拒绝。
    """
    rows = await session.execute(text("SELECT count(*) FROM budget_entry"))
    assert rows.scalar_one() == 0, "预算流水表不是干净的 —— 前置条件不满足"
    yield engine


def _manager(
    engine: AsyncEngine, scope: str, limit: Decimal, currency: str = "USD"
) -> BudgetManager:
    """构造一个用数据库账本的 BudgetManager。"""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return BudgetManager(
        Budget(currency=currency, max_daily_cost=limit),
        scope=scope,
        ledger=PostgresBudgetLedger(factory),
    )


def _request(scope: str, metric: BudgetMetric, amount: Decimal, moment: datetime) -> SpendRequest:
    """构造一个扣减请求。"""
    return SpendRequest(
        scope=scope,
        metric=metric,
        amount=amount,
        currency="USD",
        limit=Decimal("1000"),
        moment=moment,
    )


class TestConcurrentConsume:
    """② 并发扣减不超限。"""

    async def test_ten_concurrent_consumes_never_exceed_the_limit(
        self, budget_engine: AsyncEngine
    ) -> None:
        """10 次并发扣减，总额不超限。

        关键在 ``asyncio.gather``：十个协程**同时**发起，各自拿到自己的
        数据库连接。如果"查余额"与"扣减"之间有缝隙，这十个协程会各自
        看到"还有余额"然后一起扣超 —— 总和会大于上限。
        """
        manager = _manager(budget_engine, scope="concurrent-a", limit=LIMIT)

        async def worker() -> bool:
            try:
                await manager.consume(BudgetMetric.DAILY_COST, AMOUNT)
            except BudgetExhausted:
                return False
            return True

        results = await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))

        succeeded = sum(1 for ok in results if ok)
        spent = await manager.spent(BudgetMetric.DAILY_COST)

        # 不变量一：总额不超限
        assert spent <= LIMIT, f"总额 {spent} 超过上限 {LIMIT}"
        # 不变量二：总额恰好等于成功次数乘以单次金额 —— 没有凭空多扣，
        # 也没有扣了但没记账的
        assert spent == AMOUNT * succeeded
        # 100 / 15 向下取整 = 6
        assert succeeded == 6

    async def test_exhausted_budget_rejects_further_consumes(
        self, budget_engine: AsyncEngine
    ) -> None:
        """用满之后继续扣必须被拒绝，而不是悄悄放行。"""
        manager = _manager(budget_engine, scope="concurrent-b", limit=Decimal("30"))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("30"))

        with pytest.raises(BudgetExhausted):
            await manager.consume(BudgetMetric.DAILY_COST, Decimal("0.01"))

        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("30")

    async def test_blocked_attempts_are_persisted(self, budget_engine: AsyncEngine) -> None:
        """被拒绝的尝试要落库 —— 并发下也不能丢。"""
        manager = _manager(budget_engine, scope="concurrent-c", limit=LIMIT)

        async def worker() -> bool:
            try:
                await manager.consume(BudgetMetric.DAILY_COST, AMOUNT)
            except BudgetExhausted:
                return False
            return True

        await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))

        blocked = await manager.blocked_attempts()
        assert len(blocked) == CONCURRENCY - 6
        assert all(record.status is SpendStatus.BLOCKED for record in blocked)

    async def test_scopes_are_isolated_under_concurrency(self, budget_engine: AsyncEngine) -> None:
        """两个归属各扣各的 —— 预算不能串。"""
        first = _manager(budget_engine, scope="project-x", limit=LIMIT)
        second = _manager(budget_engine, scope="project-y", limit=LIMIT)

        async def worker(manager: BudgetManager) -> bool:
            try:
                await manager.consume(BudgetMetric.DAILY_COST, Decimal("10"))
            except BudgetExhausted:
                return False
            return True

        await asyncio.gather(
            *(worker(first) for _ in range(5)),
            *(worker(second) for _ in range(5)),
        )

        assert await first.spent(BudgetMetric.DAILY_COST) == Decimal("50")
        assert await second.spent(BudgetMetric.DAILY_COST) == Decimal("50")


class TestDatabaseGuards:
    """数据库层的拒绝 —— 绕过应用层也无效。"""

    async def test_spent_cannot_be_decreased(self, budget_engine: AsyncEngine) -> None:
        """把累计额改小等于绕开预算闸门，必须被拒绝。"""
        manager = _manager(budget_engine, scope="guard-a", limit=LIMIT)
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("50"))

        async with budget_engine.connect() as conn:
            with pytest.raises(DBAPIError) as exc_info:
                await conn.execute(
                    text(
                        "UPDATE budget_account SET spent = 0 "
                        "WHERE scope = 'guard-a' AND metric = 'daily_cost'"
                    )
                )
            await conn.rollback()

        assert "只增不减" in str(exc_info.value)

    async def test_account_cannot_be_deleted(self, budget_engine: AsyncEngine) -> None:
        """删掉累计额行等于把预算清零。"""
        manager = _manager(budget_engine, scope="guard-b", limit=LIMIT)
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("10"))

        async with budget_engine.connect() as conn:
            with pytest.raises(DBAPIError) as exc_info:
                await conn.execute(
                    text(
                        "DELETE FROM budget_account "
                        "WHERE scope = 'guard-b' AND metric = 'daily_cost'"
                    )
                )
            await conn.rollback()

        assert "不可删除" in str(exc_info.value)

    async def test_entry_cannot_be_updated(self, budget_engine: AsyncEngine) -> None:
        """已记下的花费不能被改写。"""
        manager = _manager(budget_engine, scope="guard-c", limit=LIMIT)
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("10"))

        async with budget_engine.connect() as conn:
            with pytest.raises(DBAPIError) as exc_info:
                await conn.execute(
                    text("UPDATE budget_entry SET amount = 0 WHERE scope = 'guard-c'")
                )
            await conn.rollback()

        assert "append-only" in str(exc_info.value)

    async def test_entry_cannot_be_deleted(self, budget_engine: AsyncEngine) -> None:
        """抹掉流水就没人能回答"钱花到哪儿了"。"""
        manager = _manager(budget_engine, scope="guard-d", limit=LIMIT)
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("10"))

        async with budget_engine.connect() as conn:
            with pytest.raises(DBAPIError) as exc_info:
                await conn.execute(text("DELETE FROM budget_entry WHERE scope = 'guard-d'"))
            await conn.rollback()

        assert "append-only" in str(exc_info.value)

    async def test_rejected_update_leaves_no_trace(self, budget_engine: AsyncEngine) -> None:
        """被拒绝的修改没有留下任何痕迹。"""
        manager = _manager(budget_engine, scope="guard-e", limit=LIMIT)
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("50"))

        async with budget_engine.connect() as conn:
            with pytest.raises(DBAPIError):
                await conn.execute(
                    text(
                        "UPDATE budget_account SET spent = 0 "
                        "WHERE scope = 'guard-e' AND metric = 'daily_cost'"
                    )
                )
            await conn.rollback()

        assert await manager.spent(BudgetMetric.DAILY_COST) == Decimal("50")


class TestCurrencyIsNotHardcoded:
    """③ 币种可配。"""

    async def test_non_usd_currency_works(self, budget_engine: AsyncEngine) -> None:
        manager = _manager(budget_engine, scope="currency-a", limit=Decimal("500"), currency="CNY")
        record = await manager.consume(BudgetMetric.DAILY_COST, Decimal("120"))

        assert record.currency == "CNY"

        async with budget_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT currency FROM budget_account "
                    "WHERE scope = 'currency-a' AND metric = 'daily_cost'"
                )
            )
            assert result.scalar_one() == "CNY"

    async def test_currency_is_stored_on_the_entry(self, budget_engine: AsyncEngine) -> None:
        manager = _manager(budget_engine, scope="currency-b", limit=Decimal("500"), currency="JPY")
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("100"))

        async with budget_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT currency FROM budget_entry WHERE scope = 'currency-b'")
            )
            assert result.scalar_one() == "JPY"

    async def test_mixed_currency_on_one_account_is_rejected(
        self, budget_engine: AsyncEngine
    ) -> None:
        """同一个账户不能混算两种币种 —— 那个数字不再代表任何东西。"""
        manager = _manager(budget_engine, scope="currency-c", limit=Decimal("500"), currency="USD")
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("10"))

        other = _manager(budget_engine, scope="currency-c", limit=Decimal("500"), currency="CNY")
        with pytest.raises(CurrencyConflictError):
            await other.consume(BudgetMetric.DAILY_COST, Decimal("10"))


class TestDurability:
    """账本跨会话存活。"""

    async def test_spent_survives_a_new_session(self, budget_engine: AsyncEngine) -> None:
        """换个会话（换个连接池）读到的余额不变 —— 账本不是进程内的缓存。"""
        manager = _manager(budget_engine, scope="durable-a", limit=LIMIT)
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("40"))

        fresh = _manager(budget_engine, scope="durable-a", limit=LIMIT)
        assert await fresh.spent(BudgetMetric.DAILY_COST) == Decimal("40")

    async def test_entries_are_readable_afterwards(self, budget_engine: AsyncEngine) -> None:
        manager = _manager(budget_engine, scope="durable-b", limit=LIMIT)
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("25"))
        await manager.consume(BudgetMetric.DAILY_COST, Decimal("15"))

        entries = await manager.ledger_entries()
        assert len(entries) == 2
        assert {entry.amount for entry in entries} == {Decimal("25"), Decimal("15")}
        assert all(entry.currency == "USD" for entry in entries)

    async def test_daily_period_rolls_over_without_deletion(
        self, budget_engine: AsyncEngine
    ) -> None:
        """日切靠周期键，不靠删记录 —— 昨天的流水必须还在。"""
        yesterday = datetime(2026, 11, 5, 23, 0, tzinfo=UTC)
        today = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)

        factory = async_sessionmaker(budget_engine, class_=AsyncSession, expire_on_commit=False)
        ledger = PostgresBudgetLedger(factory)

        await ledger.consume(
            _request("rollover", BudgetMetric.DAILY_COST, Decimal("30"), yesterday)
        )
        await ledger.consume(_request("rollover", BudgetMetric.DAILY_COST, Decimal("10"), today))

        assert await ledger.spent("rollover", BudgetMetric.DAILY_COST, today) == Decimal("10")
        assert await ledger.spent("rollover", BudgetMetric.DAILY_COST, yesterday) == Decimal("30")

        entries = await ledger.entries("rollover")
        assert len(entries) == 2
        assert {entry.period for entry in entries} == {"day:2026-11-05", "day:2026-11-06"}
