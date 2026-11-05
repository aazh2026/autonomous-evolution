"""演进周期、演进机会与演进决策的持久化实体。

设计依据：docs/design.md §3.3（Observe / Discover / Prioritize，Candidate 结构）、
§3.2（五类演进机会）、§5（Director 五阶段决策链）、docs/tasks.md TASK-P1-020、
TASK-P1-022（三表分离）、TASK-P1-054（决策三态）。

三张表的关系是本文件的核心，也是整个系统最重要的一条结构性约束：

    EvolutionCandidate ──(candidate_id, NOT NULL)──> EvolutionDecision
    EvolutionDecision ──(decision_id,  NOT NULL)──> Task

它把「发现问题」「作出决策」「执行任务」拆成三个不可压缩的步骤。
候选不能直接变成任务 —— 中间必须有一次**带理由的决策**。没有这一步，
Agent 发现一个缺陷就能直接改代码，人类就失去了对方向的掌控。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.entities.base import Base, JSONDocument, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from core.entities.task import Task


class EvolutionCycle(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个演进周期。

    周期是"一次循环 B"的边界：一次观察 → 发现 → 决策 → 规划。跨周期的
    证据必须留存（R-099），因此周期只增不改、不做清理。
    """

    __tablename__ = "evolution_cycle"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False
    )
    #: 人类可读的周期编号，如 ``cycle-0007``。
    cycle_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: 触发来源（commit / test_failure / defect / schedule / …）。阶段三补齐枚举。
    trigger: Mapped[str] = mapped_column(String(64), nullable=False, default="schedule")
    #: 周期结局：completed / aborted / budget_exhausted / blocked / …
    outcome: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict[str, object]] = mapped_column(JSONDocument, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    candidates: Mapped[list[EvolutionCandidate]] = relationship(
        back_populates="cycle", lazy="selectin", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_evolution_cycle_project_id_started_at", "project_id", "started_at"),
    )


class EvolutionCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """演进机会 —— 系统发现了什么值得改进。

    **候选不是任务**。候选只是"一个待评估的改进机会"，它还没有被批准，
    也没有成本约束，没有验收标准。把候选直接变成任务，等于把"发现"和"决定"
    压缩成一步 —— 那正是 design.md §3.6 要防的事。

    ``evidence`` 是候选存在的**唯一理由**：没有证据的候选不该被记录。
    它存结构化的证据引用（``artifact://`` / ``issue:`` / 文件路径），
    而不是一段自然语言描述 —— 证据必须能被独立核验，否则"有证据"只是说法。
    """

    __tablename__ = "evolution_candidate"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False
    )
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evolution_cycle.id", ondelete="RESTRICT")
    )
    #: 五类演进机会：product / architecture / engineering / quality / technology。
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    #: 证据列表。每条至少含 ``ref`` 与 ``kind``。
    evidence: Mapped[list[dict[str, object]]] = mapped_column(JSONDocument, nullable=False)
    #: 假设：做了这件事会怎样。
    hypothesis: Mapped[str | None] = mapped_column(Text)
    #: 预期结果。
    expected_outcome: Mapped[list[object]] = mapped_column(
        JSONDocument, nullable=False, default=list
    )

    #: 预期价值 V，归一化到 0~1（design.md §3.3 优先级公式）。
    value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=Decimal(0))
    #: 风险系数 R，归一化到 0~1。
    risk: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=Decimal(0))
    #: 证据置信度 C，归一化到 0~1。
    confidence: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=Decimal(0))
    #: 预估成本，如 ``"2d"``。保持字符串 —— 成本单位不统一是常态（人天 / token / 机器时）。
    estimated_effort: Mapped[str | None] = mapped_column(String(64))
    #: 候选自身的处理状态：proposed / accepted / rejected / deferred。
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")

    cycle: Mapped[EvolutionCycle | None] = relationship(
        back_populates="candidates", lazy="selectin"
    )
    decisions: Mapped[list[EvolutionDecision]] = relationship(
        back_populates="candidate", lazy="selectin", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("value >= 0 AND value <= 1", name="value_normalized"),
        CheckConstraint("risk >= 0 AND risk <= 1", name="risk_normalized"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_normalized"),
        Index("ix_evolution_candidate_project_id_status", "project_id", "status"),
    )


class EvolutionDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """演进决策 —— 系统为什么决定做或不做。

    **三态**（tasks.md TASK-P1-054）：``record`` / ``defer`` / ``reject``。
    "不行动"是一等公民：defer（先补充分析）与 reject（明确放弃）都是合法结局。
    如果一个系统从不拒绝任何候选，那它不是在自主决策，是在执行指令。

    两条 DB 级约束：

    1. ``candidate_id`` NOT NULL + 外键 —— 决策必须挂在某个候选上，
       凭空来的决策无法被追溯。
    2. ``decision = 'defer'`` 时 ``requested_analysis`` 必须非空 ——
       "以后再说"必须说清楚**还要补什么分析**，否则 defer 就成了
       一个不用负责的垃圾桶（R-034 / R-035）。
    """

    __tablename__ = "evolution_decision"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evolution_candidate.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False
    )
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evolution_cycle.id", ondelete="RESTRICT")
    )
    #: record / defer / reject。
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 决策理由。reject 必须有理由 —— 拒绝一个候选也要说清楚为什么。
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    #: defer 触发的补充分析项。非 defer 时为空数组。
    requested_analysis: Mapped[list[object]] = mapped_column(
        JSONDocument, nullable=False, default=list
    )
    #: 决策被 Critic 否决 / 被人工推翻时留痕。
    overridden_by: Mapped[str | None] = mapped_column(String(64))

    candidate: Mapped[EvolutionCandidate] = relationship(
        back_populates="decisions", lazy="selectin"
    )
    #: 由该决策派生出的任务。一个决策可以派生出多个任务。
    tasks: Mapped[list[Task]] = relationship("Task", back_populates="decision", lazy="selectin")

    __table_args__ = (
        CheckConstraint("decision IN ('record', 'defer', 'reject')", name="decision_state"),
        CheckConstraint(
            "decision <> 'defer' OR jsonb_array_length(requested_analysis) > 0",
            name="defer_requires_analysis",
        ),
        Index("ix_evolution_decision_project_id_created_at", "project_id", "created_at"),
    )


__all__ = ["EvolutionCandidate", "EvolutionCycle", "EvolutionDecision"]
