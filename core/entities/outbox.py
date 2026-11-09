"""Outbox 事件表与消费标记表。

设计依据：docs/design.md §3.6（PostgreSQL 为项目状态库）、docs/tasks.md
TASK-P1-025（PostgreSQL Outbox 事件表与 relay）、I-06（状态写入与事件发布
**同事务**）、R-079（事件驱动）。

**这两张表是"同事务"三个字的落点。** 没有 outbox 时，"改状态"与"发消息"
是两个独立动作，中间有一个杀进程的窗口：状态写成功了，消息没发出去 ——
于是系统认为该做的事 nobody 知道要做。outbox 把"发消息"变成"写一行"：
状态与事件行在**同一个事务**里落库，要么都在，要么都不在；relay 再从表里
把没发布过的事件取出来发出去。**它把"至少一次"从调用方的自觉变成了系统的
结构。**

两张表分工明确：

``outbox_event``
    待发布的事件。**发布之前它只是一行记录** —— 事件在这一刻还没有真的
    到达任何人。``published_at`` 为空就是"还没发出去"。

``consumed_event``
    某个消费者已经消费过某条事件。**这是幂等消费的唯一凭据**：relay 是
    至少一次投递的，同一条事件会被投递不止一次，而处理只该发生一次。

**为什么不建外键。** ``aggregate_id`` 指向的东西是**多态的** —— 它可能是
项目、任务、演进周期、审批请求，每种事件的主体不一样。建一个外键就得
挑一个父表，而那个选择对另外一半事件是错的。更实际的理由是：**事件活得比
它的主体久** —— 主体被删了（或者压根没进这个库），那条"它发生过"的事实
仍然成立。所以这里只记字符串，不建外键。

**消费标记要能被"释放"。** 消费者占了这条事件、处理到一半崩了 —— 那一行
不该永远挡着这条事件的重投。所以 ``consumed_event`` **不是 append-only**：
它是一次**占用**，占用失败要能还回去。（证据表才是 append-only 的 ——
那是另一种东西，见 ``verification_run`` 与 ``artifact``。）
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from core.entities.base import Base, JSONDocument, UUIDPrimaryKeyMixin, utcnow


class OutboxEvent(UUIDPrimaryKeyMixin, Base):
    """一条待发布的事件。

    **主键就是消费者的去重键。** ``id`` 是 ``event_id`` —— 消费者按它去重
    （TASK-P1-025 T1 ③）。所以它不能变：改了主键，同一条事件就会被当成
    两条处理两次。

    ``payload`` 装事件的数据。**它是 JSON 而不是关系表** —— 事件的形状
    随 ``event_type`` 变化，为每种事件建一张表等于把"事件"变成"第二套
    业务模型"，而那套模型永远追不上。
    """

    __tablename__ = "outbox_event"

    #: 主体是什么种类（``project`` / ``task`` / ``evolution_cycle`` / …）。
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 主体的标识。**不建外键** —— 见模块说明。
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 事件类型。消费方按它决定怎么处理。
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 事件载荷。
    payload: Mapped[dict[str, object]] = mapped_column(JSONDocument, nullable=False, default=dict)
    #: 写进库的时刻。**relay 按它排队** —— 先写的先发。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    #: 发布成功的时刻。**为空就是还没发出去。**
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 已经尝试发布过几次。**重试的次数**，不是成功的次数。
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 最近一次发布失败的原因。**留着它，坏消息才不会变成一句"没发出去"。**
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # relay 的热查询："还没发的，按写入顺序取前 N 条"。部分索引 ——
        # 已发布的那一堆不该占着索引的空间。
        Index(
            "ix_outbox_event_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        # 按主体查：这个项目有哪些事件。
        Index("ix_outbox_event_aggregate", "aggregate_type", "aggregate_id"),
    )


class ConsumedEvent(UUIDPrimaryKeyMixin, Base):
    """某个消费者已经占用过某条事件。

    **粒度是 (消费者, 事件)。** 同一个消费者对同一条事件只处理一次；不同
    消费者各处理一次 —— 两个订阅方都得收到那条消息。

    **这是"占用"，不是"记录"。** 消费者先占住、处理完就留着；处理到一半
    崩了，占用要能还回去（:meth:`release`），否则那条事件永远不会被重投。
    所以这张表**不是 append-only** —— 它记的是一次可以撤销的领取。

    ``consumed_at`` 有第二个用处：**租约**。占用了却一直不处理（进程被
    kill 了），那一行会永远挡着这条事件 —— 所以判定"这个占用是不是过期
    了"要看时间：过期了就可以被别人抢走。**没有租约，一次崩溃就等于永久
    丢一条消息。**
    """

    __tablename__ = "consumed_event"

    #: 谁消费的。**不能为空**：没有消费者的去重是假的去重。
    consumer: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 被消费的事件。与 :class:`OutboxEvent` 的主键对应。
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    #: 占用的时刻。租约从这里算起。
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        # 幂等的**执行者**：同一对 (消费者, 事件) 只能有一行。
        UniqueConstraint(
            "consumer",
            "event_id",
            name="uq_consumed_event_consumer_event_id",
        ),
        # 反查："这条事件被谁消费过"。
        Index("ix_consumed_event_event_id", "event_id"),
    )


__all__ = ["ConsumedEvent", "OutboxEvent"]
