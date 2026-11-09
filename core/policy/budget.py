"""Budget Manager —— 预算与配额管理。

设计依据：docs/design.md §3.1（Budget Manager 管理 Token、模型调用、计算资源和
时间预算；"实际应支持不同指标类型、单位、评测方法和**预算币种**"）、
docs/tasks.md TASK-P1-012。

四条硬约束：

1. **超限必须显式失败，不得静默截断**。静默截断意味着预算用尽后系统继续跑
   任务而无人察觉 —— 演进循环会一路空转到下个周期，成本不可预测。超上限时抛
   :class:`BudgetExhausted`，由调用方决定是降级、停止还是申请追加。
2. **币种可配置**。成本上限不写死 USD；不同项目用不同币种与单价表时，
   记账口径由外部配置决定（单价表见 TASK-P1-035 的 ``core/metrics/cost.py``）。
3. **扣减必须原子**。"查余额"与"扣减"之间不能有让出控制权的缝隙，否则并发
   任务会各自看到"还有余额"并同时扣超。这条约束在两个层面成立：
   同进程内由 :class:`InMemoryBudgetLedger` 的锁保证；跨进程由数据库的行级锁
   保证（``infrastructure/database/budget.py`` 的 ``SELECT … FOR UPDATE``）。
   TASK-P1-012 的 T1 ② 验的是后者 —— 真实并发写库。
4. **记账不可篡改**。流水只增不改，删掉一条就等于删掉一次花费的证据。
   落库时由数据库触发器强制（``ases_budget_account_guard`` /
   ``ases_append_only_guard``）。

**为什么把记账拆成一个端口**：``core`` 这一层不碰数据库，但它必须能回答
"扣了多少"。所以这里定义 :class:`BudgetLedger` 协议，实现分两处 ——
内存版在本模块，数据库版在 ``infrastructure/database/budget.py``。
领域逻辑（上限怎么算、币种是什么、周期怎么切）留在 core，不随存储位置漂移。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.metrics.cost import CostAmount, CurrencyMismatchError


class BudgetMetric(StrEnum):
    """预算计量维度。

    刻意不与具体模型或执行器绑定 —— 换执行器不该换掉预算口径。
    """

    DAILY_COST = "daily_cost"
    TOTAL_COST = "total_cost"
    TOKEN_USAGE = "token_usage"
    MODEL_CALLS = "model_calls"
    COMPUTE_MINUTES = "compute_minutes"
    WALL_CLOCK_MINUTES = "wall_clock_minutes"


class BudgetExhausted(Exception):  # noqa: N818 —— 名称由 docs/tasks.md TASK-P1-012 指定
    """预算耗尽。

    必须是显式异常而不是返回值 —— 调用方若忽略返回值就会静默超支，
    那正是本模块要防的事。
    """

    def __init__(self, metric: BudgetMetric, requested: Decimal, limit: Decimal) -> None:
        super().__init__(f"预算耗尽：{metric.value} 请求 {requested}，上限 {limit}")
        self.metric = metric
        self.requested = requested
        self.limit = limit


class Budget(BaseModel):
    """预算上限。全部字段可选 —— 只配置关心的维度即可。

    ``currency`` 描述成本类指标的单位；非成本类指标（token、调用次数、分钟）
    与币种无关。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: str = Field(default="USD", min_length=1, max_length=16)
    max_daily_cost: Decimal | None = Field(default=None, ge=0)
    max_total_cost: Decimal | None = Field(default=None, ge=0)
    max_token_usage: int | None = Field(default=None, ge=0)
    max_model_calls: int | None = Field(default=None, ge=0)
    max_compute_minutes: int | None = Field(default=None, ge=0)
    max_wall_clock_minutes: int | None = Field(default=None, ge=0)
    max_parallel_agents: int = Field(default=1, ge=1)

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    def limit_for(self, metric: BudgetMetric) -> Decimal | None:
        """返回某计量维度的上限；``None`` 表示该维度不设限。"""
        match metric:
            case BudgetMetric.DAILY_COST:
                return self.max_daily_cost
            case BudgetMetric.TOTAL_COST:
                return self.max_total_cost
            case BudgetMetric.TOKEN_USAGE:
                return _int_to_decimal(self.max_token_usage)
            case BudgetMetric.MODEL_CALLS:
                return _int_to_decimal(self.max_model_calls)
            case BudgetMetric.COMPUTE_MINUTES:
                return _int_to_decimal(self.max_compute_minutes)
            case BudgetMetric.WALL_CLOCK_MINUTES:
                return _int_to_decimal(self.max_wall_clock_minutes)

    def limits(self) -> dict[BudgetMetric, Decimal]:
        """全部已设置的限制。"""
        result: dict[BudgetMetric, Decimal] = {}
        for metric in BudgetMetric:
            limit = self.limit_for(metric)
            if limit is not None:
                result[metric] = limit
        return result


def _int_to_decimal(value: int | None) -> Decimal | None:
    return None if value is None else Decimal(value)


class SpendStatus(StrEnum):
    """一条流水的状态。"""

    RECORDED = "recorded"
    BLOCKED = "blocked"


def period_key(metric: BudgetMetric, moment: datetime) -> str:
    """计量周期的键。

    只有 ``daily_cost`` 按自然日重置，其余维度是生命周期累计。日切因此
    **不需要删除任何记录** —— 新的一天是新的一行累计额，累计额从零开始。
    这对 append-only 的账本很关键：账本不允许删除，日切就不能靠删除实现。
    """
    if metric is BudgetMetric.DAILY_COST:
        return f"day:{moment.date().isoformat()}"
    return "lifetime"


@dataclass(frozen=True, slots=True)
class SpendRequest:
    """一次扣减请求。

    把扣减需要的全部输入打包成一个不可变对象 —— 账本实现只需要知道
    "要扣多少、上限多少、算在哪个周期"，不需要知道预算的其余部分。
    """

    scope: str
    metric: BudgetMetric
    amount: Decimal
    currency: str
    limit: Decimal | None
    moment: datetime

    @property
    def period(self) -> str:
        return period_key(self.metric, self.moment)


@dataclass(frozen=True, slots=True)
class SpendRecord:
    """一条流水。"""

    scope: str
    metric: BudgetMetric
    amount: Decimal
    currency: str
    recorded_at: datetime
    day: date
    period: str
    status: SpendStatus = SpendStatus.RECORDED

    @property
    def is_blocked(self) -> bool:
        return self.status is SpendStatus.BLOCKED


@runtime_checkable
class BudgetLedger(Protocol):
    """记账的持久化端口。

    实现必须保证 :meth:`consume` 的原子性 —— 并发调用不会导致总额超限。
    """

    async def consume(self, request: SpendRequest) -> SpendRecord:
        """扣减；超限抛 :class:`BudgetExhausted` 且不计入累计额。"""
        ...

    async def spent(self, scope: str, metric: BudgetMetric, moment: datetime) -> Decimal:
        """已消耗量。"""
        ...

    async def entries(
        self, scope: str, metric: BudgetMetric | None = None
    ) -> tuple[SpendRecord, ...]:
        """流水。"""
        ...


class InMemoryBudgetLedger:
    """内存账本。

    同进程内的原子性由一把锁保证："查余额"和"记账"在同一临界区里完成，
    中间不让出控制权。它**不能**保证跨进程原子性 —— 两个进程各有一份
    内存账本，互相看不见。需要跨进程时用数据库账本
    （``infrastructure/database/budget.py``）。

    它仍然是有用的：单元测试不需要数据库，而 TASK-P1-012 的三条 T1 断言
    里有两条（超限失败、币种可配）与存储位置无关。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._totals: dict[tuple[str, str, str], Decimal] = {}
        self._entries: list[SpendRecord] = []

    async def consume(self, request: SpendRequest) -> SpendRecord:
        if request.amount < 0:
            raise ValueError(f"扣减金额不能为负：{request.amount}")

        key = (request.scope, request.metric.value, request.period)
        async with self._lock:
            current = self._totals.get(key, Decimal(0))
            if request.limit is not None and current + request.amount > request.limit:
                # 被拒绝的调用没有真正发生 —— 记进累计额会污染账目，
                # 但流水要留：反复出现的超限尝试本身就是需要被看见的事实。
                blocked = _make_record(request, SpendStatus.BLOCKED)
                self._entries.append(blocked)
                raise BudgetExhausted(
                    metric=request.metric,
                    requested=request.amount,
                    limit=request.limit,
                )

            record = _make_record(request, SpendStatus.RECORDED)
            self._totals[key] = current + request.amount
            self._entries.append(record)
            return record

    async def spent(self, scope: str, metric: BudgetMetric, moment: datetime) -> Decimal:
        key = (scope, metric.value, period_key(metric, moment))
        async with self._lock:
            return self._totals.get(key, Decimal(0))

    async def entries(
        self, scope: str, metric: BudgetMetric | None = None
    ) -> tuple[SpendRecord, ...]:
        async with self._lock:
            return tuple(
                record
                for record in self._entries
                if record.scope == scope and (metric is None or record.metric is metric)
            )


def _make_record(request: SpendRequest, status: SpendStatus) -> SpendRecord:
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


class BudgetManager:
    """预算账本。

    领域逻辑（上限、币种、周期）在这里；**记账委托给 :class:`BudgetLedger`**。
    默认用内存账本；需要跨进程原子性时注入数据库账本。

    查询方法是异步的 —— 它们读的是账本，而账本可能是数据库。
    """

    def __init__(
        self,
        budget: Budget,
        *,
        scope: str = "default",
        ledger: BudgetLedger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._budget = budget
        self._scope = scope
        self._ledger: BudgetLedger = ledger if ledger is not None else InMemoryBudgetLedger()
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    def _now(self) -> datetime:
        return self._clock()

    def use_clock(self, clock: Callable[[], datetime]) -> None:
        """替换时钟。用于日切任务与测试中的时间推进。"""
        self._clock = clock

    async def consume(self, metric: BudgetMetric, amount: Decimal) -> SpendRecord:
        """扣减。

        超限抛 :class:`BudgetExhausted`，且**不计入累计额** —— 被拒绝的调用
        没有真正发生，记进去会污染账目。
        """
        if amount < 0:
            raise ValueError(f"扣减金额不能为负：{amount}")
        request = SpendRequest(
            scope=self._scope,
            metric=metric,
            amount=amount,
            currency=self._budget.currency,
            limit=self._budget.limit_for(metric),
            moment=self._now(),
        )
        return await self._ledger.consume(request)

    async def consume_cost(
        self,
        metric: BudgetMetric,
        cost: CostAmount,
        *,
        scope: str | None = None,
    ) -> SpendRecord:
        """记一笔**已经换算好的**成本。

        **不替调用方换算币种。** 换算得由换算器做（``core/metrics/cost.py``）
        —— 那样三处记的才是同一个数字；这里偷偷换算一次，账本上的数就与
        适配器报告的数不一样了，而"不一样"这件事没有任何地方会报错。

        **币种对不上就报错。** 把 CNY 的钱记进 USD 的账户，比记错数字更坏 ——
        数字错了能对账，币种错了对出来的是"另一个项目的钱"。

        :param scope: 记在哪个名下。**默认是账本自己的作用域**。
        """
        if cost.currency != self._budget.currency:
            raise CurrencyMismatchError(
                f"预算币种是 {self._budget.currency}，这一笔是 {cost.currency}"
                f" —— 换算请在记账之前做（core/metrics/cost.py）"
            )
        request = SpendRequest(
            scope=scope or self._scope,
            metric=metric,
            amount=cost.amount,
            currency=cost.currency,
            limit=self._budget.limit_for(metric),
            moment=self._now(),
        )
        return await self._ledger.consume(request)

    async def spent(self, metric: BudgetMetric) -> Decimal:
        """已消耗量。

        ``DAILY_COST`` 按自然日重置，其余按生命周期累计。
        """
        return await self._ledger.spent(self._scope, metric, self._now())

    async def remaining(self, metric: BudgetMetric) -> Decimal | None:
        """剩余额度；该维度不设上限时返回 ``None``。"""
        limit = self._budget.limit_for(metric)
        if limit is None:
            return None
        return limit - await self.spent(metric)

    async def would_exceed(self, metric: BudgetMetric, amount: Decimal) -> bool:
        """若扣减 ``amount`` 是否会超限（不产生副作用）。"""
        limit = self._budget.limit_for(metric)
        if limit is None:
            return False
        return await self.spent(metric) + amount > limit

    async def ledger_entries(self) -> tuple[SpendRecord, ...]:
        """全部流水 —— 审计用。"""
        return await self._ledger.entries(self._scope)

    async def blocked_attempts(self) -> tuple[SpendRecord, ...]:
        """被拒绝的扣减尝试 —— 审计用。"""
        entries = await self._ledger.entries(self._scope)
        return tuple(record for record in entries if record.is_blocked)

    async def summary(self) -> dict[str, Decimal | None]:
        """当前用量快照。键为 ``<metric>.<spent|limit|remaining>``。"""
        result: dict[str, Decimal | None] = {}
        for metric in BudgetMetric:
            limit = self._budget.limit_for(metric)
            spent = await self.spent(metric)
            result[f"{metric.value}.spent"] = spent
            result[f"{metric.value}.limit"] = limit
            result[f"{metric.value}.remaining"] = None if limit is None else limit - spent
        return result


__all__ = [
    "Budget",
    "BudgetExhausted",
    "BudgetLedger",
    "BudgetManager",
    "BudgetMetric",
    "InMemoryBudgetLedger",
    "SpendRecord",
    "SpendRequest",
    "SpendStatus",
    "period_key",
]
