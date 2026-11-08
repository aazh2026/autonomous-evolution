"""审批门存储的数据库实现。

设计依据：docs/tasks.md TASK-P1-046（DB-backed 审批门）。

内存实现（``core/policy/approval.py`` 的 :class:`InMemoryApprovalStore`）只在本
进程内成立。审批门恰恰是**多进程**的地方：编排器等审批、API 让人批、CLI 让
人批，三者看到的必须是同一条记录。内存实现下，编排器看到"还没批"、审批服务
看到"已经批了"，两边都觉得自己是对的 —— 而这正是审批门最不能出的错。

所以生产路径用这张表。三处容易写错的地方，这里特意处理了：

1. **一条请求只能被表态一次。** 两个人同时点"批准"和"拒绝"时，后到的那次
   必须**失败**，而不是把先到的结论覆盖掉。做法是 ``SELECT … FOR UPDATE`` 锁
   住那一行，再检查它是不是还在 ``pending`` —— 锁在检查之前，否则两个事务都
   能读到 ``pending``，然后各写各的结论。

   **被拒的那次也要留下痕迹。** 覆盖掉前一个结论，就等于让后一次表态假装
   是第一次 —— 而人的意图是有先后的。

2. **过期在读取时算，不靠后台任务翻状态。** 一条 pending 的请求过期后，状态
   字段可能还写着 ``pending``；但结论必须立刻是"过期"。领域层按
   ``expires_at`` 现算（见 ``ApprovalRequest.outcome``），存储层在读的时候
   顺手把状态落成 ``expired`` —— 让"这张表现在长什么样"和"系统怎么看待它"
   保持一致。**不依赖有人记得跑那个定时任务。**

3. **自己管理事务。** 表态是一次不可分割的动作："检查还是 pending" 与
   "写入结论"必须在同一个事务里。分成两次调用，中间那次失败就留下一条既没
   批也没拒、却已经被人点过一次的记录。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.entities.approval import ApprovalRequest as ApprovalRequestRow
from core.policy.approval import (
    ApprovalAlreadyDecidedError,
    ApprovalError,
    ApprovalNotFoundError,
    ApprovalRequest,
    ApprovalStatus,
)


class PostgresApprovalStore:
    """数据库审批存储。

    实现 :class:`~core.policy.approval.ApprovalStore` 协议：跨进程可见，且
    一条请求只会被表态一次。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        """存下这条请求。

        **id 由领域层给** —— 领域层拿到的 ``request_id`` 就是这一行的身份，
        不做第二套编号。
        """
        row = ApprovalRequestRow(
            id=_to_uuid(request.request_id),
            task_id=request.task_id,
            operation=request.operation,
            diff_summary=request.diff_summary,
            evidence_ref=request.evidence_ref,
            status=request.status.value,
            requested_by=request.requested_by,
            requested_at=request.requested_at,
            decided_by=request.decided_by,
            decided_at=request.decided_at,
            expires_at=request.expires_at,
            comment=request.comment,
        )
        async with self._session_factory() as session, session.begin():
            session.add(row)
        return request

    async def get(self, request_id: str) -> ApprovalRequest:
        async with self._session_factory() as session:
            row = await session.get(ApprovalRequestRow, _to_uuid(request_id))
            if row is None:
                raise ApprovalNotFoundError(f"找不到审批请求 {request_id}")
            return _to_request(row)

    async def find(self, *, task_id: str, operation: str) -> ApprovalRequest | None:
        """这个任务在这个操作上**最近**的一条请求。

        "最近"按请求时间倒序取第一条 —— 一次拒绝之后又发起新的一次请求时，
        找出来的必须是新那一条。**只取一条**是有意的：一个任务同时有两张
        未决的审批单，说明流程出了岔子；把它们都算进来，就得回答"哪一条才
        算数"，而那个问题没有好答案。
        """
        statement = (
            select(ApprovalRequestRow)
            .where(
                ApprovalRequestRow.task_id == task_id,
                ApprovalRequestRow.operation == operation,
            )
            .order_by(ApprovalRequestRow.requested_at.desc(), ApprovalRequestRow.id.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
            if row is None:
                return None
            return await self._expire_if_needed(session, row)

    async def decide(
        self,
        *,
        request_id: str,
        status: ApprovalStatus,
        decided_by: str,
        decided_at: datetime,
        comment: str = "",
    ) -> ApprovalRequest:
        """给一条请求下结论。一条请求只会被表态一次。"""
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(ApprovalRequestRow)
                .where(ApprovalRequestRow.id == _to_uuid(request_id))
                .with_for_update()
            )
            if row is None:
                raise ApprovalNotFoundError(f"找不到审批请求 {request_id}")
            if row.status != ApprovalStatus.PENDING.value:
                raise ApprovalAlreadyDecidedError(
                    f"审批请求 {request_id} 已经是 {row.status}，不能再表态"
                )
            row.status = status.value
            row.decided_by = decided_by
            row.decided_at = decided_at
            row.comment = comment
            decided = _to_request(row)

        return decided

    async def list_for_task(self, task_id: str) -> tuple[ApprovalRequest, ...]:
        statement = (
            select(ApprovalRequestRow)
            .where(ApprovalRequestRow.task_id == task_id)
            .order_by(ApprovalRequestRow.requested_at, ApprovalRequestRow.id)
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(_to_request(row) for row in rows)

    async def _expire_if_needed(
        self, session: AsyncSession, row: ApprovalRequestRow
    ) -> ApprovalRequest:
        """过期的请求在读取时落成 ``expired``。

        **结论靠现算，状态靠顺手改。** 领域层的 ``ApprovalRequest.outcome()``
        按 ``expires_at`` 算出"过期"，所以**不依赖**这一行改没改；这一行改成
        ``expired`` 是为了让"表里现在长什么样"与"系统怎么看待它"一致 ——
        否则审批列表上会一直挂着一个永远 pending 的僵尸请求。
        """
        request = _to_request(row)
        if row.status != ApprovalStatus.PENDING.value:
            return request
        if request.expires_at is None or not request.is_expired(self._now()):
            return request
        row.status = ApprovalStatus.EXPIRED.value
        await session.commit()
        return _to_request(row)

    def _now(self) -> datetime:
        return self._clock()


def _to_uuid(request_id: str) -> uuid.UUID:
    """把领域层的字符串 id 转成 UUID。

    领域层用的是 ``str(uuid4())`` —— 它的好处是能直接进 URL 与命令行，而
    数据库这边仍按 UUID 存。转换失败说明传进来的根本不是一条审批请求的
    id，直接报错，不给"当成别的东西"的机会。
    """
    try:
        return uuid.UUID(request_id)
    except ValueError as error:
        raise ApprovalError(f"审批请求 id 不是合法的 UUID：{request_id}") from error


def _to_request(row: ApprovalRequestRow) -> ApprovalRequest:
    """ORM 行 → 领域对象。"""
    return ApprovalRequest(
        request_id=str(row.id),
        task_id=row.task_id,
        operation=row.operation,
        diff_summary=row.diff_summary,
        evidence_ref=row.evidence_ref,
        status=ApprovalStatus(row.status),
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
        comment=row.comment,
    )


__all__ = ["PostgresApprovalStore"]
