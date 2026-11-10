"""OpenCode CLI 的事件词汇表 —— 原始事件 → 统一事件。

设计依据：docs/design.md §3.4、docs/tasks.md TASK-P2-010c。
将 OpenCode CLI 输出的 NDJSON 流式事件解析映射为系统级统一事件结构，并汇总执行指标。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import cast

from core.metrics.cost import TokenUsage
from executors.cli.adapter import EventFragment, RunSummary
from executors.cli.events import RawEvent

#: 会话开始。
KIND_SESSION_STARTED: str = "session.started"
#: 助手消息。
KIND_MESSAGE_ASSISTANT: str = "message.assistant"
#: 思考过程。
KIND_MESSAGE_THINKING: str = "message.thinking"
#: 工具/动作启动。
KIND_TOOL_STARTED: str = "tool.started"
#: 观察/工具返回结果。
KIND_TOOL_COMPLETED: str = "tool.completed"
#: 完工结论。
KIND_RESULT: str = "result"
#: 未知/扩展事件。
KIND_UNHANDLED: str = "unhandled"


def _object(value: object) -> Mapping[str, object] | None:
    if isinstance(value, dict):
        return cast("Mapping[str, object]", value)
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def _usage_of(raw: RawEvent) -> TokenUsage | None:
    usage = _object(raw.get("usage"))
    if usage is None:
        return None

    input_tokens = _integer(usage.get("prompt_tokens")) or _integer(usage.get("input_tokens"))
    output_tokens = _integer(usage.get("completion_tokens")) or _integer(usage.get("output_tokens"))
    cache_read = _integer(usage.get("cache_read_tokens")) or _integer(usage.get("cached_tokens"))
    cache_write = _integer(usage.get("cache_write_tokens")) or 0

    if input_tokens is None and output_tokens is None and cache_read is None:
        return None

    return TokenUsage(
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        cache_read_tokens=cache_read or 0,
        cache_write_tokens=cache_write,
    )


def _map_session(raw: RawEvent) -> Sequence[EventFragment]:
    return (
        (
            KIND_SESSION_STARTED,
            {
                "session_id": raw.get("session_id"),
                "cwd": raw.get("cwd"),
                "model": raw.get("model"),
                "cli_version": raw.get("cli_version") or raw.get("opencode_version"),
            },
        ),
    )


def _map_thought(raw: RawEvent) -> Sequence[EventFragment]:
    text = (
        _text(raw.get("thought"))
        or _text(raw.get("plan"))
        or _text(raw.get("reasoning"))
        or _text(raw.get("content"))
    )
    if text is None:
        return ((KIND_UNHANDLED, {"raw_type": raw.type, "reason": "缺少思考内容"}),)
    return ((KIND_MESSAGE_THINKING, {"text": text}),)


def _map_message(raw: RawEvent) -> Sequence[EventFragment]:
    text = (
        _text(raw.get("content"))
        or _text(raw.get("message"))
        or _text(raw.get("text"))
        or _text(raw.get("assistant"))
    )
    if text is None:
        return ((KIND_UNHANDLED, {"raw_type": raw.type, "reason": "缺少文本内容"}),)
    return ((KIND_MESSAGE_ASSISTANT, {"text": text}),)


def _map_action(raw: RawEvent) -> Sequence[EventFragment]:
    call_id = _text(raw.get("action_id")) or _text(raw.get("call_id")) or _text(raw.get("id"))
    tool_name = _text(raw.get("action")) or _text(raw.get("name")) or _text(raw.get("tool_name"))
    return (
        (
            KIND_TOOL_STARTED,
            {
                "tool_name": tool_name,
                "tool_use_id": call_id,
                "arguments": raw.get("args") or raw.get("arguments"),
            },
        ),
    )


def _map_observation(raw: RawEvent) -> Sequence[EventFragment]:
    call_id = (
        _text(raw.get("action_id"))
        or _text(raw.get("call_id"))
        or _text(raw.get("tool_use_id"))
        or _text(raw.get("id"))
    )
    return (
        (
            KIND_TOOL_COMPLETED,
            {
                "tool_use_id": call_id,
                "is_error": bool(raw.get("is_error")),
                "content": raw.get("observation") or raw.get("content") or raw.get("output"),
            },
        ),
    )


def _map_result(raw: RawEvent) -> Sequence[EventFragment]:
    is_error = raw.get("is_error")
    status = _text(raw.get("status"))
    if is_error is None and status is not None:
        is_error = status in ("failed", "error")

    return (
        (
            KIND_RESULT,
            {
                "status": status,
                "is_error": is_error,
                "duration_ms": raw.get("duration_ms"),
                "total_cost_usd": raw.get("total_cost_usd") or raw.get("cost_usd"),
                "usage": raw.get("usage"),
                "result": raw.get("message") or raw.get("result"),
            },
        ),
    )


_EVENT_DISPATCH: Mapping[str, Callable[[RawEvent], Sequence[EventFragment]]] = {
    "session.start": _map_session,
    "init": _map_session,
    "thought": _map_thought,
    "plan": _map_thought,
    "reasoning": _map_thought,
    "message": _map_message,
    "assistant": _map_message,
    "content": _map_message,
    "action": _map_action,
    "tool_call": _map_action,
    "observation": _map_observation,
    "tool_result": _map_observation,
    "finish": _map_result,
    "result": _map_result,
    "turn.completed": _map_result,
}


def map_event(raw: RawEvent) -> Sequence[EventFragment]:
    """一条 OpenCode 原始事件 → 统一事件。"""
    mapper = _EVENT_DISPATCH.get(raw.type)
    if mapper is not None:
        return mapper(raw)
    return ((KIND_UNHANDLED, {"raw_type": raw.type, "raw": raw.raw}),)


def summarize(raws: Sequence[RawEvent]) -> RunSummary:
    """整段 OpenCode 事件流 → 执行摘要。"""
    is_error: bool | None = None
    cost: Decimal | None = None
    duration_ms: int | None = None
    text: str | None = None
    tool_calls = 0
    model: str | None = None
    usage: TokenUsage | None = None

    for raw in raws:
        if raw.type in ("session.start", "init"):
            model = _text(raw.get("model")) or model
            continue

        if raw.type in ("action", "tool_call"):
            tool_calls += 1
            continue

        if raw.type in ("message", "assistant", "content"):
            text = (
                _text(raw.get("content"))
                or _text(raw.get("message"))
                or _text(raw.get("text"))
                or text
            )
            continue

        if raw.type in ("finish", "result", "turn.completed"):
            raw_err = raw.get("is_error")
            status = _text(raw.get("status"))
            if isinstance(raw_err, bool):
                is_error = raw_err
            elif status is not None:
                is_error = status in ("failed", "error")
            else:
                is_error = False

            cost = _decimal(raw.get("total_cost_usd") or raw.get("cost_usd"))
            duration_ms = _integer(raw.get("duration_ms"))
            text = _text(raw.get("message") or raw.get("result")) or text
            usage = _usage_of(raw)

    return RunSummary(
        is_error=is_error,
        cost=cost,
        duration_ms=duration_ms,
        text=text,
        tool_calls=tool_calls,
        model=model,
        usage=usage,
    )


__all__ = [
    "KIND_MESSAGE_ASSISTANT",
    "KIND_MESSAGE_THINKING",
    "KIND_RESULT",
    "KIND_SESSION_STARTED",
    "KIND_TOOL_COMPLETED",
    "KIND_TOOL_STARTED",
    "KIND_UNHANDLED",
    "map_event",
    "summarize",
]
