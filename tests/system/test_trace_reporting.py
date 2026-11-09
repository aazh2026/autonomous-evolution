"""执行轨迹的端到端 —— **按 task_id 检索得到完整轨迹，工具调用数对得上**。

设计依据：docs/tasks.md TASK-P1-073 的两条验收：

- **T1 ①** 一次任务执行后可按 ``task_id`` 检索到完整轨迹；
- **T1 ②** 轨迹中的工具调用数与 ``ExecutionEvent`` 记录数一致。

**这里跑的是真的东西。** CLI 适配器真的起一个子进程（打桩 CLI 吐 NDJSON），
事件流是真的适配器吐的，OTel 是真的 span。**不联网、不花钱、不碰工作区。**

**为什么 T1 ② 要端到端地验。** "数出来的工具调用数"与"报上来的工具调用数"
是**两条独立的路**：一条数事件流里的 ``tool.started``，另一条数适配器
汇总出来的 ``tool_use`` 内容块。**拿同一条路的两个数互相比，比出来的永远是
0** —— 那是自证，不是核对。而这两条路真的可能对不上：适配器汇总时漏了
一类内容块、事件映射时丢了一个块 —— 那时候轨迹就少了东西，而**没有任何
一段代码会报错**。

**断言的是"一个都没漏"。** 轨迹里的条数要与事件流里的事件数对得上，工具
调用数要与适配器报的那个数对得上。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util.types import AnyValue

from core.observability.trace import TaskTrace, TraceContext, TraceKind, TraceStore
from executors.base import ExecutionEvent, ExecutionResult, ExecutionTask, SessionConfig
from executors.claude_code import ClaudeCodeAdapter
from infrastructure.observability.tracing import (
    TRACE_TOOL_CALLS_ATTRIBUTE,
    SpanTraceObserver,
    TraceRecorder,
)
from tests.unit.executors.stub_cli import claude_events, write_stub

#: 打桩 CLI 吐几次工具调用。
TOOL_CALLS = 4

#: 任务与周期的标识。
TASK_ID = "TASK-1"
CYCLE_ID = "cycle-0007"


def _tracer() -> tuple[trace.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _attributes(span: ReadableSpan) -> Mapping[str, AnyValue]:
    return span.attributes or {}


async def _aiter(events: list[ExecutionEvent]) -> AsyncIterator[ExecutionEvent]:
    for event in events:
        yield event


async def _run(
    tmp_path: Path, *, tool_calls: int = TOOL_CALLS
) -> tuple[ExecutionResult, list[ExecutionEvent], TaskTrace, TraceStore, ReadableSpan]:
    """真的跑一次执行，把轨迹真的记下来。

    返回 ``(执行结果, 事件流, 轨迹, 索引, 导出的 span)``。
    """
    script = write_stub(
        tmp_path / "cli",
        events=claude_events(cost_usd=0.25, tool_calls=tool_calls),
    )
    adapter = ClaudeCodeAdapter(binary=str(script))
    session = await adapter.create_session(
        SessionConfig(workspace=str(tmp_path / "ws"), branch="agent/TASK-1")
    )
    await adapter.execute(
        ExecutionTask(
            session_id=session.session_id,
            task_id=TASK_ID,
            title="任务",
            instruction="把 README 的一行改掉",
            timeout_seconds=30,
        )
    )
    result = await adapter.collect_result(session.session_id)
    events = [event async for event in await adapter.stream_events(session.session_id)]

    tracer, exporter = _tracer()
    store = TraceStore()
    recorder = TraceRecorder(observers=(SpanTraceObserver(tracer),), store=store)
    context = TraceContext(
        task_id=TASK_ID,
        cycle_id=CYCLE_ID,
        agent_session_id=session.session_id,
    )
    trace = await recorder.trace_execution(
        context,
        _aiter(events),
        reported_tool_calls=result.execution.tool_calls,
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return result, events, trace, store, spans[0]


class TestTheRetrieval:
    """T1 ① —— **一次任务执行后可按 ``task_id`` 检索到完整轨迹**。"""

    async def test_the_trace_is_found_by_its_task_id(self, tmp_path: Path) -> None:
        _, _, trace, store, _ = await _run(tmp_path)

        assert store.by_task(TASK_ID) is trace

    async def test_an_unknown_task_id_gives_nothing(self, tmp_path: Path) -> None:
        """**取不到是 ``None``，不是空轨迹。**"""
        _, _, _, store, _ = await _run(tmp_path)

        assert store.by_task("TASK-404") is None

    async def test_the_trace_holds_every_event(self, tmp_path: Path) -> None:
        """**完整** —— 事件流里的每一条都在轨迹里，一条不多一条不少。

        少一条就是轨迹漏了东西，而漏掉的那条永远没人会知道。
        """
        _, events, trace, _, _ = await _run(tmp_path)

        # 轨迹里比事件流多一条：那条是"这次 Agent 会话"本身。
        assert len(trace.records) == len(events) + 1

    async def test_the_session_is_in_the_trace(self, tmp_path: Path) -> None:
        """**每次 Agent 会话都带得上主体与会话标识。**"""
        _, _, trace, _, _ = await _run(tmp_path)

        sessions = trace.by_kind(TraceKind.AGENT_SESSION)
        assert len(sessions) == 1
        assert sessions[0].attributes["agent_session_id"]

    async def test_the_span_carries_the_three_ids(self, tmp_path: Path) -> None:
        """**span 上要能按 ``task_id`` 查。**"""
        _, _, _, _, span = await _run(tmp_path)

        attributes = _attributes(span)
        assert attributes["task_id"] == TASK_ID
        assert attributes["cycle_id"] == CYCLE_ID
        assert attributes["agent_session_id"]

    async def test_every_tool_call_is_a_record(self, tmp_path: Path) -> None:
        _, events, trace, _, _ = await _run(tmp_path)

        tool_events = [event for event in events if event.kind == "tool.started"]
        assert len(trace.by_kind(TraceKind.TOOL_CALL)) == len(tool_events)


class TestTheToolCallCount:
    """T1 ② —— **轨迹中的工具调用数与 ``ExecutionEvent`` 记录数一致**。"""

    async def test_the_counted_count_matches_the_event_count(self, tmp_path: Path) -> None:
        """**数出来的要与事件流里的对得上。**"""
        _, events, trace, _, _ = await _run(tmp_path)

        tool_events = [event for event in events if event.kind == "tool.started"]
        assert trace.tool_calls.recorded == len(tool_events)

    async def test_the_counted_count_matches_the_reported_count(self, tmp_path: Path) -> None:
        """**数出来的要与适配器报的那个对得上** —— 两条独立的路。

        适配器汇总数的是 ``tool_use`` 内容块，轨迹数的是 ``tool.started``
        事件。**对不上就是有一类调用没进轨迹**，而那不会有任何代码报错。
        """
        result, _, trace, _, _ = await _run(tmp_path)

        assert trace.tool_calls.recorded == result.execution.tool_calls
        assert trace.tool_calls.agrees

    async def test_the_count_is_on_the_span(self, tmp_path: Path) -> None:
        """**看板上要能直接看到那个数** —— 不然还得人去数事件。"""
        _, _, _, _, span = await _run(tmp_path)

        assert _attributes(span)[TRACE_TOOL_CALLS_ATTRIBUTE] == TOOL_CALLS
