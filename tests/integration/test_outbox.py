"""TASK-P1-025 —— outbox 的落库与 relay（真 PostgreSQL）。

**这里验的是三件事，只有连库才验得了。**

- T1 ① 状态变更与事件行在**同一个事务**里：回滚之后两者都不存在。
- T1 ② 事务提交后事件**必被 relay 取出**（不丢）。
- T1 ③ relay 重复发布时，消费者侧按 ``event_id`` **只处理一次**。

**为什么"同事务"必须连库才验得了。** 在内存里，"改状态"与"写事件"是两次
普通的调用 —— 它们碰巧一起成功，很容易给人"已经同事务了"的错觉。而真正的
同事务意味着：**提交之前，别的会话看不见那行事件**；回滚之后，那行事件和
那次状态变更**都不存在**。这两条只有真库能证。

**为什么"重复发布"要真的模拟一遍。** 最容易想到的模拟是"跑两次 relay" ——
但 relay 第一次就把事件标成已发布了，第二轮根本取不到它。真正的重复发布
来自那个窗口：**发成功了，标记没写成**（进程死在两步之间）。所以这里要
刻意**不标记**，让第二轮把它再取出来。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.entities.outbox import ConsumedEvent, OutboxEvent
from core.entities.project import Project
from core.events.outbox import EventMessage, IdempotentConsumer, OutboxRelay
from infrastructure.database.outbox import (
    OutboxWriteError,
    PostgresEventConsumerMark,
    PostgresEventOutbox,
    PostgresOutboxReader,
)


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """会话工厂。relay 与消费者**各自开自己的事务** —— 它们不在调用方的事务
    里。"""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def _count_events(factory: async_sessionmaker[AsyncSession]) -> int:
    """外面看得见几行事件。"""
    async with factory() as session:
        result = await session.execute(select(func.count()).select_from(OutboxEvent))
        return int(result.scalar_one())


async def _count_projects(factory: async_sessionmaker[AsyncSession]) -> int:
    """外面看得见几个项目。"""
    async with factory() as session:
        result = await session.execute(select(func.count()).select_from(Project))
        return int(result.scalar_one())


class _RecordingPublisher:
    """把发出去的消息记下来。**也可以被安排成失败。**"""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[EventMessage] = []
        self._fail = fail

    async def publish(self, message: EventMessage) -> None:
        if self._fail:
            raise RuntimeError("下游不收")
        self.published.append(message)


def _project(project_id: str) -> Project:
    return Project(
        id=project_id,
        name="同事务演示",
        goal={"objective": [{"id": "OBJ-1", "description": "把状态与事件写在一起"}]},
        policy={"merge_to_main": "approval_required"},
        budget={"currency": "USD"},
        repository_url="https://example.invalid/repo.git",
        tech_stack=["python"],
    )


class TestTheSameTransaction:
    """T1 ① —— 状态变更与事件行在同一事务。"""

    async def test_a_rolled_back_transaction_leaves_neither_the_state_nor_the_event(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**回滚之后两者都不存在。** 只回滚一半是最坏的状态：状态改了而事件
        没写（没人知道该做什么），或者事件写了而状态没改（有人会去处理一件
        没发生过的事）。"""
        outbox = PostgresEventOutbox(session)
        session.add(_project("p-outbox-rollback"))
        await session.flush()
        await outbox.append(
            aggregate_type="project",
            aggregate_id="p-outbox-rollback",
            event_type="project.created",
            payload={"name": "同事务演示"},
        )
        await session.rollback()

        assert await _count_projects(factory) == 0
        assert await _count_events(factory) == 0

    async def test_a_committed_transaction_leaves_both(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**提交之后两者都在** —— 这是"同事务"另一半：不是"都没有"，而是
        "都有"。"""
        outbox = PostgresEventOutbox(session)
        session.add(_project("p-outbox-commit"))
        await session.flush()
        await outbox.append(
            aggregate_type="project",
            aggregate_id="p-outbox-commit",
            event_type="project.created",
            payload={"name": "同事务演示"},
        )
        await session.commit()

        assert await _count_projects(factory) == 1
        assert await _count_events(factory) == 1

    async def test_the_event_is_invisible_before_the_commit(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**提交之前，别的会话看不见那行事件。**

        **这条比"回滚后都不存在"更直接地证了"同一个事务"。** 如果事件行的
        写入跑在**另一个**事务里，那么在状态还没提交的时候，relay 就已经能
        把它取走 —— 于是"状态改了"与"事件被处理了"之间就有了时间差，
        而那个时间差里状态可能回滚。
        """
        outbox = PostgresEventOutbox(session)
        session.add(_project("p-outbox-invisible"))
        await session.flush()
        await outbox.append(
            aggregate_type="project",
            aggregate_id="p-outbox-invisible",
            event_type="project.created",
            payload={},
        )

        assert await _count_events(factory) == 0

        await session.commit()
        assert await _count_events(factory) == 1

    async def test_the_appended_event_carries_its_aggregate(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**事件要说得清"说的是哪个主体的什么事"** —— 否则消费者拿到一条
        事件都不知道该对谁做什么。"""
        outbox = PostgresEventOutbox(session)
        event_id = await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-9",
            event_type="task.completed",
            payload={"task_id": "TASK-9", "verdict": "passed"},
        )
        await session.commit()

        async with factory() as check:
            row = await check.get(OutboxEvent, event_id)
        assert row is not None
        assert row.aggregate_type == "task"
        assert row.aggregate_id == "TASK-9"
        assert row.event_type == "task.completed"
        assert row.payload["verdict"] == "passed"
        assert row.published_at is None


class TestTheRelay:
    """T1 ② —— 事务提交后事件必被 relay 取出（不丢）。"""

    async def test_the_relay_takes_the_committed_event(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**提交之后的事件不会被落下** —— relay 一定能把它取出来。"""
        outbox = PostgresEventOutbox(session)
        await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        publisher = _RecordingPublisher()
        report = await OutboxRelay(PostgresOutboxReader(factory), publisher).publish_once()

        assert report.published == 1
        assert len(publisher.published) == 1

    async def test_the_published_event_is_not_taken_again(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**发过的不再发。** 否则每轮都重发一遍 —— 而消费者那边的幂等只是
        兜底，不是常态。"""
        outbox = PostgresEventOutbox(session)
        await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        relay = OutboxRelay(PostgresOutboxReader(factory), _RecordingPublisher())
        await relay.publish_once()
        second = await relay.publish_once()

        assert second.published == 0

    async def test_a_failed_event_stays_in_the_queue(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**发不出去的事件不能被标成"发出去了"** —— 说错了，下一轮就不会
        再试，那条消息就永远丢了。"""
        outbox = PostgresEventOutbox(session)
        event_id = await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        relay = OutboxRelay(PostgresOutboxReader(factory), _RecordingPublisher(fail=True))
        report = await relay.publish_once()

        assert report.failed == 1
        async with factory() as check:
            row = await check.get(OutboxEvent, event_id)
        assert row is not None
        assert row.published_at is None
        assert row.attempt_count == 1
        assert row.last_error is not None
        assert "下游不收" in row.last_error

    async def test_the_failed_event_is_tried_again(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**失败不是终局** —— 下一轮还要试它。"""
        outbox = PostgresEventOutbox(session)
        await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        reader = PostgresOutboxReader(factory)
        await OutboxRelay(reader, _RecordingPublisher(fail=True)).publish_once()

        publisher = _RecordingPublisher()
        report = await OutboxRelay(reader, publisher).publish_once()

        assert report.published == 1
        assert len(publisher.published) == 1

    async def test_the_event_that_failed_too_many_times_is_left_alone(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**重试到头就不再试了。** 无限重试意味着一条毒消息每一轮都被取出来、
        每一次都失败，把 relay 的额度全耗在它身上 —— 而它永远排第一。"""
        outbox = PostgresEventOutbox(session)
        await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        reader = PostgresOutboxReader(factory)
        relay = OutboxRelay(reader, _RecordingPublisher(fail=True), max_attempts=3)
        for _ in range(5):
            await relay.publish_once()

        publisher = _RecordingPublisher()
        report = await OutboxRelay(reader, publisher, max_attempts=3).publish_once()

        assert report.published == 0
        assert publisher.published == []


class TestTheIdempotentConsumer:
    """T1 ③ —— relay 重复发布时，消费者只处理一次。"""

    async def test_a_redelivered_event_is_processed_once(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**同一条事件投递两次，处理只发生一次。**

        **重复发布是真的会发生的**：消息发出去了，"标记已发布"没写成（进程
        死在两步之间），下一轮就把它再取出来发一遍。这里刻意**不标记**，
        让它真的被取出来两次。
        """
        outbox = PostgresEventOutbox(session)
        await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        reader = PostgresOutboxReader(factory)
        consumer = IdempotentConsumer(PostgresEventConsumerMark(factory), consumer="evolution")
        handled: list[str] = []

        async def handle(message: EventMessage) -> None:
            handled.append(message.event_id)

        first = await reader.fetch_unpublished(limit=10, max_attempts=5)
        assert len(first) == 1
        await consumer.consume(first[0], handle)

        second = await reader.fetch_unpublished(limit=10, max_attempts=5)
        assert len(second) == 1, "没标记已发布 → 它会被再取出来发一遍"
        outcome = await consumer.consume(second[0], handle)

        assert outcome.handled is False
        assert len(handled) == 1

    async def test_two_consumers_each_process_it(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**去重粒度是 (消费者, 事件)。** 两个订阅方都得收到那条消息 ——
        一个人吃掉了，另一个人就永远收不到。"""
        outbox = PostgresEventOutbox(session)
        await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        reader = PostgresOutboxReader(factory)
        mark = PostgresEventConsumerMark(factory)
        messages = await reader.fetch_unpublished(limit=10, max_attempts=5)
        handled: list[str] = []

        async def handle(message: EventMessage) -> None:
            handled.append(message.event_id)

        await IdempotentConsumer(mark, consumer="evolution").consume(messages[0], handle)
        await IdempotentConsumer(mark, consumer="dashboard").consume(messages[0], handle)

        assert len(handled) == 2

    async def test_only_one_of_two_concurrent_claims_wins(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**占用必须是原子的。** 分成"查有没有"加"写一行"两步的话，两个
        并发的消费者会各自看到"没人占着"然后各自占上 —— 同一条消息被处理
        两次，而**没有任何一个会报错**。"""
        outbox = PostgresEventOutbox(session)
        event_id = await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        mark = PostgresEventConsumerMark(factory)
        stale_before = datetime.now(UTC) - timedelta(minutes=5)
        results = await asyncio.gather(
            *(
                mark.claim(consumer="evolution", event_id=event_id, stale_before=stale_before)
                for _ in range(5)
            )
        )

        assert sum(1 for granted in results if granted) == 1

    async def test_a_released_claim_can_be_claimed_again(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**处理失败之后占用要能还回去** —— 没处理过的事件不该被"已经处理过"
        这个标记永久挡住，那等于静默丢消息。"""
        outbox = PostgresEventOutbox(session)
        event_id = await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        mark = PostgresEventConsumerMark(factory)
        stale_before = datetime.now(UTC) - timedelta(minutes=5)
        assert await mark.claim(consumer="evolution", event_id=event_id, stale_before=stale_before)

        await mark.release(consumer="evolution", event_id=event_id)
        assert not await mark.is_consumed(consumer="evolution", event_id=event_id)
        assert await mark.claim(consumer="evolution", event_id=event_id, stale_before=stale_before)

    async def test_a_stale_claim_can_be_stolen(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**没有租约，一次崩溃就等于永久丢一条消息。** 占用了却一直没处理
        （进程被 kill 了），那个占用必须能被抢走。"""
        outbox = PostgresEventOutbox(session)
        event_id = await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        mark = PostgresEventConsumerMark(factory)
        now = datetime.now(UTC)
        assert await mark.claim(
            consumer="evolution", event_id=event_id, stale_before=now - timedelta(minutes=5)
        )

        # 租约已过（拿一个"很久以前"当过期线）→ 抢得到
        assert await mark.claim(
            consumer="evolution", event_id=event_id, stale_before=now + timedelta(minutes=10)
        )

    async def test_a_fresh_claim_cannot_be_stolen(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**租约没到就不能抢** —— 否则两个消费者会同时处理同一条消息。"""
        outbox = PostgresEventOutbox(session)
        event_id = await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        mark = PostgresEventConsumerMark(factory)
        now = datetime.now(UTC)
        assert await mark.claim(
            consumer="evolution", event_id=event_id, stale_before=now - timedelta(minutes=5)
        )
        assert not await mark.claim(
            consumer="evolution", event_id=event_id, stale_before=now - timedelta(minutes=5)
        )

    async def test_the_consumed_marker_is_kept_per_consumer(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**标记要能查出"这条被谁消费过"** —— 出事时得能回答"谁处理过它"。"""
        outbox = PostgresEventOutbox(session)
        event_id = await outbox.append(
            aggregate_type="task",
            aggregate_id="TASK-1",
            event_type="task.completed",
            payload={},
        )
        await session.commit()

        mark = PostgresEventConsumerMark(factory)
        stale_before = datetime.now(UTC) - timedelta(minutes=5)
        await mark.claim(consumer="evolution", event_id=event_id, stale_before=stale_before)

        async with factory() as check:
            rows = (
                await check.execute(
                    select(ConsumedEvent.consumer, ConsumedEvent.event_id).where(
                        ConsumedEvent.event_id == event_id
                    )
                )
            ).all()
        assert rows == [("evolution", uuid.UUID(event_id))]


class TestTheWriteErrors:
    """写不进去的时候，说清楚是"什么写不进去了"。"""

    async def test_a_row_that_does_not_fit_is_reported_as_a_write_error(
        self, session: AsyncSession
    ) -> None:
        """**写不进去要当场说，不要拖到提交时炸。**

        ``aggregate_type`` 那一列是 ``VARCHAR(64)`` —— 比它长的值数据库收不
        下。如果不把它翻译成 :class:`OutboxWriteError`，调用方看到的是一条
        数据库的类型违例，而那条信息完全看不出"是事件的主体种类太长"。
        """
        outbox = PostgresEventOutbox(session)

        with pytest.raises(OutboxWriteError) as caught:
            await outbox.append(
                aggregate_type="x" * 200,
                aggregate_id="TASK-1",
                event_type="task.completed",
                payload={},
            )

        assert "事件写不进库" in str(caught.value)

    async def test_an_event_id_that_is_not_a_uuid_is_rejected(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**"不是 UUID"要说出来。** 直接把字符串丢给数据库的话，报出来的是
        一个类型违例 —— 而排查的人根本看不出是"事件 id 的格式不对"。"""
        reader = PostgresOutboxReader(factory)

        with pytest.raises(OutboxWriteError) as caught:
            await reader.mark_published(["不是-uuid"], published_at=datetime.now(UTC))

        assert "不是合法的 UUID" in str(caught.value)
