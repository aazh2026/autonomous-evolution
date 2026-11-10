"""TASK-P4-010 ② —— 两个项目并行演进：隔离与分别核算。

设计依据：docs/tasks.md TASK-P4-010（验收条件"两个项目并行演进，任务 / 候选 /
决策互不串扰；**预算分别核算**"）、R-016。

**为什么这些断言必须落在真实数据库上。** 串扰与算错账的后果都是"系统照常运
行，只是结果不对"：候选照记、决策照下、任务照派，钱照花 —— 没有一处会报
错。所以只能**把两个项目真的放进同一个库里同时跑**，然后逐项对照：

- 每个项目的候选 / 决策 / 任务**只在自己的作用域里**；
- **按 id 取**别的东西要报错（越权读不能退化成"查无此项"）；
- 两个项目的**预算分别核算** —— 各自的上限各管各的。

**"并行"是字面意思。** 两个项目的候选→决策→任务链路**并发**跑，不是串行
跑两遍再比对 —— 串行跑证明不了"同时发生时会不会串"。

**治理变更单独验。** 项目定义可以被改，但"改"必须带着一条**已批准的**
``modify_governance`` 审批 —— R-102 与 design.md §7：系统不得自主修改治理
自身的策略。这一层验的是**失败关闭**：审批不成立时，项目定义一个字都不许变。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.entities.evolution import EvolutionCandidate, EvolutionDecision
from core.entities.task import Task
from core.policy.approval import ApprovalRequest, ApprovalStatus
from core.policy.autonomy import Operation
from core.policy.budget import (
    Budget,
    BudgetExhausted,
    BudgetMetric,
    SpendRequest,
)
from core.project.models import Project
from core.project.scope import ProjectScope
from infrastructure.database.approval import PostgresApprovalStore
from infrastructure.database.budget import PostgresBudgetLedger
from infrastructure.database.project_budget import (
    ProjectBudget,
    ProjectBudgetLedger,
)
from infrastructure.database.project_scope import (
    ProjectRecords,
    ScopeViolationError,
    scope_of_rows,
)
from infrastructure.database.projects import (
    MissingApprovalError,
    PostgresProjectRegistry,
    ProjectConflictError,
    ProjectNotFoundError,
    UnapprovedChangeError,
)
from infrastructure.database.repositories import (
    DECISION_RECORD,
    CandidateRepository,
    DecisionRepository,
    NotFoundError,
    TaskRepository,
)

pytestmark = pytest.mark.integration

#: 固定"现在" —— 日成本按自然日重置，时间不固定就没法断言。
_NOW = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)

_PROJECT_A = "project-a"
_PROJECT_B = "project-b"

#: 三个 id 的元组 —— 候选 / 决策 / 任务。
Chain = tuple[uuid.UUID, uuid.UUID, uuid.UUID]


def _project_yaml(project_id: str, name: str, url: str, daily_budget: str) -> str:
    """一份完整合法的项目配置 —— 两个项目只有 id / 名字 / 仓库 / 预算不同。"""
    return f"""
project:
  id: {project_id}
  name: {name}
  goal:
    objective:
      - id: OBJ-001
        description: 把 {name} 做成能用的东西
    key_results:
      - id: KR-001
        metric: e2e_success_rate
        target: ">= 0.95"
    constraints:
      - id: C-001
        description: 所有代码变更必须通过自动化测试
        kind: all_changes_must_pass_tests
        machine_checkable: true
        expression: tests_passed == true
    non_goals:
      - id: NG-001
        description: 不做移动端适配
    autonomy:
      rules:
        analyze_project: {{level: L4, permission: autonomous}}
        plan_tasks: {{level: L4, permission: autonomous}}
        code_changes: {{level: L4, permission: autonomous}}
        test_execution: {{level: L4, permission: autonomous}}
        branch_creation: {{level: L4, permission: autonomous}}
        merge_to_main: {{level: L2, permission: approval_required}}
        production_deployment: {{level: L1, permission: advisory_only}}
        modify_governance: {{level: L1, permission: advisory_only}}
    stop_conditions:
      - id: SC-001
        description: 全部关键结果达成
        kind: all_key_results_met
  repository:
    url: {url}
  budget:
    currency: USD
    max_daily_cost: "{daily_budget}"
  tech_stack: [python]
  environment:
    stage: staging
"""


def _spec(project_id: str, name: str, url: str, daily_budget: str = "50") -> Project:
    """领域模型的项目定义。"""
    return Project.from_yaml(_project_yaml(project_id, name, url, daily_budget))


@pytest_asyncio.fixture
async def factory(
    engine: AsyncEngine, session: AsyncSession
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """会话工厂 —— 注册表自己管事务，它需要能自己开会话。

    **依赖 ``session`` 是为了它的副作用。** 每个测试前把全部表清空 ——
    集成测试库在两次 pytest 运行之间**不会**自动清空，而这里的断言要数
    精确的行数（"两个项目各一条候选"），脏库会让断言变成"上一轮留下的"。
    """
    rows = await session.execute(text("SELECT count(*) FROM project"))
    assert rows.scalar_one() == 0, "项目表不是干净的 —— 前置条件不满足"
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def registry(factory: async_sessionmaker[AsyncSession]) -> PostgresProjectRegistry:
    """项目注册表 —— 时钟固定，便于断言审批是否过期。"""
    return PostgresProjectRegistry(
        factory,
        approval_store=PostgresApprovalStore(factory, clock=lambda: _NOW),
        clock=lambda: _NOW,
    )


@pytest_asyncio.fixture
async def approvals(factory: async_sessionmaker[AsyncSession]) -> PostgresApprovalStore:
    """审批存储。"""
    return PostgresApprovalStore(factory, clock=lambda: _NOW)


async def _register(registry: PostgresProjectRegistry) -> None:
    """把两个项目登记进去。"""
    await registry.register(_spec(_PROJECT_A, "项目甲", "https://example.com/a"))
    await registry.register(_spec(_PROJECT_B, "项目乙", "https://example.com/b"))


async def _record_chain(
    factory: async_sessionmaker[AsyncSession],
    project_id: str,
    title: str,
) -> Chain:
    """跑一遍"候选 → 决策 → 任务"，返回三个 id。

    **自己开事务** —— 两个项目要**并行**跑，各自一个会话。

    **走仓储，不直接造实体。** 决策与任务只能由仓储层写 —— 那条路会检查"决策是
    不是 ``record``、是不是被推翻"（``tests/unit/database/test_bypass_paths.py``
    静态守着这条）。绕开它写一行任务，绕开的就是全部三道准入。

    **项目 id 只在候选上写一次。** 决策的 project_id 由候选带下来，任务的
    project_id 由决策带下来 —— **三张表的项目归属是同一条链推出来的**，不是各写
    各的。所以"决策挂到了别的项目下"这种串扰在这条路上根本写不出来。
    """
    async with factory() as session, session.begin():
        candidates = CandidateRepository(session)
        decisions = DecisionRepository(session)
        tasks = TaskRepository(session)
        candidate = await candidates.create(
            project_id=project_id,
            category="quality",
            title=f"{title}的候选",
            evidence=[{"ref": f"src/{title}/cli.py", "kind": "file"}],
            hypothesis="改了就更好",
        )
        decision = await decisions.record(
            candidate_id=candidate.id,
            decision=DECISION_RECORD,
            rationale=f"值得做：{title}",
        )
        task = await tasks.create_from_decision(
            decision.id,
            title=f"{title}的任务",
            task_type="quality",
            description=f"把 {title} 修好",
        )
        return candidate.id, decision.id, task.id


class TestRegistry:
    """项目注册表 —— 登记、读取、列举。"""

    async def test_two_projects_can_be_registered(self, registry: PostgresProjectRegistry) -> None:
        await _register(registry)
        projects = await registry.list()
        assert [item.id for item in projects] == [_PROJECT_A, _PROJECT_B]

    async def test_registration_is_idempotent(self, registry: PostgresProjectRegistry) -> None:
        """同样的内容重复登记不报错 —— 演示与长跑都会反复跑这条路径。"""
        await _register(registry)
        await _register(registry)
        assert len(await registry.list()) == 2

    async def test_a_different_definition_is_a_conflict(
        self, registry: PostgresProjectRegistry
    ) -> None:
        """**静默覆盖等于允许任何人把一个项目的策略换成另一个项目的策略。**"""
        await registry.register(_spec(_PROJECT_A, "项目甲", "https://example.com/a"))
        with pytest.raises(ProjectConflictError):
            await registry.register(_spec(_PROJECT_A, "项目甲改名了", "https://example.com/a"))

    async def test_a_conflict_does_not_overwrite(self, registry: PostgresProjectRegistry) -> None:
        """冲突之后，库里那个项目**一个字都不许变**。"""
        await registry.register(_spec(_PROJECT_A, "项目甲", "https://example.com/a"))
        with pytest.raises(ProjectConflictError):
            await registry.register(_spec(_PROJECT_A, "项目甲改名了", "https://example.com/a"))
        restored = await registry.get(_PROJECT_A)
        assert restored.name == "项目甲"

    async def test_a_project_can_be_read_back(self, registry: PostgresProjectRegistry) -> None:
        """登记进去的项目读得回来 —— 六要素、仓库、技术栈、环境都得在。"""
        await _register(registry)
        restored = await registry.get(_PROJECT_A)
        assert restored.name == "项目甲"
        assert restored.repository.url == "https://example.com/a"
        assert list(restored.tech_stack) == ["python"]
        assert dict(restored.environment) == {"stage": "staging"}
        assert restored.goal.objective[0].description == "把 项目甲 做成能用的东西"

    async def test_an_unknown_project_is_not_found(self, registry: PostgresProjectRegistry) -> None:
        with pytest.raises(ProjectNotFoundError):
            await registry.get("没有这个项目")


class TestGovernanceChange:
    """治理变更 —— 必须带着一条成立的审批。"""

    async def _approve(
        self,
        approvals: PostgresApprovalStore,
        *,
        task_id: str,
        operation: str = Operation.MODIFY_GOVERNANCE.value,
        expires_at: datetime | None = None,
    ) -> str:
        """造一条**已批准**的审批，返回它的 id。"""
        request = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            task_id=task_id,
            operation=operation,
            diff_summary="项目定义改了",
            evidence_ref="artifact://sha256:abc",
            status=ApprovalStatus.PENDING,
            requested_by="agent-1",
            requested_at=_NOW,
            expires_at=expires_at if expires_at is not None else _NOW + timedelta(hours=1),
        )
        created = await approvals.create(request)
        await approvals.decide(
            request_id=created.request_id,
            status=ApprovalStatus.APPROVED,
            decided_by="human-1",
            decided_at=_NOW,
        )
        return created.request_id

    async def test_without_an_approval_nothing_changes(
        self, registry: PostgresProjectRegistry
    ) -> None:
        await _register(registry)
        with pytest.raises(MissingApprovalError):
            await registry.replace(
                _spec(_PROJECT_A, "项目甲改名了", "https://example.com/a"),
                approval_ref="",
            )
        assert (await registry.get(_PROJECT_A)).name == "项目甲"

    async def test_a_pending_approval_does_not_release(
        self, registry: PostgresProjectRegistry, approvals: PostgresApprovalStore
    ) -> None:
        """还没表态的审批**放不了行**。"""
        await _register(registry)
        request = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            task_id=_PROJECT_A,
            operation=Operation.MODIFY_GOVERNANCE.value,
            diff_summary="项目定义改了",
            evidence_ref="artifact://sha256:abc",
            status=ApprovalStatus.PENDING,
            requested_by="agent-1",
            requested_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )
        created = await approvals.create(request)
        with pytest.raises(UnapprovedChangeError):
            await registry.replace(
                _spec(_PROJECT_A, "项目甲改名了", "https://example.com/a"),
                approval_ref=created.request_id,
            )
        assert (await registry.get(_PROJECT_A)).name == "项目甲"

    async def test_an_expired_approval_does_not_release(
        self, registry: PostgresProjectRegistry, approvals: PostgresApprovalStore
    ) -> None:
        """**过期盖住批准** —— 批的是"当时那一份"，diff 会变。"""
        await _register(registry)
        approval_ref = await self._approve(
            approvals, task_id=_PROJECT_A, expires_at=_NOW - timedelta(minutes=1)
        )
        with pytest.raises(UnapprovedChangeError):
            await registry.replace(
                _spec(_PROJECT_A, "项目甲改名了", "https://example.com/a"),
                approval_ref=approval_ref,
            )
        assert (await registry.get(_PROJECT_A)).name == "项目甲"

    async def test_an_approval_for_another_project_does_not_release(
        self, registry: PostgresProjectRegistry, approvals: PostgresApprovalStore
    ) -> None:
        """**批的是别的项目就等于没批。**"""
        await _register(registry)
        approval_ref = await self._approve(approvals, task_id=_PROJECT_B)
        with pytest.raises(UnapprovedChangeError):
            await registry.replace(
                _spec(_PROJECT_A, "项目甲改名了", "https://example.com/a"),
                approval_ref=approval_ref,
            )
        assert (await registry.get(_PROJECT_A)).name == "项目甲"

    async def test_an_approval_for_another_operation_does_not_release(
        self, registry: PostgresProjectRegistry, approvals: PostgresApprovalStore
    ) -> None:
        """批的是"合并代码"不等于批了"改治理策略"。"""
        await _register(registry)
        approval_ref = await self._approve(
            approvals, task_id=_PROJECT_A, operation=Operation.MERGE_TO_MAIN.value
        )
        with pytest.raises(UnapprovedChangeError):
            await registry.replace(
                _spec(_PROJECT_A, "项目甲改名了", "https://example.com/a"),
                approval_ref=approval_ref,
            )
        assert (await registry.get(_PROJECT_A)).name == "项目甲"

    async def test_a_missing_approval_record_does_not_release(
        self, registry: PostgresProjectRegistry
    ) -> None:
        """查不到审批记录 —— **绝不"查不到就当有"**。"""
        await _register(registry)
        with pytest.raises(UnapprovedChangeError):
            await registry.replace(
                _spec(_PROJECT_A, "项目甲改名了", "https://example.com/a"),
                approval_ref=str(uuid.uuid4()),
            )
        assert (await registry.get(_PROJECT_A)).name == "项目甲"

    async def test_an_approved_change_goes_through(
        self, registry: PostgresProjectRegistry, approvals: PostgresApprovalStore
    ) -> None:
        await _register(registry)
        approval_ref = await self._approve(approvals, task_id=_PROJECT_A)
        await registry.replace(
            _spec(_PROJECT_A, "项目甲改名了", "https://example.com/a2", "80"),
            approval_ref=approval_ref,
        )
        restored = await registry.get(_PROJECT_A)
        assert restored.name == "项目甲改名了"
        assert restored.repository.url == "https://example.com/a2"
        assert restored.budget.max_daily_cost == Decimal("80")

    async def test_changing_one_project_does_not_touch_the_other(
        self, registry: PostgresProjectRegistry, approvals: PostgresApprovalStore
    ) -> None:
        """改了 A，B 必须一点没变。"""
        await _register(registry)
        approval_ref = await self._approve(approvals, task_id=_PROJECT_A)
        await registry.replace(
            _spec(_PROJECT_A, "项目甲改名了", "https://example.com/a"),
            approval_ref=approval_ref,
        )
        assert (await registry.get(_PROJECT_B)).name == "项目乙"


class TestIsolation:
    """两个项目并行演进时，候选 / 决策 / 任务互不串扰。"""

    @pytest_asyncio.fixture
    async def both(
        self,
        factory: async_sessionmaker[AsyncSession],
        registry: PostgresProjectRegistry,
    ) -> tuple[Chain, Chain]:
        """两个项目**并行**跑完各自的候选→决策→任务链路。"""
        await _register(registry)
        chain_a, chain_b = await asyncio.gather(
            _record_chain(factory, _PROJECT_A, "alpha"),
            _record_chain(factory, _PROJECT_B, "beta"),
        )
        return chain_a, chain_b

    @pytest.mark.usefixtures("both")
    async def test_each_project_sees_only_its_own_candidates(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        scope_a = ProjectScope(_PROJECT_A)
        scope_b = ProjectScope(_PROJECT_B)
        async with factory() as session:
            records = ProjectRecords(session)
            candidates_a = await records.candidates(scope_a)
            candidates_b = await records.candidates(scope_b)
        assert len(candidates_a) == 1
        assert len(candidates_b) == 1
        assert candidates_a[0].project_id == _PROJECT_A
        assert candidates_b[0].project_id == _PROJECT_B
        assert candidates_a[0].id != candidates_b[0].id

    @pytest.mark.usefixtures("both")
    async def test_each_project_sees_only_its_own_decisions(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            records = ProjectRecords(session)
            decisions_a = await records.decisions(ProjectScope(_PROJECT_A))
            decisions_b = await records.decisions(ProjectScope(_PROJECT_B))
        assert [item.rationale for item in decisions_a] == ["值得做：alpha"]
        assert [item.rationale for item in decisions_b] == ["值得做：beta"]

    @pytest.mark.usefixtures("both")
    async def test_each_project_sees_only_its_own_tasks(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            records = ProjectRecords(session)
            tasks_a = await records.tasks(ProjectScope(_PROJECT_A))
            tasks_b = await records.tasks(ProjectScope(_PROJECT_B))
        assert [item.title for item in tasks_a] == ["alpha的任务"]
        assert [item.title for item in tasks_b] == ["beta的任务"]

    @pytest.mark.usefixtures("both")
    async def test_a_batch_of_rows_never_spans_two_projects(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**看单行看不出来，看一批才看得出来。**"""
        async with factory() as session:
            records = ProjectRecords(session)
            tasks = await records.tasks(ProjectScope(_PROJECT_A))
        assert scope_of_rows(tasks) == {_PROJECT_A}

    @pytest.mark.usefixtures("both")
    async def test_the_counts_are_separate(self, factory: async_sessionmaker[AsyncSession]) -> None:
        async with factory() as session:
            records = ProjectRecords(session)
            counts_a = await records.count_by_project(ProjectScope(_PROJECT_A))
            counts_b = await records.count_by_project(ProjectScope(_PROJECT_B))
        assert counts_a == {"candidates": 1, "decisions": 1, "tasks": 1}
        assert counts_b == {"candidates": 1, "decisions": 1, "tasks": 1}

    @pytest.mark.usefixtures("both")
    async def test_the_database_itself_keeps_the_projects_apart(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**直接查库** —— 闸门之外的那一层也得是干净的。

        闸门（``ProjectRecords``）保证"按项目读"读不到别人的东西；外键保证
        "挂不到不存在的项目上"。这两句都不保证"数据库里没有混在一起的行" ——
        所以直接数一遍：三张表，每张表两行，各属一个项目。
        """
        async with factory() as session:
            candidates = await session.execute(
                select(EvolutionCandidate.project_id).order_by(EvolutionCandidate.project_id)
            )
            decisions = await session.execute(
                select(EvolutionDecision.project_id).order_by(EvolutionDecision.project_id)
            )
            tasks = await session.execute(select(Task.project_id).order_by(Task.project_id))
        assert [row[0] for row in candidates] == [_PROJECT_A, _PROJECT_B]
        assert [row[0] for row in decisions] == [_PROJECT_A, _PROJECT_B]
        assert [row[0] for row in tasks] == [_PROJECT_A, _PROJECT_B]

    async def test_reading_another_projects_candidate_is_refused(
        self, factory: async_sessionmaker[AsyncSession], both: tuple[Chain, Chain]
    ) -> None:
        """**越权读必须被看见** —— 不能退化成"查无此项"。"""
        _, chain_b = both
        async with factory() as session:
            records = ProjectRecords(session)
            with pytest.raises(ScopeViolationError):
                await records.candidate(ProjectScope(_PROJECT_A), chain_b[0])

    async def test_reading_another_projects_decision_is_refused(
        self, factory: async_sessionmaker[AsyncSession], both: tuple[Chain, Chain]
    ) -> None:
        _, chain_b = both
        async with factory() as session:
            records = ProjectRecords(session)
            with pytest.raises(ScopeViolationError):
                await records.decision(ProjectScope(_PROJECT_A), chain_b[1])

    async def test_reading_another_projects_task_is_refused(
        self, factory: async_sessionmaker[AsyncSession], both: tuple[Chain, Chain]
    ) -> None:
        _, chain_b = both
        async with factory() as session:
            records = ProjectRecords(session)
            with pytest.raises(ScopeViolationError):
                await records.task(ProjectScope(_PROJECT_A), chain_b[2])

    @pytest.mark.usefixtures("both")
    async def test_a_missing_row_is_not_found_not_a_violation(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**"查无此项"与"在别的项目里"是两件事** —— 报错要能分开。"""
        async with factory() as session:
            records = ProjectRecords(session)
            with pytest.raises(NotFoundError):
                await records.task(ProjectScope(_PROJECT_A), uuid.uuid4())

    @pytest.mark.usefixtures("both")
    async def test_the_project_ids_with_data_are_both_there(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            records = ProjectRecords(session)
            assert await records.project_ids() == (_PROJECT_A, _PROJECT_B)


class TestSeparateBudgets:
    """预算分别核算。"""

    @pytest_asyncio.fixture
    async def ledgers(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> tuple[ProjectBudgetLedger, ProjectBudget]:
        """账本 + 甲项目的预算（上限 15）。"""
        ledger = ProjectBudgetLedger(PostgresBudgetLedger(factory))
        budget = ProjectBudget(
            ProjectScope(_PROJECT_A),
            Budget(currency="USD", max_daily_cost=Decimal("15")),
            ledger,
            clock=lambda: _NOW,
        )
        return ledger, budget

    async def test_each_project_spends_its_own_money(
        self, ledgers: tuple[ProjectBudgetLedger, ProjectBudget]
    ) -> None:
        ledger, _ = ledgers
        scope_a = ProjectScope(_PROJECT_A)
        scope_b = ProjectScope(_PROJECT_B)
        await ledger.consume(scope_a, _request(Decimal("10")))
        await ledger.consume(scope_b, _request(Decimal("30")))
        assert await ledger.spent(scope_a, BudgetMetric.DAILY_COST, _NOW) == Decimal("10")
        assert await ledger.spent(scope_b, BudgetMetric.DAILY_COST, _NOW) == Decimal("30")

    async def test_the_accounts_are_two_rows(
        self,
        factory: async_sessionmaker[AsyncSession],
        ledgers: tuple[ProjectBudgetLedger, ProjectBudget],
    ) -> None:
        """**一行一个项目** —— 混在一行里，两个口径的钱就永远分不开了。"""
        ledger, _ = ledgers
        await ledger.consume(ProjectScope(_PROJECT_A), _request(Decimal("10")))
        await ledger.consume(ProjectScope(_PROJECT_B), _request(Decimal("30")))
        async with factory() as session:
            rows = await session.execute(
                text("SELECT scope, spent FROM budget_account ORDER BY scope")
            )
        assert [tuple(row) for row in rows] == [
            (f"project:{_PROJECT_A}", Decimal("10")),
            (f"project:{_PROJECT_B}", Decimal("30")),
        ]

    async def test_the_request_scope_is_overridden(
        self, ledgers: tuple[ProjectBudgetLedger, ProjectBudget]
    ) -> None:
        """**以作用域为准** —— 调用方填错的项目 id 不能把钱记到别人头上。"""
        ledger, _ = ledgers
        scope_a = ProjectScope(_PROJECT_A)
        scope_b = ProjectScope(_PROJECT_B)
        await ledger.consume(scope_a, _request(Decimal("10"), scope="project:写错了"))
        assert await ledger.spent(scope_a, BudgetMetric.DAILY_COST, _NOW) == Decimal("10")
        assert await ledger.spent(scope_b, BudgetMetric.DAILY_COST, _NOW) == Decimal("0")

    async def test_one_projects_limit_does_not_bind_the_other(
        self, ledgers: tuple[ProjectBudgetLedger, ProjectBudget]
    ) -> None:
        """**甲的钱快花完了，乙照样能花** —— 额度是各自的。"""
        ledger, budget = ledgers
        await budget.consume(BudgetMetric.DAILY_COST, Decimal("12"))
        request = _request(Decimal("40"), limit=Decimal("50"))
        await ledger.consume(ProjectScope(_PROJECT_B), request)
        assert await ledger.spent(
            ProjectScope(_PROJECT_B), BudgetMetric.DAILY_COST, _NOW
        ) == Decimal("40")

    async def test_a_project_is_blocked_by_its_own_limit(
        self, ledgers: tuple[ProjectBudgetLedger, ProjectBudget]
    ) -> None:
        """超**它自己**的上限要报错 —— 不是静默截断。"""
        _, budget = ledgers
        await budget.consume(BudgetMetric.DAILY_COST, Decimal("12"))
        with pytest.raises(BudgetExhausted):
            await budget.consume(BudgetMetric.DAILY_COST, Decimal("5"))

    async def test_the_report_has_a_row_per_project(
        self, ledgers: tuple[ProjectBudgetLedger, ProjectBudget]
    ) -> None:
        """**报表的价值不在总数** —— 在每一行都能对回自己那个项目的账。"""
        ledger, _ = ledgers
        scope_a = ProjectScope(_PROJECT_A)
        scope_b = ProjectScope(_PROJECT_B)
        await ledger.consume(scope_a, _request(Decimal("10")))
        await ledger.consume(scope_b, _request(Decimal("30")))
        report = await ledger.report(
            [scope_a, scope_b],
            BudgetMetric.DAILY_COST,
            _NOW,
            budgets={
                _PROJECT_A: Budget(currency="USD", max_daily_cost=Decimal("15")),
                _PROJECT_B: Budget(currency="USD", max_daily_cost=Decimal("50")),
            },
        )
        row_a = report.row_for(_PROJECT_A)
        row_b = report.row_for(_PROJECT_B)
        assert row_a is not None
        assert row_b is not None
        assert row_a.spent == Decimal("10")
        assert row_b.spent == Decimal("30")
        assert row_a.remaining == Decimal("5")
        assert row_b.remaining == Decimal("20")
        assert row_a.exhausted is False
        assert report.total_spent == Decimal("40")

    async def test_a_project_with_no_spend_shows_zero_not_missing(
        self, ledgers: tuple[ProjectBudgetLedger, ProjectBudget]
    ) -> None:
        """**没花过不等于查不到** —— 报表上得有一行 0。"""
        ledger, _ = ledgers
        scope_a = ProjectScope(_PROJECT_A)
        scope_b = ProjectScope(_PROJECT_B)
        await ledger.consume(scope_a, _request(Decimal("10")))
        report = await ledger.report([scope_a, scope_b], BudgetMetric.DAILY_COST, _NOW)
        row_b = report.row_for(_PROJECT_B)
        assert row_b is not None
        assert row_b.spent == Decimal("0")
        assert row_b.entries == 0
        assert row_b.remaining is None


def _request(
    amount: Decimal,
    *,
    scope: str = "default",
    limit: Decimal | None = None,
) -> SpendRequest:
    """造一条扣减请求。"""
    return SpendRequest(
        scope=scope,
        metric=BudgetMetric.DAILY_COST,
        amount=amount,
        currency="USD",
        limit=limit,
        moment=_NOW,
    )
