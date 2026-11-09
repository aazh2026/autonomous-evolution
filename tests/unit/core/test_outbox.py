"""TASK-P1-025 —— outbox 的领域契约（至少一次投递 + 幂等消费）。

**这里验的是规则本身，不依赖数据库。** 两组东西：relay 怎么对待一批事件
（发出去、失败怎么办、失败到头怎么办），以及消费者怎么对待重复投递
（只处理一次、失败时把占用还回去、过期占用能不能被抢走）。

**为什么"重复投递只处理一次"要有独立的一组用例。** 至少一次投递意味着
同一条消息**一定**会被投递不止一次 —— 那不是异常，是常态。所以幂等不是
"出错时的补救"，它是正常路径的一部分。

**为什么"占用能还回去"也要能被证伪。** 处理失败之后如果不还回去，那条
事件会被"已经处理过"这个标记永久挡住 —— 而它**还没被处理过**。那就是
静默丢消息，而丢消息这件事在事后是查不出来的。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from core.events.outbox import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_LEASE,
    DEFAULT_MAX_ATTEMPTS,
    EventMessage,
    IdempotentConsumer,
    OutboxRelay,
    RelayReport,
)

#: 固定时刻。**所有涉及时间的断言都用它** —— 真等五分钟不现实，而"把钟
#: 拨过去"正是租约到期这件事本身。
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class _FakeClock:
    """能往前拨的钟。"""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _message(event_id: str = "e-1", *, event_type: str = "task.completed") -> EventMessage:
    return EventMessage(
        event_id=event_id,
        aggregate_type="task",
        aggregate_id="TASK-1",
        event_type=event_type,
        created_at=NOW,
        payload={"task_id": "TASK-1"},
    )


class _RecordingPublisher:
    """把发出去的消息记下来。**也可以被安排成失败。**"""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.published: list[EventMessage] = []
        self._fail_on = fail_on

    async def publish(self, message: EventMessage) -> None:
        if message.event_id == self._fail_on:
            raise RuntimeError("下游不收")
        self.published.append(message)


class _FakeReader:
    """relay 侧的假存储。"""

    def __init__(self, messages: Sequence[EventMessage] = ()) -> None:
        self._messages = list(messages)
        self.published: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.requested_limit: int | None = None
        self.requested_max_attempts: int | None = None

    async def fetch_unpublished(self, *, limit: int, max_attempts: int) -> list[EventMessage]:
        self.requested_limit = limit
        self.requested_max_attempts = max_attempts
        return list(self._messages)

    async def mark_published(self, event_ids: Sequence[str], *, published_at: datetime) -> None:
        del published_at  # 假存储不关心发布时间
        self.published.extend(event_ids)

    async def mark_failed(self, event_id: str, *, error: str) -> None:
        self.failed.append((event_id, error))


class _FakeMark:
    """消费者侧的假标记。"""

    def __init__(self) -> None:
        self.claims: dict[tuple[str, str], datetime] = {}
        self.released: list[tuple[str, str]] = []

    async def claim(self, *, consumer: str, event_id: str, stale_before: datetime) -> bool:
        key = (consumer, event_id)
        held = self.claims.get(key)
        if held is not None and held > stale_before:
            return False
        self.claims[key] = NOW
        return True

    async def release(self, *, consumer: str, event_id: str) -> None:
        self.released.append((consumer, event_id))
        self.claims.pop((consumer, event_id), None)

    async def is_consumed(self, *, consumer: str, event_id: str) -> bool:
        return (consumer, event_id) in self.claims


class TestTheRelay:
    """relay 怎么对待一批事件。"""

    async def test_the_events_it_fetched_are_published(self) -> None:
        """取出来的就要发出去 —— 否则那批消息白取了。"""
        reader = _FakeReader([_message("e-1"), _message("e-2")])
        publisher = _RecordingPublisher()

        report = await OutboxRelay(reader, publisher).publish_once()

        assert [item.event_id for item in publisher.published] == ["e-1", "e-2"]
        assert report.published == 2

    async def test_the_published_ones_are_marked(self) -> None:
        """**发出去要留下痕迹** —— 不然下一轮会再发一遍（而没人知道它发过了）。"""
        reader = _FakeReader([_message("e-1")])

        await OutboxRelay(reader, _RecordingPublisher()).publish_once()

        assert reader.published == ["e-1"]

    async def test_a_failing_event_does_not_stop_the_batch(self) -> None:
        """**一条坏消息不该卡住整批。** 否则一条毒消息会让后面所有消息都排不上
        队 —— 而它自己永远排第一。"""
        reader = _FakeReader([_message("bad"), _message("good")])
        publisher = _RecordingPublisher(fail_on="bad")

        report = await OutboxRelay(reader, publisher).publish_once()

        assert [item.event_id for item in publisher.published] == ["good"]
        assert report.published == 1
        assert report.failed == 1

    async def test_the_failure_is_recorded_with_its_reason(self) -> None:
        """**坏消息留在表里，原因要写下来** —— 只说"没发出去"的话，运维得自己
        去猜。"""
        reader = _FakeReader([_message("bad")])

        await OutboxRelay(reader, _RecordingPublisher(fail_on="bad")).publish_once()

        assert len(reader.failed) == 1
        event_id, error = reader.failed[0]
        assert event_id == "bad"
        assert "下游不收" in error

    async def test_a_failed_event_is_not_marked_as_published(self) -> None:
        """**没发出去就不能说发出去了** —— 说错了，下一轮就不会再试，那条消息
        就永远丢了。"""
        reader = _FakeReader([_message("bad")])

        await OutboxRelay(reader, _RecordingPublisher(fail_on="bad")).publish_once()

        assert reader.published == []

    async def test_the_reason_never_ends_with_a_bare_colon(self) -> None:
        """``ValueError("")`` 的消息是空的 —— 存进去就成了"失败，原因："，
        而那等于没有原因。"""
        reader = _FakeReader([_message("bad")])

        class _Empty:
            async def publish(self, _message: EventMessage) -> None:
                raise ValueError()

        await OutboxRelay(reader, _Empty()).publish_once()

        _event_id, error = reader.failed[0]
        assert error.strip() != ""
        assert not error.rstrip().endswith(":")

    async def test_the_reader_is_asked_for_the_unpublished_ones(self) -> None:
        """**重试到头的事件不再取。** 无限重试意味着一条毒消息每一轮都被取
        出来、每一次都失败，把 relay 的额度全耗在它身上。"""
        reader = _FakeReader()

        await OutboxRelay(reader, _RecordingPublisher(), max_attempts=7).publish_once()

        assert reader.requested_max_attempts == 7
        assert reader.requested_limit == DEFAULT_BATCH_SIZE

    async def test_nothing_fetched_means_nothing_happened(self) -> None:
        """空批次不是错误 —— 队列空着是常态。"""
        reader = _FakeReader()

        report = await OutboxRelay(reader, _RecordingPublisher()).publish_once()

        assert report == RelayReport(fetched=0, published=0, failed=0)

    async def test_the_report_names_the_failed_ones(self) -> None:
        """**光有"失败了几条"不够** —— 运维要能一眼看出是哪几条没发出去。"""
        reader = _FakeReader([_message("bad")])

        report = await OutboxRelay(reader, _RecordingPublisher(fail_on="bad")).publish_once()

        assert report.failed_ids == ("bad",)
        assert report.ok is False

    async def test_the_default_limits_are_sane(self) -> None:
        """**默认值要说得出理由** —— 一个"5"看着像随手写的。"""
        assert DEFAULT_MAX_ATTEMPTS == 5
        assert DEFAULT_BATCH_SIZE == 100
        assert timedelta(minutes=5) == DEFAULT_LEASE


class TestTheIdempotentConsumer:
    """消费者怎么对待重复投递。"""

    async def test_the_first_delivery_is_handled(self) -> None:
        """第一次当然要处理。"""
        mark = _FakeMark()
        consumer = IdempotentConsumer(mark, consumer="evolution")
        handled: list[EventMessage] = []

        outcome = await consumer.consume(_message(), lambda message: _record(handled, message))

        assert outcome.handled is True
        assert len(handled) == 1

    async def test_the_second_delivery_is_skipped(self) -> None:
        """**同一条消息处理一次。** 这是幂等消费的全部意义 —— relay 是至少一次
        投递的，同一条消息**一定**会被投递不止一次。"""
        mark = _FakeMark()
        consumer = IdempotentConsumer(mark, consumer="evolution")
        handled: list[EventMessage] = []

        await consumer.consume(_message(), lambda message: _record(handled, message))
        outcome = await consumer.consume(_message(), lambda message: _record(handled, message))

        assert outcome.handled is False
        assert len(handled) == 1

    async def test_a_skipped_delivery_says_why(self) -> None:
        """**理由缺了，日志里就只剩一句"没处理"** —— 而那看起来像故障。"""
        mark = _FakeMark()
        consumer = IdempotentConsumer(mark, consumer="evolution")
        handled: list[EventMessage] = []

        await consumer.consume(_message(), lambda message: _record(handled, message))
        outcome = await consumer.consume(_message(), lambda message: _record(handled, message))

        assert outcome.reason.strip() != ""
        assert "重复" in outcome.reason

    async def test_different_consumers_each_handle_it(self) -> None:
        """**去重粒度是 (消费者, 事件)。** 两个订阅方都得收到那条消息 ——
        一个人吃掉了，另一个人就永远收不到。"""
        mark = _FakeMark()
        first = IdempotentConsumer(mark, consumer="evolution")
        second = IdempotentConsumer(mark, consumer="dashboard")
        handled: list[EventMessage] = []

        await first.consume(_message(), lambda message: _record(handled, message))
        await second.consume(_message(), lambda message: _record(handled, message))

        assert len(handled) == 2

    async def test_a_failing_handler_releases_the_claim(self) -> None:
        """**没处理过的事件不该被"已经处理过"永久挡住** —— 那等于静默丢消息。"""
        mark = _FakeMark()
        consumer = IdempotentConsumer(mark, consumer="evolution")

        with pytest.raises(RuntimeError):
            await consumer.consume(_message(), _boom)

        assert mark.released == [("evolution", "e-1")]

    async def test_a_failing_handler_is_reraised(self) -> None:
        """**别把错误吞掉。** 处理失败却报告"处理过了"，下一次重投还会被跳过
        —— 那条消息就永远没人处理，而系统看上去一切正常。"""
        mark = _FakeMark()
        consumer = IdempotentConsumer(mark, consumer="evolution")

        with pytest.raises(RuntimeError, match="处理炸了"):
            await consumer.consume(_message(), _boom)

    async def test_a_released_claim_allows_a_retry(self) -> None:
        """失败之后重投，**这次要真的被处理** —— 上一条断言的另一半。"""
        mark = _FakeMark()
        consumer = IdempotentConsumer(mark, consumer="evolution")
        handled: list[EventMessage] = []

        with pytest.raises(RuntimeError):
            await consumer.consume(_message(), _boom)
        outcome = await consumer.consume(_message(), lambda message: _record(handled, message))

        assert outcome.handled is True
        assert len(handled) == 1


class TestTheLease:
    """占用的租约。"""

    async def test_a_stale_claim_can_be_stolen(self) -> None:
        """**没有租约，一次崩溃就等于永久丢一条消息。** 占用过期了就要能被抢走
        —— 那个抢占了却没处理（进程被 kill 了）的人，不会再来处理它。"""
        clock = _FakeClock()
        mark = _FakeMark()
        consumer = IdempotentConsumer(
            mark, consumer="evolution", lease=timedelta(minutes=5), clock=clock
        )

        await consumer.consume(_message(), _noop)
        clock.advance(timedelta(minutes=10))
        outcome = await consumer.consume(_message(), _noop)

        assert outcome.handled is True

    async def test_a_fresh_claim_cannot_be_stolen(self) -> None:
        """**租约没到就不能抢** —— 否则两个消费者会同时处理同一条消息。"""
        clock = _FakeClock()
        mark = _FakeMark()
        consumer = IdempotentConsumer(
            mark, consumer="evolution", lease=timedelta(minutes=5), clock=clock
        )

        await consumer.consume(_message(), _noop)
        clock.advance(timedelta(minutes=1))
        outcome = await consumer.consume(_message(), _noop)

        assert outcome.handled is False


async def _record(sink: list[EventMessage], message: EventMessage) -> None:
    """把消息记进 ``sink``。"""
    sink.append(message)


async def _noop(_message: EventMessage) -> None:
    """什么都不做。"""


async def _boom(_message: EventMessage) -> None:
    """处理失败。"""
    raise RuntimeError("处理炸了")
