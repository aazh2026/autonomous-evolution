"""Google Gemini CLI 适配器包。

导出 GeminiCliAdapter、GeminiInvocation 与事件/命令行构建工具。
"""

from __future__ import annotations

from executors.gemini_cli.adapter import DEFAULT_BINARY, GeminiCliAdapter, GeminiInvocation
from executors.gemini_cli.command import build_command, build_prompt
from executors.gemini_cli.events import map_event, summarize

__all__ = [
    "DEFAULT_BINARY",
    "GeminiCliAdapter",
    "GeminiInvocation",
    "build_command",
    "build_prompt",
    "map_event",
    "summarize",
]
