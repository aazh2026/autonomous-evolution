"""TASK-P3-015 —— 跨周期预算在真实并发写库下不超限。

**这一份是 T2 的另一半。** ``tests/system/test_budget_resource_control.py``
用内存账本验"连续运行期间每日成本不超过上限、超限时优雅停止"；这里验的是同
一件事在**真实数据库、真实并发**下依然成立。

**为什么必须再写一遍。** 内存账本靠一把 ``asyncio.Lock`` 保证同进程内的原子
性 —— 而演进循环的 Worker 迟早会横向扩展成多个进程。两个进程各有一份内存账
本，互相看不见余额，各自都看到"还有余额"然后同时扣超。**内存账本证明不了跨
进程的正确性**，所以这里用 PostgreSQL 的行级锁来验。

**判据是不变量，不是具体次数。** 无论 10 次扣减以什么顺序落地，"累计额 ≤ 上
限"和"累计额 = 成功次数 × 单次金额"都必须成立。

**最要紧的一条是最后那个用例：撞过墙之后余额看着还有余额，闸门却不放行。**
余额是 90 / 100 —— 只看余额的人会说"还能再跑一轮"。但账本里已经躺着四条被
拒绝的流水，说明今天已经撞过一次。**这是"准入门"那第二个检查存在的全部理
由**，而它读的是数据库里的流水 —— 内存账本上验不出这一条的真假。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.metrics.cost import CostAmount
from core.policy.budget import Budget, BudgetMetric
from core.policy.resource import ResourceGate, ResourceUsage
from infrastructure.database.budget import PostgresBudgetLedger

pytestmark = pytest.mark.integration

#: 并发扣减的次数 —— 与 TASK-P1-012 的口径一致。
CONCURRENCY = 10

#: 单次扣减金额。100 / 15 向下取整 = 6 次能成功。
AMOUNT = Decimal("15")
LIMIT = Decimal("100")

#: 成功次数 —— 6 次 15 元共 90，第 7 次会到 105，超过上限。
EXPECTED_SUCCESSES = 6

#: 固定的时刻 —— **不取"现在"**，否则周期键会随跑的时刻变化。
START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def budget_engine(engine: AsyncEngine, session: AsyncSession) -> AsyncIterator[AsyncEngine]:
    """干净数据库上的引擎。

    ``session`` 夹具在这里是为它的副作用：每个测试前把全部表清空。预算测试
    要断言精确的次数（"10 次里成功 6 次"），必须从干净状态开始。
    """
    rows = await session.execute(text("SELECT count(*) FROM budget_entry"))
    assert rows.scalar_one() == 0, "预算流水表不是干净的 —— 前置条件不满足"
    yield engine


def _cost(amount: str) -> CostAmount:
    """一笔钱。"""
    return CostAmount(amount=Decimal(amount), currency="USD")


def _gate(
    engine: AsyncEngine,
    *,
    scope: str,
    limit: Decimal = LIMIT,
) -> ResourceGate:
    """搭一个**用数据库账本**的闸门。

    **账本是数据库的，闸门是内存的** —— 这正是真实部署的样子：闸门是编排层
    的对象，而账要落到库里。
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return ResourceGate(
        Budget(currency="USD", max_daily_cost=limit),
        scope=scope,
        ledger=PostgresBudgetLedger(factory),
        clock=lambda: START,
    )


class TestTheConcurrentCycles:
    """并发跑周期 —— **一本账**。"""

    async def test_ten_concurrent_cycles_never_exceed_the_limit(
        self, budget_engine: AsyncEngine
    ) -> None:
        """10 个并发周期记进同一本账，总额不超限。

        关键在 ``asyncio.gather``：十个协程**同时**发起，各自拿到自己的数据
        库连接。如果"查余额"与"扣减"之间有缝隙，这十个协程会各自看到"还有余
        额"然后一起扣超 —— 总和会大于上限。
        """
        gate = _gate(budget_engine, scope="gate-concurrent")

        async def worker(index: int) -> bool:
            outcome = await gate.charge(
                ResourceUsage(cost=_cost(str(AMOUNT))), cycle_id=f"cycle-{index}"
            )
            return outcome.recorded

        results = await asyncio.gather(*(worker(index) for index in range(CONCURRENCY)))

        succeeded = sum(1 for recorded in results if recorded)
        spent = await gate.daily_spend()

        # 不变量一：总额不超限
        assert spent <= LIMIT, f"总额 {spent} 超过上限 {LIMIT}"
        # 不变量二：总额恰好等于成功次数乘以单次金额 —— 没有凭空多扣，也没有
        # 扣了但没记账的
        assert spent == AMOUNT * succeeded
        assert succeeded == EXPECTED_SUCCESSES

    async def test_the_limit_holds_in_the_database_itself(self, budget_engine: AsyncEngine) -> None:
        """**数据库里的累计额也不超限** —— 不是只有闸门记得。

        绕过闸门直接查库，是为了确认"上限"住在数据里，而不是只活在内存的判
        断里。**只靠应用层判断的预算，绕过应用层就没了。**
        """
        gate = _gate(budget_engine, scope="gate-account")

        async def worker(index: int) -> None:
            await gate.charge(ResourceUsage(cost=_cost(str(AMOUNT))), cycle_id=f"cycle-{index}")

        await asyncio.gather(*(worker(index) for index in range(CONCURRENCY)))

        async with budget_engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT spent FROM budget_account "
                    "WHERE scope = 'gate-account' AND metric = 'daily_cost'"
                )
            )
            stored = rows.scalar_one()

        assert Decimal(stored) <= LIMIT
        assert Decimal(stored) == AMOUNT * EXPECTED_SUCCESSES

    async def test_two_gates_with_the_same_scope_share_one_account(
        self, budget_engine: AsyncEngine
    ) -> None:
        """**同一个作用域 = 同一本账。**

        两个闸门对象（可能是两个进程里各建了一个）记的是同一行账户 —— 这就是
        "跨周期预算"在跨进程时仍然成立的原因。**作用域是账本的主键，不是对象
        的属性。**
        """
        first = _gate(budget_engine, scope="shared-scope")
        second = _gate(budget_engine, scope="shared-scope")

        await first.charge(ResourceUsage(cost=_cost("40")), cycle_id="cycle-a")

        assert await second.daily_spend() == Decimal("40")

    async def test_two_scopes_do_not_share(self, budget_engine: AsyncEngine) -> None:
        """**换个作用域就是换了一本账。**"""
        first = _gate(budget_engine, scope="scope-x")
        second = _gate(budget_engine, scope="scope-y")

        await first.charge(ResourceUsage(cost=_cost("40")), cycle_id="cycle-a")

        assert await second.daily_spend() == Decimal("0")


class TestTheGracefulStop:
    """撞过墙之后 —— **停下来，而不是掀桌子**。"""

    async def test_the_blocked_attempts_survive_in_the_database(
        self, budget_engine: AsyncEngine
    ) -> None:
        """**"我们想花超了"这件事要落库。**"""
        gate = _gate(budget_engine, scope="gate-blocked")

        async def worker(index: int) -> None:
            await gate.charge(ResourceUsage(cost=_cost(str(AMOUNT))), cycle_id=f"cycle-{index}")

        await asyncio.gather(*(worker(index) for index in range(CONCURRENCY)))

        blocked = await gate.blocked_attempts()
        assert len(blocked) == CONCURRENCY - EXPECTED_SUCCESSES

        async with budget_engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT count(*) FROM budget_entry "
                    "WHERE scope = 'gate-blocked' AND status = 'blocked'"
                )
            )
            stored = rows.scalar_one()

        assert stored == CONCURRENCY - EXPECTED_SUCCESSES

    async def test_the_gate_refuses_even_though_the_balance_looks_healthy(
        self, budget_engine: AsyncEngine
    ) -> None:
        """**余额看着还有余额，闸门却不放行 —— 这一条是"准入门"存在的理由。**

        撞墙的那几笔**没有记进累计额**（累计额只增不减，拦下来的钱不算花出
        去），所以余额是 90 / 100，看着还有余量。**只看余额，系统会一轮一轮地
        撞下去。** 第二个检查读的是账本里的被拒流水，数据库里躺着的四条证据
        就是它停下来的依据。
        """
        gate = _gate(budget_engine, scope="gate-admission")

        async def worker(index: int) -> None:
            await gate.charge(ResourceUsage(cost=_cost(str(AMOUNT))), cycle_id=f"cycle-{index}")

        await asyncio.gather(*(worker(index) for index in range(CONCURRENCY)))

        admission = await gate.admit()

        # 余额确实还有余量 —— 正是这一点让"只看余额"不够
        assert await gate.daily_spend() == AMOUNT * EXPECTED_SUCCESSES
        assert await gate.daily_spend() < LIMIT
        # 但闸门不放行
        assert admission.allowed is False
        assert admission.metric is BudgetMetric.DAILY_COST
        assert "撞过" in admission.reason

    async def test_the_stop_is_a_conclusion_not_an_exception(
        self, budget_engine: AsyncEngine
    ) -> None:
        """**撞墙不抛异常。** 抛了就把正在跑的东西掀掉。"""
        gate = _gate(budget_engine, scope="gate-no-raise", limit=Decimal("10"))

        outcome = await gate.charge(ResourceUsage(cost=_cost("99")), cycle_id="cycle-0")

        assert outcome.blocked is True
        assert await gate.daily_spend() == Decimal("0")
