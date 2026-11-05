"""TASK-P1-022 —— Candidate / Decision / Task 三表分离的强制约束。

验证（T1）：
① 直接 ``INSERT INTO task`` 省略 ``decision_id`` 被**数据库**拒绝
   （用原生 SQL 绕过应用层验证）；
② 插入 Decision 时引用不存在的 ``candidate_id`` 被拒绝；
③ Task 无旁路写入路径（仓储层不提供不经 Decision 直接插入 Task 的方法）。

这些断言里最关键的是 ① 的"被数据库拒绝"。如果只是"被应用层校验拦下"，
那么任何一条绕过 ORM 的 SQL 都能写出无决策来源的任务 —— 审批门、预算门、
审计就都形同虚设了。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.database.repositories import (
    DECISION_DEFER,
    DECISION_RECORD,
    DECISION_REJECT,
    CandidateRepository,
    DecisionRepository,
    NonExecutableDecisionError,
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


class TestDecisionCannotBeInvented:
    """② Decision 必须挂在真实候选上。"""

    async def test_decision_with_unknown_candidate_is_rejected(self, session: AsyncSession) -> None:
        await _insert_project(session)
        ghost_candidate = uuid.uuid4()

        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text(
                    """
                    INSERT INTO evolution_decision
                        (candidate_id, project_id, decision, rationale,
                         requested_analysis, id, created_at, updated_at)
                    VALUES
                        (:candidate_id, 'p-demo', 'record', '凭空来的决策',
                         CAST('[]' AS jsonb), gen_random_uuid(), now(), now())
                    """
                ),
                {"candidate_id": ghost_candidate},
            )
        await session.rollback()

        message = str(exc_info.value)
        assert "evolution_candidate" in message
        assert "foreign key" in message.lower()

    async def test_decision_without_candidate_id_is_rejected(self, session: AsyncSession) -> None:
        """candidate_id 本身也是 NOT NULL —— 决策不能没有候选。"""
        await _insert_project(session)

        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text(
                    """
                    INSERT INTO evolution_decision
                        (project_id, decision, rationale, requested_analysis,
                         id, created_at, updated_at)
                    VALUES
                        ('p-demo', 'record', '没有候选', CAST('[]' AS jsonb),
                         gen_random_uuid(), now(), now())
                    """
                )
            )
        await session.rollback()

        assert "candidate_id" in str(exc_info.value)


class TestTaskCannotBypassDecision:
    """① Task 必须有决策来源，且由数据库强制。"""

    async def test_raw_insert_without_decision_id_is_rejected(self, session: AsyncSession) -> None:
        """**用原生 SQL 绕过应用层** —— 数据库本身要拒绝。"""
        await _insert_project(session)

        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text(
                    """
                    INSERT INTO task
                        (project_id, title, type, priority, status, repair_count,
                         extra, id, created_at, updated_at)
                    VALUES
                        ('p-demo', '偷偷建的任务', 'bugfix', 0, 'APPROVED', 0,
                         CAST('{}' AS jsonb), gen_random_uuid(), now(), now())
                    """
                )
            )
        await session.rollback()

        message = str(exc_info.value)
        assert "decision_id" in message
        assert "null value" in message.lower()

    async def test_raw_insert_with_unknown_decision_id_is_rejected(
        self, session: AsyncSession
    ) -> None:
        await _insert_project(session)

        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text(
                    """
                    INSERT INTO task
                        (project_id, decision_id, title, type, priority, status,
                         repair_count, extra, id, created_at, updated_at)
                    VALUES
                        ('p-demo', :decision_id, '指向不存在的决策', 'bugfix', 0,
                         'APPROVED', 0, CAST('{}' AS jsonb), gen_random_uuid(),
                         now(), now())
                    """
                ),
                {"decision_id": uuid.uuid4()},
            )
        await session.rollback()

        assert "evolution_decision" in str(exc_info.value)


class TestRepositoryIsTheOnlyEntry:
    """③ 仓储层是任务唯一的创建入口。"""

    async def test_task_is_created_from_a_record_decision(self, session: AsyncSession) -> None:
        await _insert_project(session)
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)
        tasks = TaskRepository(session)

        candidate = await candidates.create(
            project_id="p-demo",
            category="quality",
            title="补一个缺失的回归测试",
            evidence=[{"kind": "coverage_report", "ref": "artifact://report-1"}],
            value=Decimal("0.6"),
            risk=Decimal("0.2"),
            confidence=Decimal("0.8"),
        )
        decision = await decisions.record(
            candidate_id=candidate.id,
            decision=DECISION_RECORD,
            rationale="证据充分，成本低",
        )
        task = await tasks.create_from_decision(
            decision.id,
            title="补一个缺失的回归测试",
            task_type="test",
        )
        await session.commit()

        assert task.decision_id == decision.id
        assert task.project_id == "p-demo"

        # 落库后再读一次，确认外键真的连上了
        stored = await tasks.get(task.id)
        assert stored.decision.candidate_id == candidate.id

    async def test_defer_decision_cannot_produce_a_task(self, session: AsyncSession) -> None:
        """defer 是合法的"不行动"，但那一轮**不创建任务**（R-034 / R-035）。"""
        await _insert_project(session)
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)
        tasks = TaskRepository(session)

        candidate = await candidates.create(
            project_id="p-demo",
            category="architecture",
            title="拆分权限模块",
            evidence=[{"kind": "coupling_report", "ref": "artifact://coupling"}],
            confidence=Decimal("0.1"),
        )
        decision = await decisions.record(
            candidate_id=candidate.id,
            decision=DECISION_DEFER,
            rationale="证据不足，先补充分析",
            requested_analysis=[{"kind": "benchmark", "description": "量化模块耦合度"}],
        )
        await session.commit()

        with pytest.raises(NonExecutableDecisionError) as exc_info:
            await tasks.create_from_decision(decision.id, title="不该被创建", task_type="refactor")
        await session.rollback()

        assert exc_info.value.decision == DECISION_DEFER

    async def test_reject_decision_cannot_produce_a_task(self, session: AsyncSession) -> None:
        await _insert_project(session)
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)
        tasks = TaskRepository(session)

        candidate = await candidates.create(
            project_id="p-demo",
            category="technology",
            title="升级到最新的框架版本",
            evidence=[{"kind": "release_note", "ref": "issue://42"}],
        )
        decision = await decisions.record(
            candidate_id=candidate.id,
            decision=DECISION_REJECT,
            rationale="对当前项目没有实际收益",
        )
        await session.commit()

        with pytest.raises(NonExecutableDecisionError):
            await tasks.create_from_decision(decision.id, title="不该被创建", task_type="upgrade")
        await session.rollback()

    async def test_task_table_is_empty_when_only_defer_happened(
        self, session: AsyncSession
    ) -> None:
        """整轮只有 defer 决策时，任务表必须是空的。"""
        await _insert_project(session)
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)

        candidate = await candidates.create(
            project_id="p-demo",
            category="product",
            title="补一个缺失的核心功能",
            evidence=[{"kind": "user_feedback", "ref": "issue://7"}],
        )
        await decisions.record(
            candidate_id=candidate.id,
            decision=DECISION_DEFER,
            rationale="需求还不清楚",
            requested_analysis=["访谈三个真实用户"],
        )
        await session.commit()

        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 0
