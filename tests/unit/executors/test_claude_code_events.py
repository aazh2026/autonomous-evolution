"""Claude Code 的事件词汇表 —— 原始事件 → 统一事件。

**这张表是对真实 CLI 的实测结果，所以它的每个字段名都不能凭感觉改。** 写
错了，后果不是"报个错"，而是**把这次执行做的事记成另一件事** —— 而那看起来
是成功的：一次失败的执行被记成"完成了"，谁也看不出来。

所以这里的断言都写成"某个字段映射成某个统一事件"，而不是"映射成某种大概
的意思"。

**内容块要拆开。** 一条 ``assistant`` 事件里可能同时有文字和三个工具调用。
合成一条就丢了"调用了几次工具"这个事实 —— 而工具调用次数是要进成本指标
的。
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import MappingProxyType

from executors.claude_code.events import (
    KIND_MESSAGE_ASSISTANT,
    KIND_RESULT,
    KIND_SESSION_STARTED,
    KIND_TOOL_COMPLETED,
    KIND_TOOL_STARTED,
    KIND_UNHANDLED,
    map_event,
    summarize,
)
from executors.cli.events import RawEvent


def _event(payload: dict[str, object]) -> RawEvent:
    """造一条原始事件 —— 直接从 JSON 走一遍，免得测试里造出"实现专用"的对象。"""
    text = json.dumps(payload, ensure_ascii=False)
    return RawEvent(
        type=str(payload["type"]),
        data=MappingProxyType({str(key): value for key, value in payload.items()}),
        raw=text,
    )


class TestTheSystemEvent:
    def test_the_init_event_starts_the_session(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "abc",
                    "cwd": "/tmp",
                    "model": "test-model",
                    "permissionMode": "default",
                    "claude_code_version": "2.1.158",
                }
            )
        )

        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_SESSION_STARTED
        assert payload["model"] == "test-model"
        assert payload["permission_mode"] == "default"
        assert payload["cli_version"] == "2.1.158"

    def test_an_unknown_system_subtype_is_unhandled(self) -> None:
        fragments = map_event(_event({"type": "system", "subtype": "compact_boundary"}))

        assert [kind for kind, _ in fragments] == [KIND_UNHANDLED]


class TestTheAssistantEvent:
    def test_a_text_block_becomes_an_assistant_message(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "你好"}]},
                }
            )
        )

        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_MESSAGE_ASSISTANT
        assert payload["text"] == "你好"

    def test_a_tool_use_block_becomes_a_started_tool(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Read",
                                "input": {"file_path": "README.md"},
                            }
                        ],
                    },
                }
            )
        )

        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_TOOL_STARTED
        assert payload["tool_name"] == "Read"
        assert payload["tool_use_id"] == "toolu_1"

    def test_the_blocks_are_split_and_kept_in_order(self) -> None:
        """一条 assistant 事件里有文字也有工具调用 —— 拆开才数得出工具次数。"""
        fragments = map_event(
            _event(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "先看看文件"},
                            {"type": "tool_use", "id": "t1", "name": "Read"},
                            {"type": "tool_use", "id": "t2", "name": "Edit"},
                        ],
                    },
                }
            )
        )

        assert [kind for kind, _ in fragments] == [
            KIND_MESSAGE_ASSISTANT,
            KIND_TOOL_STARTED,
            KIND_TOOL_STARTED,
        ]

    def test_a_thinking_block_is_kept_separately(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "让我想想"}],
                    },
                }
            )
        )

        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind != KIND_MESSAGE_ASSISTANT
        assert payload["text"] == "让我想想"

    def test_an_assistant_event_without_content_blocks_is_unhandled(self) -> None:
        fragments = map_event(_event({"type": "assistant", "message": {"role": "assistant"}}))

        assert [kind for kind, _ in fragments] == [KIND_UNHANDLED]


class TestTheUserEvent:
    def test_a_tool_result_block_completes_the_tool(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "t1", "content": "文件内容"}
                        ],
                    },
                }
            )
        )

        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_TOOL_COMPLETED
        assert payload["tool_use_id"] == "t1"


class TestTheResultEvent:
    def test_it_becomes_the_result(self) -> None:
        fragments = map_event(
            _event(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "api_error_status": None,
                    "duration_ms": 1755,
                    "num_turns": 1,
                    "result": "你好",
                    "total_cost_usd": 0.0290289,
                }
            )
        )

        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_RESULT
        assert payload["is_error"] is False
        assert payload["total_cost_usd"] == 0.0290289


class TestAnUnknownEvent:
    def test_it_is_recorded_verbatim(self) -> None:
        """不认识的事件要原样记下来 —— 信息不丢，将来核对协议时用得上。"""
        fragments = map_event(_event({"type": "brand_new_kind", "payload": 1}))

        assert len(fragments) == 1
        kind, payload = fragments[0]
        assert kind == KIND_UNHANDLED
        assert payload["raw_type"] == "brand_new_kind"


class TestTheSummary:
    def _raws(self) -> list[RawEvent]:
        return [
            _event(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "abc",
                    "model": "test-model",
                }
            ),
            _event(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "你好"},
                            {"type": "tool_use", "id": "t1", "name": "Read"},
                            {"type": "tool_use", "id": "t2", "name": "Edit"},
                        ],
                    },
                }
            ),
            _event(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "api_error_status": None,
                    "duration_ms": 1755,
                    "result": "你好",
                    "total_cost_usd": 0.0290289,
                }
            ),
        ]

    def test_a_successful_run_is_not_an_error(self) -> None:
        summary = summarize(self._raws())

        assert summary.is_error is False

    def test_the_cost_is_a_decimal(self) -> None:
        """成本会进预算比较 —— 浮点数的累加误差不会自己显形。"""
        summary = summarize(self._raws())

        assert summary.cost == Decimal("0.0290289")

    def test_the_duration_comes_from_the_result_event(self) -> None:
        summary = summarize(self._raws())

        assert summary.duration_ms == 1755

    def test_the_tool_calls_are_counted(self) -> None:
        """数的是 ``tool_use`` 内容块 —— 那才是事实，"轮数"是间接指标。"""
        summary = summarize(self._raws())

        assert summary.tool_calls == 2

    def test_the_final_text_is_kept(self) -> None:
        summary = summarize(self._raws())

        assert summary.text == "你好"

    def test_without_a_result_event_there_is_no_conclusion(self) -> None:
        """没有结果事件 = 没有结论。**这不是"没错"。**"""
        summary = summarize(
            [
                _event(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "x"}],
                        },
                    }
                )
            ]
        )

        assert summary.is_error is None

    def test_a_reported_failure_is_an_error(self) -> None:
        summary = summarize(
            [
                _event(
                    {
                        "type": "result",
                        "is_error": True,
                        "api_error_status": 500,
                        "duration_ms": 10,
                    }
                )
            ]
        )

        assert summary.is_error is True
        assert summary.api_error_status == 500


class TestTheModel:
    """**跑的是哪个模型** —— 核对成本时要用它去找单价。"""

    def test_it_comes_from_the_init_event(self) -> None:
        summary = summarize(
            [_event({"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"})]
        )

        assert summary.model == "claude-sonnet-4-6"

    def test_without_an_init_event_there_is_no_model(self) -> None:
        summary = summarize([_event({"type": "assistant", "message": {"content": []}})])

        assert summary.model is None

    def test_a_non_init_system_event_is_not_a_model(self) -> None:
        """不是 init 的系统事件不报模型 —— 那里报的是别的东西。"""
        summary = summarize([_event({"type": "system", "subtype": "other", "model": "x"})])

        assert summary.model is None


class TestTheUsage:
    """**实际用了多少 token** —— 核对成本的那条第二条路径靠它。

    **缓存 token 单独记。** 缓存读与缓存写各有各的价 —— 并进 ``input_tokens``
    的话，这笔账就按错的价算，而多算出来的钱不会有人喊。
    """

    def test_it_comes_from_the_result_event(self) -> None:
        summary = summarize(
            [
                _event(
                    {
                        "type": "result",
                        "is_error": False,
                        "usage": {
                            "input_tokens": 12_000,
                            "output_tokens": 3_500,
                            "cache_read_input_tokens": 20_000,
                            "cache_creation_input_tokens": 1_200,
                        },
                    }
                )
            ]
        )

        assert summary.usage is not None
        assert summary.usage.input_tokens == 12_000
        assert summary.usage.output_tokens == 3_500
        assert summary.usage.cache_read_tokens == 20_000
        assert summary.usage.cache_write_tokens == 1_200

    def test_the_assistant_usage_is_summed_when_the_result_has_none(self) -> None:
        """助手消息一条对应一次 API 调用 —— 加起来就是总量。"""
        summary = summarize(
            [
                _event(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "x"}],
                            "usage": {"input_tokens": 100, "output_tokens": 10},
                        },
                    }
                ),
                _event(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "y"}],
                            "usage": {"input_tokens": 200, "output_tokens": 20},
                        },
                    }
                ),
            ]
        )

        assert summary.usage is not None
        assert summary.usage.input_tokens == 300
        assert summary.usage.output_tokens == 30

    def test_the_result_usage_wins_over_the_summed_one(self) -> None:
        """汇总优先 —— 那是执行器自己算的数。"""
        summary = summarize(
            [
                _event(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "x"}],
                            "usage": {"input_tokens": 100, "output_tokens": 10},
                        },
                    }
                ),
                _event(
                    {
                        "type": "result",
                        "is_error": False,
                        "usage": {"input_tokens": 999, "output_tokens": 99},
                    }
                ),
            ]
        )

        assert summary.usage is not None
        assert summary.usage.input_tokens == 999

    def test_without_any_usage_there_is_none(self) -> None:
        """**"没读到用量"与"这次没用 token"是两件事** —— 分得开。"""
        summary = summarize([_event({"type": "result", "is_error": False})])

        assert summary.usage is None

    def test_a_usage_object_without_token_fields_is_not_a_usage(self) -> None:
        """认不出来的字段不能当成"这次用了 0 个 token"。"""
        summary = summarize(
            [_event({"type": "result", "is_error": False, "usage": {"service_tier": "standard"}})]
        )

        assert summary.usage is None
