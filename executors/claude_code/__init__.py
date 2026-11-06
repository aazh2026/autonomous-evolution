"""Claude Code 执行器 —— 通过 CLI 实现统一契约。

设计依据：docs/design.md §3.4（执行器协议选择：**CLI 接入通用性最高**；
七个方法的统一接口与统一结果 schema）、docs/tasks.md TASK-P1-031。

这个包里有三块，边界很清楚：

- :mod:`executors.claude_code.command` —— 命令行怎么拼（**实测**出来的）；
- :mod:`executors.claude_code.events` —— 事件词汇表（**实测**出来的）；
- :mod:`executors.claude_code.adapter` —— 把上面两张表装进共用骨架。

共用的部分（进程管理、事件排队、退出码 → 统一状态、产出物取事实）在
:mod:`executors.cli`。分开的理由是 R-043："不要强制所有执行器实现同一种
底层协议，而是强制它们实现同一种系统级任务契约。"
"""

from __future__ import annotations

from executors.claude_code.adapter import DEFAULT_BINARY, ClaudeCodeAdapter
from executors.claude_code.command import build_command, build_prompt
from executors.claude_code.events import map_event, summarize

__all__ = [
    "DEFAULT_BINARY",
    "ClaudeCodeAdapter",
    "build_command",
    "build_prompt",
    "map_event",
    "summarize",
]
