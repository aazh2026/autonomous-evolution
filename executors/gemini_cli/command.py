"""Google Gemini CLI 的命令行约定。

设计依据：docs/design.md §3.4、docs/tasks.md TASK-P2-010b。
构建调用 Gemini CLI 的命令行参数：
gemini exec --json --session-id <session_id> [--model <model>] "<prompt>"
"""

from __future__ import annotations

from dataclasses import dataclass

from executors.base import ExecutionTask, SessionConfig

#: 默认的可执行文件名。
DEFAULT_BINARY: str = "gemini"


@dataclass(frozen=True, slots=True)
class GeminiInvocation:
    """Gemini CLI 的命令行调用约定。"""

    #: 子命令。
    subcommand: str = "exec"
    #: 输出结构化 JSON 流的开关。
    json_flag: str = "--json"
    #: 会话 ID 参数开关。
    session_id_flag: str = "--session-id"
    #: 模型参数开关。
    model_flag: str = "--model"


def build_prompt(task: ExecutionTask) -> str:
    """把任务组装成给 CLI 的提示词。"""
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
    invocation: GeminiInvocation | None = None,
) -> tuple[str, ...]:
    """构建 Gemini CLI 执行命令行。"""
    agreed = invocation or GeminiInvocation()
    argv: list[str] = [binary, agreed.subcommand, agreed.json_flag]
    argv.extend((agreed.session_id_flag, session_id))
    if config.model:
        argv.extend((agreed.model_flag, config.model))
    argv.append(build_prompt(task))
    return tuple(argv)


__all__ = [
    "DEFAULT_BINARY",
    "GeminiInvocation",
    "build_command",
    "build_prompt",
]

