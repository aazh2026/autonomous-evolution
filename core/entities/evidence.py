"""验收记录与交付证据的持久化实体。

设计依据：docs/design.md §3.6（``VerificationRun``: checks、result；
``Artifact``: uri、hash、type）、docs/tasks.md TASK-P1-020、TASK-P1-024
（append-only 与不可丢失）、R-071 / R-099 / I-05 / I-21。

这两张表是整个系统的**证据链**。一旦任务完成，验收证据必须永久可读 ——
下一轮演进不能把上一轮的"其实没修好"记录冲掉。所以：

1. 两张表**只允许 INSERT**。DB 层用触发器禁掉 UPDATE / DELETE。
   应用层约定不够：任何一条 SQL、任何一个运维脚本都能改掉证据。
2. ``Artifact`` 带 ``sha256``。产物被篡改时能当场发现 —— 没有 hash，
   "证据还在"和"证据没被改过"是两回事。
3. ``VerificationRun.result`` 由**独立验证器**写入，不接受执行器自评
   （design.md §3.5B）。``verified_by`` 记录是谁做的验证，让"谁说的"可追溯。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.entities.base import Base, JSONDocument, UUIDPrimaryKeyMixin, utcnow


class VerificationRun(UUIDPrimaryKeyMixin, Base):
    """一次验收。

    **append-only**：已写入的验收记录不可修改、不可删除。触发器在
    ``migrations/versions/0001_initial_schema.py`` 里创建。

    ``checks`` 存这次验收实际跑了哪些检查、以及每项的结果 —— 不是只存
    一个 "passed"。只存结论的验收无法被复核，也无法定位"哪一项回归了"。
    """

    __tablename__ = "verification_run"

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task.id", ondelete="RESTRICT"), nullable=False
    )
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evolution_cycle.id", ondelete="RESTRICT")
    )
    #: 逐项检查结果。空数组不允许 —— 没跑检查的验收不算验收。
    checks: Mapped[list[dict[str, object]]] = mapped_column(JSONDocument, nullable=False)
    #: passed / failed / error / skipped。
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 执行验证的人或进程。与执行器分离 —— 自评不算验收。
    verified_by: Mapped[str] = mapped_column(String(128), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="verification_run", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("jsonb_array_length(checks) > 0", name="checks_not_empty"),
        CheckConstraint("result IN ('passed', 'failed', 'error', 'skipped')", name="result_state"),
        Index("ix_verification_run_task_id_created_at", "task_id", "created_at"),
    )


class Artifact(UUIDPrimaryKeyMixin, Base):
    """一份交付证据。

    产物存在对象存储里，这里存**指针**（``uri``）与**内容摘要**（``hash``）。
    大文件进不进库不是重点，重点是：证据以 hash 寻址，内容被改过就再也
    对不上（R-024：产物以 hash 寻址）。

    **append-only**：与 VerificationRun 同理，触发器禁掉 UPDATE / DELETE。
    ``retention_until`` 是保留期 —— 过了保留期也**不自动删除**，
    删不删由人决定。自动清理会毁掉"上一轮到底改了什么"的唯一凭据。
    """

    __tablename__ = "artifact"

    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task.id", ondelete="RESTRICT"))
    verification_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("verification_run.id", ondelete="RESTRICT")
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="RESTRICT"))
    #: 对象存储地址，如 ``s3://ases-artifacts/2026/11/report.json``。
    uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    #: 内容 sha256。产物被篡改时校验失败。
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 产物类型：test_report / log / screenshot / diff / …
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    #: 保留期截止时间。到期不自动删除。
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    verification_run: Mapped[VerificationRun | None] = relationship(
        back_populates="artifacts", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("char_length(hash) = 64", name="hash_is_sha256"),
        Index("ix_artifact_task_id_created_at", "task_id", "created_at"),
        Index("ix_artifact_hash", "hash"),
    )


__all__ = ["Artifact", "VerificationRun"]
