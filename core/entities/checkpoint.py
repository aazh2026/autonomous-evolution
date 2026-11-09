"""决策图 checkpoint 的持久化实体。

设计依据：docs/tasks.md TASK-P1-050（"图状态可持久化（checkpoint）"、
"中途异常后从 checkpoint 恢复不重复执行已完成的节点"）。

**这两张表不在 design.md §3.6 的清单里。** 那十二张核心实体装的是**项目
的业务事实**（项目、候选、决策、任务……），而这两张装的是**编排器自己的运行
状态** —— 决策图跑到哪一步了、中间写下了什么。它们的生命周期跟着一次编排走，
跟项目没关系；项目被删了，checkpoint 也就没有意义了。

**为什么不放在 ``infrastructure/`` 里。** 实体基类在 ``core/entities/base.py``，
而 Alembic 的 autogenerate 与 ``create_all`` 都只看 ``Base.metadata`` ——
实体散落在别处，迁移就会悄悄漏掉那几张表（这正是 ``core/entities/__init__.py``
模块文档里那句话的意思）。所以表还是放在这儿，只是**它记的东西**不是业务事实。

**checkpoint 本体存成 BYTEA，不存成 JSONB。** checkpoint 里装的是 Pydantic
模型、``Decimal``、元组 —— 这些都不是 JSON 原生类型。存 JSONB 就得在存之前
把它们降级成 JSON（``Decimal`` 变字符串、元组变列表），读回来再猜原来是什么
类型；**猜错类型的那份 checkpoint 比没有 checkpoint 更糟** —— 它看起来完整，
却让恢复后的流水线带着一份错形状的状态继续跑。所以这里存 LangGraph 自己
序列化出来的字节，读回来时由它自己反序列化，类型是它写进去时的样子。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.entities.base import Base, JSONDocument, TimestampMixin, UUIDPrimaryKeyMixin


class GraphCheckpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """决策图的一个 checkpoint —— 某个时刻的完整状态。"""

    __tablename__ = "graph_checkpoint"

    #: LangGraph 的 thread id —— 一次编排的标识。
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 命名空间。子图用；顶层图恒为空串。
    checkpoint_ns: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: LangGraph 的 checkpoint id（uuid6，**按时间递增**）。
    #:
    #: **顺序靠它，不靠时间戳。** uuid6 的高位是毫秒级时间戳，所以它的字符串
    #: 序就是时间序 —— 这让"取最新一个 checkpoint"是一条干净的
    #: ``ORDER BY checkpoint_id DESC LIMIT 1``，而不用在两个时间戳相等时猜。
    checkpoint_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 父 checkpoint。恢复时要沿这条链往回走。
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(64))
    #: 第几步。给人看的。
    step: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 这一步的来源（``input`` / ``loop`` / ``update`` / ``fork``）。
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="update")
    #: 序列化后的 checkpoint 本体。
    checkpoint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: 序列化器的类型标签（``msgpack`` / ``pickle`` / …）。
    #:
    #: **类型标签必须单独存一列。** 序列化器给的是 ``(类型, 字节)`` 二元组，
    #: 而字节本身不带"自己是怎么编码的"这个信息 —— 丢掉标签，恢复时就得靠
    #: 猜：猜对了能读，猜错了读出来是一堆看不懂的字节，或者干脆抛一个
    #: "未知序列化类型"。checkpoint 存的是**要接着往下跑的状态**，读不出来
    #: 等于这次演进白跑。
    serde_type: Mapped[str] = mapped_column(String(32), nullable=False, default="msgpack")
    #: 这一步的元数据（来源、步数、父链、写入者……）。
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONDocument, nullable=False, default=dict
    )

    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_graph_checkpoint_thread_ns_checkpoint",
        ),
        Index("ix_graph_checkpoint_thread", "thread_id", "checkpoint_ns", "checkpoint_id"),
    )


class GraphCheckpointWrite(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次 checkpoint 期间写下的中间结果（尚未并入状态的那些写）。

    **为什么单独一张表。** 一个节点算完了、结果还没并进整体状态 —— 这中间
    的写必须留着：节点中途失败时，已经算完的那部分不能白算。恢复时编排器
    读这些写，知道哪些活已经干完了。
    """

    __tablename__ = "graph_checkpoint_write"

    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 写来自哪个任务（节点）。
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 任务在图里的路径。
    task_path: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    #: 同一任务里的第几笔写。
    idx: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 写到了哪个通道（状态字段）。
    channel: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 写进去的值（序列化后的字节）。
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: 值的序列化类型标签。理由与 :attr:`GraphCheckpoint.serde_type` 相同。
    serde_type: Mapped[str] = mapped_column(String(32), nullable=False, default="msgpack")

    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
            name="uq_graph_checkpoint_write_thread_ns_checkpoint_task_idx",
        ),
        Index("ix_graph_checkpoint_write_thread", "thread_id", "checkpoint_ns", "checkpoint_id"),
    )


__all__ = ["GraphCheckpoint", "GraphCheckpointWrite"]
