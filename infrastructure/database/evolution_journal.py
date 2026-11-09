"""循环 B 的落库实现 —— 把"这一轮做了什么"变成库里能查到的行。

设计依据：docs/tasks.md TASK-P1-061（T1 ① 全链路跑通并产出 ``EvolutionRoadmap``；
T1 ④ 循环 B 结束后项目知识被更新）、docs/design.md §3.6（三表分离）、
§3.5D（写不进去就炸出来）。

这一层是 :class:`core.evolution.evolution_loop.EvolutionJournalPort` 的实现。
**领域层定规则，基础设施层写库** —— 依赖方向永远是 ``infrastructure → core``。

    周期 ── 候选 ── 决策 ── 任务
      │       │       │
      │       │       └── 只从 record 决策派生（仓储层的闸门）
      │       └────────── 挂在周期上
      └────────────────── 计数按周期数，不按项目数

**三件这里必须做对的事。**

**一、领域 id 与行 id 是两回事，对应关系只有这一层知道。**
领域侧的标识是 ``"EVD-cycle-0007-002"`` 这种字符串，数据库侧的主键是 uuid。
两者要是对不上，任务就会挂到别的决策上 —— **而且不会报错**：外键查得到，
只是查到了另一行。所以每一次写入都把对应关系原样返回给编排层，不猜。

**二、写失败必须炸出来。** 记录写不进去还往下跑，等于这一轮没有发生过 ——
而"没有发生过"这件事恰恰是事后才被发现的。所以这里**不吞异常**，
也不做"尽力而为"的降级。

**三、每条记录都要能自己说明自己。** 候选带着证据、决策带着理由与依据、
任务带着路线图键与验收焦点 —— 事后单看一行就知道它在说什么。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from core.evolution.candidate import EvolutionCandidate
from core.evolution.decision import DecisionRound
from core.evolution.evolution_loop import EvolutionJournalPort
from core.evolution.priority import risk_coefficient
from core.metrics.cycle import CycleStatus
from core.planning.roadmap import EvolutionRoadmap
from infrastructure.database.repositories import (
    CandidateRepository,
    CycleRepository,
    DecisionRepository,
    SnapshotRepository,
    TaskRepository,
)


class JournalMappingError(RuntimeError):
    """领域 id 与行 id 对不上。

    **这个错误值得单独一类。** 它意味着"这次写入挂错了行" —— 而挂错的行不会
    报错，只会在事后表现为"任务挂到了别的决策上"或者"这一轮的计数不对"。
    与其让它潜伏到事后，不如当场炸出来。
    """


def _as_uuid(value: str, *, what: str) -> uuid.UUID:
    """把一行 id 字符串转成 uuid。

    **转不动就报错，不返回 ``None``。** 返回 ``None`` 的那一处调用方多半会
    写个"if row is not None"跳过 —— 而那正是挂错行的开始。
    """
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise JournalMappingError(f"{what}的行 id {value!r} 不是合法的 uuid") from error


def _evidence_payload(candidate: EvolutionCandidate) -> list[dict[str, object]]:
    """候选的证据 → JSON 列能收的形状。

    **三种字段都要带上。** ``kind`` 说这是什么形态的引用，``ref`` 指向具体
    的东西，``note`` 说明它说明了什么。少一个，下游取证据的那一端就少一样
    判断依据。
    """
    return [
        {"kind": item.kind.value, "ref": item.ref, "note": item.note} for item in candidate.evidence
    ]


class EvolutionJournal(EvolutionJournalPort):
    """循环 B 的落库口。

    **一个会话，一份事务。** 写入的顺序就是三表分离的顺序：周期 → 候选 →
    决策 → 任务；反过来写，外键会当场挡下来。

    **显式继承那个端口** —— 这样 mypy 会逐个方法核对签名。结构化实现
    （duck typing）下，端口少写一个方法，编译器不会吭声，只在运行时炸。
    """

    __slots__ = (
        "_candidates",
        "_cycles",
        "_decisions",
        "_session",
        "_snapshots",
        "_tasks",
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._cycles = CycleRepository(session)
        self._candidates = CandidateRepository(session)
        self._decisions = DecisionRepository(session)
        self._tasks = TaskRepository(session)
        self._snapshots = SnapshotRepository(session)

    async def open_cycle(self, *, project_id: str, cycle_id: str, trigger: str) -> str:
        """开一个周期，返回它的行 id。

        **周期重复开要报错。** 同一个 ``cycle_id`` 开两次，说明有两条流程在
        抢同一轮 —— 那时两边的候选与决策会混进同一个周期，而计数看起来仍然
        像个整数。
        """
        row = await self._cycles.open_cycle(
            project_id=project_id,
            cycle_id=cycle_id,
            trigger=trigger,
        )
        return str(row.id)

    async def record_candidates(
        self,
        cycle_ref: str,
        candidates: Sequence[EvolutionCandidate],
    ) -> Mapping[str, str]:
        """登记这一轮发现的候选，返回「候选领域 id → 候选行 id」。

        **项目 id 从周期行上取，不从调用参数拿。** 候选必须属于周期所属的
        项目 —— 从参数拿就得由调用方保证，而调用方手上只有周期 id；
        从周期行取则是**库里说了算**。
        """
        cycle_uuid = _as_uuid(cycle_ref, what="周期")
        cycle = await self._cycles.get(cycle_uuid)

        rows: dict[str, str] = {}
        for candidate in candidates:
            row = await self._candidates.create(
                project_id=cycle.project_id,
                category=candidate.category.value,
                title=candidate.title,
                evidence=_evidence_payload(candidate),
                hypothesis=candidate.hypothesis,
                expected_outcome=list(candidate.expected_outcome),
                # 价值此刻还**没被判断** —— 判断发生在评分那一步，
                # 结论记在决策的依据里。候选行记的是"发现了什么"，
                # 不是"它值多少"。
                value=Decimal(0),
                # 风险档换算成系数 —— 档是给人看的，系数才是能算的。
                risk=risk_coefficient(candidate.risk),
                confidence=candidate.confidence,
                estimated_effort=candidate.estimated_effort or None,
                cycle_id=cycle_uuid,
            )
            rows[candidate.id] = str(row.id)
        return rows

    async def record_decisions(
        self,
        cycle_ref: str,
        decisions: DecisionRound,
        candidate_rows: Mapping[str, str],
    ) -> Mapping[str, str]:
        """登记这一轮的决策，返回「决策领域 id → 决策行 id」。

        **候选必须已经登记过。** 决策挂在候选上（外键）；候选不在，
        这条决策挂不到任何东西上 —— 所以这里直接报错，而不是留一条孤儿
        决策。
        """
        cycle_uuid = _as_uuid(cycle_ref, what="周期")
        rows: dict[str, str] = {}
        for decision in decisions.decisions:
            candidate_row = candidate_rows.get(decision.candidate_id)
            if candidate_row is None:
                raise JournalMappingError(
                    f"决策 {decision.id!r} 指向候选 {decision.candidate_id!r}，而这个候选没有登记过"
                )
            row = await self._decisions.record(
                candidate_id=_as_uuid(candidate_row, what="候选"),
                decision=decision.decision.value,
                rationale=decision.rationale,
                requested_analysis=[
                    {
                        "kind": item.kind.value,
                        "description": item.description,
                        "rationale": item.rationale,
                    }
                    for item in decision.requested_analysis
                ],
                cycle_id=cycle_uuid,
            )
            rows[decision.id] = str(row.id)
        return rows

    async def land_tasks(
        self,
        roadmap: EvolutionRoadmap,
        decision_rows: Mapping[str, str],
    ) -> Mapping[str, str]:
        """把路线图落成任务行，返回「任务标识 → 任务行 id」。

        **走的是仓储层的唯一入口**（``land_roadmap``）—— 那里会再查一遍
        "决策是不是 record、是不是被推翻"。绕开它写一行任务，就绕开了全部
        三道准入。
        """
        tasks = await self._tasks.land_roadmap(
            roadmap,
            decision_rows={
                key: _as_uuid(value, what="决策") for key, value in decision_rows.items()
            },
        )
        # ``land_roadmap`` 按路线图里的次序返回任务 —— 一一对应回去。
        pairs = zip(roadmap.tasks, tasks, strict=True)
        return {planned.key: str(task.id) for planned, task in pairs}

    async def record_snapshot(
        self,
        *,
        project_id: str,
        version: int,
        commit: str,
        branch: str,
        metrics: Mapping[str, float],
    ) -> str:
        """落一份项目快照，返回它的行 id。

        **版本号由调用方给。** 仓储层自己去查"当前最大版本 + 1"，
        两次并发落库就会撞上唯一约束，报出来的却是"违反唯一约束" ——
        看不出是哪两件事撞了。
        """
        row = await self._snapshots.create(
            project_id=project_id,
            version=version,
            commit=commit,
            branch=branch or None,
            metrics={key: float(value) for key, value in metrics.items()},
        )
        return str(row.id)

    async def close_cycle(
        self,
        cycle_ref: str,
        *,
        status: CycleStatus,
        attainment_before: Decimal | None,
        attainment_after: Decimal | None,
        evidence_refs: Sequence[str],
    ) -> None:
        """结束这个周期。

        **结局由领域模型判定，不接受外部指定。** 仓储层会自己数一遍库里
        实际有多少候选 / 决策 / 任务 —— 周期记录里写"这一轮发现了 5 个
        候选"而库里躺着 3 个，是最容易被忽略也最伤信任的一种偏差。
        """
        cycle_uuid = _as_uuid(cycle_ref, what="周期")
        await self._cycles.close_cycle(
            cycle_uuid,
            status=status,
            attainment_before=attainment_before,
            attainment_after=attainment_after,
            evidence_refs=tuple(evidence_refs),
        )


__all__ = ["EvolutionJournal", "JournalMappingError"]
