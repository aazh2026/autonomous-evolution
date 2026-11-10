"""OpenCode CLI 执行器适配器 —— 基于统一 CLI 骨架实现。

设计依据：docs/design.md §3.4、docs/tasks.md TASK-P2-010c。
统一的是系统级任务契约（CodingAgentAdapter 的七个方法与统一执行结果），
凭证通过短时环境变量 OPENCODE_API_KEY 注入。
"""

from __future__ import annotations

from collections.abc import Sequence

from core.metrics.cost import CostConverter
from executors.base import ExecutionTask, SessionConfig
from executors.cli.adapter import (
    CliAdapter,
    CredentialProvider,
    EnvProvider,
    EventFragment,
    RunSummary,
)
from executors.cli.artifacts import WorkspaceInspector
from executors.cli.events import RawEvent
from executors.opencode.command import DEFAULT_BINARY, OpenCodeInvocation, build_command
from executors.opencode.events import map_event, summarize


class OpenCodeAdapter(CliAdapter):
    """用 OpenCode CLI 执行任务的适配器。"""

    #: 凭证环境变量名。
    credential_env_name: str = "OPENCODE_API_KEY"

    #: 会话 ID 字段名。
    session_id_field: str = "session_id"

    def __init__(
        self,
        *,
        binary: str = DEFAULT_BINARY,
        invocation: OpenCodeInvocation | None = None,
        credential_provider: CredentialProvider | None = None,
        env_provider: EnvProvider | None = None,
        inspector: WorkspaceInspector | None = None,
        cost_converter: CostConverter | None = None,
    ) -> None:
        super().__init__(
            name="opencode",
            binary=binary,
            credential_provider=credential_provider,
            env_provider=env_provider,
            inspector=inspector,
            cost_converter=cost_converter,
        )
        self._invocation = invocation or OpenCodeInvocation()

    @property
    def invocation(self) -> OpenCodeInvocation:
        """命令行调用约定。"""
        return self._invocation

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
            invocation=self._invocation,
        )

    def map_event(self, raw: RawEvent) -> Sequence[EventFragment]:
        return map_event(raw)

    def summarize(self, raws: Sequence[RawEvent]) -> RunSummary:
        return summarize(raws)


__all__ = ["DEFAULT_BINARY", "OpenCodeAdapter", "OpenCodeInvocation"]

