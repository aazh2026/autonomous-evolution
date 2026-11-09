"""成本记账与观测 —— **同一个金额，三处同源**。

设计依据：docs/tasks.md TASK-P1-035（T1 ① 同一次执行在 Adapter 报告、预算
账本、OTel span 属性三处的成本数值相等；T1 ② 换算单价变更后，三处同步变化）。

**这里测的是记账这一段。** 换算在适配器那边（``tests/unit/executors/``），
观测的两个实现在基础设施那边（``tests/unit/infrastructure/``），而"三处相等"
这件事要等三段都接上才谈得上 —— 它由 ``tests/system/test_cost_reporting.py``
端到端地验。

**为什么记两遍而不是只记一遍。** 账本记的是"花了多少"，观测记的是"这次执行
花了多少"——同一个事实的两种用途。少记一处，那一处就成了"这段时间没发生
事"，而不是"这段时间的钱没记"。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.metrics.cost import CostAmount, CurrencyMismatchError
from core.policy.budget import (
    Budget,
    BudgetExhausted,
    BudgetManager,
    BudgetMetric,
    SpendStatus,
)
from orchestration.cost_accounting import ExecutionCostAccountant

FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class RecordingObserver:
    """记录它见过的每一个金额 —— **并且记录顺序**。"""

    def __init__(self, log: list[str] | None = None) -> None:
        self.seen: list[CostAmount] = []
        self.calls: list[tuple[str, str]] = []
        self.log = log if log is not None else []

    async def observe(self, cost: CostAmount, *, task_id: str, session_id: str) -> bool:
        self.seen.append(cost)
        self.calls.append((task_id, session_id))
        self.log.append("observe")
        return True


def _budget(
    *,
    currency: str = "USD",
    max_total_cost: Decimal | None = None,
) -> BudgetManager:
    return BudgetManager(
        Budget(currency=currency, max_total_cost=max_total_cost),
        scope="task-1",
        clock=lambda: FIXED_NOW,
    )


class TestTheSameAmount:
    """**同一个数**进账本、进观测。"""

    async def test_the_ledger_and_the_observer_see_the_same_amount(self) -> None:
        budget = _budget()
        observer = RecordingObserver()
        accountant = ExecutionCostAccountant(budget=budget, observer=observer)
        cost = CostAmount(Decimal("0.25"), "USD")

        await accountant.record(cost, task_id="task-1", session_id="session-1")

        entries = await budget.ledger_entries()
        assert len(entries) == 1
        assert entries[0].amount == cost.amount
        assert entries[0].currency == cost.currency
        assert observer.seen == [cost]

    async def test_the_identifiers_travel_with_the_amount(self) -> None:
        budget = _budget()
        observer = RecordingObserver()
        accountant = ExecutionCostAccountant(budget=budget, observer=observer)

        await accountant.record(
            CostAmount(Decimal("1"), "USD"), task_id="task-9", session_id="session-9"
        )

        assert observer.calls == [("task-9", "session-9")]
        assert (await budget.ledger_entries())[0].scope == "task-1"

    async def test_the_observation_happens_before_the_booking(self) -> None:
        """钱已经花了 —— 预算超了不该让这次花费从观测里消失。"""
        log: list[str] = []
        budget = _budget()
        observer = RecordingObserver(log)
        accountant = ExecutionCostAccountant(budget=budget, observer=observer)

        await accountant.record(CostAmount(Decimal("1"), "USD"), task_id="t", session_id="s")

        assert log == ["observe"]


class TestOverBudget:
    """超预算 —— **不抛异常，但留证据**。"""

    async def test_the_result_says_it_was_not_recorded(self) -> None:
        budget = _budget(max_total_cost=Decimal("1"))
        observer = RecordingObserver()
        accountant = ExecutionCostAccountant(budget=budget, observer=observer)

        accounting = await accountant.record(
            CostAmount(Decimal("2"), "USD"), task_id="task-1", session_id="session-1"
        )

        assert accounting.recorded is False
        assert accounting.cost == CostAmount(Decimal("2"), "USD")

    async def test_the_observation_still_happened(self) -> None:
        """观测不能因为超预算就丢掉这次花费。"""
        budget = _budget(max_total_cost=Decimal("1"))
        observer = RecordingObserver()
        accountant = ExecutionCostAccountant(budget=budget, observer=observer)

        await accountant.record(CostAmount(Decimal("2"), "USD"), task_id="t", session_id="s")

        assert observer.seen == [CostAmount(Decimal("2"), "USD")]

    async def test_the_blocked_attempt_is_left_in_the_ledger(self) -> None:
        """账本里那条"被拒绝"的流水就是超预算的证据。"""
        budget = _budget(max_total_cost=Decimal("1"))
        accountant = ExecutionCostAccountant(budget=budget, observer=RecordingObserver())

        await accountant.record(CostAmount(Decimal("2"), "USD"), task_id="t", session_id="s")

        blocked = await budget.blocked_attempts()
        assert len(blocked) == 1
        assert blocked[0].status is SpendStatus.BLOCKED
        assert blocked[0].amount == Decimal("2")

    async def test_a_cost_within_the_budget_is_recorded(self) -> None:
        budget = _budget(max_total_cost=Decimal("10"))
        accountant = ExecutionCostAccountant(budget=budget, observer=RecordingObserver())

        accounting = await accountant.record(
            CostAmount(Decimal("2"), "USD"), task_id="t", session_id="s"
        )

        assert accounting.recorded is True


class TestTheCurrency:
    """币种对不上是**接线错了**，不是预算问题。"""

    async def test_the_mismatch_is_not_swallowed(self) -> None:
        """把 CNY 的钱记进 USD 的账户 —— 比记错数字更坏。"""
        budget = _budget(currency="USD")
        accountant = ExecutionCostAccountant(budget=budget, observer=RecordingObserver())

        with pytest.raises(CurrencyMismatchError):
            await accountant.record(CostAmount(Decimal("1"), "CNY"), task_id="t", session_id="s")

    async def test_the_mismatch_does_not_reach_the_ledger(self) -> None:
        budget = _budget(currency="USD")
        accountant = ExecutionCostAccountant(budget=budget, observer=RecordingObserver())

        with pytest.raises(CurrencyMismatchError):
            await accountant.record(CostAmount(Decimal("1"), "CNY"), task_id="t", session_id="s")

        assert await budget.ledger_entries() == ()


class TestTheMetric:
    """记到哪个预算维度 —— 可配。"""

    async def test_the_daily_metric_is_recorded_separately(self) -> None:
        budget = _budget()
        accountant = ExecutionCostAccountant(
            budget=budget,
            observer=RecordingObserver(),
            metric=BudgetMetric.DAILY_COST,
        )

        await accountant.record(CostAmount(Decimal("1"), "USD"), task_id="t", session_id="s")

        entries = await budget.ledger_entries()
        assert entries[0].metric is BudgetMetric.DAILY_COST
        assert entries[0].period == "day:2026-01-01"

    async def test_the_default_metric_is_the_lifetime_cost(self) -> None:
        budget = _budget()
        accountant = ExecutionCostAccountant(budget=budget, observer=RecordingObserver())

        await accountant.record(CostAmount(Decimal("1"), "USD"), task_id="t", session_id="s")

        assert (await budget.ledger_entries())[0].period == "lifetime"


class TestTheBudgetExhausted:
    """账本抛的是哪一种错 —— 记账这一层自己认得出来。"""

    async def test_the_ledger_raises_budget_exhausted(self) -> None:
        budget = _budget(max_total_cost=Decimal("1"))
        with pytest.raises(BudgetExhausted):
            await budget.consume_cost(BudgetMetric.TOTAL_COST, CostAmount(Decimal("2"), "USD"))
