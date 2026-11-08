"""人工审批请求表。

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09

设计依据：docs/tasks.md TASK-P1-046（DB-backed 审批门）、docs/design.md §7
（"代码合并"的初始自主权是 L2 = **需要人工审批**）。

**为什么这张表不在 design.md §3.6 的清单里。** 十二个核心实体里没有任何一张
能承载"人批了什么"。而 §7 把"代码合并"钉成 L2 —— 需要人工审批。一条权限规
则如果连落点都没有，它就只是一句文档：没有任何地方能查"这次合并批过没有"，
于是"需要审批"和"没人管"在运行时是同一件事。

**这张表记的是"人做了什么"，不是"现在能不能做"。** 字段上就看得出来：
``requested_by`` / ``decided_by`` / ``requested_at`` / ``decided_at`` 记的是
**谁在什么时候对什么做出了什么动作**。"现在能不能放行"是策略层根据这张表
**算出来**的结论（``core/policy/approval.py``）。把两者混在一张表里、混成一
个字段，就会出现"审批状态 = approved，但过期了"这种自相矛盾的记录 —— 而
"过期不可放行"恰恰是最要紧的一条。

**约束的取舍**：

- ``status`` 用 CHECK 约束住五个取值。应用层的校验会被绕过（有人直接写
  SQL、有人临时改代码），数据库层的不会 —— 而"这条审批处于什么状态"必须有
  唯一答案，否则它没法当审计依据。
- ``task_id`` **不建外键**。审批请求可以在任务落库之前被发起（策略检查发生
  在编排早期），一条悬空引用不该让整张表写不进去。任务 id 在领域层是字符串
  （``TaskContext.task_id``），这里跟着领域层走，并建索引供查询。
- 索引建在 ``(task_id, operation)`` 上：审批门问的问题永远是"这个任务的这个
  操作批过没有"，这正是那条查询。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 合法的审批状态。必须与 ``core.policy.approval.ApprovalStatus`` 一致 ——
#: 领域枚举加了取值而这里没加，数据库就会拒写合法记录。
_APPROVAL_STATUS_VALUES = "('pending', 'approved', 'rejected', 'expired', 'withdrawn')"


def upgrade() -> None:
    op.create_table(
        "approval_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("diff_summary", sa.Text(), nullable=False),
        sa.Column("evidence_ref", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_request")),
        sa.CheckConstraint(
            f"status IN {_APPROVAL_STATUS_VALUES}",
            name=op.f("ck_approval_request_approval_status_values"),
        ),
    )
    op.create_index(
        "ix_approval_request_task_operation",
        "approval_request",
        ["task_id", "operation"],
        unique=False,
    )
    op.create_index(
        "ix_approval_request_status",
        "approval_request",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_approval_request_status", table_name="approval_request")
    op.drop_index("ix_approval_request_task_operation", table_name="approval_request")
    op.drop_table("approval_request")
