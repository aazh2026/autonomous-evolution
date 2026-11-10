"""步骤副作用账的持久化实体。

设计依据：docs/tasks.md TASK-P3-016（"在执行中强杀进程，重启后任务状态可恢复
且不重复执行已完成的副作用"）、R-095（中断后可恢复或重新执行）。

**这张表记的不是项目的业务事实，是编排器自己的运行状态** —— 跟
:mod:`core.entities.checkpoint` 是同一类东西：进程死了之后，靠它知道"跑到哪
一步了、哪一步做完了"。

**主键是 ``(task_id, step)``，不是 ``(task_id, step, attempt)``。** 一个任务
里的同一个步骤，副作用最多发生一次 —— 重试的是**同一件事**，不是另一件事。
把 attempt 放进主键，就等于允许同一个副作用发生两次，那正是这一层要防的
事。

**``(task_id, step)`` 上的唯一约束不是装饰。** 它是"两个执行者不会同时跑同
一步"这条保证的**数据库层**依托：占位写不进去的那一个，只能读到"别人正在
做"，也就只能等着。

**``lease_until`` 把"别人正在做"与"上个执行者死了"分开。** 没有它，一个进程
被强杀之后，这一步就永远卡在"别人正在做"—— 谁也不敢碰。

**``status`` 只有三档**（started / done / failed），与
:mod:`core.task.resume` 的词汇表一一对应。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.entities.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TaskStepEffect(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一步副作用的账。

    **一行 = 一个任务的某一步。** 记的是"这一步的副作用做完了没有"，以及
    "做完的是哪一次尝试"。

    **``done`` 不可逆。** 应用层保证"先到先得"；这里再兜一道 CHECK，
    免得有人手工改库把一条 ``done`` 改成 ``started`` —— 那等于把一件已经
    发生过的事说成没发生过，恢复时会把它再做一遍。
    """

    __tablename__ = "task_step_effect"

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task.id", ondelete="RESTRICT"), nullable=False
    )
    #: 步骤名。**同一任务的同一个步骤只有一行。**
    step: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 第几次尝试。**不参与唯一约束** —— 重试的是同一件事。
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: started / done / failed。
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 这一步的幂等键（``task_id:step``）。落库是为了事后对账。
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    #: 租约到什么时候。**"别人正在做"与"上个执行者死了"的分界。**
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 备注 —— 失败原因、探测结论等等。
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("task_id", "step", name="uq_task_step_effect_task_id_step"),
        Index("ix_task_step_effect_task_id_status", "task_id", "status"),
    )


__all__ = ["TaskStepEffect"]
