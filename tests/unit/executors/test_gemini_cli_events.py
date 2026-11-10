"""Google Gemini CLI 事件词汇表单元测试。

设计依据：docs/design.md §3.4、docs/tasks.md TASK-P2-010b。
验证 Gemini CLI 各类流式事件准确映射为统一事件，并正确提取执行汇总。
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import MappingProxyType

from executors.cli.events import RawEvent
from executors.gemini_cli.events import (
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
    text = json.dumps(payload, ensure_ascii=False)
    return RawEvent(
        type=str(payload["type"]),
        data=MappingProxyType({str(key): value for key, value in payload.items()}),
        raw=text,
    )


class TestGeminiSessionEvents:
    def test_session_init_maps_to_session_started(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "session.init",
                    "session_id": "gemini-s-100",
                    "cwd": "/workspace",
                    "model": "gemini-2.0-flash",
                    "cli_version": "0.2.0",
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_SESSION_STARTED
        assert payload["session_id"] == "gemini-s-100"
        assert payload["model"] == "gemini-2.0-flash"
        assert payload["cli_version"] == "0.2.0"


class TestGeminiMessageAndThoughtEvents:
    def test_thought_maps_to_thinking(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "thought",
                    "thought": "正在规划工具调用策略...",
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_MESSAGE_THINKING
        assert payload["text"] == "正在规划工具调用策略..."

    def test_content_maps_to_assistant_message(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "content",
                    "content": "代码修改已应用。",
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_MESSAGE_ASSISTANT
        assert payload["text"] == "代码修改已应用。"


class TestGeminiToolEvents:
    def test_call_maps_to_tool_started(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "call",
                    "call_id": "call_123",
                    "name": "write_file",
                    "arguments": '{"path": "test.txt"}',
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_TOOL_STARTED
        assert payload["tool_name"] == "write_file"
        assert payload["tool_use_id"] == "call_123"

    def test_response_maps_to_tool_completed(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "response",
                    "call_id": "call_123",
                    "output": "ok",
                    "is_error": False,
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_TOOL_COMPLETED
        assert payload["tool_use_id"] == "call_123"
        assert payload["is_error"] is False
        assert payload["content"] == "ok"


class TestGeminiResultAndSummary:
    def test_result_turn_maps_to_result(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "result",
                    "status": "completed",
                    "is_error": False,
                    "duration_ms": 190,
                    "total_cost_usd": 0.012,
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_RESULT
        assert payload["status"] == "completed"
        assert payload["is_error"] is False

    def test_full_stream_summarize(self) -> None:
        raws = [
            _event(
                {
                    "type": "session.init",
                    "session_id": "s1",
                    "model": "gemini-2.0-flash",
                }
            ),
            _event({"type": "thought", "thought": "分析中"}),
            _event(
                {
                    "type": "call",
                    "call_id": "c1",
                    "name": "run_test",
                    "arguments": "{}",
                }
            ),
            _event(
                {
                    "type": "response",
                    "call_id": "c1",
                    "output": "pass",
                    "is_error": False,
                }
            ),
            _event({"type": "content", "content": "已通过"}),
            _event(
                {
                    "type": "result",
                    "status": "completed",
                    "is_error": False,
                    "duration_ms": 250,
                    "total_cost_usd": 0.018,
                    "result": "任务顺利完成",
                    "usage": {
                        "prompt_token_count": 150,
                        "candidates_token_count": 60,
                        "cached_content_token_count": 40,
                    },
                }
            ),
        ]

        summary = summarize(raws)
        assert summary.is_error is False
        assert summary.cost == Decimal("0.018")
        assert summary.duration_ms == 250
        assert summary.tool_calls == 1
        assert summary.model == "gemini-2.0-flash"
        assert summary.text == "任务顺利完成"
        assert summary.usage is not None
        assert summary.usage.input_tokens == 150
        assert summary.usage.output_tokens == 60
        assert summary.usage.cache_read_tokens == 40

    def test_failure_result_summarize(self) -> None:
        raws = [
            _event(
                {
                    "type": "result",
                    "status": "failed",
                    "is_error": True,
                    "message": "模型拒绝请求",
                }
            )
        ]
        summary = summarize(raws)
        assert summary.is_error is True
        assert summary.text == "模型拒绝请求"

    def test_unhandled_event(self) -> None:
        fragments = map_event(_event({"type": "custom_extension", "val": 1}))
        assert len(fragments) == 1
        assert fragments[0][0] == KIND_UNHANDLED

