"""步骤副作用账的数据库实现。

设计依据：docs/tasks.md TASK-P3-016（"在执行中强杀进程，重启后任务状态可恢复
且不重复执行已完成的副作用"）、R-095（中断后可恢复或重新执行）。

**这一层解决的是"进程死了之后，本子还在"。** 内存账本
（:class:`core.task.resume.InMemoryStepEffectLedger`）跟进程一起没了 ——
而断点恢复要处理的恰恰是"进程没了"这件事，所以账本必须活得比进程长。

**每次写都自己提交。** 这一点与 :class:`~infrastructure.database.budget.
PostgresBudgetLedger` 同一个理由，但结论相反：预算扣减要**先于**业务事务
生效（钱已经花了），而步骤账必须**活过**业务事务 —— 记完成的那一次提交，
就是"这一步做完了"这件事变成事实的那一刻。**不提交就没有断点**，重启之后
账本会说"这一步没做完"，而它其实做完了。

**"占位"靠唯一约束原子化。** ``(task_id, step)`` 上有唯一约束，所以两个执行
者同时占同一个步骤时，只有一个插得进去；另一个只能读到"别人正在做"。**这条
保证在数据库层** —— 绕过应用层的做法同样占不到位。

**"完成"先到先得。** 更新语句的 ``WHERE`` 里带着 ``status <> 'done'``，所以
迟到的完成记录改不动已经完成的那一条；而数据库层的守卫
（``ases_task_step_effect_guard``）连手工改库都堵住了。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.entities.resume import TaskStepEffect
from core.task.resume import (
    StepClaim,
    StepClaimResult,
    StepEffect,
    StepStatus,
    UnknownStepError,
)


class PostgresStepEffectLedger:
    """数据库步骤账。

    :param session_factory: 会话工厂。
    :param lease_seconds: 租约时长（秒）。**租约是"别人正在做"与"上个执行者
        死了"的分界** —— 租约过期了，这一步就只剩一个"开始了"的残骸，得去问
        外部世界到底做完没有。
    :param clock: 时钟。测试里用它拨租约。
    """

    __slots__ = ("_clock", "_lease_seconds", "_session_factory")

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        lease_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds < 0:
            raise ValueError(f"租约时长不能为负 —— 收到 {lease_seconds}")
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def claim(
        self,
        task_id: str,
        step: str,
        *,
        attempt: int,
        idempotency_key: str,
    ) -> StepClaimResult:
        """占这一步。

        **先试着插，插不进去再看现有那一条。** 插入用的是 ``ON CONFLICT DO
        NOTHING`` —— 并发的另一个执行者会在这里**阻塞到对方提交**，于是两个
        执行者不会同时占到同一步。
        """
        task_uuid = _to_uuid(task_id)
        now = self._clock()

        async with self._session_factory() as session, session.begin():
            inserted = await session.execute(
                insert(TaskStepEffect)
                .values(
                    id=uuid.uuid4(),
                    task_id=task_uuid,
                    step=step,
                    attempt=attempt,
                    status=StepStatus.STARTED.value,
                    idempotency_key=idempotency_key,
                    lease_until=now + timedelta(seconds=self._lease_seconds),
                    note="",
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[TaskStepEffect.task_id, TaskStepEffect.step]
                )
                .returning(TaskStepEffect.id)
            )
            if inserted.scalar_one_or_none() is not None:
                row = _require(await session.scalar(_one(task_uuid, step)), task_id, step)
                return StepClaimResult(
                    claim=StepClaim.ACQUIRED,
                    effect=_to_effect(row),
                    reason="第一次做这一步",
                )

            row = _require(
                await session.scalar(_one(task_uuid, step).with_for_update()), task_id, step
            )
            return await self._judge(session, row, attempt=attempt, idempotency_key=idempotency_key)

    async def _judge(
        self,
        session: AsyncSession,
        row: TaskStepEffect,
        *,
        attempt: int,
        idempotency_key: str,
    ) -> StepClaimResult:
        """现有那一条该怎么判。

        **四条分支，顺序不能换。** 先看是不是已经做完了（那就跳过），再看是不是
        明确失败了（那可以重跑），再看租约是不是还活着（那就别碰），最后才是
        "结果未知"。
        """
        now = self._clock()

        if row.status == StepStatus.DONE.value:
            return StepClaimResult(
                claim=StepClaim.ALREADY_DONE,
                effect=_to_effect(row),
                reason="这一步已经做完了 —— 跳过",
            )

        if row.status == StepStatus.FAILED.value:
            await self._rewrite(
                session,
                row.id,
                attempt=attempt,
                idempotency_key=idempotency_key,
            )
            fresh = _require(
                await session.scalar(_one(row.task_id, row.step)), str(row.task_id), row.step
            )
            return StepClaimResult(
                claim=StepClaim.ACQUIRED,
                effect=_to_effect(fresh),
                reason="上一次尝试明确失败了 —— 副作用没有生效，可以重跑",
            )

        if row.lease_until is not None and row.lease_until > now:
            return StepClaimResult(
                claim=StepClaim.IN_PROGRESS,
                effect=_to_effect(row),
                reason="别人正在跑这一步（租约还没到期）—— 别碰",
            )

        return StepClaimResult(
            claim=StepClaim.NEEDS_PROBE,
            effect=_to_effect(row),
            reason="上一个执行者不见了 —— 这一步到底做完没有，得问外部世界",
        )

    async def take_over(
        self,
        task_id: str,
        step: str,
        *,
        attempt: int,
        idempotency_key: str,
    ) -> StepClaimResult:
        """接管一个租约已过期、结果未知的步骤。"""
        task_uuid = _to_uuid(task_id)

        async with self._session_factory() as session, session.begin():
            row = _require(
                await session.scalar(_one(task_uuid, step).with_for_update()), task_id, step
            )
            if row.status == StepStatus.DONE.value:
                return StepClaimResult(
                    claim=StepClaim.ALREADY_DONE,
                    effect=_to_effect(row),
                    reason="这一步已经做完了 —— 跳过",
                )
            await self._rewrite(
                session,
                row.id,
                attempt=attempt,
                idempotency_key=idempotency_key,
            )
            fresh = _require(await session.scalar(_one(task_uuid, step)), task_id, step)
            return StepClaimResult(
                claim=StepClaim.ACQUIRED,
                effect=_to_effect(fresh),
                reason="接管了上一次的现场（探测确认副作用没生效）",
            )

    async def complete(self, task_id: str, step: str, *, attempt: int) -> StepEffect:
        """记完成。**先到先得。**"""
        task_uuid = _to_uuid(task_id)
        now = self._clock()

        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(TaskStepEffect)
                .where(
                    TaskStepEffect.task_id == task_uuid,
                    TaskStepEffect.step == step,
                    TaskStepEffect.status != StepStatus.DONE.value,
                )
                .values(
                    status=StepStatus.DONE.value,
                    attempt=attempt,
                    lease_until=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session="fetch")
            )
            row = await session.scalar(_one(task_uuid, step))
            if row is None:
                raise UnknownStepError(task_id, step)
            return _to_effect(row)

    async def fail(self, task_id: str, step: str, *, attempt: int, reason: str) -> StepEffect:
        """记失败。**不能 undo 已经完成的事实。**"""
        task_uuid = _to_uuid(task_id)
        now = self._clock()

        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(TaskStepEffect)
                .where(
                    TaskStepEffect.task_id == task_uuid,
                    TaskStepEffect.step == step,
                    TaskStepEffect.status != StepStatus.DONE.value,
                )
                .values(
                    status=StepStatus.FAILED.value,
                    attempt=attempt,
                    lease_until=None,
                    note=reason,
                    updated_at=now,
                )
                .execution_options(synchronize_session="fetch")
            )
            row = await session.scalar(_one(task_uuid, step))
            if row is None:
                raise UnknownStepError(task_id, step)
            return _to_effect(row)

    async def effects(self, task_id: str) -> tuple[StepEffect, ...]:
        """这个任务的全部步骤记录。"""
        task_uuid = _to_uuid(task_id)
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(TaskStepEffect)
                .where(TaskStepEffect.task_id == task_uuid)
                .order_by(TaskStepEffect.created_at, TaskStepEffect.step)
            )
            return tuple(_to_effect(row) for row in rows)

    async def _rewrite(
        self,
        session: AsyncSession,
        effect_id: uuid.UUID,
        *,
        attempt: int,
        idempotency_key: str,
    ) -> None:
        """把"开始了"那一条改写成新的样子。

        **租约跟着一起续。** 接管之后这一步就是"我在做"，租约必须重新算 ——
        否则刚接管的这一步会立刻被下一个执行者当成"上个执行者死了"。

        **备注一起清空。** 上一轮的失败原因对下一轮没有意义 —— 它描述的是上
        一轮的事，留着会让人以为这一步现在还是坏的。
        """
        now = self._clock()
        await session.execute(
            update(TaskStepEffect)
            .where(TaskStepEffect.id == effect_id)
            .values(
                status=StepStatus.STARTED.value,
                attempt=attempt,
                idempotency_key=idempotency_key,
                lease_until=now + timedelta(seconds=self._lease_seconds),
                note="",
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
        )


def _one(task_uuid: uuid.UUID, step: str) -> Select[TaskStepEffect]:
    """取那一条记录。"""
    return select(TaskStepEffect).where(
        TaskStepEffect.task_id == task_uuid,
        TaskStepEffect.step == step,
    )


def _require(row: TaskStepEffect | None, task_id: str, step: str) -> TaskStepEffect:
    """取不到那一行就报错。

    **"取不到"不是"没有副作用"。** 取不到说明流程乱了 —— 静默当成"没做过"
    就会重复执行副作用，那正是这一层要防的事。
    """
    if row is None:
        raise UnknownStepError(task_id, step)
    return row


def _to_uuid(task_id: str) -> uuid.UUID:
    """领域里的 ``task_id`` 是字符串，库里是 uuid。

    **转不动就报错，不猜。** 猜一个 id 出来，等于把这一步的账记到别人头上。
    """
    try:
        return uuid.UUID(task_id)
    except ValueError as exc:
        raise ValueError(f"task_id 必须是 uuid 字符串 —— 收到 {task_id!r}") from exc


def _to_effect(row: TaskStepEffect) -> StepEffect:
    """实体 → 领域值对象。"""
    return StepEffect(
        task_id=str(row.task_id),
        step=row.step,
        attempt=row.attempt,
        status=StepStatus(row.status),
        idempotency_key=row.idempotency_key,
        recorded_at=row.created_at,
        lease_until=row.lease_until,
        note=row.note,
    )


__all__ = ["PostgresStepEffectLedger"]
