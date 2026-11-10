"""OpenCode CLI 适配器包。

导出 OpenCodeAdapter、OpenCodeInvocation 与事件/命令行构建工具。
"""

from __future__ import annotations

from executors.opencode.adapter import DEFAULT_BINARY, OpenCodeAdapter, OpenCodeInvocation
from executors.opencode.command import build_command, build_prompt
from executors.opencode.events import map_event, summarize

__all__ = [
    "DEFAULT_BINARY",
    "OpenCodeAdapter",
    "OpenCodeInvocation",
    "build_command",
    "build_prompt",
    "map_event",
    "summarize",
]
