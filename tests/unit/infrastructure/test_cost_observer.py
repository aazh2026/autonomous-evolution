"""成本观测的两个实现 —— **同一个端口，换后端不用改调用方**。

设计依据：docs/tasks.md TASK-P1-035（T1 ① 三处成本数值相等）。

**端口在领域层，实现在这里。** :class:`core.metrics.cost.CostObserver` 是领域
层里的一个协议；OTel 与 Langfuse 各有一个实现。编排层只认那个协议，所以换
观测后端不用动调用方 —— 而"换后端"迟早会发生。

**金额一律写成字符串。** OTel 的属性类型里没有十进制，Langfuse 的 JSON 里
浮点数会把 ``0.1`` 变成 ``0.1000000000000000055511151231257827…`` —— 而
金额差最后几位就是差钱。字符串保精度，也就保住了"账本上的数与观测里的数
一样"这件事。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider

from core.metrics.cost import CostAmount, CostObserver
from infrastructure.config.settings import Environment, Settings
from infrastructure.observability.langfuse import LangfuseClient, LangfuseCostObserver
from infrastructure.observability.tracing import (
    COST_AMOUNT_ATTRIBUTE,
    COST_CURRENCY_ATTRIBUTE,
    SpanCostObserver,
)


def _enabled_settings() -> Settings:
    """启用的 Langfuse 配置 —— **没启用时客户端是 no-op**。"""
    return Settings(environment=Environment.TEST, langfuse_enabled=True)


def _attributes(span: trace.Span) -> Mapping[str, Any]:
    """读 span 上的属性。

    **只有 SDK 的 span 才带属性** —— ``trace.Span`` 这个接口上并没有
    ``attributes``。所以这里显式转成 :class:`ReadableSpan`。
    """
    return cast(ReadableSpan, span).attributes or {}


class TestTheSpanObserver:
    """成本写进 span 属性。"""

    async def test_the_amount_and_the_currency_are_written(self) -> None:
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("work") as span:
            await SpanCostObserver(span).observe(
                CostAmount(Decimal("0.25"), "USD"), task_id="task-1", session_id="session-1"
            )

        attributes = _attributes(span)
        assert attributes[COST_AMOUNT_ATTRIBUTE] == "0.250000"
        assert attributes[COST_CURRENCY_ATTRIBUTE] == "USD"

    async def test_the_identifiers_are_written_too(self) -> None:
        """没有标识的那笔成本，事后对不上是谁花的。"""
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("work") as span:
            await SpanCostObserver(span).observe(
                CostAmount(Decimal("1"), "USD"), task_id="task-7", session_id="session-7"
            )

        attributes = _attributes(span)
        assert attributes["task_id"] == "task-7"
        assert attributes["agent_session_id"] == "session-7"

    async def test_the_amount_is_not_a_float(self) -> None:
        """浮点数会把 0.1 变成 0.100000000000000005… —— 那不是这个数。"""
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("work") as span:
            await SpanCostObserver(span).observe(
                CostAmount(Decimal("0.1"), "USD"), task_id="t", session_id="s"
            )

        attributes = _attributes(span)
        assert attributes[COST_AMOUNT_ATTRIBUTE] == "0.100000"

    async def test_it_says_it_wrote_it(self) -> None:
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("work") as span:
            written = await SpanCostObserver(span).observe(
                CostAmount(Decimal("1"), "USD"), task_id="t", session_id="s"
            )
        assert written is True


class TestTheLangfuseObserver:
    """成本记成一条 Langfuse trace。"""

    async def test_the_trace_carries_the_amount_and_the_currency(self) -> None:
        sent: list[tuple[str, Mapping[str, Any]]] = []

        async def transport(name: str, body: Mapping[str, object]) -> None:
            sent.append((name, body))

        client = LangfuseClient(_enabled_settings(), transport=transport)
        await LangfuseCostObserver(client).observe(
            CostAmount(Decimal("0.25"), "USD"), task_id="task-1", session_id="session-1"
        )

        assert len(sent) == 1
        name, body = sent[0]
        assert name == "execution.cost"
        assert body["metadata"]["cost.amount"] == "0.250000"
        assert body["metadata"]["cost.currency"] == "USD"

    async def test_the_identifiers_travel_with_it(self) -> None:
        sent: list[tuple[str, Mapping[str, Any]]] = []

        async def transport(name: str, body: Mapping[str, object]) -> None:
            sent.append((name, body))

        client = LangfuseClient(_enabled_settings(), transport=transport)
        await LangfuseCostObserver(client).observe(
            CostAmount(Decimal("1"), "USD"), task_id="task-7", session_id="session-7"
        )

        _, body = sent[0]
        assert body["traceId"] == "task-7"
        assert body["sessionId"] == "session-7"


class TestThePort:
    """两个实现都是同一个端口的实现 —— **换后端不用改调用方**。"""

    def test_the_span_observer_implements_the_port(self) -> None:
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("work") as span:
            assert isinstance(SpanCostObserver(span), CostObserver)

    def test_the_langfuse_observer_implements_the_port(self) -> None:
        assert isinstance(LangfuseCostObserver(LangfuseClient()), CostObserver)


class TestTheDegradedLangfuse:
    """观测不可用**不得阻塞主流程** —— 记不进去就记不进去。"""

    async def test_the_failure_is_not_raised(self) -> None:
        async def transport(_name: str, _body: Mapping[str, object]) -> None:
            raise RuntimeError("Langfuse 不可达")

        client = LangfuseClient(_enabled_settings(), transport=transport)
        observer = LangfuseCostObserver(client)

        assert (
            await observer.observe(CostAmount(Decimal("1"), "USD"), task_id="t", session_id="s")
            is False
        )
