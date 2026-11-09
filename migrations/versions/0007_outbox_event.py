"""Outbox 事件表与消费标记表。

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09

设计依据：docs/tasks.md TASK-P1-025（PostgreSQL Outbox 事件表与 relay）、
I-06（状态写入与事件发布**同事务**）、R-079（事件驱动）。

**这两张表不在 design.md §3.6 的清单里。** 但 TASK-P1-025 的"状态写入与
事件发布同事务"必须落到某张表上 —— 否则那条要求只是一句文档：一个可靠的
outbox 需要一张自己的表，而不是靠调用方自觉地把"改状态"和"发消息"放在同一
个事务里（而"自觉"正是会失效的那种东西）。

**``outbox_event`` 不建外键。** ``aggregate_id`` 指向的东西是**多态的** ——
它可能是项目、任务、演进周期、审批请求，每种事件的主体不一样。建一个外键
就得挑一个父表，而那个选择对另外一半事件是错的。更实际的理由是：**事件活得
比它的主体久** —— 主体被删了，那条"它发生过"的事实仍然成立。

**``consumed_event`` 不加 append-only 守卫。** 它记的不是"发生过的事实"，而是
一次**可以撤销的占用**：消费者处理到一半崩了，占用要能还回去，否则那条事件
永远不会被重投。证据表（``verification_run`` / ``artifact``）才是 append-only
的 —— 那是另一种东西。

**``(consumer, event_id)`` 上有唯一约束。** 它是幂等消费的执行者：同一对只能
有一行。没有它，"查有没有占着"加"写一行"就分成两步了 —— 两个并发的消费者
会各自看到"没人占着"然后各自占上，同一条消息被处理两次，而**没有任何一个
会报错**。

**``ix_outbox_event_unpublished`` 是部分索引**（``WHERE published_at IS NULL``）——
relay 的热查询只关心没发出去的那些，而已发布的那一堆不该占着索引的空间。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_event")),
    )
    op.create_index(
        "ix_outbox_event_unpublished",
        "outbox_event",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index(
        "ix_outbox_event_aggregate",
        "outbox_event",
        ["aggregate_type", "aggregate_id"],
        unique=False,
    )

    op.create_table(
        "consumed_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consumer", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consumed_event")),
        sa.UniqueConstraint(
            "consumer",
            "event_id",
            name="uq_consumed_event_consumer_event_id",
        ),
    )
    op.create_index(
        "ix_consumed_event_event_id",
        "consumed_event",
        ["event_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_consumed_event_event_id", table_name="consumed_event")
    op.drop_table("consumed_event")
    op.drop_index("ix_outbox_event_aggregate", table_name="outbox_event")
    op.drop_index("ix_outbox_event_unpublished", table_name="outbox_event")
    op.drop_table("outbox_event")
