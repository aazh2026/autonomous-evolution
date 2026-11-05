"""架构决策记录的持久化实体。

设计依据：docs/design.md §3.6（``ArchitectureDecision``: context、decision、
tradeoff）、docs/tasks.md TASK-P1-020。

架构决策进数据库的理由和"项目事实"不同：Git 里的 ADR 是**给人读的文档**，
数据库里的这条是**结构化的事实** —— 系统在评估候选时需要回答
"这个模块当初为什么这么设计"。没有结构化的架构认知，Agent 只会重复
推翻前人已经推翻过的决策。

``supersedes_id`` 让决策可以被推翻而不是删除 —— 历史不能被抹掉，
只能被后续决策取代。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.entities.base import Base, JSONDocument, TimestampMixin, UUIDPrimaryKeyMixin


class ArchitectureDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条架构决策（ADR）。"""

    __tablename__ = "architecture_decision"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False
    )
    #: 决策背景：当时面对什么问题、约束是什么。
    context: Mapped[str] = mapped_column(Text, nullable=False)
    #: 决策内容：选了什么、为什么。
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    #: 代价：放弃了什么、 引入了什么新问题。
    tradeoff: Mapped[str | None] = mapped_column(Text)
    #: proposed / accepted / rejected / superseded。
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    #: 被本决策取代的那条决策。
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("architecture_decision.id", ondelete="RESTRICT")
    )
    extra: Mapped[dict[str, Any]] = mapped_column(JSONDocument, nullable=False, default=dict)

    superseded_by: Mapped[list[ArchitectureDecision]] = relationship(
        "ArchitectureDecision", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_architecture_decision_project_id_created_at", "project_id", "created_at"),
    )


__all__ = ["ArchitectureDecision"]
