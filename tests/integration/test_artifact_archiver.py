"""TASK-P1-048 —— 验收证据归档在真实 PostgreSQL 与真实 MinIO 上的行为。

单元测试（``tests/unit/knowledge/artifacts/``）用的是替身。替身证不了两件
事，而这两件事恰恰是这条路径上最容易写错的：

1. **事务边界。** 验收记录与产物写在**同一个事务**里 —— 库里的
   ``artifact.verification_run_id`` 是外键，而"这次验收算不算通过"只有在
   证据都落了盘之后才知道。分开写会留下"验收记录写着通过、产物只有两样"
   的中间态，而那种中间态在事后看与"证据丢了"完全一样。
2. **约束是真的。** ``artifact.hash`` 上的 CHECK（长度 64）、两张表的
   append-only 触发器、外键 —— 这些都不是替身能替的。

需要 PostgreSQL 与 MinIO。不可用时跳过（见 ``tests/integration/conftest.py``）。

两条硬断言（任务书 T1）：

- **T1 ①** 三类产物均可按 URI 取回且 hash 与登记值一致；
- **T1 ②** 归档失败的验证不计为通过（fail-closed）。
"""

from __future__ import annotations

import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.entities import Artifact as ArtifactRow
from core.entities import Project
from core.entities import VerificationRun as VerificationRunRow
from core.evidence.artifact_hash import compute_hash
from core.evidence.artifacts import ArtifactKind, StoredObject
from core.task.outcome import VerificationVerdict
from infrastructure.database.artifact import PostgresArtifactRegistry
from infrastructure.database.repositories import (
    DECISION_RECORD,
    CandidateRepository,
    DecisionRepository,
    TaskRepository,
)
from infrastructure.storage.object_store import ContentAddressedStore, S3ObjectStore
from knowledge.artifacts.archiver import (
    ArchiveRequest,
    EvidenceArchiver,
    EvidenceSource,
)
from knowledge.artifacts.collector import WorkspaceCollector

pytestmark = pytest.mark.integration

PROJECT_ID = "p-archive"


async def _seed_task(session: AsyncSession) -> str:
    """建一个项目与一个任务 —— ``verification_run.task_id`` 是外键。"""
    session.add(
        Project(
            id=PROJECT_ID,
            name="归档演示项目",
            goal={"objective": []},
            policy={},
            budget={},
            repository_url="https://example.invalid/repo.git",
        )
    )
    await session.flush()

    candidates = CandidateRepository(session)
    decisions = DecisionRepository(session)
    tasks = TaskRepository(session)

    candidate = await candidates.create(
        project_id=PROJECT_ID,
        category="quality",
        title="补一个测试",
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
    task = await tasks.create_from_decision(decision.id, title="补一个测试", task_type="bugfix")
    await session.commit()
    return str(task.id)


@pytest_asyncio.fixture
async def registry(engine: AsyncEngine) -> PostgresArtifactRegistry:
    return PostgresArtifactRegistry(async_sessionmaker(engine, expire_on_commit=False))


@pytest_asyncio.fixture
async def session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """另开一个会话去读库 —— 写库的那套会话可能还持有未提交的事务。"""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一个有一个提交、并且有一处未提交改动的仓库。"""
    repo = tmp_path / "workspace"
    repo.mkdir()
    for arguments in (
        ("init",),
        ("config", "user.email", "verifier@example.invalid"),
        ("config", "user.name", "verifier"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "基线"], cwd=repo, check=True, capture_output=True)
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "junit.xml").write_bytes(b"<testsuite tests='1'/>")
    return repo


def _request(
    task_id: str,
    workspace: Path,
    *,
    run_id: str | None = None,
    required: tuple[ArtifactKind, ...] | None = None,
    produced: frozenset[ArtifactKind] | None = None,
) -> ArchiveRequest:
    kinds = tuple(ArtifactKind) if required is None else required
    return ArchiveRequest(
        run_id=run_id or str(uuid.uuid4()),
        task_id=task_id,
        verdict=VerificationVerdict.PASSED,
        verified_by="verifier",
        checks=({"name": "unit_tests_pass", "passed": True},),
        source=EvidenceSource(
            workspace=workspace,
            traces=("unit_tests_pass: exit=0", "lint_pass: exit=0"),
        ),
        required=kinds,
        produced=produced if produced is not None else frozenset(kinds),
        project_id=PROJECT_ID,
    )


class _BrokenDigestStore:
    """把摘要换成非法值的存储。

    真存储算不出这种摘要 —— 它的 key 与摘要都由内容算出。这么做的目的
    是**触发数据库的约束**，从而验证事务边界（而不是验证"摘要算错了"）。
    """

    def __init__(self, inner: ContentAddressedStore) -> None:
        self._inner = inner

    async def put_bytes(self, data: bytes) -> StoredObject:
        stored = await self._inner.put_bytes(data)
        return StoredObject(
            key=stored.key,
            uri=stored.uri,
            digest="too-short",
            size_bytes=stored.size_bytes,
            skipped=stored.skipped,
        )

    async def get_bytes(self, key: str) -> bytes:
        return await self._inner.get_bytes(key)

    async def verify(self, key: str) -> bool:
        return await self._inner.verify(key)


class TestTheThreeArtifactsAreRetrievable:
    """T1 ① —— 三类产物均可按 URI 取回且 hash 与登记值一致。"""

    async def test_all_three_kinds_are_archived_and_retrievable(
        self,
        session: AsyncSession,
        registry: PostgresArtifactRegistry,
        s3_store: ContentAddressedStore,
        workspace: Path,
    ) -> None:
        task_id = await _seed_task(session)
        archiver = EvidenceArchiver(
            storage=s3_store,
            registry=registry,
            collector=WorkspaceCollector(test_report_path=Path("junit.xml")),
        )

        result = await archiver.archive(_request(task_id, workspace))

        assert result.verdict is VerificationVerdict.PASSED
        assert result.archived is True
        assert {record.type for record in result.artifacts} == set(ArtifactKind)

        for record in result.artifacts:
            data = await archiver.retrieve(record.uri)
            assert compute_hash(data) == record.hash, "取回的内容与登记的摘要对不上"

    async def test_the_rows_are_bound_to_the_verification_run(
        self,
        session: AsyncSession,
        registry: PostgresArtifactRegistry,
        s3_store: ContentAddressedStore,
        workspace: Path,
    ) -> None:
        task_id = await _seed_task(session)
        archiver = EvidenceArchiver(
            storage=s3_store,
            registry=registry,
            collector=WorkspaceCollector(test_report_path=Path("junit.xml")),
        )
        request = _request(task_id, workspace)

        result = await archiver.archive(request)

        rows = await registry.list_for(request.run_id)
        assert len(rows) == 3
        for row in rows:
            assert row.verification_run_id == request.run_id
            assert row.task_id == task_id
        assert len(result.artifacts) == 3

    async def test_the_run_row_records_the_combined_verdict(
        self,
        session: AsyncSession,
        registry: PostgresArtifactRegistry,
        s3_store: ContentAddressedStore,
        workspace: Path,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        task_id = await _seed_task(session)
        archiver = EvidenceArchiver(
            storage=s3_store,
            registry=registry,
            collector=WorkspaceCollector(test_report_path=Path("junit.xml")),
        )
        request = _request(task_id, workspace)

        await archiver.archive(request)

        async with session_maker() as reader:
            run = await reader.get(VerificationRunRow, uuid.UUID(request.run_id))
            assert run is not None
            assert run.result == "passed"
            assert run.verified_by == "verifier"
            assert len(run.checks) == 1

    async def test_the_artifact_rows_carry_uri_hash_and_type(
        self,
        session: AsyncSession,
        registry: PostgresArtifactRegistry,
        s3_store: ContentAddressedStore,
        workspace: Path,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        task_id = await _seed_task(session)
        archiver = EvidenceArchiver(
            storage=s3_store,
            registry=registry,
            collector=WorkspaceCollector(test_report_path=Path("junit.xml")),
        )

        result = await archiver.archive(_request(task_id, workspace))

        async with session_maker() as reader:
            rows = (await reader.scalars(select(ArtifactRow))).all()
            assert len(rows) == 3
            for row in rows:
                assert row.uri in {record.uri for record in result.artifacts}
                assert len(row.hash) == 64
                assert row.type in {kind.value for kind in ArtifactKind}
                assert row.project_id == PROJECT_ID

    async def test_archiving_twice_does_not_duplicate_the_rows(
        self,
        session: AsyncSession,
        registry: PostgresArtifactRegistry,
        s3_store: ContentAddressedStore,
        workspace: Path,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """重跑归档是常事 —— 同一份内容不该多出一行登记。"""
        task_id = await _seed_task(session)
        archiver = EvidenceArchiver(
            storage=s3_store,
            registry=registry,
            collector=WorkspaceCollector(test_report_path=Path("junit.xml")),
        )
        request = _request(task_id, workspace)

        await archiver.archive(request)
        await archiver.archive(request)

        async with session_maker() as reader:
            count = await reader.scalar(select(func.count()).select_from(ArtifactRow))
            assert count == 3


class TestFailedArchiveIsNotPassed:
    """T1 ② —— 归档失败的验证不计为通过（fail-closed）。"""

    async def test_missing_evidence_is_recorded_as_failed(
        self,
        session: AsyncSession,
        registry: PostgresArtifactRegistry,
        s3_store: ContentAddressedStore,
        workspace: Path,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """证据没齐 → 结论不是"通过"，**而且落库的结论也是它**。

        只在返回值里 fail-closed 不够：库里写着"通过"而证据没归档上，
        事后看就是"验收通过了却没有证据"。
        """
        task_id = await _seed_task(session)
        archiver = EvidenceArchiver(
            storage=s3_store,
            registry=registry,
            collector=WorkspaceCollector(test_report_path=Path("junit.xml")),
        )
        request = _request(task_id, workspace, produced=frozenset({ArtifactKind.GIT_DIFF}))

        result = await archiver.archive(request)

        assert result.passed is False
        assert result.verdict is VerificationVerdict.FAILED
        async with session_maker() as reader:
            run = await reader.get(VerificationRunRow, uuid.UUID(request.run_id))
            assert run is not None
            assert run.result == "failed"

    async def test_a_failed_registration_leaves_no_trace(
        self,
        session: AsyncSession,
        registry: PostgresArtifactRegistry,
        s3_store: ContentAddressedStore,
        workspace: Path,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """验收记录与产物**同生共死** —— 一个都没写进去。

        这里用一个坏摘要把数据库的 CHECK 约束（``char_length(hash) = 64``）
        顶开，让登记在写产物那一步失败。事务必须把验收记录一起回滚 ——
        否则库里就留下一条"验收记录写着通过、产物只有两样"的中间态。
        """
        task_id = await _seed_task(session)
        archiver = EvidenceArchiver(
            storage=_BrokenDigestStore(s3_store),
            registry=registry,
            collector=WorkspaceCollector(test_report_path=Path("junit.xml")),
        )
        request = _request(task_id, workspace)

        result = await archiver.archive(request)

        assert result.passed is False
        assert result.verdict is VerificationVerdict.ERROR
        async with session_maker() as reader:
            run_count = await reader.scalar(select(func.count()).select_from(VerificationRunRow))
            artifact_count = await reader.scalar(select(func.count()).select_from(ArtifactRow))
            assert run_count == 0, "验收记录被留下了 —— 事务没有一起回滚"
            assert artifact_count == 0


class TestAnUnknownTypeInTheDatabase:
    """库里混进别的东西时当场暴露。"""

    async def test_an_unrecognised_type_is_rejected(
        self,
        session: AsyncSession,
        registry: PostgresArtifactRegistry,
    ) -> None:
        """``artifact.type`` 是自由文本 —— 别的工具可能写过 ``log``。

        认不出就说明库里混进了别的东西，那件事该当场暴露，而不是被悄悄
        读成另一种（"这是测试报告" —— 而它其实是日志）。
        """
        task_id = await _seed_task(session)
        row = ArtifactRow(
            task_id=uuid.UUID(task_id),
            uri="s3://bucket/log.txt",
            hash="a" * 64,
            type="log",
        )
        session.add(row)
        await session.commit()
        artifact_id = str(row.id)

        with pytest.raises(ValueError):
            await registry.get(artifact_id)


class TestAgainstRealObjectStorage:
    """证据真的躺在对象存储里。"""

    async def test_the_content_is_in_the_bucket(
        self,
        session: AsyncSession,
        registry: PostgresArtifactRegistry,
        s3_backend: S3ObjectStore,
        workspace: Path,
    ) -> None:
        task_id = await _seed_task(session)
        archiver = EvidenceArchiver(
            storage=ContentAddressedStore(s3_backend),
            registry=registry,
            collector=WorkspaceCollector(test_report_path=Path("junit.xml")),
        )

        result = await archiver.archive(_request(task_id, workspace))

        keys = await s3_backend.list_keys()
        assert len(keys) == 3
        for record in result.artifacts:
            assert f"sha256:{record.hash}" in keys
