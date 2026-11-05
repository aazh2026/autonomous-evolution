"""研发任务与任务依赖的持久化实体。

设计依据：docs/design.md §3.6（``Task``: type、priority、status；
``TaskDependency``: predecessor、successor）、§6（Task 状态机 14 态）、
docs/tasks.md TASK-P1-020、TASK-P1-022（三表分离）、R-051（验收契约准入）。

**三表分离的落点**就在这个文件里：

    Task.decision_id  NOT NULL  REFERENCES evolution_decision(id)

这不是"应用层记得填"，是数据库本身拒绝没有决策来源的任务。理由很直接 ——
一个没有决策来源的任务，就是"Agent 发现问题后直接改代码"。那条路径一旦
在数据库层面被打通，后面所有的审批门、预算门、审计都形同虚设。

**验收契约的落点**也在这个文件里：``status`` 一旦进入执行态，
``acceptance_contract`` 必须非空（CHECK 约束）。R-051 要求验收契约是
*准入闸门*而不是事后报告；只在 Python 状态机里检查是不够的 ——
任何一条绕过状态机的 SQL 写入都会跳过它。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.entities.base import Base, JSONDocument, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from core.task.state_machine import TaskState

if TYPE_CHECKING:
    from core.entities.evolution import EvolutionDecision

#: 14 个合法状态的字面量。直接由状态机枚举生成 ——
#: 状态机加了状态，这里的约束自动跟着变，不会两边漂移。
_TASK_STATE_VALUES = ", ".join(f"'{state.value}'" for state in TaskState)

#: 进入执行态后必须已绑定验收契约（R-051）。
#: APPROVED/PLANNED/READY 属于"还没开始干"，BLOCKED 是"没在干"，都不需要契约。
_EXECUTING_STATES = (
    "RUNNING",
    "VERIFYING",
    "PASSED",
    "REPAIRING",
    "INTEGRATING",
    "MERGED",
    "COMPLETED",
)


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """研发任务。

    任务**只能**由一条 ``record`` 决策派生。没有别的入口 —— 仓储层
    （``infrastructure/database/repositories.py``）是唯一的创建路径，
    且它会检查决策状态。三处约束（DB 外键、仓储检查、静态检查）指向同一件事：
    「发现问题」「作出决策」「执行任务」必须留下三份不同的痕迹。
    """

    __tablename__ = "task"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False
    )
    #: 决策来源。**NOT NULL** —— 三表分离的物理保证。
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evolution_decision.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: 任务类型。自由字符串 —— 任务种类随项目而变，不该由系统枚举锁死。
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 优先级。数值型，值域由 Director 的评分模型决定。
    priority: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=Decimal(0))
    #: 14 态状态机。
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TaskState.APPROVED.value
    )

    #: 验收契约（R-051）。进入执行态前必须存在并绑定本任务。
    acceptance_contract: Mapped[dict[str, Any] | None] = mapped_column(JSONDocument)

    #: 集成分支名。
    branch: Mapped[str | None] = mapped_column(String(255))
    #: 执行工作区标识。授权判定的五个要素之一（R-063）。
    workspace_id: Mapped[str | None] = mapped_column(String(64))
    #: 计划使用的执行器。
    executor: Mapped[str | None] = mapped_column(String(64))
    #: 修复次数。状态机的修复闸门以它为准。
    repair_count: Mapped[int] = mapped_column(nullable=False, default=0)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict[str, Any]] = mapped_column(JSONDocument, nullable=False, default=dict)

    decision: Mapped[EvolutionDecision] = relationship(
        "EvolutionDecision", back_populates="tasks", lazy="selectin"
    )
    predecessors: Mapped[list[Task]] = relationship(
        "Task",
        secondary="task_dependency",
        primaryjoin="Task.id == TaskDependency.successor_id",
        secondaryjoin="Task.id == TaskDependency.predecessor_id",
        back_populates="successors",
        lazy="selectin",
    )
    successors: Mapped[list[Task]] = relationship(
        "Task",
        secondary="task_dependency",
        primaryjoin="Task.id == TaskDependency.predecessor_id",
        secondaryjoin="Task.id == TaskDependency.successor_id",
        back_populates="predecessors",
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({_TASK_STATE_VALUES})", name="task_status_values"),
        CheckConstraint(
            f"status NOT IN ({', '.join(repr(s) for s in _EXECUTING_STATES)}) "
            "OR acceptance_contract IS NOT NULL",
            name="acceptance_contract_required",
        ),
        CheckConstraint("repair_count >= 0", name="repair_count_non_negative"),
        Index("ix_task_project_id_status", "project_id", "status"),
        Index("ix_task_decision_id", "decision_id"),
    )


class TaskDependency(UUIDPrimaryKeyMixin, Base):
    """任务依赖。

    ``predecessor`` 必须先于 ``successor`` 完成。自环是非法的 ——
    自己依赖自己是死锁，不是依赖。
    """

    __tablename__ = "task_dependency"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False
    )
    predecessor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task.id", ondelete="RESTRICT"), nullable=False
    )
    successor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task.id", ondelete="RESTRICT"), nullable=False
    )
    #: finish_to_start / start_to_start / finish_to_finish。
    dependency_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="finish_to_start"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("predecessor_id", "successor_id", name="uq_task_dependency_edge"),
        CheckConstraint("predecessor_id <> successor_id", name="no_self_dependency"),
        Index("ix_task_dependency_successor_id", "successor_id"),
    )


__all__ = ["Task", "TaskDependency"]
