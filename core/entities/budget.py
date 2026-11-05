"""预算账本的持久化实体。

设计依据：docs/tasks.md TASK-P1-012（Budget Manager；"并发 10 次扣减总和不超限，
用真实并发写库验证"）。

**这两张表不在 design.md §3.6 的十二个实体里** —— 那是设计文档的一处缺口，
已在 docs/tasks.md 第 6 节列为待裁决项 8。补它们的理由很直接：TASK-P1-012
要求"用真实并发写库验证"，而十二个实体里没有任何一张表能承载预算累计额。

两张表分工明确：

``budget_account``
    每个 (归属, 计量维度, 周期) 一行的**累计额**。它是热路径 —— 扣减时要
    锁住这一行，靠行级锁把"查余额"和"扣减"变成一个不可分割的动作。

``budget_entry``
    每一次扣减（或被拒绝的扣减尝试）的**流水**，append-only。
    只有累计额是不够的：出了事要能回答"钱花到哪儿了"，而累计额只回答
    "花了多少"。

一条重要的边界：**累计额只增不减**。数据库用触发器挡住把 ``spent`` 改小的
UPDATE，也挡住 DELETE / TRUNCATE —— 否则直接改一行就能把预算清零，
"总额不超限"这条不变量就形同虚设。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from core.entities.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class BudgetAccount(TimestampMixin, Base):
    """预算账户 —— 累计额。

    主键是三元组 ``(scope, metric, period)``：

    - ``scope`` —— 预算归属。阶段一只有一个项目，用固定值；阶段四多项目时
      取 project id。刻意不建外键 —— TASK-P1-012 不依赖项目注册，
      预算口径不该被项目表的生命周期绑住。
    - ``metric`` —— 计量维度（``daily_cost`` / ``token_usage`` / …）。
    - ``period`` —— 周期键。``day:2026-11-06`` 表示自然日，
      ``lifetime`` 表示生命周期累计。日切因此**不需要删除任何行**，
      新的一天就是新的一行，累计额自然从零开始。
    """

    __tablename__ = "budget_account"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    period: Mapped[str] = mapped_column(String(32), primary_key=True)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    spent: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"))

    __table_args__ = (
        # 累计额只能被应用层更新，但每次更新都要能按归属+维度查出来。
        Index("ix_budget_account_scope_metric", "scope", "metric"),
    )


class BudgetEntry(UUIDPrimaryKeyMixin, Base):
    """预算流水 —— append-only。

    ``status`` 有两个取值：``recorded``（真正扣了）与 ``blocked``（超限被拒）。
    被拒绝的尝试也要留痕 —— "系统试图超预算但被拦住了"本身就是需要被看见的
    事实，尤其是当它反复发生时。

    外键指向 :class:`BudgetAccount`：每条流水都必须有一行累计额兜底，
    不允许出现"有流水无账户"的记录。
    """

    __tablename__ = "budget_entry"

    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    period: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["scope", "metric", "period"],
            ["budget_account.scope", "budget_account.metric", "budget_account.period"],
            name="fk_budget_entry_account_budget_account",
            ondelete="RESTRICT",
        ),
        Index("ix_budget_entry_scope_metric_recorded_at", "scope", "metric", "recorded_at"),
    )


__all__ = ["BudgetAccount", "BudgetEntry"]
