"""M2 自主闭环的本地执行器。

设计依据：docs/tasks.md TASK-P1-082。

继承并重用与 M1 一致的确定性补丁应用机制，但以独立的执行器标识
``m2-local-patcher`` 运行，确保在审计日志、执行会话与任务记录中清晰可溯。
"""

from __future__ import annotations

from typing import Final

from apps.director.m1_executor import (
    NOMINAL_COST,
    NOMINAL_CURRENCY,
    PATCHES,
    M1PatchExecutor,
    Patch,
)
from executors.base import AgentSession, ExecutionResult, SessionConfig

#: M2 执行器的名称。
EXECUTOR_NAME: Final[str] = "m2-local-patcher"


class M2PatchExecutor(M1PatchExecutor):
    """M2 的本地执行器。"""

    def __init__(
        self,
        patches: tuple[Patch, ...] = PATCHES,
    ) -> None:
        super().__init__(patches=patches)

    @property
    def name(self) -> str:
        """执行器名字。"""
        return EXECUTOR_NAME

    async def create_session(self, config: SessionConfig) -> AgentSession:
        """开一次会话 —— 使用 M2 执行器标识。"""
        session = await super().create_session(config)
        return AgentSession(
            session_id=session.session_id,
            executor=EXECUTOR_NAME,
            workspace=session.workspace,
            created_at=session.created_at,
        )

    async def collect_result(self, session_id: str) -> ExecutionResult:
        """收结果 —— 使用 M2 执行器标识。"""
        result = await super().collect_result(session_id)
        return ExecutionResult(
            task_id=result.task_id,
            executor=EXECUTOR_NAME,
            status=result.status,
            artifacts=result.artifacts,
            execution=result.execution,
            self_assessment=result.self_assessment,
            idempotency_key=result.idempotency_key,
        )


__all__ = ["EXECUTOR_NAME", "NOMINAL_COST", "NOMINAL_CURRENCY", "M2PatchExecutor"]
