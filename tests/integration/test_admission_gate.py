"""TASK-P1-062 —— 验收契约准入闸门（接到真库那一层）。

**这里验的是"接线对不对"。** 单元测试（``tests/unit/orchestration/test_gates.py``）
用假存储证明了门的判定与留痕；这里证明的是同一道门接到真库上时行为不变 ——
契约真的落在那一列、审计真的多了一条、状态真的没往前走。

**为什么"状态没往前走"非得在库里看。** 门不落状态（与 :mod:`harness.approval.gate`
同一套做法：门只算迁移，仓储层写状态），所以"迁移被拒且状态仍为 READY"这句话，
只有在库里才说得清 —— 单元测试里状态是个传进去的参数，看不见它有没有被改动。

**为什么还要试引擎级的那一道。** 编排层的闸门与仓储层的迁移检查是**两道独立的
防线**：编排层可能被绕过（别的调用方直接调仓储），而仓储层做不了完整校验（模型住
在 ``harness/``）。两道都要在 —— 少一道，就有一条不经过闸门的开工路径。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import Project, Task
from core.project.models import ComparisonOperator, TargetValue
from core.task.admission import contract_fingerprint
from core.task.state_machine import (
    AcceptanceContractRequiredError,
    TaskAction,
    TaskState,
)
from harness.security.audit import AuditLog, AuditQuery, InMemoryAuditStore
from harness.verification.contract import AcceptanceContract, RequiredCheck, Threshold
from infrastructure.database.repositories import (
    DECISION_RECORD,
    CandidateRepository,
    DecisionRepository,
    TaskContractStore,
    TaskRepository,
)
from orchestration.gates import BIND_OPERATION, UPDATE_OPERATION, AdmissionGate

pytestmark = pytest.mark.integration

_PROJECT_ID = "p-admission-gate"
_AGENT_ID = "AGENT-001"


async def _seed_task(session: AsyncSession) -> Task:
    """建一个走到 READY 的任务。

    **走的正是真实路径**：项目 → 候选 → ``record`` 决策 → 任务。任务只能由决策
    派生，别处造一行任务就绕开了三表分离。
    """
    project = Project(
        id=_PROJECT_ID,
        name="准入闸门项目",
        goal={"objective": [{"id": "OBJ-1", "description": "把准入闸门接上"}]},
        policy={"merge_to_main": "approval_required"},
        budget={"currency": "USD"},
        repository_url="https://example.invalid/gate.git",
    )
    session.add(project)
    await session.flush()

    candidates = CandidateRepository(session)
    decisions = DecisionRepository(session)
    tasks = TaskRepository(session)

    candidate = await candidates.create(
        project_id=_PROJECT_ID,
        category="quality",
        title="补上准入闸门",
        evidence=[{"kind": "defect", "ref": "issue://1"}],
        value=Decimal("0.5"),
        risk=Decimal("0.3"),
        confidence=Decimal("0.9"),
    )
    decision = await decisions.record(
        candidate_id=candidate.id,
        decision=DECISION_RECORD,
        rationale="闸门缺失，任务可能在没有验收标准的情况下开工",
    )
    task = await tasks.create_from_decision(decision.id, title="补上准入闸门", task_type="bugfix")
    await tasks.transition(task.id, TaskAction.PLAN)
    await tasks.transition(task.id, TaskAction.READY)
    await session.commit()
    return task


def _contract(task_id: str, *, coverage: str = "0.85") -> AcceptanceContract:
    """一份绑定到这个任务的契约。"""
    return AcceptanceContract(
        task_id=task_id,
        required=(RequiredCheck.UNIT_TESTS_PASS, RequiredCheck.LINT_PASS),
        thresholds={
            "coverage": Threshold(
                operator=ComparisonOperator.GTE,
                value=TargetValue.parse(coverage),
            )
        },
    )


def _gate(
    session: AsyncSession,
    audit_store: InMemoryAuditStore,
) -> AdmissionGate:
    """搭一道接在真库上的闸门。"""
    return AdmissionGate(TaskContractStore(session), audit=AuditLog(audit_store))


async def _status_in_db(session: AsyncSession, task_id: uuid.UUID) -> str:
    """直接从库里读状态 —— 不看内存里那个对象。

    **要看库里那份。** 内存里的对象可能还没 flush，也可能已经被回滚了 ——
    只有库里的那一行是"任务现在到底是什么状态"的答案。
    """
    result = await session.execute(select(Task.status).where(Task.id == task_id))
    return str(result.scalar_one())


class TestWithoutAContractTheTaskCannotStart:
    """T1 ① —— 无契约的任务无法从 READY 迁移到 RUNNING。"""

    async def test_the_gate_refuses(self, session: AsyncSession) -> None:
        """**没有契约就没有验收标准。**"""
        task = await _seed_task(session)
        audit_store = InMemoryAuditStore()
        gate = _gate(session, audit_store)

        with pytest.raises(AcceptanceContractRequiredError) as exc_info:
            await gate.admit_start(task_id=str(task.id), current_state=TaskState.READY)

        assert exc_info.value.task_id == str(task.id)

    async def test_the_status_stays_ready(self, session: AsyncSession) -> None:
        """**迁移被拒，状态不动。** 门在迁移之前就抛了出来 —— 它不落状态，
        落状态的仓储层也没被叫到。"""
        task = await _seed_task(session)
        task_id = task.id
        gate = _gate(session, InMemoryAuditStore())

        with pytest.raises(AcceptanceContractRequiredError):
            await gate.admit_start(task_id=str(task_id), current_state=TaskState.READY)

        await session.rollback()
        assert await _status_in_db(session, task_id) == TaskState.READY.value

    async def test_the_engine_also_refuses(self, session: AsyncSession) -> None:
        """**引擎级那道防线也要在。** 编排层的闸门可能被绕过（别的调用方直接
        调仓储），而这一层不看编排层脸色。"""
        task = await _seed_task(session)
        task_id = task.id
        tasks = TaskRepository(session)

        with pytest.raises(AcceptanceContractRequiredError):
            await tasks.transition(task_id, TaskAction.START)

        await session.rollback()
        assert await _status_in_db(session, task_id) == TaskState.READY.value

    async def test_a_contract_bound_to_another_task_does_not_pass(
        self, session: AsyncSession
    ) -> None:
        """**拿 A 任务的契约放行 B 任务，等于没有契约。** 这一条是补上的洞 ——
        早先只看"有没有契约"，绑错的那份一样能放行。"""
        task = await _seed_task(session)
        task_id = task.id
        other = uuid.uuid4()
        tasks = TaskRepository(session)

        # 绕过仓储层的绑定检查，直接写列 —— 模拟"绑定错了但已经写进库里"。
        row = await tasks.get(task_id)
        row.acceptance_contract = _contract(str(other)).to_document()
        await session.flush()

        with pytest.raises(AcceptanceContractRequiredError):
            await tasks.transition(task_id, TaskAction.START)

        await session.rollback()
        assert await _status_in_db(session, task_id) == TaskState.READY.value


class TestWithAContractTheTaskCanStart:
    """T1 ② —— 补上契约后迁移成功。"""

    async def test_the_contract_lands_in_the_column(self, session: AsyncSession) -> None:
        """**契约真的落在那一列上** —— 不是只留在门的手里。"""
        task = await _seed_task(session)
        gate = _gate(session, InMemoryAuditStore())

        await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id)),
        )
        await session.commit()

        row = await TaskRepository(session).get(task.id)
        assert row.acceptance_contract is not None
        assert row.acceptance_contract["task_id"] == str(task.id)

    async def test_the_binding_left_an_audit_record(self, session: AsyncSession) -> None:
        """**契约的每一次改动都留审计** —— 谁改的、改的什么。"""
        task = await _seed_task(session)
        audit_store = InMemoryAuditStore()
        gate = _gate(session, audit_store)

        await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id)),
        )

        events = audit_store.query(AuditQuery(task_id=str(task.id)))
        assert len(events) == 1
        assert events[0].operation == BIND_OPERATION
        assert events[0].agent_id == _AGENT_ID
        assert events[0].project_id == _PROJECT_ID

    async def test_the_admission_reports_the_version(self, session: AsyncSession) -> None:
        """**放行的是"这一版"契约** —— 版本要能被指出来。"""
        task = await _seed_task(session)
        gate = _gate(session, InMemoryAuditStore())

        binding = await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id)),
        )
        result = await gate.admit_start(task_id=str(task.id), current_state=TaskState.READY)

        assert result.decision.admitted is True
        assert result.decision.contract_version == binding.version

    async def test_the_task_starts(self, session: AsyncSession) -> None:
        """**契约齐了就能开工** —— 闸门不额外卡人。"""
        task = await _seed_task(session)
        gate = _gate(session, InMemoryAuditStore())

        await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id)),
        )
        await session.commit()

        result = await gate.admit_start(task_id=str(task.id), current_state=TaskState.READY)
        assert result.transition.changed is True

        await TaskRepository(session).transition(task.id, TaskAction.START)
        await session.commit()

        assert await _status_in_db(session, task.id) == TaskState.RUNNING.value


class TestTheContractCanBeChangedAfterTheStart:
    """T1 ③ —— 契约在任务开始后被修改 → 下一次迁移用新版本，且修改留审计。"""

    async def test_the_change_leaves_a_second_audit_record(self, session: AsyncSession) -> None:
        """**补一份与改一份分开记** —— 事后复盘时这是两件事。"""
        task = await _seed_task(session)
        audit_store = InMemoryAuditStore()
        gate = _gate(session, audit_store)

        await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id)),
        )
        await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id), coverage="0.60"),
        )

        events = audit_store.query(AuditQuery(task_id=str(task.id)))
        assert len(events) == 2
        assert events[0].operation == BIND_OPERATION
        assert events[1].operation == UPDATE_OPERATION

    async def test_the_new_version_is_stored_in_the_column(self, session: AsyncSession) -> None:
        """**库里那一列就是新版本** —— 下一次判定读的就是它。"""
        task = await _seed_task(session)
        gate = _gate(session, InMemoryAuditStore())

        await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id)),
        )
        second = await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id), coverage="0.60"),
        )
        await session.commit()

        row = await TaskRepository(session).get(task.id)
        assert row.acceptance_contract is not None
        assert contract_fingerprint(dict(row.acceptance_contract)) == second.version

    async def test_the_next_admission_uses_the_new_version(self, session: AsyncSession) -> None:
        """**下一次迁移用新版本** —— 这是 T1 ③ 的正脸。"""
        task = await _seed_task(session)
        gate = _gate(session, InMemoryAuditStore())

        first = await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id)),
        )
        before = await gate.admit_start(task_id=str(task.id), current_state=TaskState.READY)

        await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id), coverage="0.60"),
        )
        after = await gate.admit_start(task_id=str(task.id), current_state=TaskState.READY)

        assert before.decision.contract_version == first.version
        assert after.decision.contract_version != first.version

    async def test_the_redelivery_after_the_start_is_a_noop(self, session: AsyncSession) -> None:
        """**重投不重复开工。** 任务已经在 RUNNING，又收到一次 START ——
        那次重投要的是"别重复执行副作用"。"""
        task = await _seed_task(session)
        gate = _gate(session, InMemoryAuditStore())

        await gate.bind_contract(
            task_id=str(task.id),
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(str(task.id)),
        )
        await session.commit()
        await TaskRepository(session).transition(task.id, TaskAction.START)
        await session.commit()

        result = await gate.admit_start(task_id=str(task.id), current_state=TaskState.RUNNING)

        assert result.transition.changed is False
        await session.commit()
        assert await _status_in_db(session, task.id) == TaskState.RUNNING.value
