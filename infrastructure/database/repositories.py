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
    ExecutionEvent,
    ProjectSnapshot,
    Task,
    TaskDependency,
)
from core.entities.base import utcnow
from core.metrics import cycle as cycle_domain
from core.metrics.cycle import CycleCounts, CycleStatus
from core.planning.roadmap import EvolutionRoadmap
from core.task.admission import is_bound_to
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


class OverriddenDecisionError(RepositoryError):
    """这条决策已经被推翻 —— 不能由它派生任务。

    **被推翻的决策不再派生的道理，与"决策不可变、只能追加"是同一条。** 一条
    被 Critic 否决（或被人推翻）的决策，它记录的是"当初这么想过"；要行动，
    就再记一条新的决策 —— 那条新决策会带着新的理由与新的授权。

    **这条检查堵的是"忘了"。** 拆解器已经会把没放行的决策挡在外面，但拆解
    器是应用层的一环，它可以被绕过、可以被改错、也可能压根不在那条路径上。
    仓储层是最后一道 —— 它挡的是"代码写错了"这种再普通不过的情况。
    """

    def __init__(self, decision_id: uuid.UUID, overridden_by: str) -> None:
        super().__init__(
            f"决策 {decision_id} 已被 {overridden_by!r} 推翻，不能由它派生任务 —— "
            "要行动请另记一条决策"
        )
        self.decision_id = decision_id
        self.overridden_by = overridden_by


class MissingDecisionRowError(RepositoryError):
    """路线图里的决策没有对应的数据库行。"""

    def __init__(self, decision_ids: Sequence[str]) -> None:
        super().__init__(f"路线图里的这些决策没有对应的数据库行：{'、'.join(decision_ids)}")
        self.decision_ids = tuple(decision_ids)


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

    async def overturn(self, decision_id: uuid.UUID, *, by: str) -> EvolutionDecision:
        """把一条已记录的决策标记为**被推翻**。

        **"推翻"是一个后来发生的事件，不是一条决策生下来就带着的属性。**
        决策行是一份证据 —— 它记录"当时做了什么决定、依据是什么"。推翻是
        之后另一次判断：审查层在决策落地之后回过味来，或者人改了主意。把
        这层意思压进"记录决策时顺手写一个字段"，等于让一份证据生下来就带着
        后来的结论 —— 时间线上说不通。

        **这条顺序决定了仓储层那道闸怎么读。** 闸门在**派生任务的那一刻**
        看决策的状态：

        - 推翻发生在派生**之前** → 拒。已经知道这条决策站不住了，就不该再
          从它派生任务；
        - 推翻发生在派生**之后** → 已经派生的任务**依然算数** —— 那是真做
          过的活；但这条决策不再计入"采纳率"。

        两种顺序都要能表示，所以这里给的是**一次追加**（改字段），而不是
        记录时的一个可选参数。
        """
        if not by.strip():
            raise InvalidDecisionError("推翻决策必须写明是谁推翻的")
        record = await self._session.get(EvolutionDecision, decision_id)
        if record is None:
            raise NotFoundError("演进决策", decision_id)
        if record.decision != DECISION_RECORD:
            raise InvalidDecisionError(
                f"只有 {DECISION_RECORD!r} 决策才谈得上被推翻 —— 这一条是 {record.decision!r}"
            )
        record.overridden_by = by
        await self._session.flush()
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

        **三条准入**：决策存在、是 ``record``、**没有被推翻**。第三条堵的是
        "代码写错了"—— 被 Critic 否决的决策哪怕被错传进来，也不会派生出
        任务。
        """
        record = await self._session.get(EvolutionDecision, decision_id)
        if record is None:
            raise NotFoundError("演进决策", decision_id)
        if record.decision != DECISION_RECORD:
            raise NonExecutableDecisionError(decision_id, record.decision)
        if record.overridden_by is not None:
            raise OverriddenDecisionError(decision_id, record.overridden_by)

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

    async def land_roadmap(
        self,
        roadmap: EvolutionRoadmap,
        *,
        decision_rows: Mapping[str, uuid.UUID],
    ) -> list[Task]:
        """把一份路线图落成任务行。

        **每一个任务都走 :meth:`create_from_decision`。** 这不是风格问题 ——
        那条路是全代码库唯一构造 ``Task`` 的地方，也是唯一会检查"决策是不是
        ``record``、是不是被推翻"的地方。绕开它写一行任务，就绕开了全部三道
        准入。

        **领域侧的标识与数据库侧的 id 是两回事，对应关系由调用方给出。**
        路线图里用的是决策的**领域 id**（``"EVD-cycle-0007-002"``），数据库里
        的决策行用的是 uuid。两者的对应关系是编排层知道的（它刚刚记下那些
        决策），领域层不猜 —— 猜错了就是任务挂到了别的决策上，而**不会
        报错**。

        对不上的直接报错：路线图里出现的每一个决策都必须在对应表里有着落。
        """
        if not roadmap.tasks:
            return []

        missing = sorted({task.decision_id for task in roadmap.tasks} - set(decision_rows))
        if missing:
            raise MissingDecisionRowError(missing)

        tasks: list[Task] = []
        by_key: dict[str, Task] = {}
        for planned in roadmap.tasks:
            task = await self.create_from_decision(
                decision_rows[planned.decision_id],
                title=planned.title,
                task_type=planned.task_type.value,
                description=planned.description or None,
                priority=planned.priority,
                extra={
                    "roadmap_key": planned.key,
                    "cycle_id": roadmap.cycle_id,
                    "acceptance_focus": planned.acceptance_focus,
                    "rationale": planned.rationale,
                },
            )
            tasks.append(task)
            by_key[planned.key] = task

        dependencies = TaskDependencyRepository(self._session)
        project_id = tasks[0].project_id
        for edge in roadmap.dependencies:
            await dependencies.add(
                project_id=project_id,
                predecessor_id=by_key[edge.predecessor_key].id,
                successor_id=by_key[edge.successor_key].id,
                dependency_type=edge.dependency_type.value,
            )
        return tasks

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

        **准入看的是"契约存在且绑的是这个任务"**，不只是"有契约"（R-051）。
        绑错的契约放在这儿一样能开工 —— 而事后没人说得清"它当时验的是谁
        的标准"。编排层的闸门（:mod:`orchestration.gates`）做的是完整校验
        （契约本身读不读得出来），这一层做的是**引擎级**的那一条：绑定。
        """
        task = await self.get(task_id)
        context = TransitionContext(
            task_id=str(task.id),
            acceptance_contract_bound=is_bound_to(task.acceptance_contract, str(task.id)),
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


class TaskContractStore:
    """验收契约的存取口（``core.task.admission.ContractStore`` 的实现）。

    **为什么不直接把 ``Task`` 行交给闸门。** 闸门要的是"这个任务绑着哪份
    契约"，而任务行上有一堆跟契约无关的东西（状态、修复次数、分支……）。
    把整行递过去，闸门就有了改别的字段的本事 —— 而"只能改契约"正是这道门
    该有的边界。

    **任务不存在时抛错，不返回 ``None``。** "没有契约"与"没有这个任务"是两
    件事：前者要补一份契约，后者要查任务是不是建错了。把前者当成后者，闸门
    就会对着一不存在的任务说"补一份契约"。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tasks = TaskRepository(session)

    async def contract_of(self, task_id: str) -> Mapping[str, object] | None:
        """取这个任务当前绑着的契约；没绑过就是 ``None``。"""
        task = await self._tasks.get(uuid.UUID(task_id))
        return task.acceptance_contract

    async def bind_contract(self, task_id: str, document: Mapping[str, object]) -> None:
        """把契约绑到这个任务上（覆盖已绑的那份）。"""
        await self._tasks.bind_acceptance_contract(uuid.UUID(task_id), document)

    async def unbind_contract(self, task_id: str) -> None:
        """撤回契约。

        **只对还没开工的任务有意义。** 数据库的 CHECK 约束（执行态必须有
        契约）会挡住对执行中任务的撤回 —— 那正是它该做的事：一个正在跑的任务
        被抽走了验收标准，它跑出来的结果就没有依据了。
        """
        task = await self._tasks.get(uuid.UUID(task_id))
        task.acceptance_contract = None
        await self._session.flush()


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


class SnapshotRepository:
    """项目状态快照的仓储。

    **版本号在项目内单调递增**（``(project_id, version)`` 唯一约束）。
    **版本号必须由调用方给** —— 仓储层自己去查"当前最大版本 + 1"，
    两次并发落库就会拿到同一个版本号，然后其中一个被唯一约束挡下来：
    错误信息是"违反唯一约束"，而不是"版本号重复" —— 看不出是哪两件事撞了。

    **快照要能复现。** 给定一个快照能还原到当时的 commit 与当时的指标，
    三者缺一，快照就只是一张无用的表。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        project_id: str,
        version: int,
        commit: str,
        branch: str | None = None,
        metrics: Mapping[str, object] | None = None,
    ) -> ProjectSnapshot:
        """落一份快照。"""
        snapshot = ProjectSnapshot(
            project_id=project_id,
            version=version,
            commit=commit,
            branch=branch,
            metrics=dict(metrics or {}),
        )
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def latest(self, project_id: str) -> ProjectSnapshot | None:
        """这个项目最新的一份快照 —— 没有就返回 ``None``。

            **"没有快照"与"有一份空快照"是两回事。** 达成度要拿上一份快照来比，
        拿不到就是**没有可比对的基线**（那不是"没变"）。
        """
        result = await self._session.scalars(
            select(ProjectSnapshot)
            .where(ProjectSnapshot.project_id == project_id)
            .order_by(ProjectSnapshot.version.desc())
            .limit(1)
        )
        return result.first()


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


class ExecutionEventRepository:
    """执行事件的仓储。

    **事件是只追加的。** 这里只有 ``append``，没有 ``update`` 也没有
    ``delete`` —— 一条事件写下去之后就不该再改：改了，"当时发生了什么"
    就再也答不出来了。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        event_type: str,
        payload: Mapping[str, object],
        task_id: uuid.UUID | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
        session_id: uuid.UUID | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        sequence: int | None = None,
    ) -> ExecutionEvent:
        """追加一条执行事件。

        ``session_id`` 是**会话行**的 id（外键），不是执行器侧的会话标识 ——
        两样东西混起来，事后就没法回答"这条事件属于哪一次执行"。
        """
        record = ExecutionEvent(
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            agent_id=agent_id,
            event_type=event_type,
            payload=dict(payload),
            causation_id=causation_id,
            correlation_id=correlation_id,
            sequence=sequence,
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
    "ExecutionEventRepository",
    "InvalidDecisionError",
    "NonExecutableDecisionError",
    "NotFoundError",
    "RepositoryError",
    "TaskContractStore",
    "TaskDependencyRepository",
    "TaskRepository",
]
