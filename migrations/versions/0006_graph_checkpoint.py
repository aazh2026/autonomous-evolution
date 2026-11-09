"""决策图 checkpoint 表。

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-09

设计依据：docs/tasks.md TASK-P1-050（"图状态可持久化（checkpoint）"、
"中途异常后从 checkpoint 恢复不重复执行已完成的节点"）。

**这两张表不在 design.md §3.6 的清单里。** 那十二张核心实体装的是**项目的
业务事实**（项目、候选、决策、任务……），而这两张装的是**编排器自己的运行
状态** —— 决策图跑到哪一步了、中间写下了什么。它们的生命周期跟着一次编排走，
跟项目没关系。

**checkpoint 本体是 BYTEA，不是 JSONB。** checkpoint 里装的是 Pydantic 模型、
``Decimal``、元组 —— 这些都不是 JSON 原生类型。存 JSONB 就得在存之前把它们
降级成 JSON（``Decimal`` 变字符串、元组变列表），读回来再猜原来是什么类型；
**猜错类型的那份 checkpoint 比没有 checkpoint 更糟** —— 它看起来完整，却让恢复
后的流水线带着一份错形状的状态继续跑。所以这里存 LangGraph 自己序列化出来的
字节，读回来时由它自己反序列化，类型是它写进去时的样子。

**序列化类型标签单独一列。** 序列化器给的是 ``(类型, 字节)`` 二元组，字节本身
不带"自己是怎么编码的"这个信息。丢掉标签，恢复时就得靠猜。

**顺序靠 ``checkpoint_id``，不靠时间戳。** LangGraph 的 checkpoint id 是 uuid6，
高位是毫秒级时间戳，所以它的**字符串序就是时间序**。于是"取最新一个
checkpoint"是一条干净的 ``ORDER BY checkpoint_id DESC LIMIT 1``，不用在两个时
间戳相同时猜。索引就建在这三列上。

**不建外键、不加 append-only 守卫。** 这两张表是**编排器的运行状态**，不是业务
事实 —— 它们要能被删（``adelete_thread`` 就是干这个的），删的时候不牵连别处。
业务事实那边才需要 append-only 守卫。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "graph_checkpoint",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("parent_checkpoint_id", sa.String(length=64), nullable=True),
        sa.Column("step", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("checkpoint", sa.LargeBinary(), nullable=False),
        sa.Column("serde_type", sa.String(length=32), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_checkpoint")),
        sa.UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_graph_checkpoint_thread_ns_checkpoint",
        ),
    )
    op.create_index(
        "ix_graph_checkpoint_thread",
        "graph_checkpoint",
        ["thread_id", "checkpoint_ns", "checkpoint_id"],
        unique=False,
    )

    op.create_table(
        "graph_checkpoint_write",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("task_path", sa.String(length=256), nullable=False),
        sa.Column("idx", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.String(length=128), nullable=False),
        sa.Column("value", sa.LargeBinary(), nullable=False),
        sa.Column("serde_type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_checkpoint_write")),
        sa.UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
            name="uq_graph_checkpoint_write_thread_ns_checkpoint_task_idx",
        ),
    )
    op.create_index(
        "ix_graph_checkpoint_write_thread",
        "graph_checkpoint_write",
        ["thread_id", "checkpoint_ns", "checkpoint_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_graph_checkpoint_write_thread", table_name="graph_checkpoint_write")
    op.drop_table("graph_checkpoint_write")
    op.drop_index("ix_graph_checkpoint_thread", table_name="graph_checkpoint")
    op.drop_table("graph_checkpoint")
