"""项目隔离闸门 —— 按项目取候选 / 决策 / 任务。

设计依据：docs/tasks.md ``TASK-P4-010``（验收条件"两个项目并行演进，任务 / 候选 /
决策互不串扰；预算分别核算"）、R-016。

**隔离必须是构造出来的，不是记得的。** 三个阶段都是"一个项目"，所以一条
"按 id 取任务"的路径在单项目下**看不出任何问题** —— 直到有两个项目，而那时
串了也已经记进库了。所以这里把"这一行属于哪个项目"变成**每次取行都要过的
一道闸**：不属于这个作用域的行，**报错而不是返回**。

**为什么按 id 取要报错，而列表只要过滤。** 列表本来就是"给我这个项目的东
西"，过滤是它的语义。按 id 取不一样：调用方手里有一个具体的 id，他要的是
**那一条**。这时候如果那一行属于别的项目，有两种读法：

- "查无此项" —— 读的人会以为那条候选/任务不存在；
- "它存在，但在别的项目里" —— **这是越权读**，必须被看见。

把两者混成"查无此项"，越权读就变成了统计口径问题，而不是权限问题。所以这里
**分成两个错误**：``NotFoundError`` 与 :class:`ScopeViolationError`。

**数据库已经挡住了一半。** ``evolution_candidate`` / ``evolution_decision`` /
``task`` 三张表都对 ``project.id`` 有外键 —— 候选与决策一定挂在某个**已注册**
的项目下，物理上无法挂到不存在的项目上。这一层补的是另一半：**挂对了项目，
不代表你有权按另一个项目的身份去读它。**
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities.evolution import EvolutionCandidate, EvolutionDecision
from core.entities.task import Task
from core.project.scope import ProjectScope
from infrastructure.database.repositories import NotFoundError


class ScopeViolationError(Exception):
    """取到了不属于这个作用域的行。

    **这不是"查无此项"。** 那一行确实存在 —— 它在别的项目名下。把它报成
    "不存在"，越权读就退化成了统计口径问题。
    """

    def __init__(self, kind: str, row_id: str, scope: ProjectScope) -> None:
        super().__init__(
            f"{kind} {row_id} 不属于项目 {scope.project_id} —— 取它必须用它自己项目的作用域"
        )
        self.kind = kind
        self.row_id = row_id
        self.scope = scope


class ProjectRecords:
    """按项目取候选 / 决策 / 任务。

    **一个会话一个实例。** 与其他仓储一样，它不自己开事务 —— 事务边界由
    调用方决定。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def candidates(self, scope: ProjectScope) -> tuple[EvolutionCandidate, ...]:
        """这个项目的候选。**别的项目的候选不在这儿。**"""
        statement = (
            select(EvolutionCandidate)
            .where(EvolutionCandidate.project_id == scope.project_id)
            .order_by(EvolutionCandidate.created_at, EvolutionCandidate.id)
        )
        rows = await self._session.scalars(statement)
        return tuple(rows)

    async def decisions(self, scope: ProjectScope) -> tuple[EvolutionDecision, ...]:
        """这个项目的决策。"""
        statement = (
            select(EvolutionDecision)
            .where(EvolutionDecision.project_id == scope.project_id)
            .order_by(EvolutionDecision.created_at, EvolutionDecision.id)
        )
        rows = await self._session.scalars(statement)
        return tuple(rows)

    async def tasks(self, scope: ProjectScope) -> tuple[Task, ...]:
        """这个项目的任务。"""
        statement = (
            select(Task)
            .where(Task.project_id == scope.project_id)
            .order_by(Task.created_at, Task.id)
        )
        rows = await self._session.scalars(statement)
        return tuple(rows)

    async def candidate(
        self,
        scope: ProjectScope,
        candidate_id: uuid.UUID,
    ) -> EvolutionCandidate:
        """取一条候选 —— **不是这个项目的就报错**。"""
        row = await self._session.get(EvolutionCandidate, candidate_id)
        if row is None:
            raise NotFoundError("演进机会", candidate_id)
        if not scope.owns(row.project_id):
            raise ScopeViolationError("演进机会", str(candidate_id), scope)
        return row

    async def decision(
        self,
        scope: ProjectScope,
        decision_id: uuid.UUID,
    ) -> EvolutionDecision:
        """取一条决策 —— **不是这个项目的就报错**。"""
        row = await self._session.get(EvolutionDecision, decision_id)
        if row is None:
            raise NotFoundError("演进决策", decision_id)
        if not scope.owns(row.project_id):
            raise ScopeViolationError("演进决策", str(decision_id), scope)
        return row

    async def task(self, scope: ProjectScope, task_id: uuid.UUID) -> Task:
        """取一条任务 —— **不是这个项目的就报错**。"""
        row = await self._session.get(Task, task_id)
        if row is None:
            raise NotFoundError("研发任务", task_id)
        if not scope.owns(row.project_id):
            raise ScopeViolationError("研发任务", str(task_id), scope)
        return row

    async def project_ids(self) -> tuple[str, ...]:
        """库里有数据的项目 id —— **按字母序**。

        **为什么单独列这一条。** "库里现在有哪些项目"是隔离的第一道检查：
        一个项目应当在**它自己的**候选、决策、任务里各留下一份，而**别的项目
        的表里一行都没有**。
        """
        rows = await self._session.execute(
            select(EvolutionCandidate.project_id).distinct().order_by(EvolutionCandidate.project_id)
        )
        return tuple(row[0] for row in rows)

    async def count_by_project(self, scope: ProjectScope) -> dict[str, int]:
        """这个项目名下三张表各有多少行 —— **用来断言"别人的表里没有我的东西"**。"""
        counts = {}
        for name, entity in (
            ("candidates", EvolutionCandidate),
            ("decisions", EvolutionDecision),
            ("tasks", Task),
        ):
            counts[name] = (
                await self._session.scalar(
                    select(func.count())
                    .select_from(entity)
                    .where(entity.project_id == scope.project_id)
                )
                or 0
            )
        return counts


def scope_of_rows(rows: Sequence[Task]) -> set[str]:
    """这些行属于哪些项目 —— **用来断言"一批行不跨项目"**。

    **为什么放在这里。** 串扰最常见的形状是"一批本该同属一个项目的行里掺进
    了另一个项目的"。看单行看不出来，**看一批才看得出来** —— 所以这个函数
    存在的意义是让"这批行只属于一个项目"成为一句能写出来的断言。
    """
    return {row.project_id for row in rows}


__all__ = ["ProjectRecords", "ScopeViolationError", "scope_of_rows"]
