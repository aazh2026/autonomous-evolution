"""占位写法 —— **"以后再说"的一百种写法**。

设计依据：docs/tasks.md R-034（R-035 决策三态：Record / Defer / Reject）、
R-103（系统自身改进提案机制）。

**为什么单独拎出来。** 同一个坑会有两处踩：决策里的"补充分析项"（R-034）与
自我改进提案里的"改了什么"（R-103）—— 都是**用一个看起来填了、其实信息量为
零的字符串**把事情糊过去。两处各写一份清单，迟早会对不上，而对不上的那几行
恰好是最容易被人拿来自欺的那几行。

**这些写法能通过"非空"检查，但信息量是零。** "以后再说"必须说清楚还要补什
么，"改了什么"必须说清楚改了什么 —— **否则字段看着是填的，读的人却什么也没
拿到**。
"""

from __future__ import annotations

from typing import Final

#: 占位写法 —— **比的时候先去掉首尾空白、再转小写**。
PLACEHOLDER_DESCRIPTIONS: Final[frozenset[str]] = frozenset(
    {
        "待补充",
        "待定",
        "稍后再说",
        "tbd",
        "todo",
        "n/a",
        "na",
        "none",
        "-",
        "--",
        "...",
        "无",
    }
)


def is_placeholder(text: str) -> bool:
    """这段话是不是占位写法 —— **先去掉首尾空白再比**。"""
    return text.strip().lower() in PLACEHOLDER_DESCRIPTIONS


__all__ = ["PLACEHOLDER_DESCRIPTIONS", "is_placeholder"]
