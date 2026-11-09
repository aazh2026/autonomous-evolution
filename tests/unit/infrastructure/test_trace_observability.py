"""执行轨迹的口径 —— **每个 Agent 会话、验证运行、决策节点都带得上标识**。

设计依据：docs/tasks.md TASK-P1-073（T1 ① 一次任务执行后可按 ``task_id``
检索到完整轨迹；T1 ② 轨迹中的工具调用数与 ``ExecutionEvent`` 记录数一致）、
R-062（每次工具调用记录主体 / 任务 / 资源 / 结果）。

**这一层只测口径。** 真的跑一次执行、真的起一个子进程、真的发一个 span
—— 那是 :mod:`tests.system.test_trace_reporting` 的事。口径必须能被单独
证伪：口径错了，跑得再真也是错的。

**每条断言都在盯一件"看起来差不多但含义相反"的事。** "没记下来"与"记下了
空的"、"报 0 次"与"没报"、"数出来的"与"报上来的" —— 这些在报表上常常长得
一模一样，而这正是要守住的地方。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util.types import AnyValue
from pydantic import SecretStr

from core.observability.trace import (
    TaskTrace,
    ToolCallTally,
    TraceContext,
    TraceError,
    TraceKind,
    TraceRecord,
    TraceStore,
)
from infrastructure.config.settings import Environment, Settings
from infrastructure.observability.langfuse import LangfuseClient, Transport
from infrastructure.observability.tracing import (
    TRACE_RECORD_COUNT_ATTRIBUTE,
    TRACE_REPORTED_TOOL_CALLS_ATTRIBUTE,
    TRACE_TOOL_CALLS_ATTRIBUTE,
    LangfuseTraceObserver,
    SpanTraceObserver,
    TraceRecorder,
)


@dataclass(frozen=True, slots=True)
class _Event:
    """一条假事件 —— **形状照着 :class:`executors.base.ExecutionEvent` 写**。"""

    seq: int
    kind: str
    payload: Mapping[str, object] = field(default_factory=dict)


async def _stream(*events: _Event) -> AsyncIterator[_Event]:
    for event in events:
        yield event


def _context() -> TraceContext:
    return TraceContext(
        task_id="TASK-1",
        cycle_id="cycle-0007",
        agent_session_id="session-1",
    )


def _tracer() -> tuple[trace.Tracer, InMemorySpanExporter]:
    """一个真的 OTel tracer —— **span 真的被导出**。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _span(exporter: InMemorySpanExporter) -> ReadableSpan:
    """导出后的那一个 span —— **真的只有一个**。"""
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def _attributes(span: ReadableSpan) -> Mapping[str, AnyValue]:
    """span 上的属性 —— **没有就是空的**。"""
    return span.attributes or {}


def _enabled_settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        langfuse_enabled=True,
        langfuse_host="http://langfuse.invalid",
        langfuse_public_key=SecretStr("pk-test"),
        langfuse_secret_key=SecretStr("sk-test"),
    )


def _recording_transport(sink: list[tuple[str, Mapping[str, object]]]) -> Transport:
    async def _send(name: str, body: Mapping[str, object]) -> None:
        sink.append((name, body))

    return _send


class TestTheContext:
    """三个标识 —— **缺一个就答不出 R-062 要的那几样**。"""

    def test_a_blank_task_id_is_refused(self) -> None:
        with pytest.raises(TraceError):
            TraceContext(task_id="  ")

    def test_a_context_without_any_id_is_refused(self) -> None:
        """谁都不属于的轨迹 —— 记下来也检索不到。"""
        with pytest.raises(TraceError):
            TraceContext()

    def test_only_the_present_ids_are_exposed(self) -> None:
        """**不放空串。** 空串看着像"这个标识是空的"，而"没有"与"是空的"是两回事。"""
        context = TraceContext(cycle_id="cycle-0007")
        assert context.attributes() == {"cycle_id": "cycle-0007"}

    def test_all_three_ids_are_exposed(self) -> None:
        assert set(_context().attributes()) == {"task_id", "cycle_id", "agent_session_id"}


class TestTheRecord:
    """每一类条目**必须**带哪些标识 —— 由 :data:`REQUIRED_IDS` 定死。"""

    def test_an_agent_session_without_a_session_id_is_refused(self) -> None:
        with pytest.raises(TraceError):
            TraceRecord(
                kind=TraceKind.AGENT_SESSION,
                name="agent.session",
                context=TraceContext(task_id="TASK-1"),
            )

    def test_a_tool_call_without_a_task_id_is_refused(self) -> None:
        with pytest.raises(TraceError):
            TraceRecord(
                kind=TraceKind.TOOL_CALL,
                name="tool:Read",
                context=TraceContext(agent_session_id="session-1"),
            )

    def test_a_tool_call_without_a_session_id_is_refused(self) -> None:
        """**R-062 的"主体"。** 缺了它，那条工具调用答不出"谁做的"。"""
        with pytest.raises(TraceError):
            TraceRecord(
                kind=TraceKind.TOOL_CALL,
                name="tool:Read",
                context=TraceContext(task_id="TASK-1"),
            )

    def test_a_verification_run_without_a_task_id_is_refused(self) -> None:
        with pytest.raises(TraceError):
            TraceRecord(
                kind=TraceKind.VERIFICATION_RUN,
                name="verification:passed",
                context=TraceContext(cycle_id="cycle-0007"),
            )

    def test_a_decision_without_a_cycle_id_is_refused(self) -> None:
        """决策节点没有任务（它还没造出任务），但它一定属于某一轮演进。"""
        with pytest.raises(TraceError):
            TraceRecord(
                kind=TraceKind.DECISION_NODE,
                name="decision:record",
                context=TraceContext(task_id="TASK-1"),
            )

    def test_a_decision_with_a_cycle_id_is_accepted(self) -> None:
        record = TraceRecord(
            kind=TraceKind.DECISION_NODE,
            name="decision:defer",
            context=TraceContext(cycle_id="cycle-0007"),
        )
        assert record.kind is TraceKind.DECISION_NODE

    def test_a_blank_name_is_refused(self) -> None:
        with pytest.raises(TraceError):
            TraceRecord(kind=TraceKind.TOOL_CALL, name="  ", context=_context())

    def test_the_attributes_cannot_be_mutated(self) -> None:
        """**轨迹是只读的。** 事后改一条记录，就是改证据。"""
        record = TraceRecord(
            kind=TraceKind.EXECUTION_EVENT,
            name="message",
            context=_context(),
            attributes={"text": "你好"},
        )
        with pytest.raises(TypeError):
            record.attributes["text"] = "改过了"  # type: ignore[index]


class TestTheStore:
    """按 ``task_id`` 检索 —— **T1 ① 的落点**。"""

    def test_a_trace_is_found_by_its_task_id(self) -> None:
        store = TraceStore()
        trace = TaskTrace(context=_context())
        store.add(trace)

        assert store.by_task("TASK-1") is trace

    def test_an_unknown_task_id_gives_none(self) -> None:
        """**取不到是 ``None``，不是空轨迹。** 空轨迹与"没有轨迹"长得一样。"""
        store = TraceStore()
        assert store.by_task("TASK-404") is None

    def test_the_traces_of_a_cycle_are_grouped(self) -> None:
        store = TraceStore()
        first = TaskTrace(context=TraceContext(task_id="TASK-1", cycle_id="cycle-0007"))
        second = TaskTrace(context=TraceContext(task_id="TASK-2", cycle_id="cycle-0007"))
        store.add(first)
        store.add(second)

        assert store.by_cycle("cycle-0007") == (first, second)

    def test_a_decision_trace_is_found_by_its_cycle(self) -> None:
        """决策节点没有任务 —— 它进索引靠的是周期。"""
        store = TraceStore()
        decision = TaskTrace(context=TraceContext(cycle_id="cycle-0007"))
        store.add(decision)

        assert store.by_task("TASK-1") is None
        assert store.by_cycle("cycle-0007") == (decision,)


class TestTheTally:
    """工具调用的两笔账 —— **对不上就是轨迹漏了东西**。"""

    def test_two_books_that_agree(self) -> None:
        assert ToolCallTally(recorded=4, reported=4).agrees

    def test_two_books_that_do_not(self) -> None:
        assert not ToolCallTally(recorded=3, reported=4).agrees

    def test_no_reported_count_is_not_a_disagreement(self) -> None:
        """**"没报"不是"报了 0"。** 后者会跟"真的一次都没调"分不开。"""
        assert ToolCallTally(recorded=4, reported=None).agrees


class TestTheSpanObserver:
    """OTel 那一侧 —— **一个任务一条 trace**。"""

    async def test_the_three_ids_are_on_the_span(self) -> None:
        tracer, exporter = _tracer()
        await SpanTraceObserver(tracer).observe(TaskTrace(context=_context()))

        attributes = _attributes(_span(exporter))
        assert attributes["task_id"] == "TASK-1"
        assert attributes["cycle_id"] == "cycle-0007"
        assert attributes["agent_session_id"] == "session-1"

    async def test_the_tool_call_count_is_on_the_span(self) -> None:
        tracer, exporter = _tracer()
        task_trace = TaskTrace(context=_context(), tool_calls=ToolCallTally(recorded=4, reported=4))
        await SpanTraceObserver(tracer).observe(task_trace)

        attributes = _attributes(_span(exporter))
        assert attributes[TRACE_TOOL_CALLS_ATTRIBUTE] == 4
        assert attributes[TRACE_REPORTED_TOOL_CALLS_ATTRIBUTE] == 4
        assert attributes[TRACE_RECORD_COUNT_ATTRIBUTE] == 0

    async def test_the_reported_count_is_not_written_when_there_is_none(self) -> None:
        """没报就不写 —— **别拿 0 冒充**。"""
        tracer, exporter = _tracer()
        task_trace = TaskTrace(context=_context(), tool_calls=ToolCallTally(recorded=4))
        await SpanTraceObserver(tracer).observe(task_trace)

        attributes = _attributes(_span(exporter))
        assert TRACE_REPORTED_TOOL_CALLS_ATTRIBUTE not in attributes

    async def test_every_record_becomes_a_span_event(self) -> None:
        tracer, exporter = _tracer()
        records = (
            TraceRecord(kind=TraceKind.EXECUTION_EVENT, name="message", context=_context()),
            TraceRecord(kind=TraceKind.TOOL_CALL, name="tool:Read", context=_context()),
        )
        await SpanTraceObserver(tracer).observe(TaskTrace(context=_context(), records=records))

        span = _span(exporter)
        assert [item.name for item in span.events] == ["message", "tool:Read"]

    async def test_a_none_attribute_is_dropped(self) -> None:
        """**``None`` 不是"空的值"，它是"没有这个值"。**"""
        tracer, exporter = _tracer()
        record = TraceRecord(
            kind=TraceKind.EXECUTION_EVENT,
            name="message",
            context=_context(),
            attributes={"text": None, "other": "留着"},
        )
        await SpanTraceObserver(tracer).observe(TaskTrace(context=_context(), records=(record,)))

        attributes = _span(exporter).events[0].attributes or {}
        assert "text" not in attributes
        assert attributes["other"] == "留着"


class TestTheLangfuseObserver:
    """Langfuse 那一侧 —— **同一件事，两处键名不一样**。"""

    async def test_the_trace_carries_the_ids_and_the_counts(self) -> None:
        sink: list[tuple[str, Mapping[str, object]]] = []
        client = LangfuseClient(_enabled_settings(), transport=_recording_transport(sink))
        trace = TaskTrace(
            context=_context(),
            records=(TraceRecord(kind=TraceKind.TOOL_CALL, name="tool:Read", context=_context()),),
            tool_calls=ToolCallTally(recorded=1, reported=1),
        )
        await LangfuseTraceObserver(client).observe(trace)

        body = sink[0][1]
        metadata = body["metadata"]
        assert isinstance(metadata, Mapping)
        assert metadata["task_id"] == "TASK-1"
        assert metadata["cycle_id"] == "cycle-0007"
        assert metadata["tool_calls"] == 1

    async def test_the_trace_id_is_the_task_id(self) -> None:
        """Langfuse 按 ``traceId`` 检索 —— **它得是 task_id**。"""
        sink: list[tuple[str, Mapping[str, object]]] = []
        client = LangfuseClient(_enabled_settings(), transport=_recording_transport(sink))
        await LangfuseTraceObserver(client).observe(TaskTrace(context=_context()))

        assert sink[0][1]["traceId"] == "TASK-1"


class TestTheRecorder:
    """记录器 —— **把事件流变成一条可检索的轨迹**。"""

    async def test_the_tool_calls_are_counted_from_the_events(self) -> None:
        recorder = TraceRecorder()
        trace = await recorder.trace_execution(
            _context(),
            _stream(
                _Event(seq=1, kind="tool.started", payload={"tool_use_id": "t1"}),
                _Event(seq=2, kind="tool.started", payload={"tool_use_id": "t2"}),
                _Event(seq=3, kind="message.assistant", payload={"text": "好了"}),
            ),
        )

        assert trace.tool_calls.recorded == 2

    async def test_a_result_is_not_counted_as_another_call(self) -> None:
        """**数"开始"不数"结束"。** 结束事件是同一次调用的结果，数两次就翻倍了。"""
        recorder = TraceRecorder()
        trace = await recorder.trace_execution(
            _context(),
            _stream(
                _Event(seq=1, kind="tool.started", payload={"tool_use_id": "t1"}),
                _Event(seq=2, kind="tool.completed", payload={"tool_use_id": "t1"}),
            ),
        )

        assert trace.tool_calls.recorded == 1

    async def test_the_result_is_attached_to_its_tool_call(self) -> None:
        """R-062 要的是"这次调用的结果"，不是"有个东西结束了"。"""
        recorder = TraceRecorder()
        trace = await recorder.trace_execution(
            _context(),
            _stream(
                _Event(
                    seq=1,
                    kind="tool.started",
                    payload={"tool_use_id": "t1", "tool_name": "Read"},
                ),
                _Event(
                    seq=2,
                    kind="tool.completed",
                    payload={"tool_use_id": "t1", "content": "文件内容", "is_error": False},
                ),
            ),
        )

        calls = trace.by_kind(TraceKind.TOOL_CALL)
        assert len(calls) == 1
        assert calls[0].attributes["result"] == "文件内容"
        assert calls[0].attributes["result_is_error"] is False

    async def test_the_tool_call_carries_the_subject_and_the_task(self) -> None:
        """**R-062 的主体与任务。** 缺了它就答不出"谁替哪个任务做的"。"""
        recorder = TraceRecorder()
        trace = await recorder.trace_execution(
            _context(),
            _stream(_Event(seq=1, kind="tool.started", payload={"tool_use_id": "t1"})),
        )

        call = trace.by_kind(TraceKind.TOOL_CALL)[0]
        assert call.attributes["subject"] == "session-1"
        assert call.attributes["task"] == "TASK-1"

    async def test_the_session_is_recorded(self) -> None:
        """每次 Agent 会话都有一条 —— **它带着主体与会话标识**。"""
        recorder = TraceRecorder()
        trace = await recorder.trace_execution(_context(), _stream())

        sessions = trace.by_kind(TraceKind.AGENT_SESSION)
        assert len(sessions) == 1
        assert sessions[0].attributes["agent_session_id"] == "session-1"

    async def test_without_a_session_id_no_session_record_is_made(self) -> None:
        """记一条缺了主体的会话，比不记更坏 —— 它看着像有。"""
        recorder = TraceRecorder()
        trace = await recorder.trace_execution(
            TraceContext(task_id="TASK-1"),
            _stream(),
        )

        assert trace.by_kind(TraceKind.AGENT_SESSION) == ()

    async def test_a_verification_run_is_recorded(self) -> None:
        recorder = TraceRecorder()
        trace = await recorder.trace_verification(
            _context(),
            result="failed",
            checks=["tests", "lint"],
        )

        runs = trace.by_kind(TraceKind.VERIFICATION_RUN)
        assert len(runs) == 1
        assert runs[0].attributes["result"] == "failed"

    async def test_a_decision_is_recorded(self) -> None:
        """**"不行动"同样要留痕。**"""
        recorder = TraceRecorder()
        trace = await recorder.trace_decision(
            TraceContext(cycle_id="cycle-0007"),
            decision="defer",
            candidate_id="candidate-1",
        )

        decisions = trace.by_kind(TraceKind.DECISION_NODE)
        assert len(decisions) == 1
        assert decisions[0].attributes["decision"] == "defer"

    async def test_the_trace_lands_in_the_store(self) -> None:
        """**记下来了，就得查得到。**"""
        store = TraceStore()
        recorder = TraceRecorder(store=store)
        trace = await recorder.trace_execution(_context(), _stream())

        assert store.by_task("TASK-1") is trace

    async def test_a_broken_observer_does_not_break_the_recorder(self) -> None:
        """观测不可用不得阻塞主流程 —— **一个观测口坏了，其余照常**。"""
        store = TraceStore()
        recorder = TraceRecorder(observers=(_BrokenObserver(),), store=store)
        trace = await recorder.trace_execution(_context(), _stream())

        assert store.by_task("TASK-1") is trace
        assert trace.by_kind(TraceKind.AGENT_SESSION)

    async def test_the_reported_count_is_kept_next_to_the_counted_one(self) -> None:
        """两笔账都在 —— **对不对得上是另一回事，先都记下来**。"""
        recorder = TraceRecorder()
        trace = await recorder.trace_execution(
            _context(),
            _stream(_Event(seq=1, kind="tool.started", payload={"tool_use_id": "t1"})),
            reported_tool_calls=4,
        )

        assert trace.tool_calls.recorded == 1
        assert trace.tool_calls.reported == 4
        assert not trace.tool_calls.agrees


class _BrokenObserver:
    """一个会抛异常的观测口。"""

    __slots__ = ()

    async def observe(self, trace: TaskTrace) -> bool:
        """什么都干不了 —— **就是抛异常**。"""
        raise RuntimeError(f"观测系统连不上（{len(trace.records)} 条轨迹）")
