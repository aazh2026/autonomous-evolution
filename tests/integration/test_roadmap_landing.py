"""TASK-P1-056 —— 路线图落库的端到端验证。

两条 T1：

1. **拆解产生的每个 Task 都能追溯到一个 EvolutionDecision（断言外键）** ——
   这里验的是**数据库里那一行**：``task.decision_id`` 必须指向那条决策行。
   领域模型里挂着决策 id 是"我们自己说好"，落到数据库上才是结构上的保证。
2. **未被 Critic 放行的 Decision 不产生 Task** —— 落在**任务表里到底有没有
   东西**上。

**为什么还要在仓储层再挡一道。** 拆解器（TASK-P1-056 的领域层）已经会把没
放行的决策挡在外面，但拆解器是应用层的一环 —— 它可以被绕过、可以被改错，也
可能压根不在那条路径上。仓储层是最后一道：它挡的是"代码写错了"这种再普通
不过的情况。所以这一组用例里有一条专门试**被推翻的决策**（``overridden_by``
非空）—— 拆解器那层没参与，直接打仓储层。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.evolution.candidate import EvolutionCandidate
from core.evolution.decision import (
    DecisionPolicy,
    DecisionRound,
    DecisionState,
    EvolutionDecision,
)
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.planning.roadmap import (
    DecisionApproval,
    TaskDecomposer,
    TaskDraft,
    TaskType,
)
from infrastructure.database.repositories import (
    CandidateRepository,
    DecisionRepository,
    InvalidDecisionError,
    MissingDecisionRowError,
    NonExecutableDecisionError,
    NotFoundError,
    OverriddenDecisionError,
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


async def _seed_decision(
    session: AsyncSession,
    *,
    title: str = "拆分权限模块",
    decision_id: str = "EVD-cycle-0007-001",
    overridden_by: str | None = None,
) -> tuple[EvolutionDecision, uuid.UUID]:
    """登记一条候选与一条 ``record`` 决策。返回（决策领域对象，决策行 id）。"""
    candidates = CandidateRepository(session)
    decisions = DecisionRepository(session)

    candidate = await candidates.create(
        project_id="p-demo",
        category="architecture",
        title=title,
        evidence=[{"kind": "artifact", "ref": "artifact://coupling"}],
        confidence=Decimal("0.9"),
    )
    scored = _scored(str(candidate.id))
    decision = DecisionPolicy().decide(scored, decision_id=decision_id)
    assert decision.decision is DecisionState.RECORD

    row = await decisions.record(
        candidate_id=candidate.id,
        decision=decision.decision.value,
        rationale=decision.rationale,
        overridden_by=overridden_by,
    )
    await session.commit()
    return decision, row.id


def _approval(decision_id: str, candidate_id: str) -> DecisionApproval:
    return DecisionApproval(
        decision_id=decision_id,
        candidate_id=candidate_id,
        approved=True,
        reason="九项检查全部通过，无否决",
    )


class TestEveryLandedTaskTracesBackToADecision:
    """T1 ① —— 每个任务都能追溯到一个 EvolutionDecision（断言外键）。"""

    async def test_the_task_row_points_at_the_decision_row(self, session: AsyncSession) -> None:
        await _insert_project(session)
        decision, row_id = await _seed_decision(session)
        tasks = TaskRepository(session)

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0007",
            decision_round=DecisionRound(cycle_id="cycle-0007", decisions=(decision,)),
            approvals=[_approval(decision.id, decision.candidate_id)],
            drafts=[
                TaskDraft(
                    key="T-1",
                    decision_id=decision.id,
                    task_type=TaskType.REFACTORING,
                    title="拆分权限模块",
                    rationale="耦合度高于阈值",
                )
            ],
        )

        landed = await tasks.land_roadmap(roadmap, decision_rows={decision.id: row_id})
        await session.commit()

        assert len(landed) == 1
        # 外键断言 —— 数据库里那一列真的指向那条决策行。
        assert landed[0].decision_id == row_id

        stored = await session.execute(
            text("SELECT decision_id FROM task WHERE id = :task_id"),
            {"task_id": landed[0].id},
        )
        assert stored.scalar_one() == row_id

    async def test_the_foreign_key_is_enforced_by_the_database(self, session: AsyncSession) -> None:
        """数据库自己是那道保证 —— 这里直接试着插一条挂空决策的任务。

        绕过仓储层写 SQL 是最坏的情况 —— 它证明这道保证**不依赖**应用层的
        自觉。
        """
        await _insert_project(session)

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO task
                        (id, project_id, decision_id, title, type, priority, status,
                         repair_count, extra, created_at, updated_at)
                    VALUES
                        (:id, 'p-demo', :decision_id, '孤儿任务',
                         'feature', 0, 'APPROVED', 0, '{}'::jsonb, now(), now())
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "decision_id": uuid.uuid4(),
                },
            )

    async def test_a_task_cannot_be_created_without_a_decision(self, session: AsyncSession) -> None:
        """仓储层没有旁路 —— 缺决策就构造不出任务。"""
        await _insert_project(session)
        tasks = TaskRepository(session)

        with pytest.raises(NotFoundError):
            await tasks.create_from_decision(
                uuid.UUID("00000000-0000-0000-0000-000000000000"),
                title="无源任务",
                task_type="feature",
            )

    async def test_the_landed_task_carries_the_roadmap_context(self, session: AsyncSession) -> None:
        """落库时把路线图上的东西一起带上 —— 否则路线图与任务表对不上。"""
        await _insert_project(session)
        decision, row_id = await _seed_decision(session)
        tasks = TaskRepository(session)

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0007",
            decision_round=DecisionRound(cycle_id="cycle-0007", decisions=(decision,)),
            approvals=[_approval(decision.id, decision.candidate_id)],
            drafts=[
                TaskDraft(
                    key="T-1",
                    decision_id=decision.id,
                    task_type=TaskType.PERFORMANCE,
                    title="拆分权限模块",
                    rationale="耦合度高于阈值",
                )
            ],
        )

        landed = await tasks.land_roadmap(roadmap, decision_rows={decision.id: row_id})
        await session.commit()

        extra = landed[0].extra
        assert extra["roadmap_key"] == "T-1"
        assert extra["cycle_id"] == "cycle-0007"
        assert extra["rationale"] == "耦合度高于阈值"
        # 验收重点随类型走 —— 七种类型、七种验收重点。
        assert extra["acceptance_focus"] == "优化前后的基准对比"
        assert landed[0].type == "performance"

    async def test_dependencies_are_written_as_rows(self, session: AsyncSession) -> None:
        """依赖不只是一份领域模型 —— 落库时要有对应的行。"""
        await _insert_project(session)
        decision, row_id = await _seed_decision(session)
        tasks = TaskRepository(session)

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0007",
            decision_round=DecisionRound(cycle_id="cycle-0007", decisions=(decision,)),
            approvals=[_approval(decision.id, decision.candidate_id)],
            drafts=[
                TaskDraft(
                    key="T-1",
                    decision_id=decision.id,
                    task_type=TaskType.FEATURE,
                    title="先做这件",
                ),
                TaskDraft(
                    key="T-2",
                    decision_id=decision.id,
                    task_type=TaskType.FEATURE,
                    title="后做这件",
                    depends_on=("T-1",),
                ),
            ],
        )

        landed = await tasks.land_roadmap(roadmap, decision_rows={decision.id: row_id})
        await session.commit()

        assert len(landed) == 2
        count = await session.execute(text("SELECT count(*) FROM task_dependency"))
        assert count.scalar_one() == 1

        edge = await session.execute(
            text("SELECT predecessor_id, successor_id FROM task_dependency")
        )
        predecessor, successor = edge.one()
        assert predecessor == landed[0].id
        assert successor == landed[1].id

    async def test_an_empty_roadmap_lands_nothing(self, session: AsyncSession) -> None:
        await _insert_project(session)
        tasks = TaskRepository(session)

        decision, row_id = await _seed_decision(session)
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0007",
            decision_round=DecisionRound(cycle_id="cycle-0007", decisions=(decision,)),
            approvals=[],
            drafts=[
                TaskDraft(
                    key="T-1",
                    decision_id=decision.id,
                    task_type=TaskType.FEATURE,
                    title="不该有的任务",
                )
            ],
        )

        landed = await tasks.land_roadmap(roadmap, decision_rows={decision.id: row_id})
        await session.commit()

        assert landed == []
        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 0


class TestNoApprovalMeansNoTask:
    """T1 ④ —— 未被 Critic 放行的 Decision 不产生 Task。"""

    async def test_a_decision_without_a_credential_produces_no_task(
        self, session: AsyncSession
    ) -> None:
        """**缺凭据与被否决一样**，都是不放行 —— 关不上就不开。"""
        await _insert_project(session)
        decision, row_id = await _seed_decision(session)
        tasks = TaskRepository(session)

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0007",
            decision_round=DecisionRound(cycle_id="cycle-0007", decisions=(decision,)),
            approvals=[],
            drafts=[
                TaskDraft(
                    key="T-1",
                    decision_id=decision.id,
                    task_type=TaskType.FEATURE,
                    title="不该有的任务",
                )
            ],
        )

        assert roadmap.total == 0
        landed = await tasks.land_roadmap(roadmap, decision_rows={decision.id: row_id})
        await session.commit()

        assert landed == []
        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 0, "未经放行的决策不得派生任务"

    async def test_a_vetoed_decision_produces_no_task(self, session: AsyncSession) -> None:
        await _insert_project(session)
        decision, row_id = await _seed_decision(session)
        tasks = TaskRepository(session)

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0007",
            decision_round=DecisionRound(cycle_id="cycle-0007", decisions=(decision,)),
            approvals=[
                DecisionApproval(
                    decision_id=decision.id,
                    candidate_id=decision.candidate_id,
                    approved=False,
                    reason="Critic 否决 —— 是否触碰项目 Non-goals：触碰了 NG-001",
                )
            ],
            drafts=[
                TaskDraft(
                    key="T-1",
                    decision_id=decision.id,
                    task_type=TaskType.FEATURE,
                    title="不该有的任务",
                )
            ],
        )

        assert roadmap.total == 0
        landed = await tasks.land_roadmap(roadmap, decision_rows={decision.id: row_id})
        await session.commit()

        assert landed == []
        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 0

    async def test_an_overridden_decision_cannot_produce_a_task(
        self, session: AsyncSession
    ) -> None:
        """**最后一道闸。** 被推翻的决策哪怕被错传进来，也不派生任务。

        这条用例绕开了拆解器 —— 直接打仓储层，因为仓储层才是那道兜底的闸。
        """
        await _insert_project(session)
        _, row_id = await _seed_decision(session, overridden_by="critic")
        tasks = TaskRepository(session)

        with pytest.raises(OverriddenDecisionError):
            await tasks.create_from_decision(
                row_id,
                title="不该有的任务",
                task_type="feature",
            )

        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 0, "被推翻的决策不得派生任务"

    async def test_a_decision_overturned_before_deriving_cannot_produce_a_task(
        self, session: AsyncSession
    ) -> None:
        """**顺序也要对。** 先记决策、后推翻、再想派生任务 —— 一样派生不出来。

        推翻是**后来发生的事件**：决策先落库成了证据，审查层之后才推翻它。
        闸门在派生那一刻看状态，所以这条路径也必须挡住 —— 否则"先记后推"
        能绕过闸门，而"生下来就带着推翻"反而挡得住，本末倒置。
        """
        await _insert_project(session)
        _, row_id = await _seed_decision(session)
        decisions = DecisionRepository(session)

        await decisions.overturn(row_id, by="critic")
        await session.commit()

        tasks = TaskRepository(session)
        with pytest.raises(OverriddenDecisionError):
            await tasks.create_from_decision(
                row_id,
                title="不该有的任务",
                task_type="feature",
            )

        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 0, "被推翻的决策不得派生任务"

    async def test_a_task_derived_before_the_overturn_stands(self, session: AsyncSession) -> None:
        """**反过来也一样。** 先派生的任务不会因为后来的推翻而消失。

        那是真做过的活 —— 撤不回来，也不该在账上凭空少一笔。撤掉的只是
        "这条决策还算不算被采纳"，那是指标层的事。
        """
        await _insert_project(session)
        _, row_id = await _seed_decision(session)
        tasks = TaskRepository(session)

        landed = await tasks.create_from_decision(
            row_id,
            title="已经做过的活",
            task_type="feature",
        )
        decisions = DecisionRepository(session)
        await decisions.overturn(row_id, by="critic")
        await session.commit()

        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 1, "已经派生的任务不该凭空消失"

        stored = await session.execute(
            text("SELECT overridden_by FROM evolution_decision WHERE id = :decision_id"),
            {"decision_id": row_id},
        )
        assert stored.scalar_one() == "critic"
        assert landed.decision_id == row_id

    async def test_a_deferred_decision_cannot_produce_a_task(self, session: AsyncSession) -> None:
        """defer 仍然是不行动 —— 仓储层这一道也不许它派生任务。"""
        await _insert_project(session)
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)
        tasks = TaskRepository(session)

        candidate = await candidates.create(
            project_id="p-demo",
            category="architecture",
            title="拆分权限模块",
            evidence=[{"kind": "artifact", "ref": "artifact://coupling"}],
            confidence=Decimal("0.1"),
        )
        row = await decisions.record(
            candidate_id=candidate.id,
            decision="defer",
            rationale="证据不足，先补分析",
            requested_analysis=[{"kind": "measurement", "description": "量一次耦合度"}],
        )
        await session.commit()

        with pytest.raises(NonExecutableDecisionError):
            await tasks.create_from_decision(
                row.id,
                title="不该有的任务",
                task_type="feature",
            )

        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 0


class TestOverturningADecision:
    """推翻是一次**后来发生的事件** —— 它自己也得有规矩。"""

    async def test_the_overturn_must_name_who_overturned(self, session: AsyncSession) -> None:
        """ "被推翻了"却不说是谁推的 —— 事后没人说得清这决策是怎么作废的。"""
        await _insert_project(session)
        _, row_id = await _seed_decision(session)
        decisions = DecisionRepository(session)

        with pytest.raises(InvalidDecisionError):
            await decisions.overturn(row_id, by="   ")

    async def test_a_deferred_decision_cannot_be_overturned(self, session: AsyncSession) -> None:
        """defer 本来就不行动 —— 没有"把它推翻"这回事。

        允许推翻 defer，等于让一条"先补分析"的决策变成"已经作废" —— 而那
        两句话对读的人意味着完全不同的事。
        """
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
        row = await decisions.record(
            candidate_id=candidate.id,
            decision="defer",
            rationale="证据不足，先补分析",
            requested_analysis=[{"kind": "measurement", "description": "量一次耦合度"}],
        )
        await session.commit()

        with pytest.raises(InvalidDecisionError):
            await decisions.overturn(row.id, by="critic")

        stored = await session.execute(
            text("SELECT overridden_by FROM evolution_decision WHERE id = :decision_id"),
            {"decision_id": row.id},
        )
        assert stored.scalar_one() is None, "没推翻成就不该留下痕迹"


class TestTheDecisionRowMappingIsChecked:
    """领域 id 与数据库 uuid 对不上时直接报错。"""

    async def test_a_missing_decision_row_is_refused(self, session: AsyncSession) -> None:
        """猜错就是任务挂到了别的决策上 —— 而那不会报错，所以这里要报错。"""
        await _insert_project(session)
        decision, _row_id = await _seed_decision(session)
        tasks = TaskRepository(session)

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0007",
            decision_round=DecisionRound(cycle_id="cycle-0007", decisions=(decision,)),
            approvals=[_approval(decision.id, decision.candidate_id)],
            drafts=[
                TaskDraft(
                    key="T-1",
                    decision_id=decision.id,
                    task_type=TaskType.FEATURE,
                    title="一件任务",
                )
            ],
        )

        with pytest.raises(MissingDecisionRowError) as caught:
            await tasks.land_roadmap(roadmap, decision_rows={})

        assert caught.value.decision_ids == (decision.id,)

        count = await session.execute(text("SELECT count(*) FROM task"))
        assert count.scalar_one() == 0, "对不上就不能落库 —— 半路落一半更糟"
