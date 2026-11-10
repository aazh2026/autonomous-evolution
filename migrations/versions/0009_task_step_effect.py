"""步骤副作用账 —— 断点恢复用。

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-10

设计依据：docs/tasks.md TASK-P3-016（"在执行中强杀进程，重启后任务状态可恢复
且不重复执行已完成的副作用"）、R-095（中断后可恢复或重新执行）。

**这张表不在 design.md §3.6 的十二个实体里。** 那是设计文档的一处缺口，和
预算账本表（0003）、审批请求表（0005）是同一类补充 —— 十二个实体装的是**项目
的业务事实**，而"这一步的副作用做完没有"是**编排器自己的运行状态**，跟
:mod:`core.entities.checkpoint` 一样。

**它记的东西是可核对的。** 任务行上的 ``status`` 只说得出"跑到 RUNNING 了"，
说不出"跑哪一步了、哪一步做完了"—— 断点恢复要的是后者。

**唯一约束在 ``(task_id, step)`` 上，不带 attempt。** 一个任务里的同一个步骤，
副作用最多发生一次；重试的是**同一件事**，不是另一件事。把 attempt 放进唯一
约束，就等于允许同一个副作用发生两次 —— 那正是这张表要防的事。

**数据库层的三道拒绝，不是应用层的"请记得检查"：**

1. **"完成"不可逆**
   （``ases_task_step_effect_guard``）：一条 ``done`` 的记录改成别的状态，
   UPDATE 一律拒绝。理由：把一件已经发生过的事说成没发生过，恢复时就会把
   它**再做一遍** —— 而"再做一遍"正是这张表存在的理由。

2. **账本不可删除**：DELETE 一律拒绝。已经记下的"做完 / 没做完"不能被抹掉
   —— 抹掉之后，"这一步到底跑过没有"就没人能回答了。

3. **状态只能是三档**：``started`` / ``done`` / ``failed``。别的值写不进去。

**"租约"是这张表里最容易被忽略、却最要紧的一栏。** ``lease_until`` 把"别人
正在做这一步"与"上一个执行者被强杀了"分开：没有它，被强杀的那一步会永远卡
在"别人正在做"—— 谁也不敢碰。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 步骤账守卫。
#:
#: 用 ERRCODE 'restrict_violation' —— 与 append-only 守卫同一个语义：
#: "违反限制"，不是"内部错误"。
_TASK_STEP_EFFECT_GUARD = """
CREATE OR REPLACE FUNCTION ases_task_step_effect_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            '步骤账不可删除：这一步做完没有是事实，抹掉之后没人能回答'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'done' AND NEW.status <> 'done' THEN
        RAISE EXCEPTION
            '步骤的完成状态不可逆：% / % 已经记为 done，改回去等于把'
            '已经发生过的事说成没发生过（见 docs/tasks.md TASK-P3-016）',
            OLD.task_id, OLD.step
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    op.create_table(
        "task_step_effect",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_step_effect")),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.id"],
            name=op.f("fk_task_step_effect_task_id_task"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "task_id",
            "step",
            name=op.f("uq_task_step_effect_task_id_step"),
        ),
        sa.CheckConstraint(
            "attempt >= 1",
            name=op.f("ck_task_step_effect_attempt_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('started', 'done', 'failed')",
            name=op.f("ck_task_step_effect_status_values"),
        ),
    )
    op.create_index(
        "ix_task_step_effect_task_id_status",
        "task_step_effect",
        ["task_id", "status"],
        unique=False,
    )

    # --- 守卫 ---
    op.execute(_TASK_STEP_EFFECT_GUARD)
    op.execute(
        "CREATE TRIGGER trg_task_step_effect_done_irreversible "
        "BEFORE UPDATE ON task_step_effect "
        "FOR EACH ROW EXECUTE FUNCTION ases_task_step_effect_guard();"
    )
    op.execute(
        "CREATE TRIGGER trg_task_step_effect_append_only_delete "
        "BEFORE DELETE ON task_step_effect "
        "FOR EACH ROW EXECUTE FUNCTION ases_task_step_effect_guard();"
    )
    op.execute(
        "CREATE TRIGGER trg_task_step_effect_append_only_truncate "
        "BEFORE TRUNCATE ON task_step_effect "
        "FOR EACH STATEMENT EXECUTE FUNCTION ases_append_only_guard();"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_task_step_effect_append_only_truncate ON task_step_effect;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_task_step_effect_append_only_delete ON task_step_effect;"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_task_step_effect_done_irreversible ON task_step_effect;")

    op.drop_index("ix_task_step_effect_task_id_status", table_name="task_step_effect")
    op.drop_table("task_step_effect")

    op.execute("DROP FUNCTION IF EXISTS ases_task_step_effect_guard();")
