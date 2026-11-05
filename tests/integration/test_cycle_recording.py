"""TASK-P1-070 —— 演进周期记录落库。

验证（T1 ①）：一轮周期结束后，cycle 记录完整，且各计数字段与**实际 DB 计数**
一致。

**为什么这条断言必须打到数据库上。** 计数是周期记录里最容易被编造的部分 ——
"这一轮发现了 5 个候选"这句话，写起来不需要任何依据。所以计数不由调用方
声明，而是由仓储层从库里数出来；这条测试要证明的正是"数出来的确实是库里
那些行"。

**口径必须是"按周期"，不是"按项目"。** 这两者的差别只有在一个项目有多个
周期时才显出来 —— 而那正是最需要这份记录准确的时刻。所以这里造**同一个
项目下的两个周期**，第二个周期的计数不能被第一个周期的东西污染；反过来，
第一个周期的计数也不能因为第二个周期后来才产生而变。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import EvolutionCycle, Project
from core.metrics.cycle import CycleCounts, CycleNotClosedError, CycleStatus
from core.metrics.cycle import EvolutionCycle as CycleRecord
from infrastructure.database.repositories import (
    DECISION_RECORD,
    CandidateRepository,
    CycleRepository,
    DecisionRepository,
    DuplicateCycleError,
    InvalidDecisionError,
    TaskRepository,
)

pytestmark = pytest.mark.integration

_PROJECT_ID = "p-cycle-recording"


@dataclass(frozen=True)
class _Work:
    """一个周期里要造出多少候选 / 决策 / 任务，其中多少个已完成。"""

    candidates: int = 0
    decisions: int = 0
    tasks: int = 0
    completed: int = 0


async def _seed_project(session: AsyncSession) -> Project:
    project = Project(
        id=_PROJECT_ID,
        name="周期记录项目",
        goal={"objective": [{"id": "OBJ-1", "description": "把演进闭环跑通"}]},
        policy={"merge_to_main": "approval_required"},
        budget={"currency": "USD"},
        repository_url="https://example.invalid/cycle.git",
    )
    session.add(project)
    await session.flush()
    return project


#: 默认的周期产出：什么都没有。
_NO_WORK = _Work()


async def _seed_work(
    session: AsyncSession,
    cycle_id: str,
    work: _Work = _NO_WORK,
) -> uuid.UUID:
    """在一个周期里造出候选 → 决策 → 任务这条链。

    任务没有 ``cycle_id`` 列 —— 它挂在决策下面，决策挂在周期下面。计数要
    穿过这条链路才能归到周期上，所以这里必须真的把三张表都填上，否则
    "计数与 DB 一致"就退化成"计数与空表一致"。
    """
    cycles = CycleRepository(session)
    row = await cycles.open_cycle(project_id=_PROJECT_ID, cycle_id=cycle_id, trigger="schedule")

    candidate_repo = CandidateRepository(session)
    decision_repo = DecisionRepository(session)
    task_repo = TaskRepository(session)

    made_candidates = []
    for index in range(work.candidates):
        made_candidates.append(
            await candidate_repo.create(
                project_id=_PROJECT_ID,
                category="quality",
                title=f"候选 {index}",
                evidence=[{"kind": "coverage_report", "ref": f"artifact://report-{index}"}],
                cycle_id=row.id,
            )
        )

    made_decisions = []
    for index in range(work.decisions):
        source_candidate = made_candidates[index % len(made_candidates)]
        made_decisions.append(
            await decision_repo.record(
                candidate_id=source_candidate.id,
                decision=DECISION_RECORD,
                rationale="证据充分",
                cycle_id=row.id,
            )
        )

    for index in range(work.tasks):
        source_decision = made_decisions[index % len(made_decisions)]
        task = await task_repo.create_from_decision(
            source_decision.id,
            title=f"任务 {index}",
            task_type="test",
        )
        if index < work.completed:
            # 任务进入执行态必须先有验收契约（R-051，数据库 CHECK 约束把关）。
            await task_repo.bind_acceptance_contract(
                task.id, {"task_id": str(task.id), "checks": ["unit_tests"]}
            )
            # 直接改状态：这条测试要的是"完成任务被计入"，不关心任务怎么
            # 走到 COMPLETED（状态机另有测试）。
            task.status = "COMPLETED"

    await session.flush()
    return row.id


class TestCountsMatchTheDatabase:
    """T1 ① —— 记录里的计数与库里实际的行数一致。"""

    async def test_counts_match_the_rows_that_were_actually_created(
        self, session: AsyncSession
    ) -> None:
        await _seed_project(session)
        cycle_uuid = await _seed_work(
            session,
            "cycle-0001",
            _Work(candidates=3, decisions=2, tasks=2, completed=1),
        )

        counts = await CycleRepository(session).counts(cycle_uuid)

        assert counts.candidates == 3
        assert counts.decisions == 2
        assert counts.tasks == 2
        assert counts.completed_tasks == 1

    async def test_the_record_stores_the_counts_it_counted(self, session: AsyncSession) -> None:
        """记录里写的数字必须就是刚数出来的那组数字。"""
        await _seed_project(session)
        cycle_uuid = await _seed_work(
            session,
            "cycle-0001",
            _Work(candidates=3, decisions=2, tasks=2, completed=1),
        )

        cycles = CycleRepository(session)
        counted = await cycles.counts(cycle_uuid)
        row = await cycles.close_cycle(
            cycle_uuid,
            status=CycleStatus.COMPLETED,
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
        )

        assert row.metrics["counts"] == counted.model_dump()
        assert row.metrics["counts"]["candidates"] == 3
        assert row.metrics["counts"]["completed_tasks"] == 1

    async def test_counts_are_scoped_to_the_cycle_not_the_project(
        self, session: AsyncSession
    ) -> None:
        """同一个项目下的两个周期，计数不能串。

        这是"按周期数"和"按项目数"的唯一分水岭，也是这类偏差最常见的藏身处：
        数字看上去仍然是个合理的整数，只是算错了对象。
        """
        await _seed_project(session)
        cycles = CycleRepository(session)

        first = await _seed_work(
            session, "cycle-0001", _Work(candidates=5, decisions=4, tasks=3, completed=2)
        )
        second = await _seed_work(
            session, "cycle-0002", _Work(candidates=1, decisions=1, tasks=1, completed=1)
        )

        first_counts = await cycles.counts(first)
        second_counts = await cycles.counts(second)

        assert first_counts == CycleCounts(candidates=5, decisions=4, tasks=3, completed_tasks=2)
        assert second_counts == CycleCounts(candidates=1, decisions=1, tasks=1, completed_tasks=1)

    async def test_closing_one_cycle_does_not_change_the_other(self, session: AsyncSession) -> None:
        """结束周期这个动作本身不能改动别的周期的计数。"""
        await _seed_project(session)
        cycles = CycleRepository(session)

        first = await _seed_work(session, "cycle-0001", _Work(candidates=2, decisions=1, tasks=1))
        second = await _seed_work(session, "cycle-0002", _Work(candidates=7, decisions=1, tasks=1))

        await cycles.close_cycle(first, status=CycleStatus.COMPLETED)

        assert await cycles.counts(second) == CycleCounts(
            candidates=7, decisions=1, tasks=1, completed_tasks=0
        )

    async def test_tasks_are_counted_through_the_decision_chain(
        self, session: AsyncSession
    ) -> None:
        """任务挂在决策下面 —— 计数要穿过决策才能归到周期上。

        如果这里直接按 ``task.project_id`` 数，就会把别的周期的任务也算进来。
        """
        await _seed_project(session)
        cycles = CycleRepository(session)
        first = await _seed_work(
            session, "cycle-0001", _Work(candidates=1, decisions=1, tasks=1, completed=1)
        )
        await _seed_work(
            session, "cycle-0002", _Work(candidates=1, decisions=1, tasks=1, completed=1)
        )

        counts = await cycles.counts(first)

        assert counts.tasks == 1
        assert counts.completed_tasks == 1


class TestClosingACycle:
    """结束周期 → 写出一份完整记录。"""

    async def test_close_writes_status_outcome_and_reason(self, session: AsyncSession) -> None:
        await _seed_project(session)
        cycle_uuid = await _seed_work(
            session, "cycle-0001", _Work(candidates=2, decisions=1, tasks=1, completed=1)
        )

        row = await CycleRepository(session).close_cycle(
            cycle_uuid,
            status=CycleStatus.COMPLETED,
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
        )

        assert row.status == "completed"
        assert row.outcome == "improved"
        assert row.ended_at is not None
        assert row.metrics["outcome_reason"]

    async def test_close_records_the_attainment_snapshots(self, session: AsyncSession) -> None:
        """T1 ③ —— 记录带前后两次达成度，差值算得出来。"""
        await _seed_project(session)
        cycle_uuid = await _seed_work(
            session, "cycle-0001", _Work(candidates=1, decisions=1, tasks=1, completed=1)
        )

        row = await CycleRepository(session).close_cycle(
            cycle_uuid,
            status=CycleStatus.COMPLETED,
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
        )

        record = row.to_domain()
        assert record.attainment_before == Decimal("0.40")
        assert record.attainment_after == Decimal("0.65")
        assert record.attainment_delta == Decimal("0.25")

    async def test_close_computes_the_outcome_from_the_evidence(
        self, session: AsyncSession
    ) -> None:
        """结局由判定得出 —— 落下什么就是什么，不接受声明。"""
        await _seed_project(session)
        cycle_uuid = await _seed_work(
            session, "cycle-0001", _Work(candidates=1, decisions=1, tasks=2, completed=0)
        )

        row = await CycleRepository(session).close_cycle(
            cycle_uuid,
            status=CycleStatus.COMPLETED,
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.90"),
        )

        # 达成度涨了，但一个任务都没完成 —— 这一轮没有产出。
        assert row.outcome == "stalled"

    async def test_a_budget_exhausted_cycle_is_stalled_not_improved(
        self, session: AsyncSession
    ) -> None:
        """被预算掐断的周期：跑完了，但没变好。"""
        await _seed_project(session)
        cycle_uuid = await _seed_work(
            session, "cycle-0001", _Work(candidates=1, decisions=1, tasks=1, completed=1)
        )

        row = await CycleRepository(session).close_cycle(
            cycle_uuid,
            status=CycleStatus.BUDGET_EXHAUSTED,
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.90"),
        )

        assert row.status == "budget_exhausted"
        assert row.outcome == "stalled"

    async def test_a_cycle_cannot_be_closed_twice(self, session: AsyncSession) -> None:
        """重复结束会覆盖第一次的结论 —— 必须挡住。"""
        await _seed_project(session)
        cycle_uuid = await _seed_work(session, "cycle-0001")
        cycles = CycleRepository(session)
        await cycles.close_cycle(cycle_uuid, status=CycleStatus.COMPLETED)

        with pytest.raises(InvalidDecisionError):
            await cycles.close_cycle(
                cycle_uuid, status=CycleStatus.COMPLETED, attainment_after=Decimal("0.9")
            )

    async def test_a_cycle_id_cannot_be_reused(self, session: AsyncSession) -> None:
        """周期编号全局唯一 —— 重复意味着两轮的记录混在一起。"""
        await _seed_project(session)
        await _seed_work(session, "cycle-0001")
        cycles = CycleRepository(session)

        with pytest.raises(DuplicateCycleError):
            await cycles.open_cycle(
                project_id=_PROJECT_ID, cycle_id="cycle-0001", trigger="schedule"
            )

    async def test_cost_is_stored_with_its_currency(self, session: AsyncSession) -> None:
        await _seed_project(session)
        cycle_uuid = await _seed_work(session, "cycle-0001")

        row = await CycleRepository(session).close_cycle(
            cycle_uuid,
            status=CycleStatus.COMPLETED,
            cost=Decimal("12.50"),
            currency="USD",
            evidence_refs=("artifact://report-1",),
        )

        record = row.to_domain()
        assert record.cost == Decimal("12.50")
        assert record.currency == "USD"
        assert record.evidence_refs == ("artifact://report-1",)

    async def test_an_open_cycle_cannot_be_restored_as_a_record(
        self, session: AsyncSession
    ) -> None:
        """进行中的周期没有结局 —— 还原成完整记录等于凭空造结论。"""
        await _seed_project(session)
        cycle_uuid = await _seed_work(session, "cycle-0001")

        row = await CycleRepository(session).get(cycle_uuid)

        with pytest.raises(CycleNotClosedError):
            row.to_domain()


class TestStoredRecordIsFaithful:
    """落库的那份必须和算出来的那份是同一件事。"""

    async def test_the_stored_row_roundtrips_into_the_same_record(
        self, session: AsyncSession
    ) -> None:
        await _seed_project(session)
        cycle_uuid = await _seed_work(
            session, "cycle-0001", _Work(candidates=2, decisions=1, tasks=1, completed=1)
        )

        row = await CycleRepository(session).close_cycle(
            cycle_uuid,
            status=CycleStatus.COMPLETED,
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
            cost=Decimal("3.25"),
            currency="USD",
            evidence_refs=("artifact://report-1", "issue:42"),
        )
        await session.commit()

        reloaded = await session.get(EvolutionCycle, cycle_uuid)

        assert reloaded is not None
        assert reloaded.to_domain() == row.to_domain()

    async def test_the_outcome_cannot_be_rewritten_in_the_database(
        self, session: AsyncSession
    ) -> None:
        """绕过应用层写一个自造的结局，数据库要挡住。"""
        await _seed_project(session)
        cycle_uuid = await _seed_work(
            session, "cycle-0001", _Work(candidates=1, decisions=1, tasks=1, completed=1)
        )
        await CycleRepository(session).close_cycle(
            cycle_uuid, status=CycleStatus.COMPLETED, attainment_after=Decimal("0.65")
        )
        await session.commit()

        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text("UPDATE evolution_cycle SET outcome = 'improved_but_really' WHERE id = :id"),
                {"id": cycle_uuid},
            )
        await session.rollback()

        assert "evolution_cycle_outcome_values" in str(exc_info.value)

    async def test_the_record_survives_a_project_with_more_cycles(
        self, session: AsyncSession
    ) -> None:
        """后面再开的周期不能把先前的记录改掉。"""
        await _seed_project(session)
        cycles = CycleRepository(session)

        first = await _seed_work(
            session, "cycle-0001", _Work(candidates=2, decisions=1, tasks=1, completed=1)
        )
        await cycles.close_cycle(
            first,
            status=CycleStatus.COMPLETED,
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
        )
        await session.commit()
        before = (await cycles.get(first)).to_domain()

        await _seed_work(session, "cycle-0002", _Work(candidates=9, decisions=1, tasks=1))
        await session.commit()

        after = (await cycles.get(first)).to_domain()
        assert after == before
        assert after.counts.candidates == 2


class TestCycleIdentity:
    """周期编号与触发来源。"""

    async def test_the_cycle_carries_its_trigger(self, session: AsyncSession) -> None:
        await _seed_project(session)
        cycle_uuid = await _seed_work(session, "cycle-0001")

        row = await CycleRepository(session).get(cycle_uuid)

        assert row.trigger == "schedule"
        assert row.cycle_id == "cycle-0001"
        assert row.project_id == _PROJECT_ID

    async def test_cycles_can_be_looked_up_by_project(self, session: AsyncSession) -> None:
        await _seed_project(session)
        await _seed_work(session, "cycle-0001")
        await _seed_work(session, "cycle-0002")

        result = await session.scalars(
            select(EvolutionCycle).where(EvolutionCycle.project_id == _PROJECT_ID)
        )

        assert len(list(result)) == 2


def test_cycle_started_and_ended_times_are_ordered() -> None:
    """纯逻辑断言：结束早于开始是非法的。"""
    start = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        CycleRecord.build(
            project_id=_PROJECT_ID,
            cycle_id="cycle-0001",
            trigger="schedule",
            started_at=start,
            ended_at=start - timedelta(seconds=1),
            status=CycleStatus.COMPLETED,
            counts=CycleCounts(),
        )
