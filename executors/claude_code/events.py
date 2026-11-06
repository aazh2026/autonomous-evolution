"""Claude Code 的事件词汇表 —— 原始事件 → 统一事件。

设计依据：docs/design.md §3.4（统一结果 schema 与事件流）、docs/tasks.md
TASK-P1-031。

**这张表是对真实 CLI 的实测结果，不是猜的。** Claude Code 2.1.158 的
``--print --output-format stream-json --verbose`` 会依次吐出四种事件：

===================  ==========================================================
事件                 内容
===================  ==========================================================
``system``           ``subtype="init"``，带 ``session_id`` / ``cwd`` / ``model`` /
                     ``permissionMode`` / ``claude_code_version``
``assistant``        ``message.content`` 里是内容块：``text`` / ``thinking`` /
                     ``tool_use``
``user``             ``message.content`` 里是 ``tool_result``（工具的返回值）
``result``           ``is_error`` / ``api_error_status`` / ``duration_ms`` /
                     ``total_cost_usd`` / ``result``（最终文本）
===================  ==========================================================

**为什么内容块要拆成多条统一事件。** 一条 ``assistant`` 事件里可能同时有
文字和三个工具调用。合成一条就丢了"调用了几次工具"这个事实 —— 而工具调用
次数是要进成本指标的（:class:`~executors.base.ExecutionMetrics`）。所以按块
拆开：文字一条、每个工具调用一条。

**不认识的就原样记下来。** CLI 会加新事件类型。把不认识的当坏输出，会让
系统在 CLI 升级后直接瘫掉；而不认识的事件照样会被记成 ``unhandled``，
信息不丢 —— 将来核对协议时用得上。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import cast

from executors.cli.adapter import EventFragment, RunSummary
from executors.cli.events import RawEvent

#: 会话开始（CLI 报了它的配置）。
KIND_SESSION_STARTED: str = "session.started"
#: 助手说了一句话。
KIND_MESSAGE_ASSISTANT: str = "message.assistant"
#: 助手的思考过程。
KIND_MESSAGE_THINKING: str = "message.thinking"
#: 助手调用了一个工具。
KIND_TOOL_STARTED: str = "tool.started"
#: 工具返回了结果。
KIND_TOOL_COMPLETED: str = "tool.completed"
#: 这次执行的结论。
KIND_RESULT: str = "result"
#: 认识这条事件，但不知道它是什么。
KIND_UNHANDLED: str = "unhandled"


def _object(value: object) -> Mapping[str, object] | None:
    """把 JSON 里的对象取成一张可读的表。

    JSON 解析出来的东西类型是动态的，窄化成 ``Mapping[str, object]`` 之后
    才谈得上"逐字段读"。
    """
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
    """金额转 ``Decimal`` —— 走十进制字面量，不走二进制精确值。

    成本会进预算比较，浮点数的累加误差不会自己显形（R-039：三处成本同源
    同币种）。
    """
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


def _content_blocks(raw: RawEvent) -> list[Mapping[str, object]]:
    """取出一条事件里的内容块列表。"""
    message = _object(raw.get("message"))
    if message is None:
        return []
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return []
    collected: list[Mapping[str, object]] = []
    for block in blocks:
        mapped = _object(block)
        if mapped is not None:
            collected.append(mapped)
    return collected


def _map_system(raw: RawEvent) -> Sequence[EventFragment]:
    if raw.get("subtype") != "init":
        return ((KIND_UNHANDLED, {"raw_type": raw.type, "subtype": raw.get("subtype")}),)
    return (
        (
            KIND_SESSION_STARTED,
            {
                "session_id": raw.get("session_id"),
                "cwd": raw.get("cwd"),
                "model": raw.get("model"),
                "permission_mode": raw.get("permissionMode"),
                "cli_version": raw.get("claude_code_version"),
            },
        ),
    )


def _map_assistant(raw: RawEvent) -> Sequence[EventFragment]:
    fragments: list[EventFragment] = []
    for block in _content_blocks(raw):
        block_type = block.get("type")
        if block_type == "text":
            fragments.append((KIND_MESSAGE_ASSISTANT, {"text": block.get("text")}))
        elif block_type == "thinking":
            fragments.append((KIND_MESSAGE_THINKING, {"text": block.get("thinking")}))
        elif block_type == "tool_use":
            fragments.append(
                (
                    KIND_TOOL_STARTED,
                    {
                        "tool_name": block.get("name"),
                        "tool_use_id": block.get("id"),
                    },
                )
            )
        else:
            fragments.append((KIND_UNHANDLED, {"block_type": block_type}))
    if not fragments:
        return ((KIND_UNHANDLED, {"raw_type": raw.type, "reason": "没有可识别的内容块"}),)
    return tuple(fragments)


def _map_user(raw: RawEvent) -> Sequence[EventFragment]:
    fragments: list[EventFragment] = []
    for block in _content_blocks(raw):
        block_type = block.get("type")
        if block_type == "tool_result":
            fragments.append(
                (
                    KIND_TOOL_COMPLETED,
                    {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": block.get("is_error"),
                        "content": block.get("content"),
                    },
                )
            )
        else:
            fragments.append((KIND_UNHANDLED, {"block_type": block_type}))
    if not fragments:
        return ((KIND_UNHANDLED, {"raw_type": raw.type, "reason": "没有可识别的内容块"}),)
    return tuple(fragments)


def _map_result(raw: RawEvent) -> Sequence[EventFragment]:
    return (
        (
            KIND_RESULT,
            {
                "subtype": raw.get("subtype"),
                "is_error": raw.get("is_error"),
                "api_error_status": raw.get("api_error_status"),
                "num_turns": raw.get("num_turns"),
                "stop_reason": raw.get("stop_reason"),
                "total_cost_usd": raw.get("total_cost_usd"),
                "duration_ms": raw.get("duration_ms"),
                "result": raw.get("result"),
            },
        ),
    )


def map_event(raw: RawEvent) -> Sequence[EventFragment]:
    """一条 Claude Code 原始事件 → 若干条统一事件。"""
    if raw.type == "system":
        return _map_system(raw)
    if raw.type == "assistant":
        return _map_assistant(raw)
    if raw.type == "user":
        return _map_user(raw)
    if raw.type == "result":
        return _map_result(raw)
    return ((KIND_UNHANDLED, {"raw_type": raw.type, "raw": raw.raw}),)


def summarize(raws: Sequence[RawEvent]) -> RunSummary:
    """把整段事件流读成一份摘要。

    :attr:`RunSummary.is_error` 是**三态**：``None`` 表示没有结论（连结果事件
    都没有），``False`` 表示有结论且不是失败，``True`` 表示有结论且是失败。
    这个区分是 :func:`~executors.cli.exit_codes.classify_exit` 的前提 ——
    "没结论"与"结论是成功"走的是不同的分支。

    工具调用次数数的是 ``tool_use`` 内容块 —— Claude Code 的结果事件里没有
    这个字段。数内容块是能拿到的**事实**，而不是"轮数"这种间接指标。
    """
    is_error: bool | None = None
    api_error_status: int | None = None
    cost: Decimal | None = None
    duration_ms: int | None = None
    text: str | None = None
    tool_calls = 0

    for raw in raws:
        if raw.type == "assistant":
            tool_calls += sum(
                1 for block in _content_blocks(raw) if block.get("type") == "tool_use"
            )
            continue
        if raw.type != "result":
            continue
        # 出现结果事件本身就是"这次执行有结论了"；``is_error`` 说的是结论
        # 好不好。字段缺失时按"不是失败"处理 —— 这是防御性的：真实 CLI 总是
        # 带这个字段，而缺了它就报"没有结论"会误伤一次成功的执行。
        is_error = raw.get("is_error") is True
        api_error_status = _integer(raw.get("api_error_status"))
        cost = _decimal(raw.get("total_cost_usd"))
        duration_ms = _integer(raw.get("duration_ms"))
        text = _text(raw.get("result"))

    return RunSummary(
        is_error=is_error,
        api_error_status=api_error_status,
        cost=cost,
        tool_calls=tool_calls,
        duration_ms=duration_ms,
        text=text,
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
