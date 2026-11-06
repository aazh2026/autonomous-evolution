"""契约测试的参数化装置。

**为什么用 conftest 参数化，而不是写两份测试。** 契约测试的价值在于"多个
实现跑同一份断言"。手写多份等于多份断言会各自漂移 —— 改了一条忘了改另一条，
几个实现就"都通过"了，而契约已经变了。参数化把同一段断言跑几遍，漂移无从
发生。

**参数化到类而不是实例。** 每个用例拿到一个**全新**的适配器：共用实例会让
用例之间通过内部状态互相影响，那样"通过"靠的是运气而不是契约。

**四种实现，结构真的不同。** :class:`InlineAdapter`（同步跑完就返回）、
:class:`SessionAdapter`（后台推进、有事件流、有中断确认）、Claude Code CLI
适配器与 Codex CLI 适配器（**真的起子进程**、真的读 NDJSON、真的按退出码
定性）。最后两个尤其重要：进程边界上的那些坑（stdout 没人读、stderr 写满
缓冲区把进程卡死、SIGKILL 之后还要等它真的退出）假对象一个都测不出来。

**CLI 那两个用打桩 CLI，不调真实 Agent。** 打桩 CLI 是一个真的可执行文件，
但它只按配置吐事件 —— 不联网、不花钱、不碰工作空间里的任何文件。

**工作空间沿用契约测试自己写的 ``/tmp/ases-ws*``。** 契约测试断言的就是
"会话报告的工作空间等于配置里给的那个"，所以这里不改它 —— CLI 适配器会
把这些目录建出来，那本来就是契约测试指定的地方。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from executors.base import CodingAgentAdapter
from executors.claude_code import ClaudeCodeAdapter
from executors.codex import CodexAdapter
from tests.unit.executors.doubles import InlineAdapter, SessionAdapter
from tests.unit.executors.stub_cli import claude_events, write_stub

#: 参与契约测试的实现。
ADAPTERS: tuple[str, ...] = ("inline", "session", "claude-code", "codex")


@pytest.fixture(params=ADAPTERS)
def adapter(request: pytest.FixtureRequest, tmp_path: Path) -> CodingAgentAdapter:
    """待测适配器 —— 四种结构不同的实现跑同一套契约测试。"""
    kind: str = request.param
    if kind == "inline":
        return InlineAdapter()
    if kind == "session":
        return SessionAdapter()
    script = write_stub(tmp_path / kind, events=claude_events())
    if kind == "claude-code":
        return ClaudeCodeAdapter(binary=str(script))
    return CodexAdapter(binary=str(script))
