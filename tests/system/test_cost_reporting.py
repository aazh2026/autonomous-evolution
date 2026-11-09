"""成本上报的端到端 —— **同一次执行，三处的数是同一个**。

设计依据：docs/tasks.md TASK-P1-035 的两条验收：

- **T1 ①** 同一次执行在 Adapter 报告、预算账本、OTel span 属性三处的成本数值
  相等（同一函数产出）；
- **T1 ②** 换算单价变更后，三处同步变化。

**为什么这条要端到端地验。** 每一段单独看都对：换算器算得准、账本记得对、
观测写得对。可"三处相等"不是三段各自的性质 —— 它是**三段接在一起**才有的
性质。任何一段自己改一遍口径，三段单独看还是全绿，而三处已经不一样了。

**这里跑的是真的东西。** CLI 适配器真的起一个子进程（打桩 CLI 吐 NDJSON），
预算账本是真的内存账本，OTel 是真的 span。**不联网、不花钱、不碰工作区。**

**断言的是"同一个数"，不是"三个差不多的数"。** 所以比较的是十进制字符串 ——
``0.035000`` 与 ``0.035`` 在账上是同一笔钱，但在"三处相等"这条要求下它们
不是同一个数（量化步长不同）。金额差最后几位就是差钱。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider

from core.metrics.cost import CostAmount, CostConverter, CurrencyMismatchError, RateTable
from core.policy.budget import Budget, BudgetManager, BudgetMetric
from executors.base import ExecutionResult, ExecutionTask, SessionConfig
from executors.claude_code import ClaudeCodeAdapter
from infrastructure.observability.tracing import (
    COST_AMOUNT_ATTRIBUTE,
    COST_CURRENCY_ATTRIBUTE,
    SpanCostObserver,
)
from orchestration.cost_accounting import ExecutionCostAccountant
from orchestration.loop_a import build_loop_a_graph, run_loop_a
from tests.support.execution_loop import TASK_ID, StubHandlers, ready_state
from tests.unit.executors.stub_cli import claude_events, write_stub

#: 打桩 CLI 报出来的成本（**计价币种是 CNY** —— 换算要有事可做）。
CLI_COST = 0.25

#: 一份真实一点的用量：普通输入、普通输出、缓存读、缓存写。
USAGE: dict[str, int] = {
    "input_tokens": 12_000,
    "output_tokens": 3_500,
    "cache_read_input_tokens": 20_000,
    "cache_creation_input_tokens": 1_200,
}

#: 按 :func:`_usd_converter` 那张单价表算出来的钱 ——
#: 12 000×3 + 3 500×15 + 20 000×0.3 + 1 200×3.75，全部除以一百万，得 0.099。
EXPECTED_USD = Decimal("0.099")


class _CnyClaudeCodeAdapter(ClaudeCodeAdapter):
    """一个**用 CNY 计价**的 CLI。"""

    currency = "CNY"


def _converter(cny_rate: str = "0.14") -> CostConverter:
    """单价表：记账币种 USD，1 CNY = ``cny_rate`` USD。"""
    return CostConverter(
        RateTable.from_mapping(
            {
                "base_currency": "USD",
                "model_rates": {
                    "test-model": {
                        "currency": "USD",
                        "input_price_per_million": "3.00",
                        "output_price_per_million": "15.00",
                    }
                },
                "exchange_rates": {"USD": "1", "CNY": cny_rate},
            }
        )
    )


def _attributes(span: trace.Span) -> Mapping[str, Any]:
    """读 span 上的属性。

    **只有 SDK 的 span 才带属性** —— ``trace.Span`` 这个接口上并没有
    ``attributes``。所以这里显式转成 :class:`ReadableSpan`。
    """
    return cast(ReadableSpan, span).attributes or {}


def _usd_converter(
    *,
    input_price: str = "3.00",
    output_price: str = "15.00",
    cache_read_price: str = "0.30",
    cache_write_price: str = "3.75",
) -> CostConverter:
    """一张 USD 单价表 —— 记账币种与计价币种都是 USD。

    **带缓存价。** 缓存读与缓存写各有各的价 —— 表里没有它们，按用量推算出来
    的钱就会偏小，偏差核对会把它喊出来。
    """
    return CostConverter(
        RateTable.from_mapping(
            {
                "base_currency": "USD",
                "model_rates": {
                    "test-model": {
                        "currency": "USD",
                        "input_price_per_million": input_price,
                        "output_price_per_million": output_price,
                        "cache_read_price_per_million": cache_read_price,
                        "cache_write_price_per_million": cache_write_price,
                    }
                },
                "exchange_rates": {"USD": "1"},
            }
        )
    )


def _config(workspace: Path) -> SessionConfig:
    return SessionConfig(workspace=str(workspace), branch="agent/TASK-1")


def _task(session_id: str) -> ExecutionTask:
    return ExecutionTask(
        session_id=session_id,
        task_id="TASK-1",
        title="任务",
        instruction="把 README 的一行改掉",
        timeout_seconds=30,
    )


async def _execute(tmp_path: Path, converter: CostConverter | None) -> ExecutionResult:
    """真的跑一次 CLI，收集它的结果。"""
    script = write_stub(tmp_path / "cli", events=claude_events(cost_usd=CLI_COST))
    adapter = _CnyClaudeCodeAdapter(binary=str(script), cost_converter=converter)
    session = await adapter.create_session(_config(tmp_path / "ws"))
    await adapter.execute(_task(session.session_id))
    return await adapter.collect_result(session.session_id)


async def _three_places(tmp_path: Path, converter: CostConverter | None) -> tuple[str, str, str]:
    """跑一次执行，把三个地方的成本都收回来。

    返回 ``(适配器报告的, 账本记的, span 属性里的)`` —— **都是字符串**，
    因为字符串才是"逐位相同"能被比出来的形式。
    """
    result = await _execute(tmp_path, converter)

    # 账本的币种 = **记账币种**：配了单价表就是它的 base，没配就是 CLI 自己
    # 那个 —— 两者对不上时，下面那条断言会替我们喊出来。
    budget_currency = converter.currency if converter is not None else result.execution.currency
    budget = BudgetManager(Budget(currency=budget_currency, max_total_cost=Decimal("100")))
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("execution") as span:
        accountant = ExecutionCostAccountant(budget=budget, observer=SpanCostObserver(span))
        await accountant.record(
            result.execution.amount(),
            task_id="TASK-1",
            session_id=result.task_id,
        )

    entries = await budget.ledger_entries()
    assert len(entries) == 1, "这一次执行只该记一笔"
    attributes = _attributes(span)
    return (
        str(result.execution.cost),
        str(entries[0].amount),
        str(attributes[COST_AMOUNT_ATTRIBUTE]),
    )


class TestTheThreePlaces:
    """T1 ① —— **三处的成本数值相等**。"""

    async def test_the_three_amounts_are_identical(self, tmp_path: Path) -> None:
        """适配器报告的、账本记的、span 属性里的 —— 逐位相同。"""
        reported, booked, observed = await _three_places(tmp_path, _converter())
        assert reported == booked == observed

    async def test_the_amount_is_the_converted_one(self, tmp_path: Path) -> None:
        """0.25 CNY × 0.14 = 0.035 USD。"""
        reported, _, _ = await _three_places(tmp_path, _converter())
        assert reported == "0.035000"

    async def test_the_currencies_are_identical(self, tmp_path: Path) -> None:
        """金额一样还不够 —— **币种不一样的话那不是同一笔钱**。"""
        result = await _execute(tmp_path, _converter())
        assert result.execution.currency == "USD"

    async def test_without_a_rate_table_the_three_are_still_identical(self, tmp_path: Path) -> None:
        """没配单价表就原样保留 —— 三处照样相等。"""
        reported, booked, observed = await _three_places(tmp_path, None)
        assert reported == booked == observed
        assert reported == "0.250000"

    async def test_without_a_rate_table_a_foreign_budget_is_an_error(self, tmp_path: Path) -> None:
        """**没有单价表 + 币种对不上 = 报错**，不是悄悄按错价记账。

        换个币种记一笔账，比记错数字更坏 —— 数字错了能对账，币种错了对出来
        的是"另一个项目的钱"。
        """
        result = await _execute(tmp_path, None)
        budget = BudgetManager(Budget(currency="USD", max_total_cost=Decimal("100")))
        accountant = ExecutionCostAccountant(budget=budget, observer=_NullObserver())

        with pytest.raises(CurrencyMismatchError):
            await accountant.record(result.execution.amount(), task_id="TASK-1", session_id="s")

    async def test_the_ledger_records_the_accounting_currency(self, tmp_path: Path) -> None:
        """账本记的是记账币种 —— 不是 CLI 的计价币种。"""
        result = await _execute(tmp_path, _converter())
        budget = BudgetManager(Budget(currency="USD", max_total_cost=Decimal("100")))
        accountant = ExecutionCostAccountant(budget=budget, observer=_NullObserver())
        await accountant.record(result.execution.amount(), task_id="TASK-1", session_id="s")

        entries = await budget.ledger_entries()
        assert entries[0].currency == "USD"


class TestTheRateChanges:
    """T1 ② —— **换算单价变更后，三处同步变化**。"""

    async def test_a_new_rate_moves_all_three(self, tmp_path: Path) -> None:
        reported, booked, observed = await _three_places(tmp_path, _converter("0.20"))
        assert reported == booked == observed
        assert reported == "0.050000"

    async def test_the_old_rate_gives_the_old_amount(self, tmp_path: Path) -> None:
        reported, booked, observed = await _three_places(tmp_path, _converter("0.14"))
        assert reported == booked == observed
        assert reported == "0.035000"


class TestTheSameObject:
    """**同一个对象**被三处共用 —— 不是三处各算一遍。"""

    async def test_the_amount_passed_to_the_ledger_is_the_reported_one(
        self, tmp_path: Path
    ) -> None:
        result = await _execute(tmp_path, _converter())
        amount = result.execution.amount()

        assert amount == CostAmount(result.execution.cost, result.execution.currency)
        assert amount.currency == "USD"

    async def test_the_currency_attribute_is_written_next_to_the_amount(
        self, tmp_path: Path
    ) -> None:
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("execution") as span:
            await SpanCostObserver(span).observe(
                CostAmount(Decimal("0.035"), "USD"), task_id="TASK-1", session_id="s"
            )

        attributes = _attributes(span)
        assert attributes[COST_CURRENCY_ATTRIBUTE] == "USD"


class _NullObserver:
    """什么都不做的观测口 —— 只关心账本时用得上。"""

    __slots__ = ()

    async def observe(self, cost: CostAmount, *, task_id: str, session_id: str) -> bool:
        return True


class _RecordingObserver:
    """记下它见过的每一笔，以及那笔钱的两个标识。"""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def observe(self, cost: CostAmount, *, task_id: str, session_id: str) -> bool:
        self.calls.append((task_id, session_id))
        return True


class TestTheBudgetMetric:
    """成本记到哪个预算维度。"""

    async def test_the_cost_lands_in_the_total_cost_metric(self, tmp_path: Path) -> None:
        result = await _execute(tmp_path, _converter())
        budget = BudgetManager(Budget(currency="USD", max_total_cost=Decimal("100")))
        accountant = ExecutionCostAccountant(
            budget=budget,
            observer=_NullObserver(),
            metric=BudgetMetric.TOTAL_COST,
        )
        await accountant.record(result.execution.amount(), task_id="TASK-1", session_id="s")

        entries = await budget.ledger_entries()
        assert entries[0].metric is BudgetMetric.TOTAL_COST
        assert entries[0].amount == Decimal("0.035")


class TestTheLoopRecordsTheCost:
    """编排层接上之后 —— **执行完了，成本就进了账本**。"""

    async def test_the_execution_cost_lands_in_the_ledger(self) -> None:
        """打桩执行器报 0.5 CNY —— 那笔账就该是 0.5 CNY。"""
        budget = BudgetManager(Budget(currency="CNY", max_total_cost=Decimal("100")))
        handlers = StubHandlers(
            cost_accountant=ExecutionCostAccountant(budget=budget, observer=_NullObserver())
        )

        graph = build_loop_a_graph(handlers.handlers)
        await run_loop_a(graph, ready_state())

        entries = await budget.ledger_entries()
        assert len(entries) == 1
        assert entries[0].amount == Decimal("0.5")
        assert entries[0].currency == "CNY"

    async def test_the_observation_carries_the_task_and_the_session(self) -> None:
        """没有标识的那笔成本，事后对不上是谁花的。"""
        budget = BudgetManager(Budget(currency="CNY", max_total_cost=Decimal("100")))
        observer = _RecordingObserver()
        handlers = StubHandlers(
            cost_accountant=ExecutionCostAccountant(budget=budget, observer=observer)
        )

        graph = build_loop_a_graph(handlers.handlers)
        await run_loop_a(graph, ready_state())

        assert observer.calls == [(TASK_ID, "stub-executor-session-1")]

    async def test_the_ledger_uses_the_budgets_own_scope(self) -> None:
        """账本记在**预算自己的名下** —— 不是任务名下。"""
        budget = BudgetManager(Budget(currency="CNY", max_total_cost=Decimal("100")))
        handlers = StubHandlers(
            cost_accountant=ExecutionCostAccountant(budget=budget, observer=_NullObserver())
        )

        graph = build_loop_a_graph(handlers.handlers)
        await run_loop_a(graph, ready_state())

        entries = await budget.ledger_entries()
        assert entries[0].scope == "default"

    async def test_no_accountant_means_no_ledger(self) -> None:
        """没配预算与观测时不记 —— 但**执行照跑**。"""
        handlers = StubHandlers()
        graph = build_loop_a_graph(handlers.handlers)
        final = await run_loop_a(graph, ready_state())

        assert final.outcome is not None


class TestTheDeviation:
    """T2 —— **成本记账与实际调用量的偏差 < 2%**。

    **核对必须是两条独立的路径。** 一条是执行器报上来的实际花费，另一条是按
    单价表对**实际发生的 token 数**算出来的钱。拿同一条路径的两个数互相比，
    比出来的永远是 0 —— 那是自证，不是核对。

    **所以单价表过期了这件事是看得见的**：偏差会跳出来，而不是账照记、错照
    错，直到月底对账才发现。
    """

    async def _execute(
        self,
        tmp_path: Path,
        converter: CostConverter,
        *,
        cost: float = float(EXPECTED_USD),
    ) -> ExecutionResult:
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(cost_usd=cost, usage=USAGE),
        )
        adapter = ClaudeCodeAdapter(binary=str(script), cost_converter=converter)
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        return await adapter.collect_result(session.session_id)

    async def test_the_booked_cost_matches_the_actual_usage(self, tmp_path: Path) -> None:
        """单价表与执行器报的钱一致 —— 偏差是 0。"""
        result = await self._execute(tmp_path, _usd_converter())

        assert result.execution.cost == EXPECTED_USD
        assert result.execution.cost_deviation == Decimal(0)

    async def test_the_deviation_is_under_two_percent(self, tmp_path: Path) -> None:
        """任务书 T2 给的那条线。"""
        result = await self._execute(tmp_path, _usd_converter())

        deviation = result.execution.cost_deviation
        assert deviation is not None
        assert deviation <= Decimal("0.02")

    async def test_a_stale_rate_table_shows_up(self, tmp_path: Path) -> None:
        """单价表整体打对折 —— 推算出来的钱是 0.0495，与 0.099 差 100%。"""
        result = await self._execute(
            tmp_path,
            _usd_converter(
                input_price="1.50",
                output_price="7.50",
                cache_read_price="0.15",
                cache_write_price="1.875",
            ),
        )

        assert result.execution.cost_deviation == Decimal(1)

    async def test_the_three_places_still_agree_when_the_table_is_stale(
        self, tmp_path: Path
    ) -> None:
        """**偏差再大，三处还是同一个数** —— 核对不改变记账口径。

        核对是**看**账的，不是**改**账的。核对失败时账照样记 —— 只是那次核对
        没做成，偏差是 ``None`` 而不是 0。
        """
        result = await self._execute(
            tmp_path,
            _usd_converter(
                input_price="1.50",
                output_price="7.50",
                cache_read_price="0.15",
                cache_write_price="1.875",
            ),
        )

        budget = BudgetManager(Budget(currency="USD", max_total_cost=Decimal("100")))
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("execution") as span:
            accountant = ExecutionCostAccountant(budget=budget, observer=SpanCostObserver(span))
            await accountant.record(
                result.execution.amount(),
                task_id="TASK-1",
                session_id=result.task_id,
            )

        entries = await budget.ledger_entries()
        attributes = _attributes(span)
        assert str(result.execution.cost) == str(entries[0].amount)
        assert str(result.execution.cost) == str(attributes[COST_AMOUNT_ATTRIBUTE])
        assert result.execution.cost_deviation == Decimal(1)

    async def test_without_a_rate_table_there_is_no_deviation(self, tmp_path: Path) -> None:
        """没配单价表就**没有第二条路径** —— 对不了，不假装对过。"""
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(cost_usd=float(EXPECTED_USD), usage=USAGE),
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        result = await adapter.collect_result(session.session_id)

        assert result.execution.cost_deviation is None
        assert result.execution.cost == EXPECTED_USD
