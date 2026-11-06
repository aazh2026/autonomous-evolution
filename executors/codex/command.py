"""Codex 的命令行约定 —— **尚未对真实 CLI 核实**。

设计依据：docs/design.md §3.4（执行器协议：CLI；R-043 强制统一的是**系统级
任务契约**，不是底层协议）、docs/tasks.md TASK-P1-031。

**这个模块里的每一个参数名都还没有实测。** 写它的时候这台机器上没装
codex，所以：既没有真实事件样例，也没有真实的 argv 样例。留一份"看起来像"
的默认值，好过留空 —— 留空的话骨架连跑都跑不起来；把默认值写清楚"未核实"，
至少让读代码的人知道**哪些是事实、哪些是占位**。

**接入 codex 的第一步不是写代码，是探测。** 具体三条：

1. 装上 codex CLI，跑一次真实调用，把完整的 stdout（每一行）与 argv 原样
   存下来；
2. 用这些样例把 :class:`CodexInvocation` 的四个字段换成实测值；
3. 把实测到的事件行填进 :mod:`executors.codex.events` 的事件表，再跑
   ``tests/unit/executors/test_cli_adapter.py`` 里的 codex 用例。

在那之前，:mod:`executors.codex.events` **故意不给结论** —— 没有证据就没有
结论，这和 :func:`executors.cli.exit_codes.classify_exit` 里"没有结果事件就
算失败"是同一条原则。
"""

from __future__ import annotations

from dataclasses import dataclass

from executors.base import ExecutionTask, SessionConfig

#: 默认的可执行文件名。
DEFAULT_BINARY: str = "codex"


@dataclass(frozen=True, slots=True)
class CodexInvocation:
    """codex 的命令行约定。

    **四个字段全部未经核实。** 它们只保证"骨架能跑通契约测试"，不保证
    "接上真 codex 就能用"。替换它们是这个骨架接入时的第一件事。
    """

    #: 子命令。
    subcommand: str = "exec"
    #: 让它输出结构化事件流的开关。
    json_flag: str = "--json"
    #: 会话 ID 的开关 —— 我们给，它回显。
    session_id_flag: str = "--session-id"
    #: 模型的开关。
    model_flag: str = "--model"


def build_prompt(task: ExecutionTask) -> str:
    """把任务变成给 CLI 的那段提示词。"""
    title = task.title.strip()
    instruction = task.instruction.strip()
    if not title:
        return instruction
    if not instruction:
        return title
    return f"{title}\n\n{instruction}"


def build_command(
    *,
    binary: str,
    config: SessionConfig,
    task: ExecutionTask,
    session_id: str,
    invocation: CodexInvocation | None = None,
) -> tuple[str, ...]:
    """这次执行的命令行。

    结构与 Claude Code 那份一致（开关 → 会话 → 模型 → 提示词），只是参数名
    不同 —— 而参数名正是**未核实**的那部分。
    """
    agreed = invocation or CodexInvocation()
    argv: list[str] = [binary, agreed.subcommand, agreed.json_flag]
    argv.extend((agreed.session_id_flag, session_id))
    if config.model:
        argv.extend((agreed.model_flag, config.model))
    argv.append(build_prompt(task))
    return tuple(argv)


__all__ = [
    "DEFAULT_BINARY",
    "CodexInvocation",
    "build_command",
    "build_prompt",
]
