"""Outbox 的领域契约 —— 至少一次投递 + 幂等消费。

设计依据：docs/tasks.md TASK-P1-025（T1 ① 状态变更与事件行在同一事务、
T1 ② 事务提交后事件必被 relay 取出、T1 ③ 消费者侧按 ``event_id`` 去重）、
I-06（状态写入与事件发布**同事务**）、R-079（事件驱动）、R-094（同一任务重复
提交幂等）。

**为什么是 outbox，而不是"写完状态直接发消息"。** 直接发消息有一个致命的
窗口：状态写成功了，消息发失败了 —— 进程正好在这一刻被杀、网络正好抖了
一下。于是状态变了，却没有任何人知道该做什么。**"该做的事没人知道"这件事
在事后是查不出来的**（状态是对的，日志也好看），所以它必须在结构上被挡住。

outbox 把"发消息"变成"写一行"：状态与事件行在**同一个事务**里落库，要么
都在，要么都不在。relay 再从表里把没发布过的事件取出来发出去。**它把"至少
一次"从调用方的自觉变成了系统的结构。**

**为什么是"至少一次"而不是"恰好一次"。** 恰好一次在分布式系统里做不到 ——
发布成功了但没标记成功（进程死在两步之间），那条事件会被再发一次。所以正确
的做法是**承认至少一次，然后把幂等放到消费者那边**（T1 ③）：同一条事件投递
两次，处理只发生一次。

**去重的粒度是 (消费者, event_id)。** 同一个消费者对同一条事件只处理一次；
不同消费者各处理一次 —— 两个订阅方都得收到那条消息。

**"重复投递"不是异常，是常态。** 所以去重必须便宜、必须幂等，而且**不能
把整个处理包在事务里** —— 处理可能要几秒，而一个事务会一直占着连接。

**占用要有租约。** 消费者先占住这条事件、处理完就留着；处理到一半崩了，
占用要能还回去 —— 否则那条事件永远不会被重投，而它**还没被处理过**。
还回去之后还剩一个洞：进程被 kill 了，还来不及还。所以判定"这个占用是不是
过期了"要看时间：过期了就可以被别人抢走。**没有租约，一次崩溃就等于永久
丢一条消息。**
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable

#: relay 一轮取多少条。**一批太大会让坏消息占着位置**，而坏消息是会被
#: 反复取出来的（重试机制）。
DEFAULT_BATCH_SIZE: Final[int] = 100

#: 一条事件最多试几次。**超过就不再试了** —— 无限重试意味着一条坏消息
#: 每一轮都被取出来、每一次都失败，把 relay 的额度全耗在它身上，而别的
#: 消息排不上队。重试到头的事件留在表里、``last_error`` 记着原因 —— 那是
#: 死信，运维能看见，而不是被悄悄丢掉。
DEFAULT_MAX_ATTEMPTS: Final[int] = 5

#: 消费占用的租约。**过期了就可以被别人抢走** —— 见模块说明。
DEFAULT_LEASE: Final[timedelta] = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class EventMessage:
    """一条待发布（或已发布）的消息。

    :attr:`event_id` 是**消费者的去重键** —— 它不能变。改了它，同一条事件
    就会被当成两条处理两次。
    """

    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    #: 写进库的时刻。**relay 按它排队** —— 先写的先发。
    created_at: datetime
    payload: Mapping[str, object] = field(default_factory=dict)
    #: 已经失败过几次。**不是成功的次数。**
    attempt_count: int = 0

    @property
    def key(self) -> tuple[str, str, str]:
        """事件的业务键 —— (种类, 主体, 类型)。

        **它不是去重键** —— 去重靠 ``event_id``。这个键是给人看的：
        "这条事件说的是哪个主体的什么事"。
        """
        return (self.aggregate_type, self.aggregate_id, self.event_type)


@runtime_checkable
class EventOutbox(Protocol):
    """在**调用方的事务里**追加一条待发布事件。

    **端口而不是具体实现。** 领域层不该知道数据库存在（依赖方向永远是
    ``infrastructure → core``）。

    **追加不许提交。** 这条端口的实现必须加入调用方正在开的事务 ——
    那样"状态改了但事件没写"与"事件写了但状态没改"才是**不可能**发生的
    两件事（I-06）。
    """

    async def append(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: Mapping[str, object],
    ) -> str:
        """追加一条事件，返回它的 ``event_id``。"""
        ...


@runtime_checkable
class OutboxReader(Protocol):
    """relay 侧的读写口。

    **与 :class:`EventOutbox` 分开，是因为两边的事务边界不同。** 追加要
    跟着调用方的状态写；取未发布的事件与标记发布则是 relay 自己的事务 ——
    它不能把别人的事务拖下水。
    """

    async def fetch_unpublished(self, *, limit: int, max_attempts: int) -> list[EventMessage]:
        """取还没发布的事件，按写入顺序。

        :param limit: 这一轮最多取几条。
        :param max_attempts: 失败过这么多次的事件**不再取** —— 见
            :data:`DEFAULT_MAX_ATTEMPTS`。
        """
        ...

    async def mark_published(self, event_ids: Sequence[str], *, published_at: datetime) -> None:
        """标记这几条已经发布出去了。"""
        ...

    async def mark_failed(self, event_id: str, *, error: str) -> None:
        """记一次失败：次数加一，原因写下来。**事件仍未发布** —— 它还在
        队列里，等下一轮。"""
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """把消息发出去。"""

    async def publish(self, message: EventMessage) -> None:
        """发布一条消息。**抛异常就是发布失败** —— relay 会记下来并重试。"""
        ...


@runtime_checkable
class EventConsumerMark(Protocol):
    """消费者侧的幂等标记。"""

    async def claim(self, *, consumer: str, event_id: str, stale_before: datetime) -> bool:
        """原子地占用这条事件。

        :param stale_before: 占用时间早于这个值的，可以被抢走（租约过期）。
        :return: 抢到了就返回 ``True``；别人正占着就返回 ``False``。

        **"抢到"必须是原子的。** 分成"查有没有"加"写一行"两步的话，两个
        并发的消费者会各自看到"没人占着"然后各自占上 —— 同一条消息被处理
        两次，而**没有任何一个会报错**。
        """
        ...

    async def release(self, *, consumer: str, event_id: str) -> None:
        """还回去 —— 处理失败时调用。**没处理过的事件不该被永久挡住。**"""
        ...

    async def is_consumed(self, *, consumer: str, event_id: str) -> bool:
        """这条事件被这个消费者处理过没有。"""
        ...


@dataclass(frozen=True, slots=True)
class RelayReport:
    """一轮 relay 的结果。

    :attr:`failed_ids` 是**失败的那些**。留着它们是因为"发出去几条"这个
    数字不够 —— 运维要能一眼看出是哪几条没发出去。
    """

    #: 取出来的条数。
    fetched: int = 0
    #: 发布成功的条数。
    published: int = 0
    #: 发布失败的条数。
    failed: int = 0
    #: 发布失败的 event_id。
    failed_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """这一轮有没有全部发出去。"""
        return self.failed == 0


class OutboxRelay:
    """轮询发布 outbox 里的事件。

    :param reader: 读未发布事件、标记发布/失败。
    :param publisher: 真正把消息发出去。
    :param batch_size: 一轮取多少条。
    :param max_attempts: 一条事件最多试几次。
    """

    __slots__ = ("_batch_size", "_clock", "_max_attempts", "_publisher", "_reader")

    def __init__(
        self,
        reader: OutboxReader,
        publisher: EventPublisher,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = reader
        self._publisher = publisher
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._clock = clock or (lambda: datetime.now(UTC))

    async def publish_once(self) -> RelayReport:
        """跑一轮：取一批、发出去、标记。

        **一条坏消息不该卡住整批。** 发布失败就记下原因、继续下一条 ——
        否则一条毒消息会让后面所有消息都排不上队（而它自己永远排第一）。

        **"发出去"与"标记已发布"之间有一个窗口。** 消息发成功了但标记失败
        （进程死在两步之间），下一轮会再发一次 —— **这就是至少一次**，靠
        消费者那边的幂等兜住。
        """
        messages = await self._reader.fetch_unpublished(
            limit=self._batch_size,
            max_attempts=self._max_attempts,
        )
        published = 0
        failed = 0
        failed_ids: list[str] = []
        for message in messages:
            try:
                await self._publisher.publish(message)
            except Exception as error:  # 一条坏消息不该卡住整批
                failed += 1
                failed_ids.append(message.event_id)
                await self._reader.mark_failed(message.event_id, error=_describe(error))
            else:
                published += 1
                await self._reader.mark_published([message.event_id], published_at=self._clock())
        return RelayReport(
            fetched=len(messages),
            published=published,
            failed=failed,
            failed_ids=tuple(failed_ids),
        )


@dataclass(frozen=True, slots=True)
class ConsumeOutcome:
    """一次消费的结果。

    :attr:`handled` 是"这次真的处理了没有"。**重复投递时是 ``False``** ——
    而那不是错误，是至少一次投递的正常结果。
    """

    handled: bool
    reason: str = ""


class IdempotentConsumer:
    """按 (消费者, ``event_id``) 去重的消费者。

    :param mark: 幂等标记的读写口。
    :param consumer: **消费方是谁**。不同的消费方各处理一次 —— 它是去重
        粒度的一半。
    :param lease: 占用多久算过期。**过期了可以被别人抢走** —— 一次崩溃不该
        等于永久丢一条消息。
    """

    __slots__ = ("_clock", "_consumer", "_lease", "_mark")

    def __init__(
        self,
        mark: EventConsumerMark,
        *,
        consumer: str,
        lease: timedelta = DEFAULT_LEASE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._mark = mark
        self._consumer = consumer
        self._lease = lease
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def consumer(self) -> str:
        """消费方是谁。"""
        return self._consumer

    async def consume(
        self,
        message: EventMessage,
        handler: Callable[[EventMessage], Awaitable[None]],
    ) -> ConsumeOutcome:
        """处理一条消息 —— **同一条只处理一次**。

        :param handler: 真正的处理逻辑。
        :return: 处理了还是被当成重复投递跳过了。

        **处理失败就把占用还回去。** 没处理过的事件不该被"已经处理过"这
        个标记永久挡住 —— 那等于静默丢消息。
        """
        now = self._clock()
        claimed = await self._mark.claim(
            consumer=self._consumer,
            event_id=message.event_id,
            stale_before=now - self._lease,
        )
        if not claimed:
            return ConsumeOutcome(handled=False, reason="重复投递 —— 这条事件已经处理过了")
        try:
            await handler(message)
        except Exception:
            await self._mark.release(consumer=self._consumer, event_id=message.event_id)
            raise
        return ConsumeOutcome(handled=True, reason="已处理")


def _describe(error: Exception) -> str:
    """把异常翻译成一句能存进库的话。

    **要带类型名。** 光有 ``str(error)`` 的话，``ValueError("")`` 存进去
    就是一个空字符串 —— 而"发布失败，原因："这种记录在排查时等于没有。
    """
    text = str(error).strip()
    name = type(error).__name__
    return f"{name}: {text}" if text else name


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_LEASE",
    "DEFAULT_MAX_ATTEMPTS",
    "ConsumeOutcome",
    "EventConsumerMark",
    "EventMessage",
    "EventOutbox",
    "EventPublisher",
    "IdempotentConsumer",
    "OutboxReader",
    "OutboxRelay",
    "RelayReport",
]
