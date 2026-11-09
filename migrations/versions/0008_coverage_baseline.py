"""覆盖率基线表。

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-09

设计依据：docs/tasks.md TASK-P1-042（"每模块基线存储"、"阈值 =
max(模块基线, 全局地板)"、"基线下调需审批"）、R-056（覆盖率门槛）。

**这张表不在 design.md §3.6 的清单里。** 但那十二张核心实体装的是**项目的
业务事实**，而"标准是多少"既不是测量（那是快照）也不是事实（它会随代码演进
而变）。标准得有地方放 —— 否则"阈值 = max(模块基线, 全局地板)"这句规则就没有
第二个 operand。

**地板与各模块的基线放在同一行。** 它们必须一起变：分两处存，一次写失败就
会留下"地板是新的、模块是旧的"这种组合 —— 而那正是"标准被悄悄放松了"的
样子。

**地板的下限由 CHECK 兜住。** 应用层的校验可以被绕开（脚本、手工 SQL），
约束不行。"地板低于 0.85"这件事在数据库层就不该发生得了。

**``floor`` 用 ``Numeric(7, 6)``。** ``(6, 6)`` 的上限是 0.999999 ——
"这个模块 100% 被覆盖"是真实情况，存不进去就等于"测到满覆盖的模块没法建
基线"。

**``modules`` 是 JSONB。** 各模块的基线有多少个、叫什么名字，取决于项目 ——
写死成列就得给每个项目预留行，而且"给每个项目预留"这件事本身就会逼出一堆
空行。JSONB 可以给 ``modules`` 建索引，需要按模块查的时候照样能查。

**一个项目一行。** ``project_id`` 上有唯一约束 —— 基线表里出现两个项目各一行
不是"两份基线"，是"两份标准"，而标准必须是唯一的。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coverage_baseline",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("floor", sa.Numeric(precision=7, scale=6), nullable=False),
        sa.Column(
            "modules",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("lowering_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coverage_baseline")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_coverage_baseline_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "project_id",
            name=op.f("uq_coverage_baseline_project_id"),
        ),
        sa.CheckConstraint(
            "floor >= 0.85",
            name=op.f("ck_coverage_baseline_floor"),
        ),
        sa.CheckConstraint(
            "lowering_count >= 0",
            name=op.f("ck_coverage_baseline_lowering_count_non_negative"),
        ),
    )


def downgrade() -> None:
    op.drop_table("coverage_baseline")
