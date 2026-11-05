"""执行会话与执行轨迹的持久化实体。

设计依据：docs/design.md §3.6（``AgentSession``: executor、session_id；
``ExecutionEvent``: event_type、payload）、docs/tasks.md TASK-P1-020、
R-063（Agent 身份 ≠ 任务身份）。

**身份分离**是这两个实体的存在理由。授权判定的 key 是
``(agent_id, task_id, project_id, workspace_id, expiry)`` —— 五个要素缺一不可。
其中 ``agent_id`` 与 ``task_id`` 必须**分别**记录：只记"谁在干活"，
就无法回答"这个 Agent 是不是被授权干这个任务"；只记"干的是哪个任务"，
就无法回答"是不是有别的 Agent 冒名顶替"。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.entities.base import Base, JSONDocument, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class AgentSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次 Agent 执行会话。

    ``agent_id`` 与 ``task_id`` 都必须存在，且互不替代 —— 一个 Agent 可以
    在不同任务上开多个会话，一个会话只属于一个任务。
    """

    __tablename__ = "agent_session"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task.id", ondelete="RESTRICT"), nullable=False
    )
    #: Agent 的独立身份。**不能**用它替代 task_id。
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 执行器标识（claude_code / codex / …）。
    executor: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 执行器侧的会话标识。
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 授权范围快照：agent_id / task_id / project_id / workspace_id / expiry。
    #: 落库是为了事后审计"当时授权的是什么"，而不是只在内存里存在。
    authorization: Mapped[dict[str, Any]] = mapped_column(JSONDocument, nullable=False)
    #: active / completed / cancelled / failed。
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict[str, Any]] = mapped_column(JSONDocument, nullable=False, default=dict)

    events: Mapped[list[ExecutionEvent]] = relationship(
        back_populates="session", lazy="selectin", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_agent_session_task_id_agent_id", "task_id", "agent_id"),
        Index("ix_agent_session_project_id_created_at", "project_id", "created_at"),
    )


class ExecutionEvent(UUIDPrimaryKeyMixin, Base):
    """执行轨迹上的一条事件。

    事件是**只追加**的事实记录，不是状态。``causation_id`` 串起因果链
    （I-11：演进循环 → 工程循环 → 回写 → 再次触发），``correlation_id``
    串起同一次请求的全部事件。两者分开，是因为一次触发会引发多个不同
    原因的事件 —— 把因果和关联压成一个字段，就没法回答"为什么会有这条"。
    """

    __tablename__ = "execution_event"

    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_session.id", ondelete="CASCADE")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task.id", ondelete="RESTRICT"))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="RESTRICT"))
    agent_id: Mapped[str | None] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDocument, nullable=False, default=dict)
    #: 直接原因的事件 id。
    causation_id: Mapped[str | None] = mapped_column(String(64))
    #: 同一次请求的关联 id。
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    #: 会话内的顺序号。幂等消费与重放依赖它。
    sequence: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    session: Mapped[AgentSession | None] = relationship(back_populates="events", lazy="selectin")

    __table_args__ = (
        Index("ix_execution_event_task_id_created_at", "task_id", "created_at"),
        Index("ix_execution_event_session_id_sequence", "session_id", "sequence"),
        Index("ix_execution_event_causation_id", "causation_id"),
        Index("ix_execution_event_correlation_id", "correlation_id"),
    )


__all__ = ["AgentSession", "ExecutionEvent"]
