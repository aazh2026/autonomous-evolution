"""成本核对 —— **记账的数 vs. 实际调用量推算的数**。

设计依据：docs/tasks.md TASK-P1-035 的 T2（成本记账与实际调用量偏差 < 2%）。

**为什么核对必须是两条独立的路径。** 拿执行器报上来的钱跟它自己比，比出来
的永远是 0 —— 那是自证，不是核对。真正的核对是：一边是执行器报上来的实际
花费，另一边是按单价表对**实际发生的 token 数**算出来的钱。单价表过期了、
汇率写错了、换算做了两次 —— 那些错都会在这两条路径之间露出来。

**差多少要能被问出来。** 核对的结果落在
:attr:`executors.base.ExecutionMetrics.cost_deviation` 上；``None`` 表示
**没核对**（缺用量、缺单价、没配单价表），而不是"核对过、差 0" —— 这两件事
必须分得开。

**这里跑的是真的子进程。** 打桩 CLI 真的吐 NDJSON —— 用量与成本都是从
**结果事件**里读出来的，与真实 CLI 报的一样。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from core.metrics.cost import COST_DEVIATION_TOLERANCE, CostConverter, RateTable
from executors.base import ExecutionResult, ExecutionTask, SessionConfig
from executors.claude_code import ClaudeCodeAdapter
from tests.unit.executors.stub_cli import claude_events, write_stub

#: 一份真实一点的用量：普通输入、普通输出、缓存读、缓存写。
USAGE: dict[str, int] = {
    "input_tokens": 12_000,
    "output_tokens": 3_500,
    "cache_read_input_tokens": 20_000,
    "cache_creation_input_tokens": 1_200,
}

#: 按下面那张单价表算出来的钱 —— 12 000×3 + 3 500×15 + 20 000×0.3 + 1 200×3.75，
#: 全部除以一百万，得 0.099。**这个数是算出来的，不是抄来的。**
EXPECTED_USD = Decimal("0.099")


def _converter(
    *,
    input_price: str = "3.00",
    output_price: str = "15.00",
    cache_read_price: str = "0.30",
    cache_write_price: str = "3.75",
) -> CostConverter:
    """一张单价表：记账币种 USD，模型 ``test-model``。"""
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


async def _run(
    tmp_path: Path,
    *,
    converter: CostConverter | None = None,
    cost: float = float(EXPECTED_USD),
    usage: dict[str, int] | None = None,
    model: str = "test-model",
) -> ExecutionResult:
    """真的起一次 CLI，收集它的结果。"""
    script = write_stub(
        tmp_path / "cli",
        events=claude_events(cost_usd=cost, usage=usage, model=model),
    )
    adapter = ClaudeCodeAdapter(binary=str(script), cost_converter=converter)
    session = await adapter.create_session(_config(tmp_path / "ws"))
    await adapter.execute(_task(session.session_id))
    return await adapter.collect_result(session.session_id)


class TestTheDeviationIsMeasured:
    """**偏差量出来了** —— 而且是对两条独立路径量的。"""

    async def test_a_matching_rate_table_has_no_deviation(self, tmp_path: Path) -> None:
        """单价表与执行器报的钱一致 —— 偏差是 0。"""
        result = await _run(tmp_path, converter=_converter(), usage=USAGE)

        assert result.execution.cost_deviation == Decimal(0)

    async def test_the_deviation_is_relative(self, tmp_path: Path) -> None:
        """单价表整体打对折 —— 推算出来的钱是 0.0495，与 0.099 差 100%。"""
        result = await _run(
            tmp_path,
            converter=_converter(
                input_price="1.50",
                output_price="7.50",
                cache_read_price="0.15",
                cache_write_price="1.875",
            ),
            usage=USAGE,
        )

        assert result.execution.cost_deviation == Decimal(1)

    async def test_a_stale_rate_table_is_visible(self, tmp_path: Path) -> None:
        """单价表过期了 —— 偏差会**超过容忍线**，这就是它该有的样子。"""
        result = await _run(
            tmp_path,
            converter=_converter(
                input_price="1.50",
                output_price="7.50",
                cache_read_price="0.15",
                cache_write_price="1.875",
            ),
            usage=USAGE,
        )

        deviation = result.execution.cost_deviation
        assert deviation is not None
        assert deviation > COST_DEVIATION_TOLERANCE

    async def test_the_deviation_is_not_affected_by_the_currency(self, tmp_path: Path) -> None:
        """换算只做一次 —— 偏差量的是"表算的"与"报来的"，不是"换算前后"。"""
        result = await _run(tmp_path, converter=_converter(), usage=USAGE)

        assert result.execution.cost == EXPECTED_USD
        assert result.execution.cost_deviation == Decimal(0)


class TestWhenItCannotBeChecked:
    """**核对不了就说核对不了** —— 缺任何一件都不猜。"""

    async def test_without_a_rate_table_there_is_no_deviation(self, tmp_path: Path) -> None:
        """没配单价表就没有"表算出来的那个数" —— 对不了。"""
        result = await _run(tmp_path, usage=USAGE)

        assert result.execution.cost_deviation is None

    async def test_without_usage_there_is_no_deviation(self, tmp_path: Path) -> None:
        """没读到用量 —— 拿不出第二条路径。"""
        result = await _run(tmp_path, converter=_converter(), usage=None)

        assert result.execution.cost_deviation is None

    async def test_an_unknown_model_does_not_break_the_execution(self, tmp_path: Path) -> None:
        """单价表里没有这个模型 —— **核对失败，不是记账失败**。

        执行已经做完了，账也记了。为一个核对失败把整次执行掀翻是本末倒置 ——
        但那次核对确实没做成，所以偏差是 ``None``，不是 0。
        """
        result = await _run(tmp_path, converter=_converter(), usage=USAGE, model="gpt-9")

        assert result.execution.cost_deviation is None
        assert result.execution.cost == EXPECTED_USD

    async def test_the_execution_still_completes(self, tmp_path: Path) -> None:
        """核对不了 —— **执行照跑**，账照记。"""
        result = await _run(tmp_path, converter=_converter(), usage=USAGE, model="gpt-9")

        assert result.status.value == "completed"
