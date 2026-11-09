"""执行轨迹的领域口径。

设计依据：docs/tasks.md TASK-P1-073（T1 ① 一次任务执行后可按 ``task_id``
检索到完整轨迹；T1 ② 轨迹中的工具调用数与 ``ExecutionEvent`` 记录数一致）、
R-062（每次工具调用记录主体 / 任务 / 资源 / 结果）、docs/design.md §8
（OpenTelemetry + Langfuse 必选）。
"""

from __future__ import annotations

from core.observability.trace import (
    REQUIRED_IDS,
    TOOL_CALL_KINDS,
    TOOL_RESULT_KINDS,
    TaskTrace,
    ToolCallTally,
    TraceableEvent,
    TraceContext,
    TraceError,
    TraceKind,
    TraceObserver,
    TraceRecord,
    TraceStore,
)

__all__ = [
    "REQUIRED_IDS",
    "TOOL_CALL_KINDS",
    "TOOL_RESULT_KINDS",
    "TaskTrace",
    "ToolCallTally",
    "TraceContext",
    "TraceError",
    "TraceKind",
    "TraceObserver",
    "TraceRecord",
    "TraceStore",
    "TraceableEvent",
]
