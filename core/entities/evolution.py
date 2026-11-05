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
from core.metrics import cycle
from core.metrics.cycle import CycleNotClosedError, CycleRecordError

if TYPE_CHECKING:
    from core.entities.task import Task


class EvolutionCycle(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个演进周期。

    周期是"一次循环 B"的边界：一次观察 → 发现 → 决策 → 规划。跨周期的
    证据必须留存（R-099），因此周期只增不改、不做清理。

    **同名不同层**：``core.metrics.cycle.EvolutionCycle`` 是**领域模型**（纯
    Python，可独立校验与比较）；这里是**持久化实体**。

    **两个结局字段不是一回事**：``status`` 说周期**怎么**结束的
    （completed / aborted / budget_exhausted / blocked），``outcome`` 说这一轮
    **有没有变好**（improved / no_change / regressed / stalled）。一轮被预算
    掐断的周期，跑完了但没变好 —— status 是 ``budget_exhausted``，outcome
    是 ``stalled``。两个词都叫"结局"，合成一个字段就会丢掉其中一个维度。

    ``outcome`` 的取值由数据库 CHECK 约束把关：绕过应用层写进一个自造的
    字符串，周期记录就失去了作为审计依据的意义。
    """

    __tablename__ = "evolution_cycle"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False
    )
    #: 人类可读的周期编号，如 ``cycle-0007``。
    cycle_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: 触发来源（commit / test_failure / defect / schedule / …）。阶段三补齐枚举。
    trigger: Mapped[str] = mapped_column(String(64), nullable=False, default="schedule")
    #: 周期**怎么**结束的。空表示周期还没结束。
    status: Mapped[str | None] = mapped_column(String(32))
    #: 这一轮**有没有变好**：improved / no_change / regressed / stalled。
    #: 空表示周期还没结束（此时还不存在"这一轮怎么样"这个问题）。
    outcome: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict[str, object]] = mapped_column(JSONDocument, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    candidates: Mapped[list[EvolutionCandidate]] = relationship(
        back_populates="cycle", lazy="selectin", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_evolution_cycle_project_id_started_at", "project_id", "started_at"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('improved', 'no_change', 'regressed', 'stalled')",
            name="evolution_cycle_outcome_values",
        ),
        CheckConstraint(
            "status IS NULL OR status IN ('completed', 'aborted', 'budget_exhausted', 'blocked')",
            name="evolution_cycle_status_values",
        ),
    )

    def to_domain(self) -> cycle.EvolutionCycle:
        """还原成领域模型。

        周期还没结束时还原会失败 —— 一个"进行中"的周期没有 outcome，也没有
        达成度差值，还原成完整记录等于凭空造出结论。

        计数缺失时同样失败。计数是"这一轮实际做了什么"的**唯一**凭据；缺了
        它却还原出一个全 0 的记录，等于凭空断言"这一轮什么都没做" ——
        那是一种不会报错的谎。
        """
        if self.ended_at is None or self.outcome is None or self.status is None:
            raise CycleNotClosedError(self.cycle_id)
        raw_counts = self.metrics.get("counts")
        if raw_counts is None:
            raise CycleRecordError(f"周期 {self.cycle_id!r} 缺少计数，无法还原为完整记录")
        return cycle.EvolutionCycle(
            project_id=self.project_id,
            cycle_id=self.cycle_id,
            trigger=self.trigger,
            started_at=self.started_at or self.ended_at,
            ended_at=self.ended_at,
            status=cycle.CycleStatus(self.status),
            outcome=cycle.CycleOutcome(self.outcome),
            outcome_reason=str(self.metrics.get("outcome_reason", "")),
            counts=cycle.CycleCounts.model_validate(raw_counts),
            attainment_before=_optional_decimal(self.metrics.get("attainment_before")),
            attainment_after=_optional_decimal(self.metrics.get("attainment_after")),
            cost=_optional_decimal(self.metrics.get("cost")) or Decimal(0),
            currency=str(self.metrics.get("currency", "")),
            evidence_refs=_evidence_refs(self.metrics.get("evidence_refs")),
        )

    @classmethod
    def from_domain(cls, record: cycle.EvolutionCycle) -> EvolutionCycle:
        """由领域模型构造持久化实体（尚未落库）。"""
        return cls(
            project_id=record.project_id,
            cycle_id=record.cycle_id,
            trigger=record.trigger,
            status=record.status.value,
            outcome=record.outcome.value,
            metrics={
                "outcome_reason": record.outcome_reason,
                "counts": record.counts.model_dump(),
                "attainment_before": _dump_decimal(record.attainment_before),
                "attainment_after": _dump_decimal(record.attainment_after),
                "cost": _dump_decimal(record.cost),
                "currency": record.currency,
                "evidence_refs": list(record.evidence_refs),
            },
            started_at=record.started_at,
            ended_at=record.ended_at,
        )


def _optional_decimal(value: object) -> Decimal | None:
    """从 JSON 里取一个可选的十进制数。

    JSON 没有十进制类型，``Decimal`` 存进去会变成字符串或浮点数。浮点数会
    丢精度（``0.1 + 0.2 != 0.3`` 这类），达成度差值算错一点点就可能把
    "没变"判成"变好了"。所以这里显式转回 ``Decimal``。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _dump_decimal(value: Decimal | None) -> str | None:
    """把十进制数存成字符串（JSON 里没有十进制类型）。"""
    return None if value is None else str(value)


def _evidence_refs(value: object) -> tuple[str, ...]:
    """从 JSON 里取证据引用列表。

    JSON 里的列表元素类型不固定。这里要求每一项都是字符串 —— 证据引用要么
    指得到一个东西，要么什么都不指。把一个字符串当成序列拆成一个个字符当
    引用，是最糟的一种错法：它不报错，只是让记录看起来"有证据"。
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise CycleRecordError(f"证据引用必须是列表，收到 {type(value).__name__}")
    for item in value:
        if not isinstance(item, str):
            raise CycleRecordError(f"证据引用必须是字符串，收到 {type(item).__name__}")
    return tuple(value)


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
