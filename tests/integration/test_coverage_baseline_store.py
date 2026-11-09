"""覆盖率基线落库 —— **库里那一行就是标准**。

设计依据：docs/tasks.md TASK-P1-042（"每模块基线存储"、"基线下调需审批"、
T2"基线下调次数 = 0"）。

**这里验的是"落库"这一段。** 口径在 ``tests/unit/core/test_coverage_baseline.py``，
审计翻译在 ``tests/unit/harness/security/test_baseline_audit.py`` —— 这一段只管
一件事：**存进去的基线，读回来还是那份基线**。

**读回来的是"当时的标准"，不是"现在算出来的标准"。** 所以这条用例里存完就
**把那份覆盖率报告删掉**，再读一次 —— 基线还在。如果读的时候会去重跑测试或者
重读报告，删掉之后读回来就会是"空的"或者"报错了"，而不是原来那个数。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import Project
from core.entities.project import CoverageBaseline
from core.task.coverage_baseline import (
    GLOBAL_COVERAGE_FLOOR,
    BaselineUpdateKind,
    BaselineUpdateRequest,
    CoverageBaselineService,
    CoverageBaselineSet,
    LoweringApproval,
    LoweringAttempt,
    ModuleBaseline,
)
from infrastructure.project.coverage_baselines import PostgresCoverageBaselineStore

pytestmark = pytest.mark.integration


class RecordingAuditSink:
    """把下调尝试记下来的审计口 —— **集成测试里只关心"记了没有"**。"""

    def __init__(self) -> None:
        self.attempts: list[LoweringAttempt] = []

    def record_lowering_attempt(self, attempt: LoweringAttempt) -> None:
        self.attempts.append(attempt)


async def _seed_project(session: AsyncSession, project_id: str) -> None:
    """建一个项目 —— **基线得挂在某个项目下面**。"""
    session.add(
        Project(
            id=project_id,
            name="覆盖率基线演示项目",
            goal={"objective": [{"id": "OBJ-1", "description": "把基线落下来"}]},
            policy={"merge_to_main": "approval_required"},
            budget={"currency": "USD"},
            repository_url="https://example.invalid/repo.git",
            tech_stack=["python"],
        )
    )
    await session.flush()


def _store(session: AsyncSession) -> PostgresCoverageBaselineStore:
    return PostgresCoverageBaselineStore(session)


async def test_a_baseline_survives_the_round_trip(session: AsyncSession) -> None:
    """存进去的基线，读回来还是那个数。"""
    await _seed_project(session, "proj-1")
    store = _store(session)

    await store.save(
        CoverageBaselineSet(
            project_id="proj-1",
            floor=Decimal("0.90"),
            modules={
                "tinylib.stats": ModuleBaseline(
                    module="tinylib.stats",
                    coverage=Decimal("0.912345"),
                    sample_count=3,
                    source="pytest:3",
                )
            },
            lowering_count=1,
        )
    )

    restored = await store.load("proj-1")
    assert restored.floor == Decimal("0.900000")
    baseline = restored.module_baseline("tinylib.stats")
    assert baseline is not None
    assert baseline.coverage == Decimal("0.912345")
    assert baseline.sample_count == 3
    assert baseline.source == "pytest:3"
    assert restored.lowering_count == 1


async def test_an_unknown_project_has_an_empty_baseline(session: AsyncSession) -> None:
    """没有基线是**一份空的集合**，不是 ``None`` —— 阈值照常算得出来。"""
    restored = await _store(session).load("proj-nobody")
    assert restored.floor == GLOBAL_COVERAGE_FLOOR
    assert restored.modules == {}
    assert restored.lowering_count == 0


async def test_saving_twice_updates_the_same_row(session: AsyncSession) -> None:
    """**一个项目一份基线** —— 存两次不该变成两行。"""
    await _seed_project(session, "proj-1")
    store = _store(session)

    await store.save(
        CoverageBaselineSet(
            project_id="proj-1",
            modules={"a": ModuleBaseline(module="a", coverage=Decimal("0.90"))},
        )
    )
    await store.save(
        CoverageBaselineSet(
            project_id="proj-1",
            modules={"a": ModuleBaseline(module="a", coverage=Decimal("0.95"))},
        )
    )

    restored = await store.load("proj-1")
    assert len(restored.modules) == 1
    baseline = restored.module_baseline("a")
    assert baseline is not None
    assert baseline.coverage == Decimal("0.950000")


async def test_the_threshold_survives_a_restart(session: AsyncSession) -> None:
    """**阈值是算出来的，但它得从库里的数算出来** —— 不是"现在测一遍"。

    这条用例里把那份覆盖率报告删掉：读回来时如果会去重跑测试，删掉之后
    就会读不出数，而基线应该纹丝不动。
    """
    await _seed_project(session, "proj-1")
    store = _store(session)
    await store.save(
        CoverageBaselineSet(
            project_id="proj-1",
            modules={
                "tinylib.stats": ModuleBaseline(module="tinylib.stats", coverage=Decimal("0.93"))
            },
        )
    )

    report = Path("/tmp/ases-nonexistent-coverage.json")
    assert not report.exists()

    service = CoverageBaselineService(store=store, audit=RecordingAuditSink())
    assert await service.threshold("proj-1", "tinylib.stats") == Decimal("0.930000")


async def test_the_service_writes_through_the_store(session: AsyncSession) -> None:
    """服务与存储要接得上 —— 改一次基线，库里就变了。"""
    await _seed_project(session, "proj-1")
    store = _store(session)
    service = CoverageBaselineService(store=store, audit=RecordingAuditSink())

    await service.update(
        BaselineUpdateRequest(
            project_id="proj-1",
            module="tinylib.stats",
            coverage=Decimal("0.92"),
            requested_by="agent-1",
            task_id="task-1",
        )
    )

    restored = await store.load("proj-1")
    baseline = restored.module_baseline("tinylib.stats")
    assert baseline is not None
    assert baseline.coverage == Decimal("0.920000")


async def test_a_refused_lowering_leaves_the_stored_baseline_alone(session: AsyncSession) -> None:
    """T1 ② —— 被拒不落地。"""
    await _seed_project(session, "proj-1")
    store = _store(session)
    audit = RecordingAuditSink()
    service = CoverageBaselineService(store=store, audit=audit)
    await service.update(
        BaselineUpdateRequest(
            project_id="proj-1",
            module="tinylib.stats",
            coverage=Decimal("0.92"),
            requested_by="agent-1",
        )
    )

    outcome = await service.update(
        BaselineUpdateRequest(
            project_id="proj-1",
            module="tinylib.stats",
            coverage=Decimal("0.80"),
            requested_by="agent-1",
        )
    )

    assert outcome.kind is BaselineUpdateKind.REFUSED
    assert len(audit.attempts) == 1
    restored = await store.load("proj-1")
    baseline = restored.module_baseline("tinylib.stats")
    assert baseline is not None
    assert baseline.coverage == Decimal("0.920000")
    assert restored.lowering_count == 0


async def test_an_approved_lowering_is_counted_in_the_stored_row(session: AsyncSession) -> None:
    """T2 —— 下调次数落在库里。"""
    await _seed_project(session, "proj-1")
    store = _store(session)
    service = CoverageBaselineService(store=store, audit=RecordingAuditSink())
    await service.update(
        BaselineUpdateRequest(
            project_id="proj-1",
            module="tinylib.stats",
            coverage=Decimal("0.92"),
            requested_by="agent-1",
        )
    )

    await service.update(
        BaselineUpdateRequest(
            project_id="proj-1",
            module="tinylib.stats",
            coverage=Decimal("0.80"),
            requested_by="agent-1",
            approval=LoweringApproval(
                project_id="proj-1",
                module="tinylib.stats",
                target_coverage=Decimal("0.80"),
                approver="human-1",
                approved_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
    )

    restored = await store.load("proj-1")
    assert restored.lowering_count == 1
    baseline = restored.module_baseline("tinylib.stats")
    assert baseline is not None
    assert baseline.coverage == Decimal("0.800000")


async def test_the_database_refuses_a_floor_below_the_global_one(session: AsyncSession) -> None:
    """**地板的下限由数据库兜住** —— 应用层的校验可以被绕开，约束不行。"""
    await _seed_project(session, "proj-1")
    session.add(
        CoverageBaseline(
            project_id="proj-1",
            floor=Decimal("0.10"),
            modules={},
            lowering_count=0,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_the_database_refuses_two_baselines_for_one_project(session: AsyncSession) -> None:
    """**一个项目一份标准** —— 两行就是两份标准。"""
    await _seed_project(session, "proj-1")
    session.add(
        CoverageBaseline(
            project_id="proj-1",
            floor=Decimal("0.90"),
            modules={},
            lowering_count=0,
        )
    )
    await session.flush()
    session.add(
        CoverageBaseline(
            project_id="proj-1",
            floor=Decimal("0.91"),
            modules={},
            lowering_count=0,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_a_baseline_may_not_outlive_its_project(session: AsyncSession) -> None:
    """标准得有人负责 —— 项目没了，标准不能变成悬空的一条。"""
    await _seed_project(session, "proj-1")
    store = _store(session)
    await store.save(CoverageBaselineSet(project_id="proj-1"))

    # 外键是 RESTRICT —— 违例在 DELETE 执行的那一刻就发生（不是 flush 时）。
    with pytest.raises(IntegrityError):
        await session.execute(delete(Project).where(Project.id == "proj-1"))
    await session.rollback()
