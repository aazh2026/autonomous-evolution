"""OpenTelemetry 追踪骨架 —— **以及执行轨迹**。

设计依据：docs/design.md §10（可观测性）、docs/tasks.md TASK-P1-005、
TASK-P1-073（T1 ① 一次任务执行后可按 ``task_id`` 检索到完整轨迹；
T1 ② 轨迹中的工具调用数与 ``ExecutionEvent`` 记录数一致）。

与 Langfuse 同样的原则：**追踪不可用不得阻塞主流程**。OTel collector
连不上时，span 退化成 no-op，业务照跑。

OTel 的 SDK 里已经内置了这个降级：没设置 provider 时，
``trace.get_tracer()`` 返回的是一个 proxy tracer —— 它的 span 是 no-op，
但调用链和属性都还在，日后接上 collector 不用改调用方代码。
本模块负责把 provider 装好，并且**装不上也不抛异常**。

**这里多出来的是"一条轨迹"的写法。** 口径在
:mod:`core.observability.trace`（什么是轨迹、什么是工具调用、每类条目必须
带哪些标识），这里只负责把它写成 OTel span 或 Langfuse trace。**口径不写在
这里是有意的**：换个观测后端就得再抄一遍口径，而抄的那一遍迟早抄歪。
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.util.types import AnyValue

from core.metrics.cost import CostAmount
from core.observability.trace import (
    TOOL_CALL_KINDS,
    TOOL_RESULT_KINDS,
    TaskTrace,
    ToolCallTally,
    TraceableEvent,
    TraceContext,
    TraceKind,
    TraceObserver,
    TraceRecord,
    TraceStore,
)
from infrastructure.config.settings import Settings, get_settings
from infrastructure.observability.langfuse import LangfuseClient
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


# --------------------------------------------------------------------------
# 执行轨迹（TASK-P1-073）
# --------------------------------------------------------------------------

#: 轨迹 span 的名字 —— **一个任务一条**。
TRACE_SPAN_NAME = "ases.task"

#: span 上"数出来的工具调用数"。
TRACE_TOOL_CALLS_ATTRIBUTE = "trace.tool_calls"

#: span 上"执行器报上来的工具调用数" —— **没报就不写这个属性**。
TRACE_REPORTED_TOOL_CALLS_ATTRIBUTE = "trace.reported_tool_calls"

#: span 上"轨迹里有多少条"。
TRACE_RECORD_COUNT_ATTRIBUTE = "trace.record_count"

#: 事件负载里"资源"那一栏可能的键名 —— **有就记，没有就空着**。
#:
#: **不是每个执行器都报资源。** Claude Code 的适配器**故意不把工具入参放进
#: 事件负载**（文件内容不该进轨迹），所以那一条的"资源"多半是空的。
#: **空着比编一个强** —— 编出来的资源比没有资源更坏，因为它看着像真的。
RESOURCE_KEYS: Final[tuple[str, ...]] = (
    "file_path",
    "path",
    "notebook_path",
    "url",
    "command",
)


def _span_attributes(attributes: Mapping[str, object]) -> dict[str, AnyValue]:
    """把属性变成 OTel 认得的类型 —— **没值的直接不要**。

    **``None`` 不是"空的值"，它是"没有这个值"。** OTel 的属性表里放一个
    ``None``，看板上就会显示一个空格子 —— 而那个格子到底是"没测"还是
    "测出来是空"，分不开。
    """
    result: dict[str, AnyValue] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float, str)):
            result[key] = value
        else:
            result[key] = str(value)
    return result


class SpanTraceObserver:
    """把一条轨迹记成一个 OTel span —— **一个任务一条 trace**。

    **条目挂在 span 事件上，而不是各开一个 span。** 一次任务执行是一个
    span，工具调用是它的事件 —— 这样"按 ``task_id`` 检索到完整轨迹"就是
    检索到**一个** span，而不是在几千个子 span 里拼。

    **属性写在 span 上，条目写在事件上。** 三个标识挂在 span 上（检索靠
    它们），每一条的细节挂在事件上（细节不该把 span 的属性表撑爆）。

    **实现 :class:`core.observability.trace.TraceObserver` 端口** —— 与
    :class:`SpanCostObserver` 同一套做法。
    """

    __slots__ = ("_name", "_tracer")

    def __init__(self, tracer: trace.Tracer, *, name: str = TRACE_SPAN_NAME) -> None:
        self._tracer = tracer
        self._name = name

    async def observe(self, task_trace: TaskTrace) -> bool:
        """把整条轨迹记成一个 span。"""
        attributes: dict[str, AnyValue] = {
            **task_trace.context.attributes(),
            TRACE_RECORD_COUNT_ATTRIBUTE: len(task_trace.records),
            TRACE_TOOL_CALLS_ATTRIBUTE: task_trace.tool_calls.recorded,
        }
        if task_trace.tool_calls.reported is not None:
            attributes[TRACE_REPORTED_TOOL_CALLS_ATTRIBUTE] = task_trace.tool_calls.reported

        with self._tracer.start_as_current_span(self._name, attributes=attributes) as span:
            for record in task_trace.records:
                event_attributes: dict[str, AnyValue] = {
                    "trace.kind": record.kind.value,
                    "trace.sequence": record.sequence,
                    **_span_attributes(record.attributes),
                }
                span.add_event(record.name, attributes=event_attributes)
        return True


class LangfuseTraceObserver:
    """把一条轨迹记成一条 Langfuse trace。

    **``trace_id`` 用 ``task_id``** —— 与 :class:`LangfuseCostObserver` 同一套
    映射（那边是 ``trace_id=task_id``）。**同一件事在两处的键名不一样，
    查的时候就要靠两遍** —— 而 Langfuse 的检索就是按 ``traceId`` 做的。

    **实现 :class:`core.observability.trace.TraceObserver` 端口**。
    """

    __slots__ = ("_client",)

    def __init__(self, client: LangfuseClient) -> None:
        self._client = client

    async def observe(self, task_trace: TaskTrace) -> bool:
        """记一条轨迹。返回是否真的送出去了。"""
        return await self._client.record_trace(
            "ases.task",
            trace_id=task_trace.task_id or task_trace.cycle_id,
            session_id=task_trace.context.agent_session_id,
            input_data={
                "records": [
                    {"kind": item.kind.value, "name": item.name, "sequence": item.sequence}
                    for item in task_trace.records
                ]
            },
            output_data={
                "tool_calls": task_trace.tool_calls.recorded,
                "reported_tool_calls": task_trace.tool_calls.reported,
                "record_count": len(task_trace.records),
            },
            metadata={
                **task_trace.context.attributes(),
                "tool_calls": task_trace.tool_calls.recorded,
                "record_count": len(task_trace.records),
            },
        )


def _tool_call_record(context: TraceContext, event: TraceableEvent) -> TraceRecord:
    """把一条"工具调用开始"的事件变成一条轨迹条目。

    **R-062 的四样，缺一样都记在这儿。** 主体与任务来自上下文（那是整个
    会话共用的），资源与结果来自事件负载。

    **结果不在这一条里** —— 结果是"结束"事件报上来的，要贴回来
    （见 :func:`_attach_tool_result`）。
    """
    payload = event.payload
    attributes: dict[str, object] = {
        "subject": context.agent_session_id,
        "task": context.task_id,
    }
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str):
        attributes["tool_name"] = tool_name
    tool_use_id = payload.get("tool_use_id")
    if isinstance(tool_use_id, str):
        attributes["tool_use_id"] = tool_use_id
    for key in RESOURCE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            attributes["resource"] = value
            break
    return TraceRecord(
        kind=TraceKind.TOOL_CALL,
        name=f"tool:{tool_name}" if isinstance(tool_name, str) and tool_name else "tool",
        context=context,
        attributes=attributes,
        sequence=event.seq,
    )


def _attach_tool_result(
    records: list[TraceRecord], started: Mapping[str, int], event: TraceableEvent
) -> None:
    """把工具调用的**结果**贴回发起它的那一条。

    **R-062 要的是"这次调用的结果"，不是"某次调用结束了"。** 结束事件
    自己也会记一条 —— 但那一行说的是"有个东西结束了"，贴回去的那一条才
    说得出"这次调用的结果是 X"。

    **贴不上去就放弃。** 找不到发起方（事件乱序、或者发起方根本没来）时
    不猜 —— 猜错的那条比没有更坏。
    """
    tool_use_id = event.payload.get("tool_use_id")
    if not isinstance(tool_use_id, str):
        return
    index = started.get(tool_use_id)
    if index is None:
        return
    record = records[index]
    attributes = dict(record.attributes)
    content = event.payload.get("content")
    if content is not None:
        attributes["result"] = content
    attributes["result_is_error"] = bool(event.payload.get("is_error"))
    records[index] = replace(record, attributes=attributes)


class TraceRecorder:
    """把执行、验证与决策记成可检索的轨迹。

    **三类东西走同一条路。** 一次 Agent 会话（带事件流）、一次验证运行、
    一个决策节点 —— 它们都产出 :class:`~core.observability.trace.TaskTrace`，
    都交给同一批观测口，都进同一个检索索引。**三样东西各写一套，接线的时候
    一定会漏掉一套** —— 而漏掉的那套，事后没人知道它没写。

    **"数出来的"与"报上来的"是两笔账。** 工具调用数从事件流里数
    （``recorded``），执行器报的那个数是 ``reported``。**两笔账对不上就是
    轨迹漏了东西** —— 那正是 T1 ② 要守的东西。

    **观测口坏了不许把主流程带崩。** 一个观测口抛异常就跳过它，其余照常
    —— 观测不可用不得阻塞主流程。
    """

    __slots__ = ("_observers", "_store")

    def __init__(
        self,
        *,
        observers: Sequence[TraceObserver] = (),
        store: TraceStore | None = None,
    ) -> None:
        self._observers = tuple(observers)
        self._store = store

    async def trace_execution(
        self,
        context: TraceContext,
        events: AsyncIterable[TraceableEvent],
        *,
        reported_tool_calls: int | None = None,
    ) -> TaskTrace:
        """把一次执行的事件流记成一条轨迹。

        **工具调用数从事件流里数。** 数"开始"不数"结束" —— 结束事件是同一次
        调用的结果，数两次就翻倍了。

        **没有会话标识就不记那条会话条目。** 记一条缺了主体的会话，比不记
        更坏 —— 它看着像有，可它答不出"谁做的"。
        """
        records: list[TraceRecord] = []
        started: dict[str, int] = {}
        tool_calls = 0

        if context.agent_session_id is not None:
            records.append(
                TraceRecord(
                    kind=TraceKind.AGENT_SESSION,
                    name=f"agent.session:{context.agent_session_id}",
                    context=context,
                    attributes={"agent_session_id": context.agent_session_id},
                )
            )

        async for event in events:
            if event.kind in TOOL_CALL_KINDS:
                tool_calls += 1
                records.append(_tool_call_record(context, event))
                tool_use_id = event.payload.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    started[tool_use_id] = len(records) - 1
            else:
                records.append(
                    TraceRecord(
                        kind=TraceKind.EXECUTION_EVENT,
                        name=event.kind,
                        context=context,
                        attributes=dict(event.payload),
                        sequence=event.seq,
                    )
                )
                if event.kind in TOOL_RESULT_KINDS:
                    _attach_tool_result(records, started, event)

        task_trace = TaskTrace(
            context=context,
            records=tuple(records),
            tool_calls=ToolCallTally(recorded=tool_calls, reported=reported_tool_calls),
        )
        await self._publish(task_trace)
        return task_trace

    async def trace_verification(
        self,
        context: TraceContext,
        *,
        result: str,
        checks: Sequence[str] = (),
        duration_ms: int | None = None,
    ) -> TaskTrace:
        """记一次验证运行 —— **验证没过也是一次验证，照记**。"""
        attributes: dict[str, object] = {"result": result}
        if checks:
            attributes["checks"] = list(checks)
        if duration_ms is not None:
            attributes["duration_ms"] = duration_ms
        record = TraceRecord(
            kind=TraceKind.VERIFICATION_RUN,
            name=f"verification:{result}",
            context=context,
            attributes=attributes,
        )
        task_trace = TaskTrace(context=context, records=(record,))
        await self._publish(task_trace)
        return task_trace

    async def trace_decision(
        self,
        context: TraceContext,
        *,
        decision: str,
        candidate_id: str,
        rationale: str = "",
    ) -> TaskTrace:
        """记一个决策节点 —— **defer 与 reject 也算**。

        **"不行动"同样要留痕。** 只记"做了什么"，事后就分不清"这个候选
        被否了"与"这个候选压根没人看过"。
        """
        attributes: dict[str, object] = {
            "decision": decision,
            "candidate_id": candidate_id,
        }
        if rationale:
            attributes["rationale"] = rationale
        record = TraceRecord(
            kind=TraceKind.DECISION_NODE,
            name=f"decision:{decision}",
            context=context,
            attributes=attributes,
        )
        task_trace = TaskTrace(context=context, records=(record,))
        await self._publish(task_trace)
        return task_trace

    async def _publish(self, task_trace: TaskTrace) -> None:
        """把一条轨迹交给检索索引与观测口。

        **索引先、观测后。** 索引是"事后能查到"的保证，观测口可能连不上 ——
        观测断了不该让轨迹也断。
        """
        if self._store is not None:
            self._store.add(task_trace)
        for observer in self._observers:
            try:
                await observer.observe(task_trace)
            except Exception as exc:
                get_logger(__name__).warning(
                    "trace_observer_failed",
                    degraded=True,
                    reason=f"{type(exc).__name__}",
                )


__all__ = [
    "COST_AMOUNT_ATTRIBUTE",
    "COST_CURRENCY_ATTRIBUTE",
    "RESOURCE_KEYS",
    "TRACE_RECORD_COUNT_ATTRIBUTE",
    "TRACE_REPORTED_TOOL_CALLS_ATTRIBUTE",
    "TRACE_SPAN_NAME",
    "TRACE_TOOL_CALLS_ATTRIBUTE",
    "LangfuseTraceObserver",
    "SpanCostObserver",
    "SpanTraceObserver",
    "TraceRecorder",
    "configure_tracing",
    "current_span_attributes",
    "get_tracer",
    "is_configured",
    "shutdown_tracing",
]
