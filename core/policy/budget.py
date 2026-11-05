"""Budget Manager —— 预算与配额管理。

设计依据：docs/design.md §3.1（Budget Manager 管理 Token、模型调用、计算资源和时间预算；
"实际应支持不同指标类型、单位、评测方法和**预算币种**"）、docs/tasks.md TASK-P1-012。

三条硬约束：

1. **超限必须显式失败，不得静默截断**。静默截断意味着预算用尽后系统继续跑任务
   而无人察觉 —— 演进循环会一路空转到下个周期，成本不可预测。超上限时抛
   :class:`BudgetExhausted`，由调用方决定是降级、停止还是申请追加。
2. **币种可配置**。成本上限不写死 USD；不同项目用不同币种与单价表时，
   记账口径由外部配置决定（单价表见 TASK-P1-035 的 ``core/metrics/cost.py``）。
3. **同进程内扣减必须原子**。``consume()`` 在检查与写入之间不能被让出，
   否则并发任务会各自看到"还有余额"并同时扣超。跨进程原子性由数据层的
   行级锁保证（阶段一增量 1c）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class SpendRecord(BaseModel):
    """一次扣减的账目条目。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: BudgetMetric
    amount: Decimal
    currency: str
    recorded_at: datetime
    day: date


class BudgetManager:
    """预算账本。

    线程/协程安全：``consume()`` 内部持锁完成"检查 + 记账"全过程。
    """

    def __init__(
        self,
        budget: Budget,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._budget = budget
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._spend: list[SpendRecord] = []
        self._blocked: list[SpendRecord] = []

    @property
    def budget(self) -> Budget:
        return self._budget

    def _now(self) -> datetime:
        return self._clock()

    def use_clock(self, clock: Callable[[], datetime]) -> None:
        """替换时钟。用于日切任务与测试中的时间推进。"""
        self._clock = clock

    def _daily_spend(self, metric: BudgetMetric) -> Decimal:
        today = self._now().date()
        return sum(
            (
                record.amount
                for record in self._spend
                if record.metric is metric and record.day == today
            ),
            start=Decimal(0),
        )

    def _lifetime_spend(self, metric: BudgetMetric) -> Decimal:
        return sum(
            (record.amount for record in self._spend if record.metric is metric),
            start=Decimal(0),
        )

    def spent(self, metric: BudgetMetric) -> Decimal:
        """已消耗量。

        ``DAILY_COST`` 按自然日重置，其余按生命周期累计。
        """
        if metric is BudgetMetric.DAILY_COST:
            return self._daily_spend(metric)
        return self._lifetime_spend(metric)

    def remaining(self, metric: BudgetMetric) -> Decimal | None:
        """剩余额度；该维度不设上限时返回 ``None``。"""
        limit = self._budget.limit_for(metric)
        if limit is None:
            return None
        return limit - self.spent(metric)

    def would_exceed(self, metric: BudgetMetric, amount: Decimal) -> bool:
        """若扣减 ``amount`` 是否会超限（不产生副作用）。"""
        limit = self._budget.limit_for(metric)
        if limit is None:
            return False
        return self.spent(metric) + amount > limit

    async def consume(self, metric: BudgetMetric, amount: Decimal) -> SpendRecord:
        """原子扣减。

        超限抛 :class:`BudgetExhausted`，且**不记账** —— 被拒绝的调用没有
        真正发生，记进去会污染账目。
        """
        if amount < 0:
            raise ValueError(f"扣减金额不能为负：{amount}")

        async with self._lock:
            now = self._now()
            limit = self._budget.limit_for(metric)
            current = self.spent(metric)
            if limit is not None and current + amount > limit:
                self._blocked.append(
                    SpendRecord(
                        metric=metric,
                        amount=amount,
                        currency=self._budget.currency,
                        recorded_at=now,
                        day=now.date(),
                    )
                )
                raise BudgetExhausted(metric=metric, requested=amount, limit=limit)

            record = SpendRecord(
                metric=metric,
                amount=amount,
                currency=self._budget.currency,
                recorded_at=now,
                day=now.date(),
            )
            self._spend.append(record)
            return record

    def blocked_attempts(self) -> tuple[SpendRecord, ...]:
        """被拒绝的扣减尝试 —— 审计用。"""
        return tuple(self._blocked)

    def ledger(self) -> tuple[SpendRecord, ...]:
        """全部记账条目 —— 审计用。"""
        return tuple(self._spend)

    def reset_daily(self) -> None:
        """清空当日记账。

        跨日自动重置由 :meth:`spent` 的按日聚合实现；本方法用于测试与手工重置。
        整体替换列表是原子的，不会与并发的 :meth:`consume` 交错。
        """
        today = self._now().date()
        self._spend = [record for record in self._spend if record.day != today]

    def summary(self) -> dict[str, Decimal | None]:
        """当前用量快照。键为 ``<metric>.<spent|limit|remaining>``。"""
        result: dict[str, Decimal | None] = {}
        for metric in BudgetMetric:
            limit = self._budget.limit_for(metric)
            spent = self.spent(metric)
            result[f"{metric.value}.spent"] = spent
            result[f"{metric.value}.limit"] = limit
            result[f"{metric.value}.remaining"] = None if limit is None else limit - spent
        return result


__all__ = [
    "Budget",
    "BudgetExhausted",
    "BudgetManager",
    "BudgetMetric",
    "SpendRecord",
]
