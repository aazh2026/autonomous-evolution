"""预算账本的数据库实现。

设计依据：docs/tasks.md TASK-P1-012 —— "并发 10 次扣减总和不超限（无竞态，
**用真实并发写库验证**）"。

内存账本（``core/policy/budget.py`` 的 :class:`InMemoryBudgetLedger`）用一把
``asyncio.Lock`` 保证同进程内的原子性，但**跨进程就失效了**：两个进程各有一份
内存账本，互相看不见余额，各自都看到"还有余额"然后同时扣超。演进循环的
多个 Worker 是会横向扩展的，这个场景迟早出现。

所以这里给出一个跨进程也成立的实现，靠的是数据库的行级锁：

    SELECT ... FOR UPDATE 锁住 (scope, metric, period) 那一行

在 PostgreSQL 的 READ COMMITTED 隔离级别下，``SELECT … FOR UPDATE`` 在拿到锁
之后会**重新读取**该行的最新已提交版本 —— 于是"查余额"与"扣减"之间没有缝隙：
后来的事务拿到锁时看到的是前面事务扣完之后的余额。

两处容易写错的地方，这里特意处理了：

1. **首次扣减时账户行还不存在**。直接 ``SELECT … FOR UPDATE`` 锁不到任何行，
   两个并发事务会各自看到"没有账户"然后各自插入 —— 插入冲突的一方要么报错，
   要么插出两行。所以先 ``INSERT … ON CONFLICT DO NOTHING`` 建行（PostgreSQL
   会让并发插入的那一方阻塞到另一方提交），再 ``SELECT … FOR UPDATE`` 锁它。
   顺序反了就会丢掉串行化。

2. **超限被拒的流水必须落库**。如果超限时直接抛异常，事务回滚，那条
   "系统试图超预算但被拦住了"的痕迹就没了 —— 而这恰恰是最该被看见的事实。
   所以：先记流水、提交事务，**再**抛异常。

数据库层还有两道独立的拒绝（``migrations/versions/0003_budget_ledger.py``）：
累计额只增不减（不能把计数器调回去）、流水不可改写。这里的应用层检查是为了
给出可读的错误信息，那两道是为了让绕过应用层的做法也无效。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.entities.budget import BudgetAccount, BudgetEntry
from core.policy.budget import (
    BudgetExhausted,
    BudgetLedger,
    BudgetMetric,
    SpendRecord,
    SpendRequest,
    SpendStatus,
    period_key,
)


class CurrencyConflictError(Exception):
    """同一预算账户出现两种币种。

    累计额是一个数字，数字要有个单位。同一行里先记了 USD 又记了 CNY，
    这个数字就不再代表任何东西 —— 与其悄悄混算，不如直接报错。
    """

    def __init__(self, scope: str, metric: BudgetMetric, period: str) -> None:
        super().__init__(
            f"预算账户 {scope}/{metric.value}/{period} 的币种与既有记录不一致 —— "
            "累计额必须以同一币种记账，不能混算"
        )
        self.scope = scope
        self.metric = metric
        self.period = period


class PostgresBudgetLedger:
    """数据库预算账本。

    **自己管理事务**。理由：预算扣减必须先于业务事务生效。业务事务回滚了，
    但模型调用的钱已经花了 —— 把扣减放进业务事务里等于允许业务回滚来退还
    预算，那不是预算是返现。

    代价是调用方不能把预算扣减和业务写入放进同一个事务。需要那种原子性的
    场景（比如"扣预算 + 建任务"必须同时成功）应当由调用方在应用层补偿，
    而不是靠共享一个数据库事务。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def consume(self, request: SpendRequest) -> SpendRecord:
        """扣减。超限抛 :class:`BudgetExhausted`，且不计入累计额。"""
        blocked: SpendRecord | None = None
        recorded: SpendRecord | None = None

        async with self._session_factory() as session, session.begin():
            account = await self._lock_account(session, request)
            current = Decimal(account.spent)

            if request.limit is not None and current + request.amount > request.limit:
                # 超限：留痕但不动累计额。事务在这里正常提交 —— 流水要留下。
                blocked = self._append_entry(session, request, SpendStatus.BLOCKED)
            else:
                account.spent = current + request.amount
                account.currency = request.currency
                recorded = self._append_entry(session, request, SpendStatus.RECORDED)

        if blocked is not None:
            raise BudgetExhausted(
                metric=request.metric,
                requested=request.amount,
                limit=request.limit if request.limit is not None else Decimal(0),
            )
        if recorded is None:  # pragma: no cover —— 两个分支必居其一
            raise RuntimeError("预算扣减既没成功也没被拒绝，这是 bug")
        return recorded

    async def spent(self, scope: str, metric: BudgetMetric, moment: datetime) -> Decimal:
        """已消耗量（当前周期）。"""
        period = period_key(metric, moment)
        async with self._session_factory() as session:
            result = await session.execute(
                select(BudgetAccount.spent).where(
                    BudgetAccount.scope == scope,
                    BudgetAccount.metric == metric.value,
                    BudgetAccount.period == period,
                )
            )
            value = result.scalar_one_or_none()
            return Decimal(0) if value is None else Decimal(value)

    async def entries(
        self, scope: str, metric: BudgetMetric | None = None
    ) -> tuple[SpendRecord, ...]:
        """流水。"""
        statement = select(BudgetEntry).where(BudgetEntry.scope == scope)
        if metric is not None:
            statement = statement.where(BudgetEntry.metric == metric.value)
        statement = statement.order_by(BudgetEntry.recorded_at, BudgetEntry.id)

        async with self._session_factory() as session:
            rows = await session.scalars(statement)
            return tuple(_to_record(row) for row in rows)

    async def _lock_account(self, session: AsyncSession, request: SpendRequest) -> BudgetAccount:
        """取到并锁住账户行。

        **先建行再锁行**，顺序不能反（见模块文档）。``ON CONFLICT DO NOTHING``
        在 PostgreSQL 上会让并发的同键插入阻塞到对方提交，于是建行本身也是
        串行化的。
        """
        await session.execute(
            insert(BudgetAccount)
            .values(
                scope=request.scope,
                metric=request.metric.value,
                period=request.period,
                currency=request.currency,
                spent=Decimal("0"),
            )
            .on_conflict_do_nothing(
                index_elements=[BudgetAccount.scope, BudgetAccount.metric, BudgetAccount.period]
            )
        )

        result = await session.execute(
            select(BudgetAccount)
            .where(
                BudgetAccount.scope == request.scope,
                BudgetAccount.metric == request.metric.value,
                BudgetAccount.period == request.period,
            )
            .with_for_update()
        )
        account = result.scalar_one()
        if account.currency != request.currency:
            raise CurrencyConflictError(request.scope, request.metric, request.period)
        return account

    def _append_entry(
        self,
        session: AsyncSession,
        request: SpendRequest,
        status: SpendStatus,
    ) -> SpendRecord:
        """写一条流水。"""
        entry = BudgetEntry(
            scope=request.scope,
            metric=request.metric.value,
            period=request.period,
            amount=request.amount,
            currency=request.currency,
            status=status.value,
            recorded_at=request.moment,
        )
        session.add(entry)
        return SpendRecord(
            scope=request.scope,
            metric=request.metric,
            amount=request.amount,
            currency=request.currency,
            recorded_at=request.moment,
            day=request.moment.date(),
            period=request.period,
            status=status,
        )


def _to_record(row: BudgetEntry) -> SpendRecord:
    recorded_at = row.recorded_at
    return SpendRecord(
        scope=row.scope,
        metric=BudgetMetric(row.metric),
        amount=Decimal(row.amount),
        currency=row.currency,
        recorded_at=recorded_at,
        day=recorded_at.date(),
        period=row.period,
        status=SpendStatus(row.status),
    )


def _assert_implements_protocol() -> None:
    """静态检查：数据库账本确实满足 ``BudgetLedger`` 协议。

    放在运行时而非测试里，是因为协议不匹配是**编码期**错误，不该等到线上
    才发现。``isinstance`` 对 ``Protocol`` 的检查只验证方法名存在，不验证
    签名 —— 所以这里只做存在性断言，签名由 mypy 保证。
    """
    if not isinstance(PostgresBudgetLedger.__new__(PostgresBudgetLedger), BudgetLedger):
        raise TypeError("PostgresBudgetLedger 未实现 BudgetLedger 协议")


__all__ = ["CurrencyConflictError", "PostgresBudgetLedger"]
