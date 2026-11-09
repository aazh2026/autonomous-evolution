"""Codex 的执行器适配器 —— 同契约的骨架。

设计依据：docs/design.md §3.4（R-043：统一的是**系统级任务契约**，不是底层
协议；R-122/R-123：执行器接入优先级）、docs/tasks.md TASK-P1-031。

**这是一个骨架，不是可用的接入。** 它证明的是"接一个 CLI 执行器不需要重写
系统"：进程怎么起、事件怎么排队、退出码怎么变成统一状态、产出物怎么取事实
—— 全部来自 :mod:`executors.cli`，这里只声明协议表的位置。

**协议细节（事件词汇表、命令行参数）尚未对真实 CLI 核实**，所以：

- :mod:`executors.codex.events` 的事件表是空的 —— 事件原样记下，不给结论；
- :mod:`executors.codex.command` 的参数名是占位 —— 接入时按实测替换。

因此这个适配器跑完一次成功执行后，结论是**失败**（"没有产出结果事件"）。
这不是 bug，是"没有证据就不给结论"的直接后果 —— 等事件表填上，它才会给出
真正的结论。
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
from executors.codex.command import DEFAULT_BINARY, CodexInvocation, build_command
from executors.codex.events import map_event, summarize


class CodexAdapter(CliAdapter):
    """用 Codex CLI 干活的执行器 —— 骨架。"""

    #: 凭证放进子进程环境的变量名。**未核实** —— codex 实际读哪个变量，
    #: 要等真实探测。
    credential_env_name: str = "OPENAI_API_KEY"

    #: Codex 报会话 ID 时用的字段名。**未核实**。
    session_id_field: str = "session_id"

    def __init__(
        self,
        *,
        binary: str = DEFAULT_BINARY,
        invocation: CodexInvocation | None = None,
        credential_provider: CredentialProvider | None = None,
        env_provider: EnvProvider | None = None,
        inspector: WorkspaceInspector | None = None,
        cost_converter: CostConverter | None = None,
    ) -> None:
        super().__init__(
            name="codex",
            binary=binary,
            credential_provider=credential_provider,
            env_provider=env_provider,
            inspector=inspector,
            cost_converter=cost_converter,
        )
        self._invocation = invocation or CodexInvocation()

    @property
    def invocation(self) -> CodexInvocation:
        """命令行约定 —— 接入时按实测替换。"""
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


__all__ = [
    "CodexAdapter",
]
