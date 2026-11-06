"""Claude Code 的执行器适配器 —— 通过 CLI 实现统一契约。

设计依据：docs/design.md §3.4（"CLI 接入通用性最高"；七个方法的统一接口）、
docs/tasks.md TASK-P1-031。

**这个类几乎不写逻辑 —— 逻辑在 :mod:`executors.cli.adapter` 里。** 它只提供
三张协议表：命令行怎么拼、事件怎么映射、结果怎么读。这不是偷懒，而是
R-043 的直接后果："不要强制所有执行器实现同一种底层协议，而是强制它们实现
同一种系统级任务契约"。共用骨架 + 各写协议表，既保住了契约，又不强迫
Claude Code 与 Codex 长成同一个样子。

**凭证只走环境变量。** 长期密钥不进 argv（argv 会出现在 ``ps`` 的输出里）、
不进提示词（提示词会被回传给模型）。适配器只声明"凭证放在哪个环境变量名
下"，由调用方通过 ``credential_provider`` 注入**短时令牌**。
"""

from __future__ import annotations

from collections.abc import Sequence

from executors.base import ExecutionTask, SessionConfig
from executors.claude_code.command import build_command
from executors.claude_code.events import map_event, summarize
from executors.cli.adapter import (
    CliAdapter,
    CredentialProvider,
    EnvProvider,
    EventFragment,
    RunSummary,
)
from executors.cli.artifacts import WorkspaceInspector
from executors.cli.events import RawEvent

#: 默认的可执行文件名。
DEFAULT_BINARY: str = "claude"


class ClaudeCodeAdapter(CliAdapter):
    """用 Claude Code CLI 干活的执行器。"""

    #: 凭证放进子进程环境的变量名。Claude Code 从这里读 API key。
    credential_env_name: str = "ANTHROPIC_API_KEY"

    #: Claude Code 在每一条事件的 ``session_id`` 字段里回显会话 ID。
    session_id_field: str = "session_id"

    def __init__(
        self,
        *,
        binary: str = DEFAULT_BINARY,
        permission_mode: str = "default",
        credential_provider: CredentialProvider | None = None,
        env_provider: EnvProvider | None = None,
        inspector: WorkspaceInspector | None = None,
    ) -> None:
        super().__init__(
            name="claude-code",
            binary=binary,
            credential_provider=credential_provider,
            env_provider=env_provider,
            inspector=inspector,
        )
        self._permission_mode = permission_mode

    @property
    def permission_mode(self) -> str:
        """权限模式 —— 对应自治级别 L1~L4 的 CLI 侧落法。"""
        return self._permission_mode

    def build_command(
        self,
        config: SessionConfig,
        task: ExecutionTask,
        session_id: str,
    ) -> tuple[str, ...]:
        return build_command(
            binary=self.binary,
            config=config,
            task=task,
            session_id=session_id,
            permission_mode=self._permission_mode,
        )

    def map_event(self, raw: RawEvent) -> Sequence[EventFragment]:
        return map_event(raw)

    def summarize(self, raws: Sequence[RawEvent]) -> RunSummary:
        return summarize(raws)


__all__ = [
    "DEFAULT_BINARY",
    "ClaudeCodeAdapter",
]
