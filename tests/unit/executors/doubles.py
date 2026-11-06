"""两个用于契约测试的参考实现。

这两个实现**故意不一样**：一个同步跑完就返回，另一个在后台推进、支持事件流
与中断确认。它们通过**同一套**契约测试（``test_adapter_contract.py``）—— 这正是
"执行器可替换"的证据：契约挑的是行为，不是实现方式。

如果这两个实现只是同一个类的两个参数化实例，那这套契约测试证明不了什么 ——
它只证明了"一个实现符合自己"。真正的证据是：**两种不同的做法都能满足同一份
契约**。所以 :class:`InlineAdapter` 与 :class:`SessionAdapter` 在结构上就不同：

- 前者没有后台任务、没有事件、没有取消；
- 后者有。

契约测试要在这两种结构上都成立。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from core.task.outcome import ExecutionStatus
from executors.base import (
    AgentSession,
    CancellationAck,
    CancellationState,
    Capability,
    CodingAgentAdapter,
    ErrorCategory,
    ExecutionArtifacts,
    ExecutionEvent,
    ExecutionHandle,
    ExecutionMetrics,
    ExecutionResult,
    ExecutionTask,
    ExecutorError,
    SelfAssessment,
    SessionConfig,
    UnknownSessionError,
    build_idempotency_key,
)


@dataclass
class _Session:
    """适配器内部记的会话状态。"""

    session_id: str
    executor: str
    workspace: str
    created_at: datetime
    status: ExecutionStatus = ExecutionStatus.PENDING
    task: ExecutionTask | None = None
    events: list[ExecutionEvent] = field(default_factory=list)
    result: ExecutionResult | None = None
    worker: asyncio.Task[None] | None = None


def _build_result(
    executor: str,
    task: ExecutionTask,
    status: ExecutionStatus,
    *,
    commit: str | None,
    changed_files: int,
    tool_calls: int,
    cost: str,
) -> ExecutionResult:
    """造一个合规的统一结果。"""
    return ExecutionResult(
        task_id=task.task_id,
        executor=executor,
        status=status,
        artifacts=ExecutionArtifacts(
            branch=f"agent/{task.task_id}",
            commit=commit,
            changed_files=changed_files,
        ),
        execution=ExecutionMetrics(
            duration=Decimal("1.5"),
            tool_calls=tool_calls,
            cost=Decimal(cost),
            currency="USD",
        ),
        # 自我报告跟着状态走：被取消的任务不会自认完成。
        self_assessment=SelfAssessment(completed=status is ExecutionStatus.COMPLETED),
        idempotency_key=build_idempotency_key(task.task_id, task.attempt),
    )


class InlineAdapter(CodingAgentAdapter):
    """同步跑完就返回的适配器。

    不支持事件流，也不支持中断 —— 它把任务一次跑完，然后就没了。这是最"小"的
    一个执行器：能力声明里只有 :attr:`Capability.STRUCTURED_OUTPUT`。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._counter = 0

    @property
    def name(self) -> str:
        return "inline"

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.STRUCTURED_OUTPUT})

    async def create_session(self, config: SessionConfig) -> AgentSession:
        self._counter += 1
        session_id = f"inline-{self._counter}"
        created_at = datetime.now(UTC)
        self._sessions[session_id] = _Session(
            session_id=session_id,
            executor=self.name,
            workspace=config.workspace,
            created_at=created_at,
        )
        return AgentSession(
            session_id=session_id,
            executor=self.name,
            workspace=config.workspace,
            created_at=created_at,
        )

    async def execute(self, task: ExecutionTask) -> ExecutionHandle:
        session = self._session(task.session_id)
        session.task = task
        session.status = ExecutionStatus.COMPLETED
        session.result = _build_result(
            self.name,
            task,
            ExecutionStatus.COMPLETED,
            commit="inline-0001",
            changed_files=3,
            tool_calls=7,
            cost="1.25",
        )
        return ExecutionHandle(
            session_id=session.session_id,
            task_id=task.task_id,
            idempotency_key=self.idempotency_key(task),
            attempt=task.attempt,
            accepted_at=datetime.now(UTC),
        )

    async def get_status(self, session_id: str) -> ExecutionStatus:
        return self._session(session_id).status

    async def stream_events(self, session_id: str) -> AsyncIterator[ExecutionEvent]:
        """不支持事件流 —— 明确拒绝，而不是安静地返回空流。"""
        self.require_capability(Capability.STREAM_EVENTS)
        raise AssertionError("require_capability 必定抛错")

    async def interrupt(self, session_id: str) -> CancellationAck:
        self._session(session_id)
        return CancellationAck(
            session_id=session_id,
            state=CancellationState.REFUSED,
            detail="该执行器不支持中断",
        )

    async def collect_result(self, session_id: str) -> ExecutionResult:
        session = self._session(session_id)
        if session.result is None:
            raise ExecutorError(
                "会话尚未执行，没有可收集的结果",
                category=ErrorCategory.NON_RETRYABLE,
                executor=self.name,
                session_id=session_id,
            )
        return session.result

    async def terminate(self, session_id: str) -> None:
        session = self._session(session_id)
        if not session.status.is_terminal:
            session.status = ExecutionStatus.CANCELLED

    def _session(self, session_id: str) -> _Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise UnknownSessionError(self.name, session_id) from None


class SessionAdapter(CodingAgentAdapter):
    """后台推进的适配器。

    工作跑在一个后台任务里，事件一条条冒出来，中断要等后台任务**真的**停下
    才算确认。这三条恰好都是真实 CLI 执行器会遇到的事，所以它比"同步跑完"更
    接近将来的实现 —— 契约测试要在这两种结构上都成立。
    """

    def __init__(self, *, event_count: int = 3) -> None:
        self._sessions: dict[str, _Session] = {}
        self._counter = 0
        self._event_count = event_count

    @property
    def name(self) -> str:
        return "session"

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.STREAM_EVENTS,
                Capability.INTERRUPT,
                Capability.PARALLEL_SESSIONS,
                Capability.STRUCTURED_OUTPUT,
            }
        )

    async def create_session(self, config: SessionConfig) -> AgentSession:
        self._counter += 1
        session_id = f"session-{self._counter}"
        created_at = datetime.now(UTC)
        self._sessions[session_id] = _Session(
            session_id=session_id,
            executor=self.name,
            workspace=config.workspace,
            created_at=created_at,
        )
        return AgentSession(
            session_id=session_id,
            executor=self.name,
            workspace=config.workspace,
            created_at=created_at,
        )

    async def execute(self, task: ExecutionTask) -> ExecutionHandle:
        session = self._session(task.session_id)
        session.task = task
        session.status = ExecutionStatus.RUNNING
        # ``create_task`` 不会立刻跑 —— 后台任务要到下一次让出控制权才开始。
        # 这一点让"刚提交就中断"是确定的：中断一定赶在任务真正开始之前，
        # 于是"取消后拿到的是 cancelled 而不是 completed"不靠运气。
        session.worker = asyncio.create_task(self._run(session, task))
        return ExecutionHandle(
            session_id=session.session_id,
            task_id=task.task_id,
            idempotency_key=self.idempotency_key(task),
            attempt=task.attempt,
            accepted_at=datetime.now(UTC),
        )

    async def _run(self, session: _Session, task: ExecutionTask) -> None:
        for seq in range(1, self._event_count + 1):
            await asyncio.sleep(0)
            session.events.append(
                ExecutionEvent(
                    session_id=session.session_id,
                    task_id=task.task_id,
                    seq=seq,
                    kind="progress",
                    occurred_at=datetime.now(UTC),
                    payload={"step": seq},
                )
            )
        session.result = _build_result(
            self.name,
            task,
            ExecutionStatus.COMPLETED,
            commit="session-0001",
            changed_files=5,
            tool_calls=11,
            cost="2.5",
        )
        session.status = ExecutionStatus.COMPLETED

    async def get_status(self, session_id: str) -> ExecutionStatus:
        return self._session(session_id).status

    async def stream_events(self, session_id: str) -> AsyncIterator[ExecutionEvent]:
        # 能力检查在前，会话查找在后 —— 两个实现都得是这个顺序。
        # 能力是**适配器**的属性，与参数无关： incapable 的适配器对任何会话
        # 都给不了事件流，先说这一点，调用方才知道该换适配器还是换会话。
        self.require_capability(Capability.STREAM_EVENTS)
        session = self._session(session_id)

        async def _generator() -> AsyncIterator[ExecutionEvent]:
            cursor = 0
            while True:
                while cursor < len(session.events):
                    yield session.events[cursor]
                    cursor += 1
                if session.status.is_terminal:
                    return
                await asyncio.sleep(0)

        return _generator()

    async def interrupt(self, session_id: str) -> CancellationAck:
        session = self._session(session_id)
        worker = session.worker
        if worker is not None and not worker.done():
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            session.status = ExecutionStatus.CANCELLED
            if session.task is not None:
                session.result = _build_result(
                    self.name,
                    session.task,
                    ExecutionStatus.CANCELLED,
                    commit=None,
                    changed_files=0,
                    tool_calls=0,
                    cost="0",
                )
            return CancellationAck(
                session_id=session_id,
                state=CancellationState.CONFIRMED,
                detail="后台执行已取消",
            )
        return CancellationAck(
            session_id=session_id,
            state=CancellationState.CONFIRMED,
            detail="会话已不在运行",
        )

    async def collect_result(self, session_id: str) -> ExecutionResult:
        session = self._session(session_id)
        # 正在跑就等它跑完 —— "收集结果"不该返回一个半成品。
        while session.status is ExecutionStatus.RUNNING:
            await asyncio.sleep(0)
        if session.result is None:
            raise ExecutorError(
                "会话尚未执行，没有可收集的结果",
                category=ErrorCategory.NON_RETRYABLE,
                executor=self.name,
                session_id=session_id,
            )
        return session.result

    async def terminate(self, session_id: str) -> None:
        session = self._session(session_id)
        worker = session.worker
        if worker is not None and not worker.done():
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        if not session.status.is_terminal:
            session.status = ExecutionStatus.CANCELLED

    def _session(self, session_id: str) -> _Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise UnknownSessionError(self.name, session_id) from None
