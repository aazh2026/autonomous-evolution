"""适配器里的成本换算 —— **换一次，后面的都拿这个数**。

设计依据：docs/tasks.md TASK-P1-035（T1 ① 同一次执行在 Adapter 报告、预算
账本、OTel span 属性三处的成本数值相等；T1 ② 换算单价变更后，三处同步变化）。

**为什么换算落在适配器。** 只有这里知道"CLI 报来的这个数是哪个币种的"。
账本与观测只看得见已经换算好的金额 —— 让它们各自再算一遍，等于把一条口径
复制三份，而**改价时漏掉的那一份不会报错**，它只会安静地记下另一个数。

**"三处相等"是从这里开始的。** 适配器报出去的那个数，与后面账本记的、观测
写的，必须是同一个。这里守住换算这一步；端到端的那条断言在
``tests/system/test_cost_reporting.py``。

**这里跑的是真的子进程。** 打桩 CLI 真的吐 NDJSON —— 成本是从**结果事件**
里读出来的，与真实 CLI 报的一样。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from core.metrics.cost import CostAmount, CostConverter, RateTable
from executors.base import ExecutionResult, ExecutionTask, SessionConfig
from executors.claude_code import ClaudeCodeAdapter
from tests.unit.executors.stub_cli import claude_events, write_stub


class _CnyClaudeCodeAdapter(ClaudeCodeAdapter):
    """一个**用 CNY 计价**的 CLI —— 计价币种与记账币种不是一回事。"""

    currency = "CNY"


def _converter(cny_rate: str = "0.14") -> CostConverter:
    """一张单价表：记账币种 USD，1 CNY = ``cny_rate`` USD。"""
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


async def _run(
    tmp_path: Path,
    adapter_class: type[ClaudeCodeAdapter],
    *,
    converter: CostConverter | None = None,
    cost: float = 0.25,
) -> ExecutionResult:
    """真的起一次 CLI，收集它的结果。"""
    script = write_stub(tmp_path / "cli", events=claude_events(cost_usd=cost))
    adapter = adapter_class(binary=str(script), cost_converter=converter)
    session = await adapter.create_session(_config(tmp_path / "ws"))
    await adapter.execute(_task(session.session_id))
    return await adapter.collect_result(session.session_id)


class TestWithoutARateTable:
    """没配单价表 —— **不猜价**，原样保留 CLI 报出来的金额与币种。"""

    async def test_the_reported_currency_is_the_clis_own(self, tmp_path: Path) -> None:
        result = await _run(tmp_path, _CnyClaudeCodeAdapter)

        assert result.execution.currency == "CNY"
        assert result.execution.cost == Decimal("0.25")

    async def test_the_default_currency_is_usd(self, tmp_path: Path) -> None:
        result = await _run(tmp_path, ClaudeCodeAdapter)

        assert result.execution.currency == "USD"


class TestWithARateTable:
    """配了单价表 —— **换算在适配器里做一次**。"""

    async def test_the_amount_is_converted_to_the_accounting_currency(self, tmp_path: Path) -> None:
        result = await _run(tmp_path, _CnyClaudeCodeAdapter, converter=_converter())

        assert result.execution.currency == "USD"
        assert result.execution.cost == Decimal("0.035000")

    async def test_the_reported_amount_is_the_converted_one(self, tmp_path: Path) -> None:
        """报出去的那个数就是换算器算出来的那个数 —— 不是另一个。"""
        converter = _converter()
        result = await _run(tmp_path, _CnyClaudeCodeAdapter, converter=converter)

        expected = converter.convert(CostAmount(Decimal("0.25"), "CNY"))
        assert result.execution.cost == expected.amount
        assert result.execution.currency == expected.currency

    async def test_the_amount_is_quantized(self, tmp_path: Path) -> None:
        """账本那一列是 ``Numeric(20, 6)`` —— 量化步长必须一样。"""
        result = await _run(tmp_path, _CnyClaudeCodeAdapter, converter=_converter())

        assert result.execution.cost.as_tuple().exponent == -6

    async def test_the_same_currency_is_not_converted_twice(self, tmp_path: Path) -> None:
        """CLI 本来就用 USD 记账 —— 再换一次就成两次了。"""
        result = await _run(tmp_path, ClaudeCodeAdapter, converter=_converter())

        assert result.execution.cost == Decimal("0.25")
        assert result.execution.currency == "USD"


class TestTheRateChanges:
    """T1 ② —— **换算单价变更后，三处同步变化**。

    这里验的是最前面那一处：适配器报出来的数跟着单价走。账本与观测拿的是
    这个数，所以它们也跟着走 —— 端到端的那条断言在
    ``tests/system/test_cost_reporting.py``。
    """

    async def test_a_new_rate_moves_the_reported_amount(self, tmp_path: Path) -> None:
        result = await _run(tmp_path, _CnyClaudeCodeAdapter, converter=_converter("0.20"))

        assert result.execution.cost == Decimal("0.050000")
        assert result.execution.currency == "USD"

    async def test_the_old_rate_gives_the_old_amount(self, tmp_path: Path) -> None:
        result = await _run(tmp_path, _CnyClaudeCodeAdapter, converter=_converter("0.14"))

        assert result.execution.cost == Decimal("0.035000")


class TestTheReportedAmount:
    """适配器报出来的那个数，**可以原样交给账本与观测**。"""

    async def test_the_amount_carries_both_the_number_and_the_currency(
        self, tmp_path: Path
    ) -> None:
        result = await _run(tmp_path, _CnyClaudeCodeAdapter, converter=_converter())

        assert result.execution.amount() == CostAmount(Decimal("0.035000"), "USD")

    async def test_deriving_the_amount_twice_gives_the_same_number(self, tmp_path: Path) -> None:
        """重复派生是幂等的 —— 所以账本与观测拿到的与适配器报出来的是同一个。"""
        result = await _run(tmp_path, _CnyClaudeCodeAdapter, converter=_converter())

        assert result.execution.amount() == result.execution.amount()
        assert result.execution.amount().amount == result.execution.cost
