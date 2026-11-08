"""人工审批请求的持久化实体。

设计依据：docs/tasks.md TASK-P1-046（DB-backed 审批门）、docs/design.md §7。

**这张表是"人做了什么"的记录，不是"现在能不能做"的开关。** 两者的区别在字段
上就看得出来：表里有 ``requested_by`` / ``decided_by`` / ``requested_at`` /
``decided_at`` —— 它记的是**谁在什么时候对什么做出了什么动作**。当前能不能
放行是策略层根据这张表**算出来**的结论（``core/policy/approval.py``）。

两个约束值得单独说：

1. **状态只能是那五个值。** 用 CHECK 约束而不是只靠应用层校验：绕过应用层的
   写入（脚本、手工 SQL）也必须落在合法的状态集合里 —— 否则"这条审批到底
   处于什么状态"这个问题就没有唯一答案。
2. **``task_id`` 不建外键。** 审批请求可以在任务还没落库时被发起（策略检查
   发生在编排早期），一条悬空引用不该让整张表写不进去。任务 id 在领域层是
   字符串（``TaskContext.task_id``），这里跟着领域层走，并建索引供查询。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.entities.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from core.policy.approval import ApprovalStatus

#: 合法状态的字面量。直接由枚举生成 —— 枚举加了状态，约束自动跟着变。
_APPROVAL_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in ApprovalStatus)


class ApprovalRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次审批请求。

    ``id`` 就是领域层 ``ApprovalRequest.request_id`` 的字符串形式 —— 一条记录
    一个身份，不搞"数据库 id"与"领域 id"两套编号。
    """

    __tablename__ = "approval_request"

    #: 任务 id。字符串，与领域层一致（见模块文档）。
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 被审批的操作。取值来自 ``core.policy.autonomy.Operation``。
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    #: diff 摘要 —— 人批的是他看到的那份改动。
    diff_summary: Mapped[str] = mapped_column(Text, nullable=False)
    #: 验证证据引用。指向验证记录或产物 URI。
    evidence_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ApprovalStatus.PENDING.value
    )
    #: 请求人。
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 审批人。还没有结论时为空。
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 有效期。过期后不能用于放行（I-12）。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 审批意见 / 撤回理由。
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_APPROVAL_STATUS_VALUES})",
            name="approval_status_values",
        ),
        Index("ix_approval_request_task_operation", "task_id", "operation"),
        Index("ix_approval_request_status", "status"),
    )


def approval_status_values() -> list[str]:
    """合法的审批状态取值。供迁移脚本与文档引用。"""
    return [status.value for status in ApprovalStatus]


__all__ = ["ApprovalRequest", "approval_status_values"]
