"""按项目分别核算的预算账本。

设计依据：docs/tasks.md ``TASK-P4-010``（验收条件"两个项目并行演进，任务 /
候选 / 决策互不串扰；**预算分别核算**"）、R-016；docs/design.md §3.1（Budget
Manager 管理 Token、模型调用、计算资源和时间预算）、§7（治理策略变更需审批）。

**"分别核算"不是"分两本账"。** 一本账、两条记录也行 —— 但那样"这个项目花
了多少"要靠人把流水按 scope 分组求和。求和是人做的，人会忘。所以**账本里每
一行的 scope 就是项目的 scope**（``project:<id>``），而**取数的那一层**直接
按项目给数：``spent(scope, …)`` 返回的数就是**这个项目**花的，不是"某个
scope 的"。

**为什么扣减时以作用域为准，而不是以请求为准。** ``SpendRequest.scope`` 是
必填字段，调用方总要填点什么；填错了就会把 A 项目的花记到 B 项目的账上 ——
而两笔账都还在，谁也不会发现。所以这里**以 :class:`ProjectScope` 为准**，
请求里的 scope 一律被覆盖。**记错项目比记错数字更难发现**：数字错了能对
账，项目记错了对出来的是"另一个项目的钱"。

**上限从哪来。** 每个项目有自己的 :class:`core.policy.budget.Budget` ——
:func:`core.policy.budget.Budget.limit_for` 给出该维度的上限。上限**跟着项
目走**，不跟账本走：账本是记钱的，项目才是花钱的那一方。

**周期是"按 metric 定的"，不是"按项目定的"。** 日成本按自然日重置，其余按
生命周期累计（见 ``core/policy/budget.py`` 的 ``period_key``）。两个项目同一
天的日成本各是各的 —— 账号行的主键是 ``(scope, metric, period)``，scope 不
同就是不同的行。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from core.policy.budget import (
    Budget,
    BudgetMetric,
    SpendRecord,
    SpendRequest,
)
from core.project.scope import ProjectScope
from infrastructure.database.budget import PostgresBudgetLedger


@dataclass(frozen=True, slots=True)
class ProjectSpend:
    """一个项目在某维度上的花销 —— **一行就是一个项目**。

    :param project_id: 项目 id。
    :param scope: 账本里的 scope 键（``project:<id>``）。
    :param metric: 计量维度。
    :param spent: 已消耗量。
    :param limit: 该项目该维度的上限；``None`` 表示不设限。
    :param entries: 流水条数 —— **被拒绝的尝试也算一条**。
    """

    project_id: str
    scope: str
    metric: BudgetMetric
    spent: Decimal
    limit: Decimal | None = None
    entries: int = 0

    @property
    def remaining(self) -> Decimal | None:
        """还剩多少 —— **不设限就不知道，所以是 ``None`` 而不是无穷大**。"""
        if self.limit is None:
            return None
        return self.limit - self.spent

    @property
    def exhausted(self) -> bool:
        """这个项目的这条额度用完了吗。"""
        return self.limit is not None and self.spent >= self.limit


@dataclass(frozen=True, slots=True)
class ProjectSpendReport:
    """一份按项目分开的花销报表。

     **"分别核算"的产物。** 报表的价值不在总数 —— 总数只说明"两个项目一共花
    了多少"，那与分别核算无关。价值在于**每一行都能对回它自己那个项目的账**
     ：这一行是 A 项目的，那 A 项目的流水必须只落在 ``project:A`` 这一行里。
    """

    metric: BudgetMetric
    rows: tuple[ProjectSpend, ...] = ()

    @property
    def total_spent(self) -> Decimal:
        """合计 —— **只是各行之和，不作别的用途**。"""
        return sum((row.spent for row in self.rows), Decimal(0))

    def row_for(self, project_id: str) -> ProjectSpend | None:
        """取某个项目的那一行。"""
        for row in self.rows:
            if row.project_id == project_id:
                return row
        return None


class ProjectBudgetLedger:
    """按项目记账的账本 —— 底层还是那个账本。

    **不另起一套账。** 记账的原子性、超限拦截、流水不可篡改都由
    :class:`PostgresBudgetLedger` 负责（数据库行级锁 + 触发器）。这里只做
    一件事：**把"项目"翻译成"scope"，并且让调用方没法绕过它**。
    """

    def __init__(self, ledger: PostgresBudgetLedger) -> None:
        self._ledger = ledger

    @property
    def ledger(self) -> PostgresBudgetLedger:
        """底层账本 —— 需要流水明细时直接用它。"""
        return self._ledger

    async def consume(self, scope: ProjectScope, request: SpendRequest) -> SpendRecord:
        """扣减 —— **记在 ``scope`` 指的那个项目名下**。

        **请求里的 scope 一律被覆盖。** 见模块文档。
        """
        return await self._ledger.consume(replace(request, scope=scope.budget_scope))

    async def spent(
        self,
        scope: ProjectScope,
        metric: BudgetMetric,
        moment: datetime,
    ) -> Decimal:
        """这个项目在某维度上花了多少。"""
        return await self._ledger.spent(scope.budget_scope, metric, moment)

    async def entries(
        self,
        scope: ProjectScope,
        metric: BudgetMetric | None = None,
    ) -> tuple[SpendRecord, ...]:
        """这个项目的流水 —— **别的项目的流水不在这儿**。"""
        return await self._ledger.entries(scope.budget_scope, metric)

    async def report(
        self,
        scopes: Sequence[ProjectScope],
        metric: BudgetMetric,
        moment: datetime,
        *,
        budgets: dict[str, Budget] | None = None,
    ) -> ProjectSpendReport:
        """出一份按项目分开的花销报表。

        :param scopes: 要出报的项目作用域。**按传入顺序出报表** —— 报表的
            行序不该由数据库的返回顺序决定。
        :param metric: 计量维度。
        :param moment: "现在" —— 决定周期（今天 / 生命周期）。
        :param budgets: 各项目的预算，用于补上"还剩多少"。缺了就不补上限 ——
            **不猜**。
        """
        rows: list[ProjectSpend] = []
        for scope in scopes:
            spent = await self.spent(scope, metric, moment)
            entries = await self.entries(scope, metric)
            limit = None
            if budgets is not None:
                budget = budgets.get(scope.project_id)
                if budget is not None:
                    limit = budget.limit_for(metric)
            rows.append(
                ProjectSpend(
                    project_id=scope.project_id,
                    scope=scope.budget_scope,
                    metric=metric,
                    spent=spent,
                    limit=limit,
                    entries=len(entries),
                )
            )
        return ProjectSpendReport(metric=metric, rows=tuple(rows))


class ProjectBudget:
    """一个项目的预算 —— **上限与作用域绑在一起**。

    **为什么不直接用 ``BudgetManager``。** ``BudgetManager`` 的 scope 是构造
    时传进去的一个字符串，而且它**不校验**那个字符串对不对。这里多了一件事：
    作用域是 :class:`ProjectScope`，构造时就保证了它能被解析回项目 id；扣减
    时也只可能记在这个项目名下。
    """

    def __init__(
        self,
        scope: ProjectScope,
        budget: Budget,
        ledger: ProjectBudgetLedger,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._scope = scope
        self._budget = budget
        self._ledger = ledger
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def scope(self) -> ProjectScope:
        return self._scope

    @property
    def budget(self) -> Budget:
        return self._budget

    async def consume(self, metric: BudgetMetric, amount: Decimal) -> SpendRecord:
        """扣这个项目的预算 —— **超它自己的上限就报错**。"""
        request = SpendRequest(
            scope=self._scope.budget_scope,
            metric=metric,
            amount=amount,
            currency=self._budget.currency,
            limit=self._budget.limit_for(metric),
            moment=self._clock(),
        )
        return await self._ledger.consume(self._scope, request)

    async def spent(self, metric: BudgetMetric) -> Decimal:
        """这个项目在某维度上花了多少。"""
        return await self._ledger.spent(self._scope, metric, self._clock())

    async def remaining(self, metric: BudgetMetric) -> Decimal | None:
        """还剩多少 —— **不设限就是 ``None``**。"""
        limit = self._budget.limit_for(metric)
        if limit is None:
            return None
        return limit - await self.spent(metric)


__all__ = [
    "ProjectBudget",
    "ProjectBudgetLedger",
    "ProjectSpend",
    "ProjectSpendReport",
]
