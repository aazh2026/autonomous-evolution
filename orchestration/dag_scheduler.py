"""DAG 多任务调度器与失败传播引擎（TASK-P2-012）。

设计依据：
- docs/design.md §3.5C 单任务与多任务集成路径；
- docs/tasks.md TASK-P2-012（R-056 依赖图调度、DAG 执行、依赖失败的传播）。

核心保证：
1. **前置依赖满足闸门（Precondition Gate）**：
   任务只有在其所有直接前置任务均已完成（``COMPLETED`` / ``MERGED``）的前提下，
   才能被解锁并迁移至 ``READY`` 态；依赖未满足的任务绝不误启动（"依赖未满足的任务不启动"）。
2. **上游失败级联阻断（Cascading Failure Propagation）**：
   当上游任务失败（执行失败、验证不通过、修复超限或人工否决）进入 ``BLOCKED`` 态时，
   调度器通过依赖图因果传递闭包，自动将所有直接与间接后继任务级联阻断为 ``BLOCKED`` 态，
   并记录被哪条上游任务阻断及原始原因，绝不误跑（"上游失败时下游进入正确状态而非误跑"）。
3. **波次调度与进度统计（Wave Scheduling & Progress Tracking）**：
   实时提供当前就绪可并发执行的任务列表（``get_runnable_tasks``）与整体执行进度摘要。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.planning.dag import DAGTaskNode, TaskDAG, UnknownNodeError
from core.task.state_machine import TaskAction, TaskState, TransitionContext, apply_action

if TYPE_CHECKING:
    from infrastructure.database.repositories import TaskRepository


class SchedulerError(Exception):
    """调度器基类异常。"""


class IllegalExecutionError(SchedulerError):
    """违反依赖约束的非法执行尝试。"""

    def __init__(self, task_id: str, uncompleted_predecessors: Sequence[str]) -> None:
        preds = ", ".join(repr(p) for p in sorted(uncompleted_predecessors))
        super().__init__(f"任务 {task_id!r} 的前置依赖尚未完成 ({preds})，禁止启动！")
        self.task_id = task_id
        self.uncompleted_predecessors = tuple(uncompleted_predecessors)


@dataclass(frozen=True, slots=True)
class BlockageRecord:
    """任务被阻断的因果追溯记录。"""

    task_id: str
    blocked_by: str
    reason: str
    blocked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "blocked_at": self.blocked_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class DAGProgress:
    """DAG 执行进度概览。"""

    total: int
    completed: int
    blocked: int
    running: int
    ready: int
    planned: int

    @property
    def is_finished(self) -> bool:
        """是否所有任务均已终止（成功完成或被阻断）。"""
        return (self.completed + self.blocked) == self.total

    @property
    def is_successful(self) -> bool:
        """是否全量任务成功完成（无阻断）。"""
        return self.completed == self.total and self.total > 0


class DAGScheduler:
    """DAG 多任务依赖调度器。"""

    # 视为"已完成"的前置状态
    _SATISFIED_STATES: frozenset[TaskState] = frozenset(
        {
            TaskState.COMPLETED,
            TaskState.MERGED,
            TaskState.PASSED,
        }
    )

    def __init__(self, dag: TaskDAG) -> None:
        self._dag = dag
        # 实时任务状态跟踪表: task_id -> TaskState
        self._states: dict[str, TaskState] = {
            node.task_id: node.status for node in dag.list_nodes()
        }
        # 阻断追溯表: task_id -> BlockageRecord
        self._blockages: dict[str, BlockageRecord] = {}

        # 初始检查：将所有入度为 0 且处于 PLANNED 的任务自动转为 READY
        self._unlock_ready_tasks()

    @property
    def dag(self) -> TaskDAG:
        """底层依赖图。"""
        return self._dag

    def get_state(self, task_id: str) -> TaskState:
        """获取任务当前状态。"""
        if task_id not in self._states:
            raise UnknownNodeError(task_id)
        return self._states[task_id]

    def get_blockage(self, task_id: str) -> BlockageRecord | None:
        """获取任务的阻断信息（若有）。"""
        return self._blockages.get(task_id)

    def list_blockages(self) -> tuple[BlockageRecord, ...]:
        """获取所有阻断记录。"""
        return tuple(self._blockages.values())

    def can_start(self, task_id: str) -> bool:
        """检查任务当前是否允许启动执行（状态为 READY 且所有前置已满足）。"""
        if task_id not in self._states:
            raise UnknownNodeError(task_id)

        if self._states[task_id] is not TaskState.READY:
            return False

        predecessors = self._dag.direct_predecessors(task_id)
        return all(self._states[p] in self._SATISFIED_STATES for p in predecessors)

    def get_runnable_tasks(self) -> tuple[DAGTaskNode, ...]:
        """获取当前所有处于 READY 且前置依赖已满足的可执行任务。"""
        runnable_nodes: list[DAGTaskNode] = []
        for node in self._dag.list_nodes():
            if self.can_start(node.task_id):
                runnable_nodes.append(node)
        # 保持确定性：按优先级降序、标识升序
        runnable_nodes.sort(key=lambda n: (-n.priority, n.task_id))
        return tuple(runnable_nodes)

    def mark_running(self, task_id: str) -> None:
        """将就绪任务标记为执行中（RUNNING）。"""
        if not self.can_start(task_id):
            uncompleted = [
                p
                for p in self._dag.direct_predecessors(task_id)
                if self._states[p] not in self._SATISFIED_STATES
            ]
            raise IllegalExecutionError(task_id, uncompleted)

        curr = self._states[task_id]
        ctx = TransitionContext(task_id=task_id, acceptance_contract_bound=True)
        res = apply_action(curr, TaskAction.START, context=ctx)
        self._states[task_id] = res.current

    def mark_completed(self, task_id: str) -> tuple[str, ...]:
        """标记任务成功完成，并触发后继任务就绪检查。

        返回本次由于该任务完成而新解锁进入 ``READY`` 态的后继任务标识元组。
        """
        if task_id not in self._states:
            raise UnknownNodeError(task_id)

        curr = self._states[task_id]
        # 如果还在 RUNNING/PASSED/MERGED，将其推入 COMPLETED
        if curr is not TaskState.COMPLETED:
            self._states[task_id] = TaskState.COMPLETED

        # 检查所有直接后继，若其全部前驱均已完成，则将其解锁为 READY
        return self._unlock_ready_tasks()

    def mark_failed(self, task_id: str, reason: str = "") -> tuple[str, ...]:
        """标记任务失败阻断，并自动级联阻断其所有直接与传递后继任务。

        返回本次被级联阻断的所有下游任务标识元组（包含直接和间接下游）。
        """
        if task_id not in self._states:
            raise UnknownNodeError(task_id)

        # 1. 阻断当前任务
        curr = self._states[task_id]
        if curr is not TaskState.BLOCKED:
            res = apply_action(curr, TaskAction.BLOCK)
            self._states[task_id] = res.current
            self._blockages[task_id] = BlockageRecord(
                task_id=task_id,
                blocked_by=task_id,
                reason=reason or "任务自身执行或验证失败",
            )

        # 2. 级联阻断全部下游
        downstream = self._dag.all_descendants(task_id)
        blocked_downstream: list[str] = []

        for desc_id in sorted(downstream):
            d_state = self._states[desc_id]
            if d_state is not TaskState.BLOCKED:
                res = apply_action(d_state, TaskAction.BLOCK)
                self._states[desc_id] = res.current
                record = BlockageRecord(
                    task_id=desc_id,
                    blocked_by=task_id,
                    reason=f"上游任务 {task_id!r} 失败阻断: {reason or '未知原因'}",
                )
                self._blockages[desc_id] = record
                blocked_downstream.append(desc_id)

        return tuple(blocked_downstream)

    def _unlock_ready_tasks(self) -> tuple[str, ...]:
        """扫描并解锁所有前驱已满足的 PLANNED 任务为 READY。"""
        unlocked: list[str] = []
        for node in self._dag.list_nodes():
            tid = node.task_id
            if self._states[tid] is TaskState.PLANNED:
                predecessors = self._dag.direct_predecessors(tid)
                if all(self._states[p] in self._SATISFIED_STATES for p in predecessors):
                    res = apply_action(TaskState.PLANNED, TaskAction.READY)
                    self._states[tid] = res.current
                    unlocked.append(tid)
        return tuple(unlocked)

    def progress(self) -> DAGProgress:
        """获取当前依赖图执行进度。"""
        counts: dict[TaskState, int] = dict.fromkeys(TaskState, 0)
        for s in self._states.values():
            counts[s] += 1

        return DAGProgress(
            total=len(self._states),
            completed=counts[TaskState.COMPLETED]
            + counts[TaskState.MERGED]
            + counts[TaskState.PASSED],
            blocked=counts[TaskState.BLOCKED],
            running=counts[TaskState.RUNNING]
            + counts[TaskState.VERIFYING]
            + counts[TaskState.REPAIRING]
            + counts[TaskState.INTEGRATING],
            ready=counts[TaskState.READY],
            planned=counts[TaskState.PLANNED],
        )

    async def sync_to_repository(self, repository: TaskRepository) -> None:
        """将调度器的状态机变迁与阻断元数据同步持久化至数据库。"""
        for tid, state in self._states.items():
            try:
                task_uuid = uuid.UUID(tid)
            except ValueError:
                # 非 UUID 字符串标识（如测试桩任务）跳过数据库同步
                continue

            db_task = await repository.get(task_uuid)
            if db_task.status != state.value:
                # 确定对应的 TaskAction
                if state is TaskState.READY and db_task.status == TaskState.PLANNED.value:
                    await repository.transition(task_uuid, TaskAction.READY)
                elif state is TaskState.BLOCKED:
                    await repository.transition(task_uuid, TaskAction.BLOCK)

            if tid in self._blockages:
                extra = dict(db_task.extra or {})
                extra["blockage_record"] = self._blockages[tid].as_dict()
                db_task.extra = extra
