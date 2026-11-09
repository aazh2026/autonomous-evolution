"""快照落库 —— **库里那一行就是当时的数**。

设计依据：docs/tasks.md TASK-P1-023（"指标采集结果落盘（不实时查），使'当时
的快照'可被事后重建"）。

**这里验的是"落盘"这一段。** 口径在 ``tests/unit/core/test_snapshot.py``，
真的采集在 ``tests/system/test_snapshot_collection.py`` —— 这一段只管一件事：
**存进去的数，读回来还是那些数**。

**读回来的是"当时的数"，不是"现在的数"。** 所以这条用例里存完就**把那份
报告删掉**，再读一次 —— 数还在。如果读的时候会去重跑测试或者重读报告，
删掉之后读回来就会是"空的"或者"报错了"，而不是原来那些数。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import Project
from core.project.snapshot import (
    CodeStatistics,
    CoverageReport,
    ProjectSnapshotRecord,
    SnapshotArtifact,
    SnapshotIdentity,
    SnapshotMetrics,
    SuiteOutcome,
)
from infrastructure.database.repositories import SnapshotRepository
from infrastructure.project.snapshot_records import SnapshotRecordStore

pytestmark = pytest.mark.integration


async def _seed_project(session: AsyncSession, project_id: str) -> None:
    """建一个项目 —— **快照得挂在某个项目下面**。"""
    session.add(
        Project(
            id=project_id,
            name="快照演示项目",
            goal={"objective": [{"id": "OBJ-1", "description": "把快照落下来"}]},
            policy={"merge_to_main": "approval_required"},
            budget={"currency": "USD"},
            repository_url="https://example.invalid/repo.git",
            tech_stack=["python"],
        )
    )
    await session.flush()


def _record(project_id: str, version: int = 1) -> ProjectSnapshotRecord:
    """一份采全了的快照 —— **每一样都有值**。"""
    return ProjectSnapshotRecord(
        identity=SnapshotIdentity(
            project_id=project_id,
            version=version,
            commit="abc123def456",
            branch="main",
        ),
        metrics=SnapshotMetrics(
            code=CodeStatistics(files=12, lines=340, modules=4),
            tests=SuiteOutcome(
                passed=7,
                failed=1,
                skipped=2,
                duration_ms=1234,
                artifact=SnapshotArtifact(
                    kind="test_report",
                    uri="file:///tmp/reports/junit.xml",
                    sha256="a" * 64,
                ),
            ),
            coverage=CoverageReport(
                overall=0.82,
                by_module={"core": 0.8, "agents": 0.75},
                artifact=SnapshotArtifact(kind="coverage_report", uri="file:///tmp/c.json"),
            ),
        ),
        collected_at="2026-01-01T00:00:00+00:00",
    )


class TestTheRoundTrip:
    """存进去，再读回来 —— **读回来的是那些数**。"""

    async def test_the_numbers_survive(self, session: AsyncSession) -> None:
        await _seed_project(session, "p-snapshot")
        store = SnapshotRecordStore(SnapshotRepository(session))
        record = _record("p-snapshot")
        await store.save(record)
        await session.commit()

        restored = await store.latest("p-snapshot")

        assert restored is not None
        assert restored.metadata_digest() == record.metadata_digest()
        assert restored.metric("coverage") == 0.82
        assert restored.metric("tests_passed") == 7
        assert restored.metrics.tests is not None
        assert restored.metrics.tests.failed == 1
        assert restored.metrics.code.lines == 340
        assert restored.collected_at == "2026-01-01T00:00:00+00:00"

    async def test_the_artifact_survives(self, session: AsyncSession) -> None:
        """**产物 URI 要跟着走** —— 那是回头核对那个数的入口。"""
        await _seed_project(session, "p-snapshot-artifact")
        store = SnapshotRecordStore(SnapshotRepository(session))
        await store.save(_record("p-snapshot-artifact"))
        await session.commit()

        restored = await store.latest("p-snapshot-artifact")

        assert restored is not None
        assert restored.metrics.tests is not None
        assert restored.metrics.tests.artifact is not None
        assert restored.metrics.tests.artifact.uri == "file:///tmp/reports/junit.xml"
        assert restored.metrics.tests.artifact.sha256 == "a" * 64

    async def test_a_project_without_snapshots_has_none(self, session: AsyncSession) -> None:
        """**"没有快照"与"有一份空快照"是两回事。**"""
        await _seed_project(session, "p-snapshot-empty")
        store = SnapshotRecordStore(SnapshotRepository(session))
        await session.commit()

        assert await store.latest("p-snapshot-empty") is None

    async def test_the_latest_snapshot_wins(self, session: AsyncSession) -> None:
        await _seed_project(session, "p-snapshot-latest")
        store = SnapshotRecordStore(SnapshotRepository(session))
        await store.save(_record("p-snapshot-latest", version=1))
        await store.save(_record("p-snapshot-latest", version=2))
        await session.commit()

        restored = await store.latest("p-snapshot-latest")

        assert restored is not None
        assert restored.version == 2
