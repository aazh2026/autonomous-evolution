"""OpenTelemetry 追踪骨架。

设计依据：docs/design.md §10（可观测性）、docs/tasks.md TASK-P1-005。

与 Langfuse 同样的原则：**追踪不可用不得阻塞主流程**。OTel collector
连不上时，span 退化成 no-op，业务照跑。

OTel 的 SDK 里已经内置了这个降级：没设置 provider 时，
``trace.get_tracer()`` 返回的是一个 proxy tracer —— 它的 span 是 no-op，
但调用链和属性都还在，日后接上 collector 不用改调用方代码。
本模块负责把 provider 装好，并且**装不上也不抛异常**。
"""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from core.metrics.cost import CostAmount
from infrastructure.config.settings import Settings, get_settings
from infrastructure.observability.logging import get_logger


@dataclass
class _TracingState:
    """进程内的追踪状态。

    用一个可变的持有对象，而不是 ``global`` 变量 ——
    状态只有一份，但读写不需要全局语句。
    """

    provider: TracerProvider | None = None


_state = _TracingState()


def configure_tracing(settings: Settings | None = None) -> TracerProvider | None:
    """配置 OTel tracer provider。

    返回 ``None`` 表示当前是 no-op 模式：要么配置里没开 OTel，
    要么 exporter 构造失败（collector 地址不对、依赖缺失等）。
    两种情况都不抛异常 —— 追踪是旁路，不该拖垮主流程。
    """
    if _state.provider is not None:
        return _state.provider

    cfg = settings or get_settings()
    if not cfg.otel_enabled:
        return None

    try:
        exporter = OTLPSpanExporter(endpoint=cfg.otel_endpoint)
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception as exc:
        get_logger(__name__).warning(
            "otel_unavailable",
            degraded=True,
            reason=f"{type(exc).__name__}",
            endpoint=cfg.otel_endpoint,
        )
        return None

    _state.provider = provider
    get_logger(__name__).info("otel_configured", degraded=False, endpoint=cfg.otel_endpoint)
    return provider


def get_tracer(name: str) -> trace.Tracer:
    """取一个 tracer。

    无论 provider 装没装好都能调用 —— 装不好时返回的是 no-op tracer，
    调用方代码不需要知道自己处在哪种模式。
    """
    return trace.get_tracer(name)


def shutdown_tracing() -> None:
    """把待发送的 span 冲出去并关闭 provider。进程退出前调用。"""
    provider = _state.provider
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception as exc:
        get_logger(__name__).warning("otel_shutdown_failed", reason=f"{type(exc).__name__}")
    finally:
        _state.provider = None


def is_configured() -> bool:
    """当前是否真的装上了 provider。"""
    return _state.provider is not None


def current_span_attributes() -> dict[str, object]:
    """当前活跃 span 的属性。诊断用 —— 没有活跃 span 时返回空字典。

    没装 provider 时 OTel 会给出一个**无效**的 span（全零 ID），
    它不是 ``None``。所以判据是"上下文是否有效"，而不是"span 是否为 None"。
    """
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": f"{context.trace_id:032x}", "span_id": f"{context.span_id:016x}"}


#: 成本相关的 span 属性名。**一个成本要占两个属性位** ——
#: 金额与币种分开记，因为"多少钱"这个问题没有币种就没有答案。
COST_AMOUNT_ATTRIBUTE = "execution.cost.amount"
COST_CURRENCY_ATTRIBUTE = "execution.cost.currency"


class SpanCostObserver:
    """把一次执行的成本写进 span 属性。

    **实现 :class:`core.metrics.cost.CostObserver` 端口** —— 端口在领域层，
    实现在这里，与 ``BudgetLedger`` 同一套做法。

    **金额写成字符串。** OTel 的属性类型里没有十进制 —— 传浮点数会丢掉精度
    （``0.1`` 变成 ``0.1000000000000000055511151231257827``），而金额差最后
    几位就是差钱。字符串保精度，也保住了"账本上的数与观测里的数一样"这件事。
    """

    __slots__ = ("_span",)

    def __init__(self, span: trace.Span) -> None:
        self._span = span

    async def observe(self, cost: CostAmount, *, task_id: str, session_id: str) -> bool:
        """把金额、币种与两个标识写进 span。"""
        self._span.set_attribute(COST_AMOUNT_ATTRIBUTE, str(cost.amount))
        self._span.set_attribute(COST_CURRENCY_ATTRIBUTE, cost.currency)
        self._span.set_attribute("task_id", task_id)
        self._span.set_attribute("agent_session_id", session_id)
        return True


__all__ = [
    "COST_AMOUNT_ATTRIBUTE",
    "COST_CURRENCY_ATTRIBUTE",
    "SpanCostObserver",
    "configure_tracing",
    "current_span_attributes",
    "get_tracer",
    "is_configured",
    "shutdown_tracing",
]
