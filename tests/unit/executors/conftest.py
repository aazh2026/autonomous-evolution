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

import asyncio
import gc
from pathlib import Path

import pytest

from executors.base import CodingAgentAdapter
from executors.claude_code import ClaudeCodeAdapter
from executors.codex import CodexAdapter
from executors.gemini_cli import GeminiCliAdapter
from executors.opencode import OpenCodeAdapter
from tests.unit.executors.doubles import InlineAdapter, SessionAdapter
from tests.unit.executors.stub_cli import (
    claude_events,
    codex_events,
    gemini_cli_events,
    opencode_events,
    write_stub,
)

#: 参与契约测试的实现。
ADAPTERS: tuple[str, ...] = (
    "inline",
    "session",
    "claude-code",
    "codex",
    "gemini-cli",
    "opencode",
)

#: 上一条用例的节点 ID —— 漏了资源时要说清楚**是谁**漏的。
_PREVIOUS_ITEM_ID: dict[str, str] = {"nodeid": ""}


def pytest_runtest_setup(item: pytest.Item) -> None:
    """每条用例**开始之前**先收一次垃圾，并检查上一条有没有漏子进程。

    **为什么是"开始之前"而不是"结束之后"。** 上一条用例收尾时，
    pytest-asyncio 会把那条用例的事件循环**关掉**；循环一关，asyncio 就
    再也收不到"子进程退出了"的通知 —— 那个通知是子进程监视线程排进事件
    循环的**回调**，循环关了它就跑不到了。于是**没被回收**的子进程对象要等到
    下一条用例运行时才被垃圾回收，而它在回收时喊的那句
    ``ResourceWarning: subprocess N is still running`` 会被 pytest 算在**下一条**
    用例头上。

    **这个坑真的踩过。** CI 挂在
    ``tests/unit/executors/test_cli_events.py::TestStructurallyBrokenOutput::
    test_the_broken_line_is_kept`` 上 —— 那条用例连子进程都不碰，它只是恰好
    排在后面。而真正漏资源的是前一个文件里的一条用例。

    **"没被回收"怎么认。** 传输层（transport）**没被关闭**就是没被回收 ——
    asyncio 一知道子进程退出了，就会关闭传输层（连带那两根管道），那一串
    ``ResourceWarning`` 一个都不会有。所以看 ``is_closing()`` 就够了。

    **为什么不能只看退出码。** ``Process.returncode`` 在 asyncio 收到退出
    通知时就被填上了，可那一刻传输层**可能还没关**（管道还没读到 EOF）。
    只看退出码会把"管道还开着"当成"已经收干净"。

    **为什么还要自己查一遍。** 光靠强制回收，pytest 只会把那句警告原样甩给
    下一条用例，看不出**是谁**漏的。自己查一遍就能点名 —— 报错要说清楚是谁
    的责任，不是让别人猜。
    """
    previous = _PREVIOUS_ITEM_ID["nodeid"]
    _PREVIOUS_ITEM_ID["nodeid"] = item.nodeid
    leaked = _subprocesses_that_were_not_reaped()
    gc.collect()
    if leaked:
        pytest.fail(
            f"上一个用例漏了子进程，没有回收：{', '.join(leaked)}\n"
            f"漏在：{previous}\n"
            "子进程被杀掉之后还要**回收**（await process.wait()）—— "
            "否则 asyncio 不知道它退出了，传输层不会关闭，"
            "垃圾回收时它会喊 ResourceWarning，而 pytest 会把那句话算在"
            "**下一条**用例头上。",
            pytrace=False,
        )


def _subprocesses_that_were_not_reaped() -> list[str]:
    """找出那些传输层还没关闭的子进程 —— 也就是"没被回收"的那些。"""
    leaked: list[str] = []
    for obj in gc.get_objects():
        if not isinstance(obj, asyncio.subprocess.Process):
            continue
        # 传输层是 asyncio 的私有实现细节 —— 没有公开的办法问"关没关"。
        # 但这一条正是要盯住它的理由：它在正常路径上会被关闭，
        # 只有"没回收"的时候才会留在那儿。
        transport = obj._transport  # type: ignore[attr-defined]
        if transport is not None and not transport.is_closing():
            leaked.append(f"pid={obj.pid}")
    return leaked


@pytest.fixture(params=ADAPTERS)
def adapter(request: pytest.FixtureRequest, tmp_path: Path) -> CodingAgentAdapter:
    """待测适配器 —— 四种结构不同的实现跑同一套契约测试。"""
    kind: str = request.param
    if kind == "inline":
        return InlineAdapter()
    if kind == "session":
        return SessionAdapter()
    if kind == "claude-code":
        script = write_stub(tmp_path / kind, events=claude_events())
        return ClaudeCodeAdapter(binary=str(script))
    if kind == "codex":
        script = write_stub(tmp_path / kind, events=codex_events())
        return CodexAdapter(binary=str(script))
    if kind == "gemini-cli":
        script = write_stub(tmp_path / kind, events=gemini_cli_events())
        return GeminiCliAdapter(binary=str(script))
    if kind == "opencode":
        script = write_stub(tmp_path / kind, events=opencode_events())
        return OpenCodeAdapter(binary=str(script))
    raise ValueError(f"Unknown adapter kind: {kind}")
