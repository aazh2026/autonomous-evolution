"""演进周期：一分为二的结局字段 + 取值约束。

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06

设计依据：docs/tasks.md TASK-P1-070（EvolutionCycle 记录）。

**为什么改列。** 原来 ``evolution_cycle.outcome`` 一个字段承担了两件事：
"周期怎么结束的"（completed / aborted / budget_exhausted / blocked）和
"这一轮有没有变好"。两件事都叫"结局"，但它们是两个维度 —— 一轮被预算掐断
的周期，跑完了但没变好。合成一个字段，就必然丢掉其中一个：要么预算掐断的
周期被记成"完成"，要么它被记成"没改善"，而这两个说法都不准确。

所以拆成两个字段：

- ``status`` —— 周期**怎么**结束的（新增列）；
- ``outcome`` —— 这一轮**有没有变好**：improved / no_change / regressed /
  stalled。

``outcome`` 的取值由数据库 CHECK 约束把关。理由与 ``task_status_values``、
``decision_state`` 一样：应用层的检查会被绕过（有人直接写 SQL、有人临时改
代码），数据库层的不会。一个能被随手写进 ``outcome`` 的周期记录，作为审计
依据就没有意义了。

**旧数据的迁移。** 老列里存的是"怎么结束"的取值，直接加 CHECK 会失败。
按下面的对应关系拆开：

| 旧 outcome        | 新 status          | 新 outcome |
| ----------------- | ------------------ | ---------- |
| completed         | completed          | no_change  |
| aborted           | aborted            | stalled    |
| budget_exhausted  | budget_exhausted   | stalled    |
| blocked           | blocked            | stalled    |

老记录从来没有"变好了没有"这个信息，所以除 ``completed`` 之外一律落到
``stalled``（而不是 ``no_change``）：那几轮根本没跑完，"没有可验证的改善"
和"什么也没落地"里，只有后者是当时的事实。

本项目尚未部署到任何生产环境，老数据只可能存在于本地开发库里；但迁移
不假设这一点 —— 它对两种情况都成立。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 旧 ``outcome`` 取值 → (新 ``status``, 新 ``outcome``)。
_LEGACY_SPLIT: tuple[tuple[str, str, str], ...] = (
    ("completed", "completed", "no_change"),
    ("aborted", "aborted", "stalled"),
    ("budget_exhausted", "budget_exhausted", "stalled"),
    ("blocked", "blocked", "stalled"),
)


def upgrade() -> None:
    op.add_column(
        "evolution_cycle",
        sa.Column("status", sa.String(length=32), nullable=True),
    )

    # 旧记录按对应关系拆成两个字段。
    for legacy, status, outcome in _LEGACY_SPLIT:
        op.execute(
            sa.text(
                "UPDATE evolution_cycle SET status = :status, outcome = :outcome "
                "WHERE outcome = :legacy"
            ).bindparams(status=status, outcome=outcome, legacy=legacy)
        )

    op.create_check_constraint(
        "evolution_cycle_outcome_values",
        "evolution_cycle",
        "outcome IS NULL OR outcome IN ('improved', 'no_change', 'regressed', 'stalled')",
    )
    op.create_check_constraint(
        "evolution_cycle_status_values",
        "evolution_cycle",
        "status IS NULL OR status IN ('completed', 'aborted', 'budget_exhausted', 'blocked')",
    )


def downgrade() -> None:
    op.drop_constraint("evolution_cycle_status_values", "evolution_cycle", type_="check")
    op.drop_constraint("evolution_cycle_outcome_values", "evolution_cycle", type_="check")

    # 拆开之前的样子：两个字段合回一个，只保留"怎么结束"。
    op.execute(sa.text("UPDATE evolution_cycle SET outcome = status WHERE status IS NOT NULL"))
    op.execute(sa.text("UPDATE evolution_cycle SET outcome = 'completed' WHERE outcome IS NULL"))

    op.drop_column("evolution_cycle", "status")
