"""TASK-P1-024 —— 验收证据的 append-only 与不可丢失。

验证（T1）：
① 对已写入的 VerificationRun 执行 UPDATE / DELETE 被**数据库**拒绝；
② 下一轮演进周期执行后，历史的 VerificationRun 与 Artifact 仍完整可读；
③ 产物被篡改时 hash 校验失败并报错。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import Artifact, EvolutionCycle, Project, VerificationRun
from core.evidence.artifact_hash import (
    ArtifactHashMismatchError,
    compute_hash,
    verify_hash,
)
from infrastructure.database.repositories import (
    DECISION_RECORD,
    CandidateRepository,
    DecisionRepository,
    TaskRepository,
)

pytestmark = pytest.mark.integration

_SHA256 = "a" * 64


async def _seed_task(session: AsyncSession, project_id: str = "p-evidence") -> object:
    project = Project(
        id=project_id,
        name="证据演示项目",
        goal={"objective": []},
        policy={},
        budget={},
        repository_url="https://example.invalid/repo.git",
    )
    session.add(project)
    await session.flush()

    candidates = CandidateRepository(session)
    decisions = DecisionRepository(session)
    tasks = TaskRepository(session)

    candidate = await candidates.create(
        project_id=project_id,
        category="quality",
        title="修一个缺陷",
        evidence=[{"kind": "defect", "ref": "issue://1"}],
        value=Decimal("0.5"),
        risk=Decimal("0.3"),
        confidence=Decimal("0.9"),
    )
    decision = await decisions.record(
        candidate_id=candidate.id,
        decision=DECISION_RECORD,
        rationale="确有其事",
    )
    task = await tasks.create_from_decision(decision.id, title="修一个缺陷", task_type="bugfix")
    await session.commit()
    return task


class TestAppendOnly:
    """① UPDATE / DELETE 被数据库拒绝。"""

    async def test_update_verification_run_is_rejected(self, session: AsyncSession) -> None:
        task = await _seed_task(session)
        run = VerificationRun(
            task_id=task.id,  # type: ignore[attr-defined]
            checks=[{"name": "unit_tests", "result": "passed"}],
            result="passed",
            verified_by="verifier",
        )
        session.add(run)
        await session.commit()

        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text("UPDATE verification_run SET result = 'failed' WHERE id = :id"),
                {"id": run.id},
            )
        await session.rollback()

        assert "append-only" in str(exc_info.value)

    async def test_delete_verification_run_is_rejected(self, session: AsyncSession) -> None:
        task = await _seed_task(session)
        run = VerificationRun(
            task_id=task.id,  # type: ignore[attr-defined]
            checks=[{"name": "unit_tests", "result": "passed"}],
            result="passed",
            verified_by="verifier",
        )
        session.add(run)
        await session.commit()

        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text("DELETE FROM verification_run WHERE id = :id"), {"id": run.id}
            )
        await session.rollback()

        assert "append-only" in str(exc_info.value)

    async def test_update_artifact_is_rejected(self, session: AsyncSession) -> None:
        task = await _seed_task(session)
        artifact = Artifact(
            task_id=task.id,  # type: ignore[attr-defined]
            project_id="p-evidence",
            uri="s3://bucket/report.json",
            hash=_SHA256,
            type="test_report",
        )
        session.add(artifact)
        await session.commit()

        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE artifact SET hash = :hash WHERE id = :id"),
                {"hash": "b" * 64, "id": artifact.id},
            )
        await session.rollback()

    async def test_delete_artifact_is_rejected(self, session: AsyncSession) -> None:
        task = await _seed_task(session)
        artifact = Artifact(
            task_id=task.id,  # type: ignore[attr-defined]
            project_id="p-evidence",
            uri="s3://bucket/report.json",
            hash=_SHA256,
            type="test_report",
        )
        session.add(artifact)
        await session.commit()

        with pytest.raises(DBAPIError):
            await session.execute(text("DELETE FROM artifact WHERE id = :id"), {"id": artifact.id})
        await session.rollback()

    async def test_truncate_verification_run_is_rejected(self, session: AsyncSession) -> None:
        with pytest.raises(DBAPIError):
            await session.execute(text("TRUNCATE TABLE verification_run"))
        await session.rollback()

    async def test_verification_run_is_still_readable_after_rejected_update(
        self, session: AsyncSession
    ) -> None:
        """被拒绝的修改没有留下任何痕迹。"""
        task = await _seed_task(session)
        run = VerificationRun(
            task_id=task.id,  # type: ignore[attr-defined]
            checks=[{"name": "unit_tests", "result": "passed"}],
            result="passed",
            verified_by="verifier",
        )
        session.add(run)
        await session.commit()
        # 回滚会让 ORM 对象过期，之后再访问属性会触发同步惰性加载。
        # 先把 id 取出来存成局部变量。
        run_id = run.id

        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE verification_run SET result = 'failed' WHERE id = :id"),
                {"id": run_id},
            )
        await session.rollback()

        loaded = await session.get(VerificationRun, run_id, populate_existing=True)
        assert loaded is not None
        assert loaded.result == "passed"


class TestCrossCycleDurability:
    """② 跨周期不丢失。"""

    async def test_history_survives_a_new_evolution_cycle(self, session: AsyncSession) -> None:
        task = await _seed_task(session)

        cycle_one = EvolutionCycle(
            project_id="p-evidence", cycle_id="cycle-0001", trigger="schedule", outcome="completed"
        )
        session.add(cycle_one)
        await session.flush()

        run_one = VerificationRun(
            task_id=task.id,  # type: ignore[attr-defined]
            cycle_id=cycle_one.id,
            checks=[{"name": "unit_tests", "result": "passed"}],
            result="passed",
            verified_by="verifier",
        )
        artifact_one = Artifact(
            task_id=task.id,  # type: ignore[attr-defined]
            verification_run_id=run_one.id,
            project_id="p-evidence",
            uri="s3://bucket/cycle-1/report.json",
            hash=_SHA256,
            type="test_report",
        )
        session.add_all([run_one, artifact_one])
        await session.commit()

        # --- 第二轮演进 ---
        cycle_two = EvolutionCycle(
            project_id="p-evidence", cycle_id="cycle-0002", trigger="commit", outcome="completed"
        )
        session.add(cycle_two)
        await session.flush()
        run_two = VerificationRun(
            task_id=task.id,  # type: ignore[attr-defined]
            cycle_id=cycle_two.id,
            checks=[{"name": "unit_tests", "result": "passed"}],
            result="passed",
            verified_by="verifier",
        )
        session.add(run_two)
        await session.commit()

        # 第一轮的证据仍然完整可读
        first = await session.get(VerificationRun, run_one.id)
        assert first is not None
        assert first.result == "passed"
        assert first.checks[0]["name"] == "unit_tests"

        stored_artifact = await session.get(Artifact, artifact_one.id)
        assert stored_artifact is not None
        assert stored_artifact.uri == "s3://bucket/cycle-1/report.json"
        assert stored_artifact.hash == _SHA256

        # 两轮证据都在
        runs = await session.scalars(select(VerificationRun))
        assert len(list(runs)) == 2


class TestArtifactHashVerification:
    """③ 产物被篡改时 hash 校验失败并报错。"""

    def test_tampered_content_is_rejected(self) -> None:
        original = b'{"result": "passed"}'
        expected = compute_hash(original)

        verify_hash(original, expected)  # 未被篡改时通过

        tampered = b'{"result": "failed"}'
        with pytest.raises(ArtifactHashMismatchError) as exc_info:
            verify_hash(tampered, expected, uri="s3://bucket/report.json")

        assert exc_info.value.expected == expected
        assert exc_info.value.actual == compute_hash(tampered)
        assert "s3://bucket/report.json" in str(exc_info.value)

    def test_hash_is_stable(self) -> None:
        content = b"evidence"
        assert compute_hash(content) == compute_hash(content)
        assert len(compute_hash(content)) == 64

    async def test_artifact_stored_hash_matches_content(self, session: AsyncSession) -> None:
        """落库的 hash 必须等于内容的真实 hash。"""
        task = await _seed_task(session)
        content = b'{"checks": 12}'
        digest = compute_hash(content)

        artifact = Artifact(
            task_id=task.id,  # type: ignore[attr-defined]
            project_id="p-evidence",
            uri="s3://bucket/report.json",
            hash=digest,
            type="test_report",
            retention_until=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(artifact)
        await session.commit()

        loaded = await session.get(Artifact, artifact.id)
        assert loaded is not None
        assert verify_hash(content, loaded.hash, uri=loaded.uri) == loaded.hash
