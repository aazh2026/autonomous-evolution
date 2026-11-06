"""Codex 的事件词汇表 —— **尚未对真实 CLI 核实**。

设计依据：docs/design.md §3.4（R-043）、docs/tasks.md TASK-P1-031。

**这里为什么是空的。** 与 :mod:`executors.claude_code.events` 那张实测出来的
表相比，这张表一个字段都没有 —— 因为写它的时候这台机器上没装 codex，没有
任何真实事件样例。

**空表比猜出来的表好。** 猜错事件名，后果不是"报个错"，而是**把这次执行做
的事记成另一件事**：猜 `"done"` 而实际是 `"thread.completed"`，一次失败的执行
会被记成"完成了"；反过来，一次成功的执行会被记成"还在跑"。而这些错误在结果
里**看不出来** —— 结果只说"完成了"，只有逐条比对事件才发现对不上。

所以这里只做一件诚实的事：**原样记下我们不认识的事件**（``unhandled``），
并且**不给结论**（:func:`summarize` 返回空摘要）。空摘要会被
:func:`executors.cli.exit_codes.classify_exit` 判成"没有产出结果事件 ⇒ 失败"
—— 这正是"我们没有证据"应有的结论。

**接入时要做的三件事**（详见 :mod:`executors.codex.command` 的模块文档）：

1. 装上 codex，跑一次真实调用，把完整 stdout 存下来；
2. 把实测到的事件类型填成一张表（类型 → 统一事件种类），不确定的字段宁可
   不映射；
3. 把结果事件里"成功/失败""成本""时长"三个字段的位置填进 :func:`summarize`。
"""

from __future__ import annotations

from collections.abc import Sequence

from executors.cli.adapter import EventFragment, RunSummary
from executors.cli.events import RawEvent

#: 认识这条事件，但不知道它是什么。
KIND_UNHANDLED: str = "unhandled"


def map_event(raw: RawEvent) -> Sequence[EventFragment]:
    """一条 Codex 原始事件 → 统一事件。

    **当前只原样记下。** 见模块文档：没有实测样例时，猜出来的映射比没有
    映射更危险。
    """
    return ((KIND_UNHANDLED, {"raw_type": raw.type, "raw": raw.raw}),)


def summarize(raws: Sequence[RawEvent]) -> RunSummary:
    """整段事件流 → 摘要。

    **当前是空的。** 没有实测过结果事件里"成功/失败"在哪个字段，就不能读
    它 —— 读错字段的后果是把失败读成成功。
    """
    return RunSummary()


__all__ = [
    "KIND_UNHANDLED",
    "map_event",
    "summarize",
]
