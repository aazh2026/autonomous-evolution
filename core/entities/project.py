"""项目与项目快照的持久化实体。

设计依据：docs/design.md §3.6 核心数据模型表（``Project``: id、goal、policy；
``ProjectSnapshot``: version、commit、metrics）、docs/tasks.md TASK-P1-020。

**同名不同层**：``core.project.models.Project`` 是**领域模型**（纯 Python，
六要素强校验，可从 YAML 载入）；``core.entities.Project`` 是**持久化实体**
（SQLAlchemy ORM，被数据库结构约束）。两者同名是有意的：它们描述的是同一个
东西的两种形态。把它们合成一个类，等于让数据库结构决定领域模型能不能存在。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.entities.base import Base, JSONDocument, TimestampMixin, UUIDPrimaryKeyMixin
from core.project import models as domain


class Project(TimestampMixin, Base):
    """项目定义。

    ``goal`` 存六要素原文（Objective / Key Results / Constraints / Non-goals /
    Autonomy Policy / Stop Conditions）。存原文而不是存"已解析好的几张表"，
    是为了让治理策略可被外部读取与审计 —— design.md §7 要求系统**不得**自主
    修改治理策略，那么治理策略就必须以可读的形式存在，而不是散落在代码里。

    ``policy`` 单独成列：它与目标相互独立，且变更需要审批（tasks.md §7）。
    """

    __tablename__ = "project"

    #: 项目 slug。与领域模型 ``Project.id`` 同源（YAML 里的 ``id: my-product``）。
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[dict[str, object]] = mapped_column(JSONDocument, nullable=False)
    policy: Mapped[dict[str, object]] = mapped_column(JSONDocument, nullable=False)
    budget: Mapped[dict[str, object]] = mapped_column(JSONDocument, nullable=False, default=dict)
    repository_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    tech_stack: Mapped[list[str]] = mapped_column(JSONDocument, nullable=False, default=list)

    snapshots: Mapped[list[ProjectSnapshot]] = relationship(
        back_populates="project", lazy="selectin", cascade="all, delete-orphan"
    )

    def to_domain(self) -> domain.Project:
        """还原成领域模型。

        存进去的时候已经校验过六要素；这里再校验一次是刻意的 ——
        库里可能被人工改过，还原时必须让不合法的数据**当场暴露**，
        而不是带着残缺的目标继续跑。
        """
        return domain.Project.model_validate(
            {
                "id": self.id,
                "name": self.name,
                "goal": self.goal,
                "budget": self.budget,
                "repository": {
                    "url": self.repository_url,
                    "default_branch": self.default_branch,
                },
                "tech_stack": list(self.tech_stack),
            }
        )

    @classmethod
    def from_domain(cls, spec: domain.Project) -> Project:
        """由领域模型构造持久化实体（尚未落库）。"""
        return cls(
            id=spec.id,
            name=spec.name,
            goal=spec.goal.model_dump(mode="json"),
            policy=spec.goal.autonomy.model_dump(mode="json"),
            budget=spec.budget.model_dump(mode="json"),
            repository_url=spec.repository.url,
            default_branch=spec.repository.default_branch,
            tech_stack=list(spec.tech_stack),
        )


class ProjectSnapshot(UUIDPrimaryKeyMixin, Base):
    """项目状态快照。

    快照必须**可复现**：给定一个 snapshot，能还原到当时的 commit 与当时的
    指标。``version`` 在项目内单调递增，``commit`` 指向代码，
    ``metrics`` 存当时的度量值。三者缺一，快照就只是一张无用的表。
    """

    __tablename__ = "project_snapshot"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    commit: Mapped[str] = mapped_column(String(64), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(255))
    metrics: Mapped[dict[str, object]] = mapped_column(JSONDocument, nullable=False, default=dict)

    project: Mapped[Project] = relationship(back_populates="snapshots", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_project_snapshot_project_id_version"),
        Index("ix_project_snapshot_project_id_commit", "project_id", "commit"),
    )


__all__ = ["Project", "ProjectSnapshot"]
