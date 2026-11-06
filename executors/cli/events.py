"""NDJSON 的一行 —— 事件流的最小单位。

**为什么是 NDJSON 而不是 JSON。** CLI 的事件流要**边跑边出**：等跑完再解析
一个整块的 JSON，中途就没有任何东西可看 —— 而"边跑边看"正是事件流存在的
理由（执行器要能报进度、能被取消、能观察失败）。NDJSON 是一行一个 JSON
对象：可以逐行解析、逐行交付，坏了一行也不至于把整块输出废掉。

**一行什么算"坏"。** 这里只认三种坏法，因为它们都是**结构性的**、不可能靠
重试修好的：

1. 不是合法 JSON —— 解析不出来；
2. 解析出来了但不是对象 —— 是数组、数字、字符串；
3. 是对象但没有字符串类型的 ``type`` —— 不知道这条事件是什么。

除此之外的一切都**不**算坏：一个我们不认识的 ``type`` 只是"不认识"，不是
"坏了"。CLI 会加新事件类型，把不认识的当坏输出会让系统在 CLI 升级后直接
瘫掉；而不认识的事件照样会被原样记下来（``unhandled``），信息不会丢。

**空行不是事件。** CLI 的输出里可能夹空行（缓冲 flush 的边界、管道切分）。
空行返回 ``None`` —— 调用方跳过它，而不是把它当成一条坏输出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from executors.cli.errors import MalformedOutputError

#: 事件里用来标注"这一条是什么"的字段名。
EVENT_TYPE_FIELD: Final[str] = "type"


@dataclass(frozen=True, slots=True)
class RawEvent:
    """一行 NDJSON 解析出来的原始事件。

    :attr:`data` 是**只读**的 —— 事件一旦被解析出来就不该被就地改写：同一份
    原始输出被读两次必须给出同一份事件，否则"重放"就重放不出同样的东西。
    """

    type: str
    data: MappingProxyType[str, object]
    raw: str

    def get(self, name: str) -> object | None:
        """取一个字段 —— 没有就是 ``None``。"""
        return self.data.get(name)


def parse_event_line(line: str, *, executor: str = "cli") -> RawEvent | None:
    """一行 NDJSON → 原始事件。

    空行返回 ``None``（空行不是事件）。结构坏掉的行抛
    :class:`~executors.cli.errors.MalformedOutputError` —— 它带
    ``NON_RETRYABLE`` 分类，因为坏输出重跑一次还是坏的。
    """
    text = line.strip()
    if not text:
        return None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedOutputError(
            f"输出不是合法的 JSON：{exc.msg}",
            executor=executor,
            line=text,
        ) from exc

    if not isinstance(payload, dict):
        raise MalformedOutputError(
            f"输出不是 JSON 对象，而是 {type(payload).__name__}",
            executor=executor,
            line=text,
        )

    raw_type = payload.get(EVENT_TYPE_FIELD)
    if not isinstance(raw_type, str):
        raise MalformedOutputError(
            f"事件缺少字符串类型的 {EVENT_TYPE_FIELD!r} 字段",
            executor=executor,
            line=text,
        )

    data: dict[str, object] = {str(key): value for key, value in payload.items()}
    return RawEvent(type=raw_type, data=MappingProxyType(data), raw=text)


__all__ = [
    "EVENT_TYPE_FIELD",
    "RawEvent",
    "parse_event_line",
]
