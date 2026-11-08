"""产物登记的数据库实现。

设计依据：docs/tasks.md TASK-P1-048（登记 ``Artifact``，与 ``VerificationRun``
绑定）、TASK-P1-024（append-only、证据不可丢失）；docs/design.md §3.6。

实现 :class:`~core.evidence.artifacts.ArtifactRegistry` 端口。同源先例：
``core/policy/approval.py`` → ``infrastructure/database/approval.py``、
``core/policy/budget.py`` → ``infrastructure/database/budget.py``。

三处容易写错的地方，这里特意处理了：

1. **验收记录与产物写在同一个事务里。** ``artifact.verification_run_id``
   是外键，而"这次验收算不算通过"只有在证据都落了盘之后才知道。分开写
   会留下"验收记录写着通过、产物只有两样"的中间态 —— 那种中间态在事后
   看与"证据丢了"完全一样。事务把两种可能都排除了：要么都写进去，要么
   都没写。

2. **登记是幂等的。** 同一份内容（同一个 ``verification_run_id`` + 摘要 +
   类型）重复登记不会多出一行 —— 重跑归档是常事，而重复登记会把"有几份
   证据"这件事搅浑。已存在的验收记录也一样：同一个 ``run_id`` 再归档
   一遍不会多出一条验收记录。

3. **只 INSERT。** 两张表都被触发器禁掉了 UPDATE / DELETE
   （``ases_append_only_guard()``）。这里不写任何 UPDATE —— 不是"不写
   UPDATE 更好"，是写了会被数据库拒绝。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.entities.evidence import Artifact as ArtifactRow
from core.entities.evidence import VerificationRun as VerificationRunRow
from core.evidence.artifacts import (
    ArtifactKind,
    ArtifactRecord,
    VerificationRunRecord,
)


class PostgresArtifactRegistry:
    """产物登记的数据库实现。

    实现 :class:`~core.evidence.artifacts.ArtifactRegistry` 协议：跨进程可见，
    登记幂等，验收记录与产物同生共死。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_archive(
        self,
        run: VerificationRunRecord,
        artifacts: Sequence[ArtifactRecord],
    ) -> tuple[ArtifactRecord, ...]:
        """把一次验收与它的产物一起记下来。

        **已经登记过的部分不重复登记。** 验收记录按 ``run_id`` 判重，产物按
        （验收 + 摘要 + 类型）判重 —— 后者才是"同一份内容"真正的身份。
        """
        async with self._session_factory() as session, session.begin():
            existing_run = await session.get(VerificationRunRow, _to_uuid(run.run_id))
            if existing_run is None:
                session.add(
                    VerificationRunRow(
                        id=_to_uuid(run.run_id),
                        task_id=_to_uuid(run.task_id),
                        cycle_id=_to_uuid(run.cycle_id) if run.cycle_id else None,
                        checks=[dict(item) for item in run.checks],
                        result=run.result,
                        verified_by=run.verified_by,
                        duration_ms=run.duration_ms,
                    )
                )

            registered: list[ArtifactRecord] = []
            for record in artifacts:
                existing = await self._find_by_content(session, record)
                if existing is not None:
                    registered.append(_to_record(existing))
                    continue
                row = ArtifactRow(
                    id=_to_uuid(record.artifact_id),
                    task_id=_to_uuid(record.task_id),
                    verification_run_id=_to_uuid(record.verification_run_id),
                    # 项目 id 是**字符串 slug**（``my-product``），不是 UUID ——
                    # ``project.id`` 列就是 ``String(64)``。拿 _to_uuid 去解它
                    # 会把每个正常的项目 id 都判成非法。
                    project_id=record.project_id or None,
                    uri=record.uri,
                    hash=record.hash,
                    type=record.type.value,
                    size_bytes=record.size_bytes,
                )
                session.add(row)
                registered.append(record)
            await session.flush()
        return tuple(registered)

    async def get(self, artifact_id: str) -> ArtifactRecord | None:
        async with self._session_factory() as session:
            row = await session.get(ArtifactRow, _to_uuid(artifact_id))
            if row is None:
                return None
            return _to_record(row)

    async def get_by_uri(self, uri: str) -> ArtifactRecord | None:
        async with self._session_factory() as session:
            statement = select(ArtifactRow).where(ArtifactRow.uri == uri).limit(1)
            row = await session.scalar(statement)
            if row is None:
                return None
            return _to_record(row)

    async def list_for(self, verification_run_id: str) -> tuple[ArtifactRecord, ...]:
        """某次验收登记的全部产物。按登记时间返回。"""
        async with self._session_factory() as session:
            statement = (
                select(ArtifactRow)
                .where(ArtifactRow.verification_run_id == _to_uuid(verification_run_id))
                .order_by(ArtifactRow.created_at, ArtifactRow.id)
            )
            rows = (await session.scalars(statement)).all()
            return tuple(_to_record(row) for row in rows)

    async def _find_by_content(
        self,
        session: AsyncSession,
        record: ArtifactRecord,
    ) -> ArtifactRow | None:
        """按（验收 + 摘要 + 类型）找一份已登记的产物。"""
        statement = (
            select(ArtifactRow)
            .where(
                ArtifactRow.verification_run_id == _to_uuid(record.verification_run_id),
                ArtifactRow.hash == record.hash,
                ArtifactRow.type == record.type.value,
            )
            .limit(1)
        )
        return await session.scalar(statement)


def _to_record(row: ArtifactRow) -> ArtifactRecord:
    """数据库行 → 领域记录。

    ``type`` 认不出是哪一种时会抛错 —— 那一列是自由文本（别的工具可能写过
    ``log`` / ``screenshot``），而本系统的词表只有三种。认不出就说明库里
    混进了别的东西，那件事该当场暴露，而不是被悄悄读成另一种。
    """
    return ArtifactRecord(
        artifact_id=str(row.id),
        task_id=str(row.task_id) if row.task_id is not None else "",
        verification_run_id=str(row.verification_run_id)
        if row.verification_run_id is not None
        else "",
        uri=row.uri,
        hash=row.hash,
        type=ArtifactKind(row.type),
        size_bytes=row.size_bytes,
        project_id=str(row.project_id) if row.project_id is not None else None,
    )


def _to_uuid(value: str) -> uuid.UUID:
    """字符串 → UUID。

    外键列是 UUID 类型，传进去一个非 UUID 的字符串会在数据库层炸掉；提前
    在这里炸，错误信息能指名是哪个 id。
    """
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise ValueError(f"不是合法的 UUID：{value!r}") from error


__all__ = ["PostgresArtifactRegistry"]
