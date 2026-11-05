"""仓储层 —— 数据写入的唯一受控入口。

设计依据：docs/design.md §3.6（三表分离）、docs/tasks.md TASK-P1-022
（``Task`` 无旁路写入路径）、TASK-P1-051（R-051 验收契约准入）、
R-034 / R-035（defer 语义）。

这个模块存在的唯一理由，是把"三表分离"从**约定**变成**结构**：

    EvolutionCandidate → EvolutionDecision → Task

每一跳都必须在仓储层留痕，而且**没有捷径**：

- 没有 ``add(task)`` 这种通用写入口 —— 只有
  :meth:`TaskRepository.create_from_decision`。
- 只有 ``record`` 决策能派生任务 —— ``defer`` / ``reject`` 决策产不出任务
  （R-034：defer 那一轮不得创建 Task）。
- 没有 ``EvolutionDecision``，就构造不出 ``Task`` —— 数据库的 NOT NULL
  外键会独立地把这条规则再执行一遍。

应用层的检查会被绕过（有人直接写 SQL、有人临时改代码），数据库层的
不会。所以两边都做：应用层给出可读的错误，数据库层给出不可绕过的保证。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import (
    AgentSession,
    EvolutionCandidate,
    EvolutionCycle,
    EvolutionDecision,
    Task,
    TaskDependency,
)
from core.entities.base import utcnow
from core.metrics import cycle as cycle_domain
from core.metrics.cycle import CycleCounts, CycleStatus
from core.task.state_machine import TaskAction, TaskState, TransitionContext, apply_action

#: 决策三态（tasks.md TASK-P1-054）。
DECISION_RECORD = "record"
DECISION_DEFER = "defer"
DECISION_REJECT = "reject"
DECISION_STATES = (DECISION_RECORD, DECISION_DEFER, DECISION_REJECT)


class RepositoryError(Exception):
    """仓储层错误基类。"""


class NotFoundError(RepositoryError):
    """对象不存在。"""

    def __init__(self, kind: str, identifier: object) -> None:
        super().__init__(f"{kind} 不存在：{identifier}")
        self.kind = kind
        self.identifier = identifier


class InvalidDecisionError(RepositoryError):
    """决策不合法。"""


class NonExecutableDecisionError(RepositoryError):
    """该决策不能派生任务。

    只有 ``record`` 决策可以执行。``defer``（先补分析）与 ``reject``（放弃）
    都是合法的"不行动"结局 —— 它们**不能**产生任务。
    """

    def __init__(self, decision_id: uuid.UUID, decision: str) -> None:
        super().__init__(
            f"决策 {decision_id} 的状态是 {decision!r}，只有 {DECISION_RECORD!r} 决策能派生任务"
        )
        self.decision_id = decision_id
        self.decision = decision


class AcceptanceContractError(RepositoryError):
    """验收契约缺失或与任务不匹配（R-051）。"""


class DuplicateCycleError(RepositoryError):
    """周期编号重复。

    ``cycle_id`` 是全局唯一的人类可读编号。重复意味着两轮演进的记录会
    混在一起 —— 计数对不上，证据串了，而表面上看不出来。
    """

    def __init__(self, cycle_id: str) -> None:
        super().__init__(f"周期编号 {cycle_id!r} 已经存在")
        self.cycle_id = cycle_id


class CandidateRepository:
    """演进机会的仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        project_id: str,
        category: str,
        title: str,
        evidence: Sequence[Mapping[str, object]],
        hypothesis: str | None = None,
        expected_outcome: Sequence[object] = (),
        value: Decimal = Decimal(0),
        risk: Decimal = Decimal(0),
        confidence: Decimal = Decimal(0),
        estimated_effort: str | None = None,
        cycle_id: uuid.UUID | None = None,
    ) -> EvolutionCandidate:
        """登记一个演进机会。

        ``evidence`` 不能为空 —— 没有证据的候选不该存在。
        """
        if not evidence:
            raise InvalidDecisionError("演进机会必须带证据 —— 没有证据的候选只是猜测")
        candidate = EvolutionCandidate(
            project_id=project_id,
            cycle_id=cycle_id,
            category=category,
            title=title,
            evidence=[dict(item) for item in evidence],
            hypothesis=hypothesis,
            expected_outcome=list(expected_outcome),
            value=value,
            risk=risk,
            confidence=confidence,
            estimated_effort=estimated_effort,
        )
        self._session.add(candidate)
        await self._session.flush()
        return candidate

    async def get(self, candidate_id: uuid.UUID) -> EvolutionCandidate:
        candidate = await self._session.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise NotFoundError("演进机会", candidate_id)
        return candidate


class DecisionRepository:
    """演进决策的仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        candidate_id: uuid.UUID,
        decision: str,
        rationale: str,
        requested_analysis: Sequence[object] = (),
        cycle_id: uuid.UUID | None = None,
        overridden_by: str | None = None,
    ) -> EvolutionDecision:
        """对某个候选作出决策。

        - 决策必须挂在已有候选上（``candidate_id`` 外键会再挡一次）。
        - ``defer`` 必须给出至少一条补充分析项（R-034 / R-035）：
          "以后再说"要说清楚还要补什么。
        - ``reject`` 必须有理由 —— 放弃也要说清楚为什么。
        """
        if decision not in DECISION_STATES:
            raise InvalidDecisionError(
                f"未知决策 {decision!r}，合法取值：{', '.join(DECISION_STATES)}"
            )
        if decision == DECISION_DEFER and not requested_analysis:
            raise InvalidDecisionError("defer 决策必须产出至少一条补充分析项")
        if not rationale.strip():
            raise InvalidDecisionError("决策必须写明理由")

        candidate = await self._session.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise NotFoundError("演进机会", candidate_id)

        record = EvolutionDecision(
            candidate_id=candidate_id,
            project_id=candidate.project_id,
            cycle_id=cycle_id if cycle_id is not None else candidate.cycle_id,
            decision=decision,
            rationale=rationale,
            requested_analysis=list(requested_analysis),
            overridden_by=overridden_by,
        )
        candidate.status = {
            DECISION_RECORD: "accepted",
            DECISION_DEFER: "deferred",
            DECISION_REJECT: "rejected",
        }[decision]
        self._session.add(record)
        await self._session.flush()
        return record

    async def get(self, decision_id: uuid.UUID) -> EvolutionDecision:
        record = await self._session.get(EvolutionDecision, decision_id)
        if record is None:
            raise NotFoundError("演进决策", decision_id)
        return record


class TaskRepository:
    """研发任务的仓储 —— **任务唯一的新增入口**。

    这个类刻意不提供 ``add(task)`` / ``create(task)`` 之类的通用方法。
    任何 ``Task`` 实例都必须由 :meth:`create_from_decision` 产生，
    也就是必须能追溯到一条 ``record`` 决策。
    """

    def __init__(self, session: AsyncSession, *, max_repairs: int = 3) -> None:
        self._session = session
        self._max_repairs = max_repairs

    async def create_from_decision(
        self,
        decision_id: uuid.UUID,
        *,
        title: str,
        task_type: str,
        description: str | None = None,
        priority: Decimal = Decimal(0),
        branch: str | None = None,
        workspace_id: str | None = None,
        executor: str | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> Task:
        """由一条 ``record`` 决策派生任务。

        这是全代码库唯一构造 :class:`~core.entities.Task` 的地方
        （``tests/unit/database/test_bypass_paths.py`` 静态校验这一点）。
        """
        record = await self._session.get(EvolutionDecision, decision_id)
        if record is None:
            raise NotFoundError("演进决策", decision_id)
        if record.decision != DECISION_RECORD:
            raise NonExecutableDecisionError(decision_id, record.decision)

        task = Task(
            project_id=record.project_id,
            decision_id=record.id,
            title=title,
            description=description,
            type=task_type,
            priority=priority,
            status=TaskState.APPROVED.value,
            branch=branch,
            workspace_id=workspace_id,
            executor=executor,
            extra=dict(extra or {}),
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def get(self, task_id: uuid.UUID) -> Task:
        task = await self._session.get(Task, task_id)
        if task is None:
            raise NotFoundError("任务", task_id)
        return task

    async def list_by_project(self, project_id: str) -> list[Task]:
        result = await self._session.scalars(
            select(Task).where(Task.project_id == project_id).order_by(Task.created_at)
        )
        return list(result)

    async def bind_acceptance_contract(
        self, task_id: uuid.UUID, contract: Mapping[str, object]
    ) -> Task:
        """绑定验收契约（R-051）。

        契约必须写明它属于哪个任务，且该 task_id 必须与本任务一致 ——
        把 A 任务的验收契约绑到 B 任务上，等于给 B 开了一张假通行证。
        """
        task = await self.get(task_id)
        bound = contract.get("task_id")
        if bound is None:
            raise AcceptanceContractError("验收契约必须写明 task_id")
        if str(bound) != str(task.id):
            raise AcceptanceContractError(f"验收契约绑定的是任务 {bound}，与任务 {task.id} 不一致")
        task.acceptance_contract = dict(contract)
        await self._session.flush()
        return task

    async def transition(self, task_id: uuid.UUID, action: TaskAction) -> Task:
        """按状态机推进任务状态。

        非法迁移由状态机抛错（不静默吞掉）；幂等重投是空操作。
        """
        task = await self.get(task_id)
        context = TransitionContext(
            task_id=str(task.id),
            acceptance_contract_bound=task.acceptance_contract is not None,
            repair_count=task.repair_count,
            max_repairs=self._max_repairs,
        )
        result = apply_action(TaskState(task.status), action, context)
        if not result.changed:
            return task
        task.status = result.current.value
        now = utcnow()
        if result.current is TaskState.RUNNING and task.started_at is None:
            task.started_at = now
        if result.current is TaskState.COMPLETED:
            task.completed_at = now
        await self._session.flush()
        return task


class CycleRepository:
    """演进周期的仓储。

    **计数由数据库数出来，不接受调用方声明。** 周期记录里写"这一轮发现了
    5 个候选"而库里躺着 3 个，是最容易被忽略也最伤信任的一种偏差 —— 记录
    看起来很完整，但它描述的不是实际发生的事。所以 :meth:`close_cycle`
    自己数，自己写。

    计数必须**按周期**数，不能按项目数。这两者的差别在多周期并行时才会显
    出来，而那正是最需要这份记录准确的时刻：统计口径错了，"这一轮到底做
    成了什么"就再也答不上来。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open_cycle(
        self,
        *,
        project_id: str,
        cycle_id: str,
        trigger: str,
        started_at: datetime | None = None,
    ) -> EvolutionCycle:
        """开一个周期。

        周期在**开始时**就要落库 —— 候选与决策挂在它身上（``cycle_id``
        外键）。事后补记的周期挂不住那一轮已经产生的东西，那一轮的产出就
        永远算不进它的计数里。
        """
        existing = await self._session.scalar(
            select(EvolutionCycle).where(EvolutionCycle.cycle_id == cycle_id)
        )
        if existing is not None:
            raise DuplicateCycleError(cycle_id)

        row = EvolutionCycle(
            project_id=project_id,
            cycle_id=cycle_id,
            trigger=trigger,
            status=None,
            outcome=None,
            metrics={},
            started_at=started_at or utcnow(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, cycle_uuid: uuid.UUID) -> EvolutionCycle:
        row = await self._session.get(EvolutionCycle, cycle_uuid)
        if row is None:
            raise NotFoundError("演进周期", cycle_uuid)
        return row

    async def counts(self, cycle_uuid: uuid.UUID) -> CycleCounts:
        """数出这个周期里实际有多少候选 / 决策 / 任务 / 已完成任务。

        任务没有 ``cycle_id`` 列 —— 它挂在决策下面，决策挂在周期下面。所以
        任务要**穿过决策**才能归到周期上。这条链路断一处，计数就多算或
        少算，而数字看上去仍然"像个整数"。
        """
        candidates = await self._session.scalar(
            select(func.count())
            .select_from(EvolutionCandidate)
            .where(EvolutionCandidate.cycle_id == cycle_uuid)
        )
        decisions = await self._session.scalar(
            select(func.count())
            .select_from(EvolutionDecision)
            .where(EvolutionDecision.cycle_id == cycle_uuid)
        )
        tasks = await self._session.scalar(
            select(func.count())
            .select_from(Task)
            .join(EvolutionDecision, EvolutionDecision.id == Task.decision_id)
            .where(EvolutionDecision.cycle_id == cycle_uuid)
        )
        completed = await self._session.scalar(
            select(func.count())
            .select_from(Task)
            .join(EvolutionDecision, EvolutionDecision.id == Task.decision_id)
            .where(
                EvolutionDecision.cycle_id == cycle_uuid,
                Task.status == TaskState.COMPLETED.value,
            )
        )
        return CycleCounts(
            candidates=candidates or 0,
            decisions=decisions or 0,
            tasks=tasks or 0,
            completed_tasks=completed or 0,
        )

    async def close_cycle(
        self,
        cycle_uuid: uuid.UUID,
        *,
        status: CycleStatus,
        attainment_before: Decimal | None = None,
        attainment_after: Decimal | None = None,
        cost: Decimal = Decimal(0),
        currency: str = "",
        evidence_refs: Sequence[str] = (),
        ended_at: datetime | None = None,
    ) -> EvolutionCycle:
        """结束一个周期，写入完整记录。

        结局（improved / no_change / regressed / stalled）由领域模型判定，
        **不接受外部指定** —— 给了也能被改写，那这份记录就不再是判定结果，
        而是"某个人声称的结果"。
        """
        row = await self.get(cycle_uuid)
        if row.ended_at is not None:
            raise InvalidDecisionError(f"周期 {row.cycle_id!r} 已经结束过了，不能重复结束")

        record = cycle_domain.EvolutionCycle.build(
            project_id=row.project_id,
            cycle_id=row.cycle_id,
            trigger=row.trigger,
            started_at=row.started_at or utcnow(),
            ended_at=ended_at or utcnow(),
            status=status,
            counts=await self.counts(cycle_uuid),
            attainment_before=attainment_before,
            attainment_after=attainment_after,
            cost=cost,
            currency=currency,
            evidence_refs=tuple(evidence_refs),
        )

        stored = EvolutionCycle.from_domain(record)
        row.status = stored.status
        row.outcome = stored.outcome
        row.metrics = stored.metrics
        row.ended_at = stored.ended_at
        await self._session.flush()
        return row


class TaskDependencyRepository:
    """任务依赖的仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        project_id: str,
        predecessor_id: uuid.UUID,
        successor_id: uuid.UUID,
        dependency_type: str = "finish_to_start",
    ) -> TaskDependency:
        if predecessor_id == successor_id:
            raise InvalidDecisionError("任务不能依赖自己 —— 那是死锁，不是依赖")
        dependency = TaskDependency(
            project_id=project_id,
            predecessor_id=predecessor_id,
            successor_id=successor_id,
            dependency_type=dependency_type,
        )
        self._session.add(dependency)
        await self._session.flush()
        return dependency


class AgentSessionRepository:
    """执行会话的仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open(
        self,
        *,
        project_id: str,
        task_id: uuid.UUID,
        agent_id: str,
        executor: str,
        session_id: str,
        authorization: Mapping[str, object],
        started_at: datetime | None = None,
    ) -> AgentSession:
        """开一个执行会话。

        ``authorization`` 落库的是授权范围快照（agent_id / task_id /
        project_id / workspace_id / expiry）—— 事后审计要能回答
        "当时授权的是什么"，而不是"当时应该授权什么"。
        """
        record = AgentSession(
            project_id=project_id,
            task_id=task_id,
            agent_id=agent_id,
            executor=executor,
            session_id=session_id,
            authorization=dict(authorization),
            started_at=started_at or utcnow(),
        )
        self._session.add(record)
        await self._session.flush()
        return record


__all__ = [
    "DECISION_DEFER",
    "DECISION_RECORD",
    "DECISION_REJECT",
    "DECISION_STATES",
    "AcceptanceContractError",
    "AgentSessionRepository",
    "CandidateRepository",
    "CycleRepository",
    "DecisionRepository",
    "DuplicateCycleError",
    "InvalidDecisionError",
    "NonExecutableDecisionError",
    "NotFoundError",
    "RepositoryError",
    "TaskDependencyRepository",
    "TaskRepository",
]
