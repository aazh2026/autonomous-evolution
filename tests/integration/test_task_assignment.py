"""TASK-P1-081 —— 任务行上的"谁来做"与"在哪儿干"。

两条东西要记在**任务行**上，不是在会话行上：

- **执行器** —— 资源调度那一步把执行器分给了某个任务，但调度结果只活在编排层
  的内存里；任务行上不记，事后查一条任务就不知道它是哪个执行器做的。
- **工作区与分支** —— "在哪儿干的"也是任务的一部分。分支正是回滚与追责时
  要找的那条线。

**为什么这两栏不许被改写。** 一个任务被两个执行器先后认领、或者在两个工作区
里各干一遍，改派之后查到的就只剩最后一个 —— 而先前那个执行器留下的会话与
事件仍然在库里，两者对不上，且没人说得清哪个是真的。所以**已经有主了再指派，
要报错，而不是悄悄改掉历史**。

**指派同一个执行器是空操作。** 重放同一条路线图不该报错 —— 那是同一件事，
不是改派。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.evolution.candidate import EvolutionCandidate
from core.evolution.decision import DecisionPolicy, EvolutionDecision
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from infrastructure.database.repositories import (
    AlreadyAssignedError,
    CandidateRepository,
    DecisionRepository,
    TaskRepository,
)

pytestmark = pytest.mark.integration

_INSERT_PROJECT_SQL = text(
    """
    INSERT INTO project
        (id, name, goal, policy, budget, repository_url, default_branch,
         tech_stack, environment, created_at, updated_at)
    VALUES
        (:id, :name, CAST(:goal AS jsonb), CAST(:policy AS jsonb),
         CAST(:budget AS jsonb), :url, 'main', CAST(:tech_stack AS jsonb),
         CAST(:environment AS jsonb), now(), now())
    """
)


async def _insert_project(session: AsyncSession, project_id: str = "p-demo") -> None:
    await session.execute(
        _INSERT_PROJECT_SQL,
        {
            "id": project_id,
            "name": "演示项目",
            "goal": '{"objective": [{"id": "OBJ-1", "description": "x"}]}',
            "policy": "{}",
            "budget": "{}",
            "url": "https://example.invalid/repo.git",
            "tech_stack": "[]",
            "environment": "{}",
        },
    )
    await session.commit()


def _scored(candidate_id: str) -> ScoredCandidate:
    """一条会得到 ``record`` 决策的候选。"""
    candidate = EvolutionCandidate(
        id=candidate_id,
        category=OpportunityCategory.ARCHITECTURE,
        title="拆分权限模块",
        evidence=(Evidence(kind=EvidenceKind.ARTIFACT, ref="artifact://coupling"),),
        hypothesis="拆分权限领域服务可以降低模块耦合。",
        expected_outcome=("降低模块依赖数量",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.9"),
    )
    factors = PriorityFactors.from_candidate(
        candidate,
        value=Decimal("0.8"),
        impact=Decimal("0.6"),
        urgency=Decimal("0.5"),
        cost=Decimal("0.5"),
    )
    return ScoredCandidate(candidate_id=candidate_id, factors=factors)


async def _seed_task(session: AsyncSession) -> uuid.UUID:
    """登记项目、候选、决策，并从那条决策派生一个任务。返回任务行 id。"""
    await _insert_project(session)
    candidates = CandidateRepository(session)
    decisions = DecisionRepository(session)

    candidate = await candidates.create(
        project_id="p-demo",
        category="architecture",
        title="拆分权限模块",
        evidence=[{"kind": "artifact", "ref": "artifact://coupling"}],
        confidence=Decimal("0.9"),
    )
    scored = _scored(str(candidate.id))
    decision: EvolutionDecision = DecisionPolicy().decide(scored, decision_id="EVD-cycle-0007-001")
    row = await decisions.record(
        candidate_id=candidate.id,
        decision=decision.decision.value,
        rationale=decision.rationale,
    )
    await session.commit()

    tasks = TaskRepository(session)
    task = await tasks.create_from_decision(
        row.id,
        title="拆分权限模块",
        task_type="refactoring",
    )
    await session.commit()
    return task.id


#: 允许被拼进 SQL 的列名 —— **列名不能参数化**，所以这里自己划一条白名单，
#: 而不是让调用方随便传一个字符串进来。
_ALLOWED_COLUMNS = ("executor", "branch", "workspace_id")


async def _column(session: AsyncSession, task_id: uuid.UUID, column: str) -> object:
    """读任务行的某一列 —— **从数据库读**，不是从内存里的对象读。"""
    assert column in _ALLOWED_COLUMNS
    stored = await session.execute(
        text(f"SELECT {column} FROM task WHERE id = :task_id"),
        {"task_id": task_id},
    )
    return stored.scalar_one()


class TestTheExecutorIsRecordedOnTheTaskRow:
    """执行器记在任务行上 —— 事后查得到"这件事是谁做的"。"""

    async def test_the_executor_is_written_to_the_row(self, session: AsyncSession) -> None:
        task_id = await _seed_task(session)
        tasks = TaskRepository(session)

        await tasks.assign_executor(task_id, "m1-local-patcher")
        await session.commit()

        assert await _column(session, task_id, "executor") == "m1-local-patcher"

    async def test_assigning_the_same_executor_again_changes_nothing(
        self, session: AsyncSession
    ) -> None:
        """重放同一条路线图不该报错 —— 那是同一件事，不是改派。"""
        task_id = await _seed_task(session)
        tasks = TaskRepository(session)

        await tasks.assign_executor(task_id, "m1-local-patcher")
        await tasks.assign_executor(task_id, "m1-local-patcher")
        await session.commit()

        assert await _column(session, task_id, "executor") == "m1-local-patcher"

    async def test_a_second_executor_is_refused(self, session: AsyncSession) -> None:
        """改派等于改写历史 —— 报错，而且**不改**。"""
        task_id = await _seed_task(session)
        tasks = TaskRepository(session)

        await tasks.assign_executor(task_id, "m1-local-patcher")
        await session.commit()

        with pytest.raises(AlreadyAssignedError):
            await tasks.assign_executor(task_id, "codex-cli")

        await session.commit()
        assert await _column(session, task_id, "executor") == "m1-local-patcher"


class TestTheWorkspaceIsRecordedOnTheTaskRow:
    """工作区与分支记在任务行上 —— 事后查得到"在哪儿干的"。"""

    async def test_the_branch_and_the_workspace_are_written_to_the_row(
        self, session: AsyncSession
    ) -> None:
        task_id = await _seed_task(session)
        tasks = TaskRepository(session)

        await tasks.assign_workspace(task_id, branch=f"agent/{task_id}", workspace_id=str(task_id))
        await session.commit()

        assert await _column(session, task_id, "branch") == f"agent/{task_id}"
        assert await _column(session, task_id, "workspace_id") == str(task_id)

    async def test_assigning_the_same_workspace_again_changes_nothing(
        self, session: AsyncSession
    ) -> None:
        task_id = await _seed_task(session)
        tasks = TaskRepository(session)

        await tasks.assign_workspace(task_id, branch="agent/x", workspace_id="ws-1")
        await tasks.assign_workspace(task_id, branch="agent/x", workspace_id="ws-1")
        await session.commit()

        assert await _column(session, task_id, "workspace_id") == "ws-1"

    async def test_a_second_workspace_is_refused(self, session: AsyncSession) -> None:
        task_id = await _seed_task(session)
        tasks = TaskRepository(session)

        await tasks.assign_workspace(task_id, branch="agent/x", workspace_id="ws-1")
        await session.commit()

        with pytest.raises(AlreadyAssignedError):
            await tasks.assign_workspace(task_id, branch="agent/y", workspace_id="ws-2")

        await session.commit()
        assert await _column(session, task_id, "workspace_id") == "ws-1"
        assert await _column(session, task_id, "branch") == "agent/x"


class TestTheRefusalSaysWhatHappened:
    """报错要说清楚"哪个任务、哪一栏、原来是谁、要改成谁"。"""

    async def test_the_error_names_the_task_and_both_values(self, session: AsyncSession) -> None:
        task_id = await _seed_task(session)
        tasks = TaskRepository(session)

        await tasks.assign_executor(task_id, "m1-local-patcher")
        await session.commit()

        with pytest.raises(AlreadyAssignedError) as caught:
            await tasks.assign_executor(task_id, "codex-cli")

        error = caught.value
        assert error.task_id == task_id
        assert error.field == "执行器"
        assert error.assigned == "m1-local-patcher"
        assert error.requested == "codex-cli"
        assert "m1-local-patcher" in str(error)
        assert "codex-cli" in str(error)
