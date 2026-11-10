"""Codex 的事件词汇表 —— 原始事件 → 统一事件的单元测试。

设计依据：docs/design.md §3.4、docs/tasks.md TASK-P2-010。
验证 Codex 的各类流式事件能否准确映射为统一事件种类，并准确提取汇总摘要。
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import MappingProxyType

from executors.cli.events import RawEvent
from executors.codex.events import (
    KIND_MESSAGE_ASSISTANT,
    KIND_MESSAGE_THINKING,
    KIND_RESULT,
    KIND_SESSION_STARTED,
    KIND_TOOL_COMPLETED,
    KIND_TOOL_STARTED,
    KIND_UNHANDLED,
    map_event,
    summarize,
)


def _event(payload: dict[str, object]) -> RawEvent:
    """造一条原始事件。"""
    text = json.dumps(payload, ensure_ascii=False)
    return RawEvent(
        type=str(payload["type"]),
        data=MappingProxyType({str(key): value for key, value in payload.items()}),
        raw=text,
    )


class TestCodexSessionEvents:
    def test_session_start_maps_to_session_started(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "session.start",
                    "session_id": "codex-s-123",
                    "cwd": "/workspace",
                    "model": "gpt-4o",
                    "codex_version": "0.1.0",
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_SESSION_STARTED
        assert payload["session_id"] == "codex-s-123"
        assert payload["cwd"] == "/workspace"
        assert payload["model"] == "gpt-4o"
        assert payload["cli_version"] == "0.1.0"


class TestCodexMessageAndThoughtEvents:
    def test_thought_maps_to_thinking(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "thought",
                    "content": "正在审查代码逻辑...",
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_MESSAGE_THINKING
        assert payload["text"] == "正在审查代码逻辑..."

    def test_message_maps_to_assistant_message(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "message",
                    "content": "已完成修改，请检查。",
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_MESSAGE_ASSISTANT
        assert payload["text"] == "已完成修改，请检查。"


class TestCodexToolEvents:
    def test_tool_call_maps_to_tool_started(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "tool_call",
                    "call_id": "call_999",
                    "name": "edit_file",
                    "arguments": '{"path": "foo.py"}',
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_TOOL_STARTED
        assert payload["tool_name"] == "edit_file"
        assert payload["tool_use_id"] == "call_999"

    def test_tool_result_maps_to_tool_completed(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "tool_result",
                    "call_id": "call_999",
                    "output": "file written",
                    "is_error": False,
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_TOOL_COMPLETED
        assert payload["tool_use_id"] == "call_999"
        assert payload["is_error"] is False
        assert payload["content"] == "file written"


class TestCodexResultAndSummary:
    def test_turn_completed_maps_to_result(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "turn.completed",
                    "status": "completed",
                    "is_error": False,
                    "duration_ms": 120,
                    "total_cost_usd": 0.01,
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_RESULT
        assert payload["status"] == "completed"
        assert payload["is_error"] is False

    def test_turn_completed_summarize(self) -> None:
        raws = [
            _event({"type": "session.start", "session_id": "s1", "model": "gpt-4o"}),
            _event({"type": "thought", "content": "思考中"}),
            _event(
                {
                    "type": "tool_call",
                    "call_id": "c1",
                    "name": "read_file",
                    "arguments": "{}",
                }
            ),
            _event(
                {
                    "type": "tool_result",
                    "call_id": "c1",
                    "output": "data",
                    "is_error": False,
                }
            ),
            _event({"type": "message", "content": "好的"}),
            _event(
                {
                    "type": "turn.completed",
                    "status": "completed",
                    "is_error": False,
                    "duration_ms": 350,
                    "total_cost_usd": 0.025,
                    "message": "全部搞定",
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "cached_tokens": 30,
                    },
                }
            ),
        ]

        summary = summarize(raws)
        assert summary.is_error is False
        assert summary.cost == Decimal("0.025")
        assert summary.duration_ms == 350
        assert summary.tool_calls == 1
        assert summary.model == "gpt-4o"
        assert summary.text == "全部搞定"
        assert summary.usage is not None
        assert summary.usage.input_tokens == 100
        assert summary.usage.output_tokens == 50
        assert summary.usage.cache_read_tokens == 30

    def test_failure_turn_summarize(self) -> None:
        raws = [
            _event(
                {
                    "type": "turn.completed",
                    "status": "failed",
                    "is_error": True,
                    "message": "执行超时",
                }
            )
        ]
        summary = summarize(raws)
        assert summary.is_error is True
        assert summary.text == "执行超时"

    def test_unrecognized_event_maps_to_unhandled(self) -> None:
        fragments = map_event(_event({"type": "some_arbitrary_future_event", "foo": "bar"}))
        assert len(fragments) == 1
        assert fragments[0][0] == KIND_UNHANDLED
