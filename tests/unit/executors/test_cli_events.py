"""NDJSON 一行 → 原始事件。

**这条链路的规矩只有一条：坏输出要能立刻认出来，而且认出来之后不该重试。**

CLI 的输出是一条条 NDJSON。解析它看起来是个小活儿，但"什么算坏"这个决定
会直接影响编排层的行为 —— 判错了，两种后果都很贵：

- 把坏输出当成"暂时没数据" → 继续等，等到超时；
- 把不认识的事件类型当成坏输出 → CLI 一升级，整个系统瘫掉。

所以这里把三种**结构性**的坏法钉死（不是合法 JSON / 不是 JSON 对象 / 没有
字符串类型的 ``type``），其余的一律不算坏。
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from executors.base import ErrorCategory
from executors.cli.errors import MalformedOutputError
from executors.cli.events import EVENT_TYPE_FIELD, RawEvent, parse_event_line


class TestALegalLine:
    def test_it_becomes_an_event(self) -> None:
        event = parse_event_line('{"type": "assistant", "text": "你好"}')

        assert event is not None
        assert event.type == "assistant"
        assert event.get("text") == "你好"

    def test_the_missing_field_is_none(self) -> None:
        event = parse_event_line('{"type": "assistant"}')

        assert event is not None
        assert event.get("text") is None

    def test_the_raw_line_is_kept(self) -> None:
        """原始那行要留着 —— 排查时日志可能已经被截断过了。"""
        line = '{"type": "assistant", "text": "你好"}'

        event = parse_event_line(line)

        assert event is not None
        assert event.raw == line

    def test_the_data_is_read_only(self) -> None:
        """事件一旦解析出来就不该被就地改写 —— 重放必须重放得出同样东西。"""
        event = parse_event_line('{"type": "assistant"}')

        assert event is not None
        with pytest.raises(TypeError):
            event.data["type"] = "别的"  # type: ignore[index]

    def test_surrounding_whitespace_is_ignored(self) -> None:
        event = parse_event_line('  {"type": "assistant"}  \n')

        assert event is not None
        assert event.type == "assistant"


class TestABlankLine:
    def test_it_is_not_an_event(self) -> None:
        """空行不是事件 —— CLI 的输出里会夹空行（缓冲 flush 的边界）。"""
        assert parse_event_line("") is None
        assert parse_event_line("   \n") is None


class TestStructurallyBrokenOutput:
    """坏输出必须**立刻**炸出来，而且不可重试。

    不可重试的理由：同一份输入重跑一次，得到的还是同一份坏输出。重试不是
    修复，是再付一次钱。
    """

    def test_output_that_is_not_json(self) -> None:
        with pytest.raises(MalformedOutputError) as excinfo:
            parse_event_line("{这不是 JSON}")

        assert excinfo.value.category is ErrorCategory.NON_RETRYABLE

    def test_output_that_is_not_an_object(self) -> None:
        """JSON 数组、数字、字符串 —— 都解析得出来，但都不是事件。"""
        for line in ("[1, 2]", "42", '"一个字符串"'):
            with pytest.raises(MalformedOutputError) as excinfo:
                parse_event_line(line)

            assert excinfo.value.category is ErrorCategory.NON_RETRYABLE

    def test_an_object_without_a_type(self) -> None:
        with pytest.raises(MalformedOutputError) as excinfo:
            parse_event_line('{"text": "你好"}')

        assert excinfo.value.category is ErrorCategory.NON_RETRYABLE

    def test_an_object_whose_type_is_not_a_string(self) -> None:
        with pytest.raises(MalformedOutputError) as excinfo:
            parse_event_line('{"type": 42}')

        assert excinfo.value.category is ErrorCategory.NON_RETRYABLE

    def test_the_broken_line_is_kept(self) -> None:
        """坏在哪一行要说得出来 —— 只有"输出坏了"是查不出问题的。"""
        with pytest.raises(MalformedOutputError) as excinfo:
            parse_event_line("{这不是 JSON}")

        assert excinfo.value.line == "{这不是 JSON}"


class TestAnUnknownEventType:
    def test_it_is_still_an_event(self) -> None:
        """不认识 ≠ 坏了。

        CLI 会加新事件类型。把不认识的当坏输出，会让系统在 CLI 升级后直接
        瘫掉 —— 而"不认识的"照样能原样记下来，信息不丢。
        """
        event = parse_event_line('{"type": "brand_new_kind", "payload": 1}')

        assert event is not None
        assert event.type == "brand_new_kind"
        assert event.get("payload") == 1


class TestTheEventType:
    def test_the_type_field_is_the_documented_one(self) -> None:
        assert EVENT_TYPE_FIELD == "type"


class TestRawEvent:
    def test_it_can_be_built_directly(self) -> None:
        """RawEvent 也要能自己造 —— 测试里造假事件比拼字符串方便。"""
        event = RawEvent(type="x", data=MappingProxyType({"a": 1}), raw='{"type":"x"}')

        assert event.get("a") == 1
        assert event.get("missing") is None
