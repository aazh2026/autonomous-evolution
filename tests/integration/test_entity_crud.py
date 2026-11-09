"""TASK-P1-020 —— 十二个核心实体的 schema 与 CRUD 往返。

验证（T1）：对每个实体做一轮 CRUD 往返；断言关键字段存在
（``Candidate.evidence``/``value``/``risk``、``Decision.decision``/``rationale``、
``Artifact.uri``/``hash``/``type``、``Snapshot.version``/``commit``/``metrics``、
``TaskDependency.predecessor``/``successor``）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import (
    ALL_ENTITIES,
    AgentSession,
    ArchitectureDecision,
    Artifact,
    EvolutionCandidate,
    EvolutionCycle,
    EvolutionDecision,
    ExecutionEvent,
    Project,
    ProjectSnapshot,
    Task,
    TaskDependency,
    VerificationRun,
)
from core.metrics.cycle import CycleCounts, CycleStatus
from core.metrics.cycle import EvolutionCycle as CycleRecord
from infrastructure.database.repositories import (
    DECISION_RECORD,
    CandidateRepository,
    DecisionRepository,
    InvalidDecisionError,
    TaskDependencyRepository,
    TaskRepository,
)

pytestmark = pytest.mark.integration

_SHA256 = "a" * 64


async def _seed_chain(session: AsyncSession) -> dict[str, object]:
    """建出候选 → 决策 → 任务这条主链，后面所有实体都挂在它上面。"""
    project = Project(
        id="p-crud",
        name="CRUD 演示项目",
        goal={"objective": [{"id": "OBJ-1", "description": "把系统跑通"}]},
        policy={"merge_to_main": "approval_required"},
        budget={"currency": "USD"},
        repository_url="https://example.invalid/repo.git",
        tech_stack=["python"],
    )
    session.add(project)
    await session.flush()

    cycle = EvolutionCycle.from_domain(
        CycleRecord.build(
            project_id=project.id,
            cycle_id="cycle-0001",
            trigger="schedule",
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            status=CycleStatus.COMPLETED,
            counts=CycleCounts(candidates=1, decisions=1, tasks=1, completed_tasks=1),
        )
    )
    session.add(cycle)
    await session.flush()

    candidates = CandidateRepository(session)
    decisions = DecisionRepository(session)
    tasks = TaskRepository(session)

    candidate = await candidates.create(
        project_id=project.id,
        category="quality",
        title="补一个缺失的回归测试",
        evidence=[{"kind": "coverage_report", "ref": "artifact://report-1"}],
        value=Decimal("0.6"),
        risk=Decimal("0.2"),
        confidence=Decimal("0.8"),
        cycle_id=cycle.id,
    )
    decision = await decisions.record(
        candidate_id=candidate.id,
        decision=DECISION_RECORD,
        rationale="证据充分",
        cycle_id=cycle.id,
    )
    task = await tasks.create_from_decision(
        decision.id,
        title="补一个缺失的回归测试",
        task_type="test",
        priority=Decimal("70"),
    )
    await session.commit()
    return {
        "project": project,
        "cycle": cycle,
        "candidate": candidate,
        "decision": decision,
        "task": task,
    }


class TestProjectAndSnapshot:
    """Project / ProjectSnapshot。"""

    async def test_project_roundtrip(self, session: AsyncSession) -> None:
        project = Project(
            id="p-round",
            name="往返项目",
            goal={"objective": []},
            policy={},
            budget={},
            repository_url="https://example.invalid/x.git",
            environment={"stage": "staging", "region": "cn-north-1"},
        )
        session.add(project)
        await session.commit()

        loaded = await session.get(Project, "p-round")
        assert loaded is not None
        assert loaded.name == "往返项目"
        assert loaded.repository_url == "https://example.invalid/x.git"
        # 环境配置必须往返不丢 —— 领域模型里有、库里没有的话，
        # 落库这一层会把它静默吃掉。
        assert loaded.environment == {"stage": "staging", "region": "cn-north-1"}

        loaded.name = "改名后的项目"
        await session.commit()
        reloaded = await session.get(Project, "p-round")
        assert reloaded is not None
        assert reloaded.name == "改名后的项目"

    async def test_project_environment_defaults_to_empty(self, session: AsyncSession) -> None:
        """没给环境配置的项目也是合法的 —— 不该因为补了一列就被判非法。"""
        project = Project(
            id="p-no-env",
            name="无环境项目",
            goal={"objective": []},
            policy={},
            budget={},
            repository_url="https://example.invalid/y.git",
        )
        session.add(project)
        await session.commit()

        loaded = await session.get(Project, "p-no-env")
        assert loaded is not None
        assert loaded.environment == {}

    async def test_snapshot_carries_version_commit_and_metrics(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        project = chain["project"]
        assert isinstance(project, Project)

        snapshot = ProjectSnapshot(
            project_id=project.id,
            version=1,
            commit="c" * 40,
            branch="main",
            metrics={"coverage": 0.82, "open_defects": 3},
        )
        session.add(snapshot)
        await session.commit()

        loaded = await session.get(ProjectSnapshot, snapshot.id)
        assert loaded is not None
        assert loaded.version == 1
        assert loaded.commit == "c" * 40
        assert loaded.metrics["coverage"] == 0.82

    async def test_snapshot_version_is_unique_per_project(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        project = chain["project"]
        assert isinstance(project, Project)

        for _ in range(2):
            session.add(
                ProjectSnapshot(project_id=project.id, version=7, commit="d" * 40, metrics={})
            )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


class TestEvolutionEntities:
    """EvolutionCandidate / EvolutionDecision / EvolutionCycle。"""

    async def test_candidate_keeps_evidence_value_and_risk(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        candidate = chain["candidate"]
        assert isinstance(candidate, EvolutionCandidate)

        loaded = await session.get(EvolutionCandidate, candidate.id)
        assert loaded is not None
        assert loaded.evidence[0]["ref"] == "artifact://report-1"
        assert loaded.value == Decimal("0.6000")
        assert loaded.risk == Decimal("0.2000")
        assert loaded.category == "quality"

    async def test_decision_keeps_decision_and_rationale(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        decision = chain["decision"]
        assert isinstance(decision, EvolutionDecision)

        loaded = await session.get(EvolutionDecision, decision.id)
        assert loaded is not None
        assert loaded.decision == "record"
        assert loaded.rationale == "证据充分"

    async def test_candidate_value_out_of_range_is_rejected(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        project = chain["project"]
        assert isinstance(project, Project)

        session.add(
            EvolutionCandidate(
                project_id=project.id,
                category="quality",
                title="越界的价值",
                evidence=[{"kind": "x", "ref": "artifact://x"}],
                value=Decimal("1.5"),
                risk=Decimal("0.1"),
                confidence=Decimal("0.1"),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_cycle_roundtrip(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        cycle = chain["cycle"]
        assert isinstance(cycle, EvolutionCycle)

        loaded = await session.get(EvolutionCycle, cycle.id)
        assert loaded is not None
        assert loaded.cycle_id == "cycle-0001"
        # 结局分两个维度：周期**怎么**结束、这一轮**有没有变好**。
        assert loaded.status == "completed"
        assert loaded.outcome == "no_change"

    async def test_cycle_outcome_values_are_enforced_by_the_database(
        self, session: AsyncSession
    ) -> None:
        """取值由 CHECK 约束把关 —— 不是只有应用层在管。

        有人直接写 SQL 把 ``outcome`` 改成自造的字符串时，周期记录就失去了
        作为审计依据的意义。
        """
        chain = await _seed_chain(session)
        cycle = chain["cycle"]
        assert isinstance(cycle, EvolutionCycle)
        cycle_id = cycle.id

        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text("UPDATE evolution_cycle SET outcome = 'pretty_good' WHERE id = :id"),
                {"id": cycle_id},
            )
        await session.rollback()

        assert "evolution_cycle_outcome_values" in str(exc_info.value)

    async def test_cycle_status_values_are_enforced_by_the_database(
        self, session: AsyncSession
    ) -> None:
        chain = await _seed_chain(session)
        cycle = chain["cycle"]
        assert isinstance(cycle, EvolutionCycle)
        cycle_id = cycle.id

        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text("UPDATE evolution_cycle SET status = 'finished' WHERE id = :id"),
                {"id": cycle_id},
            )
        await session.rollback()

        assert "evolution_cycle_status_values" in str(exc_info.value)


class TestTaskEntities:
    """Task / TaskDependency。"""

    async def test_task_roundtrip(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        task = chain["task"]
        assert isinstance(task, Task)

        loaded = await session.get(Task, task.id)
        assert loaded is not None
        assert loaded.status == "APPROVED"
        assert loaded.priority == Decimal("70.0000")

        loaded.status = "PLANNED"
        await session.commit()
        reloaded = await session.get(Task, task.id)
        assert reloaded is not None
        assert reloaded.status == "PLANNED"

    async def test_invalid_status_is_rejected_by_database(self, session: AsyncSession) -> None:
        """状态值由 CHECK 约束把关 —— 不是只有 Python 状态机在管。"""
        chain = await _seed_chain(session)
        task = chain["task"]
        assert isinstance(task, Task)
        task_id = task.id

        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text("UPDATE task SET status = 'NOT_A_STATE' WHERE id = :id"), {"id": task_id}
            )
        await session.rollback()

        assert "task_status_values" in str(exc_info.value)

    async def test_task_dependency_links_two_tasks(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        first = chain["task"]
        assert isinstance(first, Task)

        tasks = TaskRepository(session)
        second = await tasks.create_from_decision(
            first.decision_id,
            title="依赖前一个任务",
            task_type="refactor",
        )
        await session.commit()

        dependencies = TaskDependencyRepository(session)
        dependency = await dependencies.add(
            project_id="p-crud",
            predecessor_id=first.id,
            successor_id=second.id,
        )
        await session.commit()

        loaded = await session.get(TaskDependency, dependency.id)
        assert loaded is not None
        assert loaded.predecessor_id == first.id
        assert loaded.successor_id == second.id

        relation = await session.get(EvolutionDecision, first.decision_id)
        assert relation is not None

    async def test_self_dependency_is_rejected_by_repository(self, session: AsyncSession) -> None:
        """仓储层先拦一道 —— 给出可读的错误，而不是让数据库报约束名。"""
        chain = await _seed_chain(session)
        task = chain["task"]
        assert isinstance(task, Task)

        dependencies = TaskDependencyRepository(session)
        with pytest.raises(InvalidDecisionError):
            await dependencies.add(
                project_id="p-crud",
                predecessor_id=task.id,
                successor_id=task.id,
            )

    async def test_self_dependency_is_rejected_by_database(self, session: AsyncSession) -> None:
        """数据库也拦一道 —— 绕过仓储层写 SQL 也进不来。"""
        chain = await _seed_chain(session)
        task = chain["task"]
        assert isinstance(task, Task)
        task_id = task.id

        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text(
                    """
                    INSERT INTO task_dependency
                        (project_id, predecessor_id, successor_id, dependency_type,
                         created_at, id)
                    VALUES
                        ('p-crud', :task_id, :task_id, 'finish_to_start',
                         now(), gen_random_uuid())
                    """
                ),
                {"task_id": task_id},
            )
        await session.rollback()

        assert "no_self_dependency" in str(exc_info.value)


class TestExecutionEntities:
    """AgentSession / ExecutionEvent。"""

    async def test_agent_session_keeps_executor_and_session_id(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        task = chain["task"]
        assert isinstance(task, Task)

        agent_session = AgentSession(
            project_id="p-crud",
            task_id=task.id,
            agent_id="agent-analyst-01",
            executor="claude_code",
            session_id="sess-abc",
            authorization={
                "agent_id": "agent-analyst-01",
                "task_id": str(task.id),
                "project_id": "p-crud",
                "workspace_id": "ws-1",
                "expiry": "2026-11-07T00:00:00Z",
            },
        )
        session.add(agent_session)
        await session.commit()

        loaded = await session.get(AgentSession, agent_session.id)
        assert loaded is not None
        assert loaded.executor == "claude_code"
        assert loaded.session_id == "sess-abc"
        assert loaded.agent_id == "agent-analyst-01"
        assert loaded.task_id == task.id

    async def test_execution_event_keeps_type_and_payload(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        task = chain["task"]
        assert isinstance(task, Task)

        event = ExecutionEvent(
            task_id=task.id,
            project_id="p-crud",
            agent_id="agent-analyst-01",
            event_type="tool_call",
            payload={"tool": "Read", "path": "core/task/state_machine.py"},
            causation_id="cause-1",
            correlation_id="corr-1",
            sequence=1,
        )
        session.add(event)
        await session.commit()

        loaded = await session.get(ExecutionEvent, event.id)
        assert loaded is not None
        assert loaded.event_type == "tool_call"
        assert loaded.payload["tool"] == "Read"
        assert loaded.causation_id == "cause-1"
        assert loaded.correlation_id == "corr-1"

    async def test_events_can_be_queried_by_task(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        task = chain["task"]
        assert isinstance(task, Task)

        for index in range(3):
            session.add(
                ExecutionEvent(
                    task_id=task.id,
                    project_id="p-crud",
                    event_type="log",
                    payload={"index": index},
                )
            )
        await session.commit()

        result = await session.scalars(
            select(ExecutionEvent).where(ExecutionEvent.task_id == task.id)
        )
        assert len(list(result)) == 3


class TestEvidenceEntities:
    """VerificationRun / Artifact。"""

    async def test_verification_run_keeps_checks_and_result(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        task = chain["task"]
        assert isinstance(task, Task)

        run = VerificationRun(
            task_id=task.id,
            checks=[{"name": "unit_tests", "result": "passed"}],
            result="passed",
            verified_by="verifier-independent",
            duration_ms=1234,
        )
        session.add(run)
        await session.commit()

        loaded = await session.get(VerificationRun, run.id)
        assert loaded is not None
        assert loaded.result == "passed"
        assert loaded.checks[0]["name"] == "unit_tests"
        assert loaded.verified_by == "verifier-independent"

    async def test_artifact_keeps_uri_hash_and_type(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        task = chain["task"]
        assert isinstance(task, Task)

        artifact = Artifact(
            task_id=task.id,
            project_id="p-crud",
            uri="s3://ases-artifacts/2026/11/report.json",
            hash=_SHA256,
            type="test_report",
            size_bytes=2048,
            retention_until=datetime.now(UTC) + timedelta(days=90),
        )
        session.add(artifact)
        await session.commit()

        loaded = await session.get(Artifact, artifact.id)
        assert loaded is not None
        assert loaded.uri == "s3://ases-artifacts/2026/11/report.json"
        assert loaded.hash == _SHA256
        assert loaded.type == "test_report"


class TestArchitectureDecision:
    """ArchitectureDecision。"""

    async def test_adr_roundtrip(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        project = chain["project"]
        assert isinstance(project, Project)

        adr = ArchitectureDecision(
            project_id=project.id,
            context="用户模块同时承担鉴权与渲染，耦合度过高",
            decision="抽出独立的权限领域服务",
            tradeoff="多一层间接调用，调试路径变长",
            status="accepted",
        )
        session.add(adr)
        await session.commit()

        loaded = await session.get(ArchitectureDecision, adr.id)
        assert loaded is not None
        assert loaded.decision == "抽出独立的权限领域服务"
        assert loaded.tradeoff is not None

    async def test_adr_can_supersede_an_earlier_one(self, session: AsyncSession) -> None:
        chain = await _seed_chain(session)
        project = chain["project"]
        assert isinstance(project, Project)

        first = ArchitectureDecision(
            project_id=project.id,
            context="v1 背景",
            decision="v1 决策",
        )
        session.add(first)
        await session.flush()

        second = ArchitectureDecision(
            project_id=project.id,
            context="v2 背景",
            decision="v2 决策",
            supersedes_id=first.id,
        )
        session.add(second)
        await session.commit()

        loaded = await session.get(ArchitectureDecision, second.id)
        assert loaded is not None
        assert loaded.supersedes_id == first.id


class TestEntityCount:
    """实体总数与 design.md §3.6 的表格一致。

    十二个核心实体之外还有两张预算账本表（TASK-P1-012）、一张审批请求表
    （TASK-P1-046）、两张编排状态表（TASK-P1-050）与两张事件表（TASK-P1-025）
    —— 它们都不在 design.md 的清单里：预算那两张是设计文档的一处缺口，已在
    docs/tasks.md 第 6 节列为待裁决项 8；审批那张是 §7「代码合并 = L2 需要人工
    审批」必须落到的地方；checkpoint 那两张记的是编排器自己的运行状态，不是
    项目的业务事实；outbox 那两张是「状态写入与事件发布同事务」（I-06）必须
    落到的地方 —— 没有自己的表，那条要求就只能靠调用方自觉。
    """

    def test_twelve_core_entities_plus_the_budget_approval_checkpoint_and_event_tables_are_mapped(
        self,
    ) -> None:
        assert len(ALL_ENTITIES) == 19

    async def test_every_entity_has_a_table(self, session: AsyncSession) -> None:
        tables = await session.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"
            )
        )
        names = {row[0] for row in tables}
        assert names == {
            "project",
            "project_snapshot",
            "evolution_candidate",
            "evolution_decision",
            "task",
            "task_dependency",
            "agent_session",
            "execution_event",
            "verification_run",
            "artifact",
            "architecture_decision",
            "evolution_cycle",
            "budget_account",
            "budget_entry",
            "approval_request",
            "graph_checkpoint",
            "graph_checkpoint_write",
            "outbox_event",
            "consumed_event",
        }


def test_unique_id_generation() -> None:
    """主键是 UUID —— 不用自增整数，避免从 ID 推断业务量。"""
    assert uuid.uuid4() != uuid.uuid4()
