"""CLI 执行器的共同骨架 —— 进程、事件流、退出码、产出物。

设计依据：docs/design.md §3.4（"统一执行器的核心接口应该是这七个方法"；
执行器协议选择：CLI / ACP / SDK / OpenHands Server，其中 **CLI 接入通用性最高**）、
§3.4 R-043（"不要强制所有执行器实现同一种底层协议，而是强制它们实现同一种
系统级任务契约"）、docs/tasks.md TASK-P1-031。

**这一层存在的理由是"共性与差异要分开"。** Claude Code 与 Codex 都是"起一个
进程、读它的 NDJSON、看它怎么退出"的 CLI。进程怎么起、事件怎么排队、退出码
怎么变成统一状态 —— 这些在两个适配器里**完全一样**；不一样的只有三件事：
命令行怎么拼、一行输出怎么变成事件、结果事件怎么读出结论。

所以这里放共性，:mod:`executors.claude_code` 与 :mod:`executors.codex` 各放
一张**协议表**。这正是 R-043 的落地方式：底层协议各写各的，系统级契约由
:class:`~executors.base.CodingAgentAdapter` 统一。

分四块：

- :mod:`executors.cli.adapter` —— 骨架本体（:class:`~executors.cli.adapter.CliAdapter`）；
- :mod:`executors.cli.events` —— NDJSON 的一行怎么变成一条原始事件；
- :mod:`executors.cli.exit_codes` —— 退出码怎么变成统一状态与错误分类；
- :mod:`executors.cli.artifacts` —— 产出物的事实来源（工作空间的 git 状态）。

**产出物只从事实里来。** CLI 说"我改了什么"是**自我报告**；工作空间里
git 记的"仓库现在什么样"才是事实（design.md §3.4："self_assessment 只是
Agent 的自我报告，不是验收结果"）。所以 :mod:`executors.cli.artifacts` 从工作
空间读，而不是从 CLI 的输出里猜。
"""

from __future__ import annotations

from executors.cli.adapter import CliAdapter, CredentialProvider, EnvProvider, RunSummary
from executors.cli.artifacts import (
    GitWorkspaceInspector,
    WorkspaceInspector,
    WorkspaceState,
    fallback_branch,
)
from executors.cli.errors import (
    CliError,
    CliInvocationError,
    CliNotFoundError,
    MalformedOutputError,
    SessionMismatchError,
)
from executors.cli.events import RawEvent, parse_event_line
from executors.cli.exit_codes import (
    EXIT_CODE_CATEGORY,
    ExitOutcome,
    classify_exit,
)

__all__ = [
    "EXIT_CODE_CATEGORY",
    "CliAdapter",
    "CliError",
    "CliInvocationError",
    "CliNotFoundError",
    "CredentialProvider",
    "EnvProvider",
    "ExitOutcome",
    "GitWorkspaceInspector",
    "MalformedOutputError",
    "RawEvent",
    "RunSummary",
    "SessionMismatchError",
    "WorkspaceInspector",
    "WorkspaceState",
    "classify_exit",
    "fallback_branch",
    "parse_event_line",
]
