"""项目环境配置列。

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06

design.md §3.1 说 Project Registry 管"多个项目及其**仓库、环境和技术栈**"。
仓库与技术栈在 0001 里已有对应列，环境配置没有 —— 领域模型
``core.project.models.Project.environment`` 存在，但落库时会丢。

这一列补上的是那个缺口。存量行给 ``{}``：没有环境配置的项目本来就是
合法的，不该因为补一列就被判为非法数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column(
            "environment",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # server_default 只是为了给存量行兜底。留着它的话，将来新增的行会
    # 悄悄继承一个"看不见的默认值" —— 想清空环境配置的人得记得显式写
    # '{}'，否则删不掉。默认值属于 schema 变更时的临时手段，用完即撤。
    op.alter_column("project", "environment", server_default=None)


def downgrade() -> None:
    op.drop_column("project", "environment")
