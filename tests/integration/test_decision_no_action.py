"""TASK-P1-054 —— "不行动"的端到端落地。

验证（T1 ①）：``confidence = 0.1`` 的候选 → **系统产出 defer 决策**、写入
rationale、**且任务表中无对应 Task**。

这一条断言的关键在最后半句。defer 与 reject 都是合法的"不行动"，但"合法"
不等于"随便"：如果一条 defer 决策最后还是派生出了任务，那这个决策就只是在
**流程上**走了一遍 defer，实质上还是做了 —— 记录与现实对不上，下游读记录
的人会以为"这轮没做"，而代码仓库里已经有一份改动。

所以这里不看"决策写对没有"，看**任务表里到底有没有东西**。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.evolution.candidate import EvolutionCandidate
from core.evolution.decision import DecisionPolicy, DecisionState
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from infrastructure.database.repositories import (
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


def _scored(candidate_id: str, confidence: str) -> ScoredCandidate:
    """把一条置信度 ``confidence`` 的候选装配成已评分的候选。"""
    candidate = EvolutionCandidate(
        id=candidate_id,
        category=OpportunityCategory.ARCHITECTURE,
        title="拆分权限模块",
        evidence=(Evidence(kind=EvidenceKind.ARTIFACT, ref="artifact://coupling"),),
        hypothesis="拆分权限领域服务可以降低模块耦合。",
        expected_outcome=("降低模块依赖数量",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal(confidence),
    )
    factors = PriorityFactors.from_candidate(
        candidate,
        value=Decimal("0.8"),
        impact=Decimal("0.6"),
        urgency=Decimal("0.5"),
        cost=Decimal("0.5"),
    )
    return ScoredCandidate(candidate_id=candidate_id, factors=factors)


class TestLowConfidenceProducesADeferAndNoTask:
    """T1 ① —— 证据不足的候选：记一条 defer，那一轮不创建任务。"""

    async def test_a_low_confidence_candidate_is_deferred_and_leaves_no_task(
        self, session: AsyncSession
    ) -> None:
        await _insert_project(session)
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)

        candidate = await candidates.create(
            project_id="p-demo",
            category="architecture",
            title="拆分权限模块",
            evidence=[{"kind": "artifact", "ref": "artifact://coupling"}],
            confidence=Decimal("0.1"),
        )
        scored = _scored(str(candidate.id), confidence="0.1")

        decision = DecisionPolicy().decide(scored, decision_id="EVD-cycle-0007-001")
        assert decision.decision is DecisionState.DEFER
        assert decision.produces_task is False

        stored = await decisions.record(
            candidate_id=candidate.id,
            decision=decision.decision.value,
            rationale=decision.rationale,
            requested_analysis=decision.analysis_payload(),
        )
        await session.commit()

        assert stored.rationale.strip(), "defer 决策必须写明理由"
        assert stored.requested_analysis, "defer 决策必须留下还要补什么"

        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 0, "defer 那一轮不得创建任务"

    async def test_the_candidate_is_marked_as_deferred(self, session: AsyncSession) -> None:
        """候选自身的状态也要跟着改 —— 否则读候选的人以为它还悬着。"""
        await _insert_project(session)
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)

        candidate = await candidates.create(
            project_id="p-demo",
            category="architecture",
            title="拆分权限模块",
            evidence=[{"kind": "artifact", "ref": "artifact://coupling"}],
            confidence=Decimal("0.1"),
        )
        scored = _scored(str(candidate.id), confidence="0.1")
        decision = DecisionPolicy().decide(scored, decision_id="EVD-cycle-0007-001")

        await decisions.record(
            candidate_id=candidate.id,
            decision=decision.decision.value,
            rationale=decision.rationale,
            requested_analysis=decision.analysis_payload(),
        )
        await session.commit()

        stored_candidate = await candidates.get(candidate.id)
        assert stored_candidate.status == "deferred"

    async def test_the_stored_decision_keeps_the_analysis_items(
        self, session: AsyncSession
    ) -> None:
        """补充分析项要真的落库 —— 它是下一轮的待办。"""
        await _insert_project(session)
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)

        candidate = await candidates.create(
            project_id="p-demo",
            category="architecture",
            title="拆分权限模块",
            evidence=[{"kind": "artifact", "ref": "artifact://coupling"}],
            confidence=Decimal("0.1"),
        )
        scored = _scored(str(candidate.id), confidence="0.1")
        decision = DecisionPolicy().decide(scored, decision_id="EVD-cycle-0007-001")

        stored = await decisions.record(
            candidate_id=candidate.id,
            decision=decision.decision.value,
            rationale=decision.rationale,
            requested_analysis=decision.analysis_payload(),
        )
        await session.commit()

        first = stored.requested_analysis[0]
        assert isinstance(first, dict)
        assert first["kind"] == "measurement"
        assert str(first["description"]).strip()


class TestTheContrast:
    """对照组 —— record 决策**会**派生任务。

    没有这条，"任务表是空的"就可能只是因为这条链路上根本没接到任务那一步，
    断言看着成立，其实什么都没验到。
    """

    async def test_a_record_decision_does_produce_a_task(self, session: AsyncSession) -> None:
        await _insert_project(session)
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)
        tasks = TaskRepository(session)

        candidate = await candidates.create(
            project_id="p-demo",
            category="quality",
            title="补一个缺失的回归测试",
            evidence=[{"kind": "artifact", "ref": "artifact://coverage"}],
            confidence=Decimal("0.9"),
        )
        scored = _scored(str(candidate.id), confidence="0.9")
        decision = DecisionPolicy().decide(scored, decision_id="EVD-cycle-0007-002")
        assert decision.decision is DecisionState.RECORD

        stored = await decisions.record(
            candidate_id=candidate.id,
            decision=decision.decision.value,
            rationale=decision.rationale,
        )
        await tasks.create_from_decision(
            stored.id,
            title="补一个缺失的回归测试",
            task_type="test",
        )
        await session.commit()

        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 1
