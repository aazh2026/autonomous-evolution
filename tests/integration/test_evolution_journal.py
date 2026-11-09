"""TASK-P1-061 —— 循环 B 的落库口（``EvolutionJournal``）端到端验证。

**这里验的是"库里真的多了那些行"，不是"函数返回了对的东西"。**

理由很简单：这一层要解决的核心问题是**领域 id 与行 id 对不上**。领域侧的
标识是 ``"EVD-cycle-0007-002"`` 这种字符串，数据库侧的主键是 uuid。两者要是
挂错了，**不会报错** —— 外键查得到，只是查到了另一行。所以断言必须打在
**外键指向**上：任务挂在哪条决策上、决策挂在哪个候选上、候选挂在哪个周期上。

**为什么计数要数库里的行。** 周期记录里写"这一轮发现了 5 个候选"而库里躺着
3 个，是最容易被忽略也最伤信任的一种偏差 —— 记录看起来很完整，但它描述的
不是实际发生的事。所以计数由仓储层从库里数出来；这条测试要证明的正是
"数出来的确实是库里那些行"。

**为什么要试"挂错候选"与"挂错周期"。** 错误发生得最多的地方不是"忘了写"，
而是"写到了别的地方" —— 而那种错误在应用层完全看不出来。

**为什么重复开周期要报错。** 同一个 ``cycle_id`` 开两次，说明有两条流程在抢
同一轮 —— 那时两边的候选与决策会混进同一个周期，而计数看起来仍然像个整数。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import EvolutionCandidate as CandidateRow
from core.entities import EvolutionCycle as CycleRow
from core.entities import EvolutionDecision as DecisionRow
from core.entities import Project, ProjectSnapshot, Task
from core.evolution.candidate import EvolutionCandidate
from core.evolution.decision import (
    AnalysisKind,
    DecisionRound,
    DecisionState,
    EvolutionDecision,
    FollowUpAnalysis,
)
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.priority import PriorityFactors
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.metrics.cycle import CycleOutcome, CycleStatus
from core.planning.roadmap import (
    EvolutionRoadmap,
    PlannedTask,
    ScheduleSlot,
    TaskType,
)
from infrastructure.database.evolution_journal import EvolutionJournal, JournalMappingError
from infrastructure.database.repositories import DuplicateCycleError

pytestmark = pytest.mark.integration

_PROJECT_ID = "p-evolution-journal"
_CYCLE_ID = "cycle-0007"
_CANDIDATE_ID = "CAND-0001"


async def _seed_project(session: AsyncSession) -> None:
    """建一个项目 —— 候选、周期、快照都挂在它下面。"""
    project = Project(
        id=_PROJECT_ID,
        name="演进落库项目",
        goal={"objective": [{"id": "OBJ-1", "description": "把演进闭环跑通"}]},
        policy={"merge_to_main": "approval_required"},
        budget={"currency": "USD"},
        repository_url="https://example.invalid/journal.git",
    )
    session.add(project)
    await session.flush()


def _candidate() -> EvolutionCandidate:
    return EvolutionCandidate(
        id=_CANDIDATE_ID,
        category=OpportunityCategory.ENGINEERING,
        title="把落库口补齐",
        evidence=(Evidence(kind=EvidenceKind.ISSUE, ref="issue:ISSUE-1", note="落库口缺失"),),
        hypothesis="补上落库口之后，这一轮的产出就能被查到",
        expected_outcome=("这一轮的候选与决策能在库里查到",),
        risk=RiskLevel.LOW,
        confidence=Decimal("0.8"),
        estimated_effort="1d",
    )


def _factors() -> PriorityFactors:
    return PriorityFactors(
        value=Decimal("0.7"),
        impact=Decimal("0.6"),
        confidence=Decimal("0.8"),
        urgency=Decimal("0.4"),
        cost=Decimal("0.3"),
        risk=Decimal("0.2"),
    )


def _recorded_round() -> DecisionRound:
    """一轮只有一条 record 决策的决策轮。"""
    return DecisionRound(
        cycle_id=_CYCLE_ID,
        decisions=(
            EvolutionDecision(
                id="EVD-cycle-0007-001",
                candidate_id=_CANDIDATE_ID,
                decision=DecisionState.RECORD,
                rationale="证据充分，值得做",
                factors=_factors(),
            ),
        ),
    )


def _deferred_round() -> DecisionRound:
    """一轮只有一条 defer 决策的决策轮 —— defer 必须带补充分析项。"""
    return DecisionRound(
        cycle_id=_CYCLE_ID,
        decisions=(
            EvolutionDecision(
                id="EVD-cycle-0007-001",
                candidate_id=_CANDIDATE_ID,
                decision=DecisionState.DEFER,
                rationale="证据还不够，先补一次测量",
                requested_analysis=(
                    FollowUpAnalysis(
                        kind=AnalysisKind.MEASUREMENT,
                        description="对候选做一次可复现的测量",
                    ),
                ),
                factors=_factors(),
            ),
        ),
    )


def _roadmap(decision_id: str = "EVD-cycle-0007-001") -> EvolutionRoadmap:
    """一份只含一个任务的路线图。"""
    task = PlannedTask(
        key="T-0001",
        decision_id=decision_id,
        candidate_id=_CANDIDATE_ID,
        title="把落库口补齐",
        task_type=TaskType.FEATURE,
    )
    return EvolutionRoadmap(
        cycle_id=_CYCLE_ID,
        tasks=(task,),
        dependencies=(),
        schedule=(ScheduleSlot(position=1, key="T-0001", wave=1),),
    )


class TestTheWholeRoundTrip:
    """一轮完整的落库 —— 周期 → 候选 → 决策 → 任务 → 快照 → 结束周期。"""

    async def test_every_step_leaves_the_rows_it_promised(self, session: AsyncSession) -> None:
        """**每一行都要在库里。** 这是"这一轮发生过"的全部证据。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="quality",
        )
        candidate_rows = await journal.record_candidates(cycle_ref, (_candidate(),))
        decision_rows = await journal.record_decisions(
            cycle_ref,
            _recorded_round(),
            candidate_rows,
        )
        await journal.land_tasks(_roadmap(), decision_rows)
        snapshot_ref = await journal.record_snapshot(
            project_id=_PROJECT_ID,
            version=1,
            commit="c" * 40,
            branch="main",
            metrics={"coverage": 0.82},
        )
        await journal.close_cycle(
            cycle_ref,
            status=CycleStatus.COMPLETED,
            attainment_before=Decimal("0.6"),
            attainment_after=Decimal("0.7"),
            evidence_refs=("issue:ISSUE-1",),
        )
        await session.commit()

        cycle = await session.scalar(select(CycleRow).where(CycleRow.cycle_id == _CYCLE_ID))
        candidates = list(await session.scalars(select(CandidateRow)))
        decisions = list(await session.scalars(select(DecisionRow)))
        tasks = list(await session.scalars(select(Task)))
        snapshots = list(await session.scalars(select(ProjectSnapshot)))

        assert cycle is not None
        assert len(candidates) == 1
        assert len(decisions) == 1
        assert len(tasks) == 1
        assert len(snapshots) == 1
        assert snapshot_ref == str(snapshots[0].id)

    async def test_the_ids_are_mapped_not_guessed(self, session: AsyncSession) -> None:
        """**领域 id 与行 id 的对应关系要能查回来。** 返回的映射就是唯一
        知道对应关系的那一份 —— 它错了，后面全错，而且不报错。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )
        candidate_rows = await journal.record_candidates(cycle_ref, (_candidate(),))
        decision_rows = await journal.record_decisions(
            cycle_ref,
            _recorded_round(),
            candidate_rows,
        )
        task_rows = await journal.land_tasks(_roadmap(), decision_rows)

        assert set(candidate_rows) == {_CANDIDATE_ID}
        assert set(decision_rows) == {"EVD-cycle-0007-001"}
        assert set(task_rows) == {"T-0001"}
        for value in (*candidate_rows.values(), *decision_rows.values(), *task_rows.values()):
            uuid.UUID(value)

    async def test_the_task_hangs_off_the_decision(self, session: AsyncSession) -> None:
        """**三表分离的物理保证。** 任务必须挂在决策上 —— 没有决策来源的
        任务，就是"发现问题后直接改代码"。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )
        candidate_rows = await journal.record_candidates(cycle_ref, (_candidate(),))
        decision_rows = await journal.record_decisions(
            cycle_ref,
            _recorded_round(),
            candidate_rows,
        )
        task_rows = await journal.land_tasks(_roadmap(), decision_rows)
        await session.commit()

        task = await session.get(Task, uuid.UUID(task_rows["T-0001"]))

        assert task is not None
        assert str(task.decision_id) == decision_rows["EVD-cycle-0007-001"]
        assert task.project_id == _PROJECT_ID

    async def test_the_candidate_carries_its_evidence(self, session: AsyncSession) -> None:
        """**证据要跟着候选落库。** 事后查"这个候选凭什么被提出来"，靠的就是
        这一列。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )
        await journal.record_candidates(cycle_ref, (_candidate(),))
        await session.commit()

        row = await session.scalar(select(CandidateRow))

        assert row is not None
        assert row.evidence == [{"kind": "issue", "ref": "issue:ISSUE-1", "note": "落库口缺失"}]
        assert row.cycle_id is not None
        assert str(row.cycle_id) == cycle_ref

    async def test_the_candidate_takes_the_project_from_the_cycle(
        self, session: AsyncSession
    ) -> None:
        """**项目 id 从周期行上取。** 候选必须属于周期所属的项目 —— 从参数拿
        就得由调用方保证，而调用方手上只有周期 id。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )
        await journal.record_candidates(cycle_ref, (_candidate(),))
        await session.commit()

        row = await session.scalar(select(CandidateRow))

        assert row is not None
        assert row.project_id == _PROJECT_ID

    async def test_the_snapshot_is_reproducible(self, session: AsyncSession) -> None:
        """**给定快照能还原到当时的提交与当时的指标。** 少了任何一样，快照
        就只是一张无用的表。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        await journal.record_snapshot(
            project_id=_PROJECT_ID,
            version=7,
            commit="abc123",
            branch="main",
            metrics={"coverage": 0.82},
        )
        await session.commit()

        row = await session.scalar(select(ProjectSnapshot))

        assert row is not None
        assert row.version == 7
        assert row.commit == "abc123"
        assert row.branch == "main"
        assert row.metrics == {"coverage": 0.82}

    async def test_the_cycle_is_closed_with_a_judgement(self, session: AsyncSession) -> None:
        """**周期要结束，且结局是判定出来的。** 一个任务都没完成 → stalled。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )
        candidate_rows = await journal.record_candidates(cycle_ref, (_candidate(),))
        decision_rows = await journal.record_decisions(
            cycle_ref,
            _recorded_round(),
            candidate_rows,
        )
        await journal.land_tasks(_roadmap(), decision_rows)
        await journal.close_cycle(
            cycle_ref,
            status=CycleStatus.COMPLETED,
            attainment_before=Decimal("0.6"),
            attainment_after=Decimal("0.7"),
            evidence_refs=("issue:ISSUE-1",),
        )
        await session.commit()

        cycle = await session.scalar(select(CycleRow).where(CycleRow.cycle_id == _CYCLE_ID))

        assert cycle is not None
        assert cycle.status == CycleStatus.COMPLETED.value
        assert cycle.outcome == CycleOutcome.STALLED.value
        assert cycle.ended_at is not None

    async def test_the_counts_come_from_the_rows(self, session: AsyncSession) -> None:
        """**计数按周期数，不按项目数。** 记录里写"这一轮发现了 5 个候选"
        而库里躺着 3 个，是最容易被忽略也最伤信任的一种偏差。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )
        candidate_rows = await journal.record_candidates(cycle_ref, (_candidate(),))
        decision_rows = await journal.record_decisions(
            cycle_ref,
            _recorded_round(),
            candidate_rows,
        )
        await journal.land_tasks(_roadmap(), decision_rows)
        await journal.close_cycle(
            cycle_ref,
            status=CycleStatus.COMPLETED,
            attainment_before=None,
            attainment_after=None,
            evidence_refs=(),
        )
        await session.commit()

        cycle = await session.scalar(select(CycleRow).where(CycleRow.cycle_id == _CYCLE_ID))

        assert cycle is not None
        counts = cycle.to_domain().counts
        assert counts.candidates == 1
        assert counts.decisions == 1
        assert counts.tasks == 1
        assert counts.completed_tasks == 0


class TestTheJournalRefusesToGuess:
    """挂错行要当场炸出来 —— 事后才被发现就太晚了。"""

    async def test_a_decision_for_an_unregistered_candidate_is_refused(
        self, session: AsyncSession
    ) -> None:
        """**决策挂在候选上（外键）。** 候选不在，这条决策挂不到任何东西上 ——
        留一条孤儿决策，比报错更坏。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )

        with pytest.raises(JournalMappingError, match="没有登记过"):
            await journal.record_decisions(cycle_ref, _recorded_round(), {})

    async def test_a_cycle_ref_that_is_not_a_uuid_is_refused(self, session: AsyncSession) -> None:
        """**转不动就报错，不返回 ``None``。** 返回 ``None`` 的那一处调用方
        多半会写个"if row is not None"跳过 —— 而那正是挂错行的开始。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        with pytest.raises(JournalMappingError, match="不是合法的 uuid"):
            await journal.record_candidates("cycle-row-1", (_candidate(),))

    async def test_opening_the_same_cycle_twice_is_refused(self, session: AsyncSession) -> None:
        """**同一个 ``cycle_id`` 开两次，说明有两条流程在抢同一轮。**"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )

        with pytest.raises(DuplicateCycleError):
            await journal.open_cycle(
                project_id=_PROJECT_ID,
                cycle_id=_CYCLE_ID,
                trigger="schedule",
            )


class TestTheDeferredCycle:
    """一轮"决定什么都不做"也是一轮 —— 它也要留痕。"""

    async def test_a_defer_leaves_a_decision_but_no_task(self, session: AsyncSession) -> None:
        """**defer 不派生任务** —— 但决策一定要留下（R-035）。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )
        candidate_rows = await journal.record_candidates(cycle_ref, (_candidate(),))
        await journal.record_decisions(cycle_ref, _deferred_round(), candidate_rows)
        await journal.close_cycle(
            cycle_ref,
            status=CycleStatus.COMPLETED,
            attainment_before=None,
            attainment_after=None,
            evidence_refs=(),
        )
        await session.commit()

        decisions = list(await session.scalars(select(DecisionRow)))
        tasks = list(await session.scalars(select(Task)))

        assert len(decisions) == 1
        assert decisions[0].decision == DecisionState.DEFER.value
        assert decisions[0].requested_analysis
        assert tasks == []

    async def test_the_defer_keeps_its_requested_analysis(self, session: AsyncSession) -> None:
        """**defer 必须说清要补什么分析。** 只说"先缓缓"而不说补什么，
        下一轮就会原样再 defer 一遍。"""
        await _seed_project(session)
        journal = EvolutionJournal(session)

        cycle_ref = await journal.open_cycle(
            project_id=_PROJECT_ID,
            cycle_id=_CYCLE_ID,
            trigger="schedule",
        )
        candidate_rows = await journal.record_candidates(cycle_ref, (_candidate(),))
        await journal.record_decisions(cycle_ref, _deferred_round(), candidate_rows)
        await session.commit()

        decision = await session.scalar(select(DecisionRow))

        assert decision is not None
        assert decision.requested_analysis == [
            {
                "kind": "measurement",
                "description": "对候选做一次可复现的测量",
                "rationale": "",
            }
        ]
