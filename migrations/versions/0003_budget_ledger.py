"""预算账本 —— 累计额 + append-only 流水。

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06

设计依据：docs/tasks.md TASK-P1-012（"并发 10 次扣减总和不超限（无竞态，
用真实并发写库验证）"）。

**这两张表不在 design.md §3.6 的十二个实体里** —— 那是设计文档的缺口，
已在 docs/tasks.md 第 6 节列为待裁决项 8。补它们的理由：TASK-P1-012 要求
"用真实并发写库验证"，而十二个实体里没有任何一张表能承载预算累计额。

这里有两条**数据库层的拒绝**，不是应用层的"请记得检查"：

1. **累计额只增不减**（``ases_budget_account_guard``）
   把 ``budget_account.spent`` 改小的 UPDATE 一律拒绝，DELETE 也拒绝。
   为什么这么严：预算是防超支的闸门，而闸门最怕的就是"有人能把计数器
   调回去"。允许改小，就等于允许清零 —— "总额不超限"这条不变量立刻失效。

2. **流水不可改写**（复用 ``ases_append_only_guard``）
   ``budget_entry`` 的 UPDATE / DELETE / TRUNCATE 一律拒绝。
   已经记下的花费不能被抹掉 —— 抹掉之后，"这个项目到底花了多少钱"就
   没人能回答了。

日切不需要任何删除操作：周期键是 ``day:2026-11-06`` 这样的字符串，
新的一天自然是一行新的累计额，累计额从零开始。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 累计额守卫。UPDATE 不得让 ``spent`` 变小；DELETE 一律拒绝。
#:
#: 用 ERRCODE 'restrict_violation' —— 与 append-only 守卫同一个语义：
#: "违反限制"，不是"内部错误"。
_BUDGET_ACCOUNT_GUARD = """
CREATE OR REPLACE FUNCTION ases_budget_account_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            '预算累计额不可删除：预算账本是审计证据（见 docs/tasks.md TASK-P1-012）'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.spent < OLD.spent THEN
        RAISE EXCEPTION
            '预算累计额只增不减：% → %（把计数器调回去等于绕开预算闸门，'
            '见 docs/tasks.md TASK-P1-012）',
            OLD.spent, NEW.spent
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    op.create_table(
        "budget_account",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("period", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("spent", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "scope", "metric", "period", name=op.f("pk_budget_account")
        ),
    )
    op.create_index(
        "ix_budget_account_scope_metric", "budget_account", ["scope", "metric"], unique=False
    )

    op.create_table(
        "budget_entry",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("period", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope", "metric", "period"],
            ["budget_account.scope", "budget_account.metric", "budget_account.period"],
            name="fk_budget_entry_account_budget_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_entry")),
    )
    op.create_index(
        "ix_budget_entry_scope_metric_recorded_at",
        "budget_entry",
        ["scope", "metric", "recorded_at"],
        unique=False,
    )

    # --- 守卫 ---
    op.execute(_BUDGET_ACCOUNT_GUARD)
    op.execute(
        "CREATE TRIGGER trg_budget_account_monotonic "
        "BEFORE UPDATE ON budget_account "
        "FOR EACH ROW EXECUTE FUNCTION ases_budget_account_guard();"
    )
    op.execute(
        "CREATE TRIGGER trg_budget_account_append_only_delete "
        "BEFORE DELETE ON budget_account "
        "FOR EACH ROW EXECUTE FUNCTION ases_budget_account_guard();"
    )
    op.execute(
        "CREATE TRIGGER trg_budget_account_append_only_truncate "
        "BEFORE TRUNCATE ON budget_account "
        "FOR EACH STATEMENT EXECUTE FUNCTION ases_append_only_guard();"
    )
    op.execute(
        "CREATE TRIGGER trg_budget_entry_append_only_update_delete "
        "BEFORE UPDATE OR DELETE ON budget_entry "
        "FOR EACH ROW EXECUTE FUNCTION ases_append_only_guard();"
    )
    op.execute(
        "CREATE TRIGGER trg_budget_entry_append_only_truncate "
        "BEFORE TRUNCATE ON budget_entry "
        "FOR EACH STATEMENT EXECUTE FUNCTION ases_append_only_guard();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_budget_entry_append_only_truncate ON budget_entry;")
    op.execute("DROP TRIGGER IF EXISTS trg_budget_entry_append_only_update_delete ON budget_entry;")
    op.execute("DROP TRIGGER IF EXISTS trg_budget_account_append_only_truncate ON budget_account;")
    op.execute("DROP TRIGGER IF EXISTS trg_budget_account_append_only_delete ON budget_account;")
    op.execute("DROP TRIGGER IF EXISTS trg_budget_account_monotonic ON budget_account;")

    op.drop_index("ix_budget_entry_scope_metric_recorded_at", table_name="budget_entry")
    op.drop_table("budget_entry")
    op.drop_index("ix_budget_account_scope_metric", table_name="budget_account")
    op.drop_table("budget_account")

    op.execute("DROP FUNCTION IF EXISTS ases_budget_account_guard();")
