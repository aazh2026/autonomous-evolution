"""Outbox 事件表的落库口与 relay。

设计依据：docs/design.md §3.6（PostgreSQL 为项目状态库）、docs/tasks.md
TASK-P1-025（PostgreSQL Outbox 事件表与 relay）、I-06（状态写入与事件发布
**同事务**）、R-079（事件驱动）。

**这一层只做翻译。** 领域层的 :mod:`core.events.outbox` 是**端口** ——
它只知道"追加一条事件""取没发布过的""标记已发布""占一条消息"，不知道数据
库存在。规则（至少一次、幂等消费、重试到头就是死信）在领域层，写库的
**路径**在这里。

**三个类，三种事务边界 —— 这不是随意划的。**

:class:`PostgresEventOutbox`
    **加入调用方的事务。** 追加事件时只 ``flush``，不 ``commit`` ——
    提交与否由调用方决定。这样"状态改了但事件没写"与"事件写了但状态没改"
    才是不可能的（I-06）。

:class:`PostgresOutboxReader`
    **relay 自己的事务。** 它不能把别人正在开的事务拖下水 ——
    发布可能要几秒，而一个事务会一直占着连接。

:class:`PostgresEventConsumerMark`
    **消费者自己的事务。** 占用的可见性必须立刻生效 —— 另一个消费者要
    能马上看到"这条已经有人在处理了"。

**"发布成功"与"标记已发布"之间有一个窗口。** 消息发成功了但标记失败
（进程死在两步之间），那条事件会被再发一次 —— **这就是至少一次**，靠
消费者那边的幂等兜住（:class:`core.events.outbox.IdempotentConsumer`）。
所以 relay 不需要、也不该假装自己能保证"恰好一次"。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.entities.base import utcnow
from core.entities.outbox import ConsumedEvent, OutboxEvent
from core.events.outbox import EventMessage
from infrastructure.database.engine import session_scope


class OutboxWriteError(RuntimeError):
    """事件写不进去。

    **单独一个异常类型，是为了让它在调用方那里能被一眼认出来** ——
    而不是混在一堆数据库错误里。
    """


class PostgresEventOutbox:
    """在**调用方的事务里**追加事件。

    :param session: 调用方开着的会话。**不要在这里提交** ——
        提交与否是调用方的事（``session_scope``）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: Mapping[str, object],
    ) -> str:
        """追加一条待发布事件，返回它的 ``event_id``。

        **只 flush，不 commit。** 这一行与调用方的状态写在同一个事务里 ——
        提交或回滚由调用方决定（I-06）。

        **flush 是为了拿 id。** 主键在 INSERT 时才生成，而 ``event_id`` 是
        消费者的去重键，也是因果链的起点 —— 调用方拿不到它就没法串链。
        """
        row = OutboxEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=dict(payload),
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except Exception as error:  # 统一翻译成"写不进去"
            raise OutboxWriteError(f"事件写不进库：{error}") from error
        return str(row.id)


class PostgresOutboxReader:
    """relay 侧 —— 取未发布的事件、标记已发布、记录失败。

    :param session_factory: 会话工厂。**每次操作开自己的会话与事务** ——
        relay 不该待在别人的事务里。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def fetch_unpublished(self, *, limit: int, max_attempts: int) -> list[EventMessage]:
        """取还没发布的事件，按写入顺序。

        **失败到头的事件不再取。** 无限重试意味着一条毒消息每一轮都被取
        出来、每一次都失败，把 relay 的额度全耗在它身上 —— 而它永远排第
        一。它留在表里、``last_error`` 记着原因，那是死信，运维看得见。
        """
        async with session_scope(self._session_factory) as session:
            result = await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.attempt_count < max_attempts,
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(limit)
            )
            return [_to_message(row) for row in result.scalars().all()]

    async def mark_published(self, event_ids: Sequence[str], *, published_at: datetime) -> None:
        """标记这几条已经发布出去了。

        **标记的是"已经发出去了"，不是"对方处理了"。** 后者是消费者那边
        的事 —— 把两件事混成一件，就会出现"事件标成已发布，但消费者压根
        没收到"这种没法查的状态。
        """
        if not event_ids:
            return
        async with session_scope(self._session_factory) as session:
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id.in_([_as_uuid(value) for value in event_ids]))
                .values(published_at=published_at)
            )

    async def mark_failed(self, event_id: str, *, error: str) -> None:
        """记一次失败：次数加一，原因写下来。**事件仍未发布** —— 它还在
        队列里，等下一轮。
        """
        async with session_scope(self._session_factory) as session:
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == _as_uuid(event_id))
                .values(attempt_count=OutboxEvent.attempt_count + 1, last_error=error)
            )


class PostgresEventConsumerMark:
    """消费者侧的幂等标记。

    :param session_factory: 会话工厂。每次操作开自己的事务 ——
        占用的可见性必须立刻生效。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(self, *, consumer: str, event_id: str, stale_before: datetime) -> bool:
        """原子地占用这条事件。

        **这一步必须是原子的。** 分成"查有没有"加"写一行"两步的话，两个
        并发的消费者会各自看到"没人占着"然后各自占上 —— 同一条消息被处理
        两次，而**没有任何一个会报错**。所以这里用 ``ON CONFLICT``：
        插入成功、或者把过期的占用抢过来，都算抢到。

        **抢过期占用时会更新占用时间** —— 否则它会一直"看起来是过期的"，
        下一个人接着抢，抢成死循环。
        """
        now = utcnow()
        async with session_scope(self._session_factory) as session:
            result = await session.execute(
                insert(ConsumedEvent)
                .values(
                    id=uuid.uuid4(),
                    consumer=consumer,
                    event_id=_as_uuid(event_id),
                    consumed_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[ConsumedEvent.consumer, ConsumedEvent.event_id],
                    set_={"consumed_at": now},
                    where=ConsumedEvent.consumed_at <= stale_before,
                )
                .returning(ConsumedEvent.id)
            )
            return result.scalar_one_or_none() is not None

    async def release(self, *, consumer: str, event_id: str) -> None:
        """还回去 —— 处理失败时调用。

        **没处理过的事件不该被"已经处理过"这个标记永久挡住** ——
        那等于静默丢消息。
        """
        async with session_scope(self._session_factory) as session:
            await session.execute(
                delete(ConsumedEvent).where(
                    ConsumedEvent.consumer == consumer,
                    ConsumedEvent.event_id == _as_uuid(event_id),
                )
            )

    async def is_consumed(self, *, consumer: str, event_id: str) -> bool:
        """这条事件被这个消费者处理过没有。"""
        async with session_scope(self._session_factory) as session:
            result = await session.execute(
                select(ConsumedEvent.id).where(
                    ConsumedEvent.consumer == consumer,
                    ConsumedEvent.event_id == _as_uuid(event_id),
                )
            )
            return result.scalar_one_or_none() is not None


def _to_message(row: OutboxEvent) -> EventMessage:
    """ORM 行 → 领域消息。"""
    return EventMessage(
        event_id=str(row.id),
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_type=row.event_type,
        payload=row.payload,
        created_at=row.created_at,
        attempt_count=row.attempt_count,
    )


def _as_uuid(value: str) -> uuid.UUID:
    """把字符串当 UUID 解析 —— 解析不了就说清楚是哪一样东西解析不了。

    **为什么在这里报错而不是让数据库报错。** 传一个"不是 UUID"的 id 过去，
    数据库会报一个类型违例，而那条信息完全看不出是"事件 id 的格式不对"。
    """
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise OutboxWriteError(f"事件 id {value!r} 不是合法的 UUID") from error


__all__ = [
    "OutboxWriteError",
    "PostgresEventConsumerMark",
    "PostgresEventOutbox",
    "PostgresOutboxReader",
]
