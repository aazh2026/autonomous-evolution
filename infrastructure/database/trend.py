"""趋势看板的原料读取 —— 从库里取出算指标要的那些行。

设计依据：docs/tasks.md TASK-P3-017（交付物"跨周期趋势看板（达成度、质量、
成本）"；验收"能导出 ≥7 天连续运行的四层指标趋势"；验证 T2）、R-039 /
I-13（成本同源同币种）、R-117 / R-118。

**这一层只取数，不算指标。** 算指标是 :mod:`evaluation.trend` 的事 ——
指标层不碰数据库，而这一层不知道什么叫"有效发现率"。**两边都不碰对方
的活**，所以每一格都能被单独测。

**取数有三条纪律。**

- **只取已闭合的周期。** 没闭合的周期没有结局，它下面的任务也还没有最终
  状态 —— 拿它算指标，等于把"还在跑"当成"没跑完"。
- **取全部历史，不只取窗口里的。** 看板的曲线是**累计**口径：第 D 天的
  点算的是"截至第 D 天为止"。所以窗口开始之前的历史也要取出来 ——
  否则第一天与第二天的分母不是一回事，那条曲线自己跟自己都没法比。
- **成本只认账本。** 钱记在账本上（:mod:`infrastructure.database.budget`），
  周期记录里的 ``cost`` 是调用方抄过去的汇总 —— 看板用账本，因为那是
  **同源**的那一份（R-039）。

**被拒绝的花费不进成本。** 账本里 ``blocked`` 的流水是"系统试图超预算
但被拦住了"的证据 —— 那不是花掉的钱。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.entities.budget import BudgetEntry
from core.entities.evidence import VerificationRun
from core.entities.evolution import EvolutionCandidate, EvolutionCycle, EvolutionDecision
from core.entities.task import Task
from core.policy.budget import BudgetMetric, SpendStatus
from core.task.state_machine import TaskState
from evaluation.evolution_evaluation.decision import DecisionRecord
from evaluation.evolution_evaluation.discovery import Discovery
from evaluation.task_evaluation.delivery import TaskOutcome
from evaluation.trend import CostSample, CycleSample, TrendInput, VerificationSample


class PostgresTrendReader:
    """看板原料的读取口。

    **读的是全部历史，止于 ``until``。** 窗口只是横轴的范围 —— 累计口径
    下，第 D 天的点要算"截至第 D 天为止"，所以更早的行也得取出来。
    """

    __slots__ = ("_cost_metric", "_session_factory")

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cost_metric: BudgetMetric = BudgetMetric.TOTAL_COST,
    ) -> None:
        self._session_factory = session_factory
        self._cost_metric = cost_metric

    @property
    def cost_metric(self) -> BudgetMetric:
        """成本取自哪个预算维度。"""
        return self._cost_metric

    async def read(
        self,
        *,
        project_id: str,
        until: date,
        scope: str | None = None,
    ) -> TrendInput:
        """取出一个项目的全部原料。

        :param until: 取到哪一天为止（含）。**按 UTC 日切** —— 全库的时间
            都是 UTC（:func:`core.entities.base.utcnow`），看板按本地时区
            切日会让同一条记录在两台机器上落到不同的格子里。
        :param scope: 预算账本的作用域。**默认就是项目 id** —— 账本的
            ``scope`` 记的是"这笔钱算在谁头上"。
        """
        budget_scope = scope if scope is not None else project_id
        upper = datetime.combine(until + timedelta(days=1), time.min, tzinfo=UTC)

        async with self._session_factory() as session:
            cycles = await self._cycles(session, project_id=project_id, upper=upper)
            cycle_ids = [row.id for row in cycles]
            candidates = await self._candidates(session, cycle_ids=cycle_ids)
            decisions = await self._decisions(session, cycle_ids=cycle_ids)
            tasks = await self._tasks(session, cycle_ids=cycle_ids)
            runs = await self._verifications(
                session,
                cycle_ids=cycle_ids,
                task_ids=[row.id for row in tasks],
                upper=upper,
            )
            entries = await self._costs(
                session,
                scope=budget_scope,
                upper=upper,
            )

        return _to_input(
            _Rows(
                cycles=cycles,
                candidates=candidates,
                decisions=decisions,
                tasks=tasks,
                runs=runs,
                entries=entries,
            )
        )

    async def _cycles(
        self,
        session: AsyncSession,
        *,
        project_id: str,
        upper: datetime,
    ) -> list[EvolutionCycle]:
        """已闭合的周期 —— 按结束时间排。"""
        statement = (
            select(EvolutionCycle)
            .where(
                EvolutionCycle.project_id == project_id,
                EvolutionCycle.ended_at.is_not(None),
                EvolutionCycle.ended_at < upper,
            )
            .order_by(EvolutionCycle.ended_at, EvolutionCycle.id)
        )
        return list(await session.scalars(statement))

    async def _candidates(
        self,
        session: AsyncSession,
        *,
        cycle_ids: list[uuid.UUID],
    ) -> list[EvolutionCandidate]:
        """这些周期里的候选 —— 按发现时间排。"""
        if not cycle_ids:
            return []
        statement = (
            select(EvolutionCandidate)
            .where(EvolutionCandidate.cycle_id.in_(cycle_ids))
            .order_by(EvolutionCandidate.created_at, EvolutionCandidate.id)
        )
        return list(await session.scalars(statement))

    async def _decisions(
        self,
        session: AsyncSession,
        *,
        cycle_ids: list[uuid.UUID],
    ) -> list[EvolutionDecision]:
        """这些周期里的决策 —— 按作出时间排。"""
        if not cycle_ids:
            return []
        statement = (
            select(EvolutionDecision)
            .where(EvolutionDecision.cycle_id.in_(cycle_ids))
            .order_by(EvolutionDecision.created_at, EvolutionDecision.id)
        )
        return list(await session.scalars(statement))

    async def _tasks(
        self,
        session: AsyncSession,
        *,
        cycle_ids: list[uuid.UUID],
    ) -> list[Task]:
        """这些周期里的任务。

        **任务要穿过决策才能归到周期上** —— 任务表上没有 ``cycle_id`` 列。
        """
        if not cycle_ids:
            return []
        statement = (
            select(Task)
            .join(EvolutionDecision, EvolutionDecision.id == Task.decision_id)
            .where(EvolutionDecision.cycle_id.in_(cycle_ids))
            .order_by(Task.created_at, Task.id)
        )
        return list(await session.scalars(statement))

    async def _verifications(
        self,
        session: AsyncSession,
        *,
        cycle_ids: list[uuid.UUID],
        task_ids: list[uuid.UUID],
        upper: datetime,
    ) -> list[VerificationRun]:
        """验收记录 —— 按发生的时刻排。

        **两条路都认**：挂着周期的验收，与挂在这些周期的任务上的验收。
        只认一条的话，另一条就成了"查不到" —— 而查不到与没验过长得
        一模一样。
        """
        if not cycle_ids and not task_ids:
            return []
        conditions = []
        if cycle_ids:
            conditions.append(VerificationRun.cycle_id.in_(cycle_ids))
        if task_ids:
            conditions.append(VerificationRun.task_id.in_(task_ids))
        statement = (
            select(VerificationRun)
            .where(or_(*conditions), VerificationRun.created_at < upper)
            .order_by(VerificationRun.created_at, VerificationRun.id)
        )
        return list(await session.scalars(statement))

    async def _costs(
        self,
        session: AsyncSession,
        *,
        scope: str,
        upper: datetime,
    ) -> list[BudgetEntry]:
        """预算账本里的成本流水。

        **只取记下来的（``recorded``）。** 被拒绝的流水是"系统试图超预算
        但被拦住了"的证据，不是花掉的钱。
        """
        statement = (
            select(BudgetEntry)
            .where(
                BudgetEntry.scope == scope,
                BudgetEntry.metric == self._cost_metric.value,
                BudgetEntry.status == SpendStatus.RECORDED.value,
                BudgetEntry.recorded_at < upper,
            )
            .order_by(BudgetEntry.recorded_at, BudgetEntry.id)
        )
        return list(await session.scalars(statement))


@dataclass(frozen=True, slots=True)
class _Rows:
    """从库里取出来的那几堆行。"""

    cycles: list[EvolutionCycle]
    candidates: list[EvolutionCandidate]
    decisions: list[EvolutionDecision]
    tasks: list[Task]
    runs: list[VerificationRun]
    entries: list[BudgetEntry]


def _to_input(rows: _Rows) -> TrendInput:
    """把库里的行摆成看板的原料。

    **按周期归堆。** 周期是看板的时间单位 —— 一个周期闭合的那一刻，它底
    下的一切都成了定论。
    """
    discoveries_by_cycle: dict[uuid.UUID, list[Discovery]] = {}
    for candidate in rows.candidates:
        if candidate.cycle_id is None:
            continue
        discoveries_by_cycle.setdefault(candidate.cycle_id, []).append(
            Discovery(
                candidate_id=str(candidate.id),
                cycle_id=str(candidate.cycle_id),
                title=candidate.title,
                evidence_refs=_evidence_refs(candidate.evidence),
            )
        )

    cycle_of_decision: dict[uuid.UUID, uuid.UUID] = {}
    decisions_by_cycle: dict[uuid.UUID, list[DecisionRecord]] = {}
    for decision in rows.decisions:
        if decision.cycle_id is None:
            continue
        cycle_of_decision[decision.id] = decision.cycle_id
        decisions_by_cycle.setdefault(decision.cycle_id, []).append(
            DecisionRecord(
                decision_id=str(decision.id),
                candidate_id=str(decision.candidate_id),
                decision=decision.decision,
                overridden_by=decision.overridden_by,
            )
        )

    outcomes_by_cycle: dict[uuid.UUID, list[TaskOutcome]] = {}
    for task in rows.tasks:
        cycle_id = cycle_of_decision.get(task.decision_id)
        if cycle_id is None:
            continue
        outcomes_by_cycle.setdefault(cycle_id, []).append(
            TaskOutcome(
                task_id=str(task.id),
                completed=task.status == TaskState.COMPLETED.value,
                repair_count=task.repair_count,
                # 缺陷数据尚不可得 —— 阶段一的库里没有缺陷表。**不是 0**：
                # "没查过"与"查了没有"是两件事。
                introduced_defects=None,
            )
        )

    samples: list[CycleSample] = []
    for cycle in rows.cycles:
        if cycle.ended_at is None:
            continue
        samples.append(
            CycleSample(
                cycle_id=cycle.cycle_id,
                closed_on=cycle.ended_at.date(),
                attainment_before=_decimal(cycle.metrics.get("attainment_before")),
                attainment_after=_decimal(cycle.metrics.get("attainment_after")),
                outcome=cycle.outcome or "",
                discoveries=tuple(discoveries_by_cycle.get(cycle.id, [])),
                decisions=tuple(decisions_by_cycle.get(cycle.id, [])),
                outcomes=tuple(outcomes_by_cycle.get(cycle.id, [])),
            )
        )

    verifications = tuple(
        VerificationSample(
            run_id=str(run.id),
            day=run.created_at.date(),
            result=run.result,
        )
        for run in rows.runs
    )
    costs = tuple(
        CostSample(
            day=entry.recorded_at.date(),
            amount=Decimal(entry.amount),
            currency=entry.currency,
        )
        for entry in rows.entries
    )
    return TrendInput(cycles=tuple(samples), verifications=verifications, costs=costs)


def _evidence_refs(evidence: list[dict[str, object]]) -> tuple[str, ...]:
    """候选的证据引用 —— 取每条证据的 ``ref``。

    **取不到 ``ref`` 的证据不算引用。** 证据结构由调用方决定，而没有
    ``ref`` 的证据指不到任何东西 —— 把它算成一条引用，重复发现率的分母
    就多了一个假的。
    """
    refs: list[str] = []
    for item in evidence:
        ref = item.get("ref")
        if isinstance(ref, str) and ref.strip():
            refs.append(ref)
    return tuple(refs)


def _decimal(value: object) -> Decimal | None:
    """从 JSON 里取一个十进制数 —— **取不到就是 ``None``，不是 0**。

    **布尔不是数。** ``bool`` 是 ``int`` 的子类，不挡一下的话 ``True`` 会被
    当成 1 记进去 —— 而"达成度是 1"和"达成度是 True"在报表上长得一样。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        return _parse_decimal(value)
    return None


def _parse_decimal(text: str) -> Decimal | None:
    """解析一个十进制字符串 —— **解析不了就是 ``None``**。"""
    try:
        return Decimal(text)
    except ArithmeticError:
        return None


__all__ = ["PostgresTrendReader"]
