"""OpenCode CLI 事件词汇表单元测试。

设计依据：docs/design.md §3.4、docs/tasks.md TASK-P2-010c。
验证 OpenCode CLI 各类流式事件准确映射为统一事件，并正确提取执行汇总。
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import MappingProxyType

from executors.cli.events import RawEvent
from executors.opencode.events import (
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


class TestOpenCodeSessionEvents:
    def test_session_start_maps_to_session_started(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "session.start",
                    "session_id": "opencode-s-001",
                    "cwd": "/workspace",
                    "model": "opencode-deepseek",
                    "opencode_version": "0.1.0",
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_SESSION_STARTED
        assert payload["session_id"] == "opencode-s-001"
        assert payload["model"] == "opencode-deepseek"
        assert payload["cli_version"] == "0.1.0"


class TestOpenCodeMessageAndThoughtEvents:
    def test_plan_maps_to_thinking(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "plan",
                    "content": "正在制定多步修改策略...",
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_MESSAGE_THINKING
        assert payload["text"] == "正在制定多步修改策略..."

    def test_message_maps_to_assistant_message(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "message",
                    "content": "变更完成。",
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_MESSAGE_ASSISTANT
        assert payload["text"] == "变更完成。"


class TestOpenCodeActionAndObservationEvents:
    def test_action_maps_to_tool_started(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "action",
                    "action_id": "act_001",
                    "action": "run_command",
                    "args": '{"cmd": "pytest"}',
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_TOOL_STARTED
        assert payload["tool_name"] == "run_command"
        assert payload["tool_use_id"] == "act_001"

    def test_observation_maps_to_tool_completed(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "observation",
                    "action_id": "act_001",
                    "observation": "all passed",
                    "is_error": False,
                }
            )
        )
        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_TOOL_COMPLETED
        assert payload["tool_use_id"] == "act_001"
        assert payload["is_error"] is False
        assert payload["content"] == "all passed"


class TestOpenCodeResultAndSummary:
    def test_finish_turn_maps_to_result(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "finish",
                    "status": "completed",
                    "is_error": False,
                    "duration_ms": 110,
                    "total_cost_usd": 0.005,
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
                    "type": "session.start",
                    "session_id": "s1",
                    "model": "deepseek-coder",
                }
            ),
            _event({"type": "plan", "content": "分析中"}),
            _event(
                {
                    "type": "action",
                    "action_id": "a1",
                    "action": "edit",
                    "args": "{}",
                }
            ),
            _event(
                {
                    "type": "observation",
                    "action_id": "a1",
                    "observation": "done",
                    "is_error": False,
                }
            ),
            _event({"type": "message", "content": "任务已完成"}),
            _event(
                {
                    "type": "finish",
                    "status": "completed",
                    "is_error": False,
                    "duration_ms": 140,
                    "total_cost_usd": 0.01,
                    "message": "全部成功",
                    "usage": {
                        "prompt_tokens": 80,
                        "completion_tokens": 40,
                        "cache_read_tokens": 20,
                    },
                }
            ),
        ]

        summary = summarize(raws)
        assert summary.is_error is False
        assert summary.cost == Decimal("0.01")
        assert summary.duration_ms == 140
        assert summary.tool_calls == 1
        assert summary.model == "deepseek-coder"
        assert summary.text == "全部成功"
        assert summary.usage is not None
        assert summary.usage.input_tokens == 80
        assert summary.usage.output_tokens == 40
        assert summary.usage.cache_read_tokens == 20

    def test_failure_finish_summarize(self) -> None:
        raws = [
            _event(
                {
                    "type": "finish",
                    "status": "failed",
                    "is_error": True,
                    "message": "执行出错",
                }
            )
        ]
        summary = summarize(raws)
        assert summary.is_error is True
        assert summary.text == "执行出错"

    def test_unhandled_event(self) -> None:
        fragments = map_event(_event({"type": "unknown_future_event", "foo": 123}))
        assert len(fragments) == 1
        assert fragments[0][0] == KIND_UNHANDLED
