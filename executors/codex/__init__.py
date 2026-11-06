"""Codex 执行器 —— 同契约的**骨架**。

设计依据：docs/design.md §3.4（执行器协议选择；R-043）、docs/tasks.md
TASK-P1-031。

**骨架与可用实现的区别在这里说清楚。** :mod:`executors.claude_code` 的协议
细节是对真实 CLI **实测**出来的；这里的**没有** —— 写它的时候这台机器上没装
codex。所以：

- :mod:`executors.codex.command` —— 命令行参数是**占位**，接入时按实测替换；
- :mod:`executors.codex.events` —— 事件表是**空的**，事件原样记下、不给结论；
- :mod:`executors.codex.adapter` —— 把上面两块装进 :mod:`executors.cli` 的
  共用骨架。

骨架证明的是"接一个 CLI 执行器不需要重写系统"；它**不是**一个能用的 codex
接入。接入步骤见 :mod:`executors.codex.command` 的模块文档。
"""

from __future__ import annotations

from executors.codex.adapter import CodexAdapter
from executors.codex.command import DEFAULT_BINARY, CodexInvocation, build_command, build_prompt
from executors.codex.events import map_event, summarize

__all__ = [
    "DEFAULT_BINARY",
    "CodexAdapter",
    "CodexInvocation",
    "build_command",
    "build_prompt",
    "map_event",
    "summarize",
]
