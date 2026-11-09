"""TASK-P2-017 —— 多任务集成引擎。

**这一组测的是"次序与处置"。** 假 git 记下每一步的调用序列，于是"先验后合、
冲突要撤销、失败要清场"这些规矩能被**一条断言**钉住 —— 钉的是次序，不是
"最后是什么状态"。

**真 git 的行为另有一组测试**（``tests/integration/test_multi_task_integration.py``）：
那里真的建仓库、真的撞冲突、真的看 main 有没有前进。

**验收条件在这里落地**：T1 —— "构造两个各自单测通过但联合运行时产生符号冲
突的任务，集成引擎精准检出并阻断合并"。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from core.policy.approval import InMemoryApprovalStore
from core.policy.autonomy import PolicyManager
from harness.approval.gate import ApprovalGate
from harness.integration.multi_task import (
    IntegrationStage,
    MergeStage,
    MultiTaskIntegrationEngine,
    MultiTaskRequest,
    SymbolClaim,
    TaskBranch,
    find_symbol_conflicts,
    module_symbols,
)
from harness.integration.pipeline import MergeConflictError
from harness.verification.contract import AcceptanceContract, RequiredCheck
from harness.verification.engine import (
    CheckExecution,
    CheckSpec,
    VerificationEngine,
    VerificationPlan,
)
from tests.unit.harness.integration.doubles import FakeCheckRunner
from tests.unit.harness.integration.multi_task_doubles import FakeIntegrationGit

#: 这一批的标识。
BATCH = "batch-1"

#: 集成分支名。**必须带 ``integration/`` 前缀。**
BRANCH = "integration/batch-1"


def _contract(task_id: str) -> AcceptanceContract:
    """一份最小的契约 —— 只要求单测通过。"""
    return AcceptanceContract(task_id=task_id, required=(RequiredCheck.UNIT_TESTS_PASS,))


def _plan() -> VerificationPlan:
    """一份最小的计划。"""
    return VerificationPlan(
        checks=(CheckSpec(check=RequiredCheck.UNIT_TESTS_PASS, command=("pytest", "-q")),)
    )


def _task(task_id: str, workspace: Path) -> TaskBranch:
    """一个待集成的任务分支。"""
    return TaskBranch(
        task_id=task_id,
        branch=f"agent/{task_id}",
        contract=_contract(task_id),
        plan=_plan(),
        workspace=workspace,
    )


def _at(events: list[str], prefix: str) -> int:
    """事件流水里第一条以 ``prefix`` 开头的事件在第几位。"""
    for index, event in enumerate(events):
        if event.startswith(prefix):
            return index
    raise AssertionError(f"事件流水里没有 {prefix}：{events}")


def _count(events: list[str], prefix: str) -> int:
    """事件流水里以 ``prefix`` 开头的事件有几条。"""
    return sum(1 for event in events if event.startswith(prefix))


@dataclass
class Rig:
    """一组搭好的东西 —— 引擎、替身与请求。"""

    engine: MultiTaskIntegrationEngine
    git: FakeIntegrationGit
    gate: ApprovalGate
    runner: FakeCheckRunner
    events: list[str]
    request: MultiTaskRequest
    main: Path
    integration: Path
    task_a: Path
    task_b: Path
    #: 工作区 → 这一次的检查结果。**与运行器共用同一份** —— 装置建好之后
    #: 再往里填，填完运行器立刻就看得见。
    results: dict[Path, CheckExecution] = field(default_factory=dict)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """一套现成的多任务集成装置。"""
    main = tmp_path / "main"
    integration = tmp_path / "integration"
    task_a = tmp_path / "task-a"
    task_b = tmp_path / "task-b"
    for path in (main, integration, task_a, task_b):
        path.mkdir()

    events: list[str] = []
    results: dict[Path, CheckExecution] = {}
    git = FakeIntegrationGit(events=events)
    runner = FakeCheckRunner(events=events, results=results)
    gate = ApprovalGate(InMemoryApprovalStore())
    engine = MultiTaskIntegrationEngine(
        git=git,
        engine=VerificationEngine(runner=runner),
        gate=gate,
        policy=PolicyManager.default(),
    )
    request = MultiTaskRequest(
        batch_id=BATCH,
        project_id="P-1",
        agent_id="agent-1",
        integration_branch=BRANCH,
        integration_workspace=integration,
        main_workspace=main,
        tasks=(_task("task-a", task_a), _task("task-b", task_b)),
        joint_contract=_contract(BATCH),
        joint_plan=_plan(),
        requested_by="编排层",
        diff_summary="两个任务合到一起",
    )
    return Rig(
        engine=engine,
        git=git,
        gate=gate,
        runner=runner,
        events=events,
        request=request,
        main=main,
        integration=integration,
        task_a=task_a,
        task_b=task_b,
        results=results,
    )


def _declare(rig: Rig, task_id: str, path: str, source: str) -> None:
    """登记"某个任务新增了某个符号"。"""
    branch = f"agent/{task_id}"
    rig.git.changed[branch] = (path,)
    rig.git.files[(branch, path)] = source


class TestTheSymbolsAreExtracted:
    """符号是从源码里读出来的 —— 读法得准。"""

    def test_the_top_level_definitions_are_found(self) -> None:
        source = """import os


def run():
    return 1


class Thing:
    pass


LIMIT = 3
"""

        symbols = module_symbols("pkg/mod.py", source)

        assert [symbol.name for symbol in symbols] == ["run", "Thing", "LIMIT"]

    def test_the_nested_definitions_are_not_counted(self) -> None:
        """**嵌套的东西不构成命名空间冲突** —— 两个任务各在自己的函数里起
        同名局部变量，谁也碰不到谁。"""
        source = "def outer():\n    def inner():\n        pass\n    return inner\n"

        symbols = module_symbols("pkg/mod.py", source)

        assert [symbol.name for symbol in symbols] == ["outer"]

    def test_the_non_python_files_are_skipped(self) -> None:
        assert module_symbols("docs/readme.md", "# 标题\n") == ()

    def test_the_broken_syntax_is_skipped(self) -> None:
        """**解析不了就当没有符号。** 语法错误是别的事，这一层不越权下结论。"""
        assert module_symbols("pkg/mod.py", "def broken(:\n") == ()

    def test_the_source_is_normalized(self) -> None:
        """**压成一行只为比"是不是同一份"。** 缩进不同不该判成两个定义。"""
        first = module_symbols("pkg/mod.py", "def run():\n    return 1\n")
        second = module_symbols("pkg/mod.py", "def run():\n        return 1\n")

        assert first[0].source == second[0].source


class TestTheSymbolConflictsAreFound:
    """符号冲突的判定 —— 要准，也要能指得出是谁。"""

    def test_two_tasks_defining_the_same_symbol_conflict(self) -> None:
        claims = [
            SymbolClaim(task_id="task-a", path="pkg/mod.py", name="run", source="def run(): ..."),
            SymbolClaim(task_id="task-b", path="pkg/mod.py", name="run", source="def run(): ..."),
        ]
        claims[1] = SymbolClaim(
            task_id="task-b", path="pkg/mod.py", name="run", source="def run(): other"
        )

        conflicts = find_symbol_conflicts(claims)

        assert len(conflicts) == 1
        assert conflicts[0].name == "run"
        assert conflicts[0].path == "pkg/mod.py"
        assert conflicts[0].tasks == ("task-a", "task-b")

    def test_the_same_content_is_not_a_conflict(self) -> None:
        """**两个任务写了同一个定义 —— 那不是打架，是做了同一件事。**"""
        claims = [
            SymbolClaim(task_id="task-a", path="pkg/mod.py", name="run", source="def run(): ..."),
            SymbolClaim(task_id="task-b", path="pkg/mod.py", name="run", source="def run(): ..."),
        ]

        assert find_symbol_conflicts(claims) == ()

    def test_the_same_name_in_different_modules_is_not_a_conflict(self) -> None:
        claims = [
            SymbolClaim(task_id="task-a", path="pkg/a.py", name="run", source="def run(): 1"),
            SymbolClaim(task_id="task-b", path="pkg/b.py", name="run", source="def run(): 2"),
        ]

        assert find_symbol_conflicts(claims) == ()

    def test_one_task_is_not_a_conflict(self) -> None:
        claims = [SymbolClaim(task_id="task-a", path="pkg/mod.py", name="run", source="def run()")]

        assert find_symbol_conflicts(claims) == ()


class TestTheRequestIsChecked:
    """请求本身要站得住 —— 护栏在引擎动手之前。"""

    def test_the_integration_branch_must_have_the_prefix(self, tmp_path: Path) -> None:
        """**引擎只复位带 ``integration/`` 前缀的分支。** 别的分支不是草稿。"""
        with pytest.raises(ValueError, match="前缀"):
            _request(tmp_path, integration_branch="main-ish")

    def test_the_integration_workspace_must_differ_from_main(self, tmp_path: Path) -> None:
        """**清场要清的就是那个工作区** —— 集成工作区与 main 同一个的话，失败
        一次就把 main 的现场抹了。"""
        with pytest.raises(ValueError, match="不能与 main"):
            _request(tmp_path, same_workspace=True)

    def test_the_joint_contract_must_bind_the_batch(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="联合契约"):
            _request(tmp_path, joint_task_id="别的批次")

    def test_the_batch_must_not_be_empty(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="至少要有一个任务分支"):
            _request(tmp_path, tasks=())

    def test_a_task_cannot_appear_twice(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        integration = tmp_path / "integration"
        task_a = tmp_path / "task-a"
        for path in (main, integration, task_a):
            path.mkdir(exist_ok=True)

        with pytest.raises(ValueError, match="不能在一批里出现两次"):
            _request(tmp_path, tasks=(_task("task-a", task_a), _task("task-a", task_a)))

    def test_the_task_contract_must_bind_the_task(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="验收契约"):
            TaskBranch(
                task_id="task-a",
                branch="agent/task-a",
                contract=_contract("别的任务"),
                plan=_plan(),
                workspace=tmp_path,
            )


def _request(
    tmp_path: Path,
    *,
    integration_branch: str = BRANCH,
    same_workspace: bool = False,
    joint_task_id: str = BATCH,
    tasks: tuple[TaskBranch, ...] | None = None,
) -> MultiTaskRequest:
    """造一份集成请求 —— 用来试护栏。"""
    main = tmp_path / "main"
    integration = tmp_path / "integration"
    task_a = tmp_path / "task-a"
    for path in (main, integration, task_a):
        path.mkdir(exist_ok=True)
    chosen = tasks
    if chosen is None:
        chosen = (_task("task-a", task_a),)
    return MultiTaskRequest(
        batch_id=BATCH,
        project_id="P-1",
        agent_id="agent-1",
        integration_branch=integration_branch,
        integration_workspace=main if same_workspace else integration,
        main_workspace=main,
        tasks=chosen,
        joint_contract=_contract(joint_task_id),
        joint_plan=_plan(),
        requested_by="编排层",
        diff_summary="两个任务合到一起",
    )


class TestTheHappyPath:
    """一条都通的路 —— 次序被钉住。"""

    async def test_the_batch_is_integrated(self, rig: Rig) -> None:
        outcome = await rig.engine.integrate(rig.request)

        assert outcome.stage is IntegrationStage.INTEGRATED
        assert outcome.integrated
        assert outcome.integrated_tasks == ("task-a", "task-b")
        assert outcome.integration_commit is not None

    async def test_the_local_validation_happens_before_the_merge(self, rig: Rig) -> None:
        """**没验过的分支不该被合进来。**"""
        await rig.engine.integrate(rig.request)

        assert _at(rig.events, f"verify:{rig.task_a}") < _at(
            rig.events, f"stage_merge:{rig.integration}:agent/task-a"
        )
        assert _at(rig.events, f"verify:{rig.task_b}") < _at(
            rig.events, f"stage_merge:{rig.integration}:agent/task-b"
        )

    async def test_the_joint_regression_happens_after_the_merges(self, rig: Rig) -> None:
        """**联合回归看的是合在一起的东西** —— 它必须在两个任务都合完之后跑。"""
        await rig.engine.integrate(rig.request)

        assert _at(rig.events, f"stage_merge:{rig.integration}:agent/task-b") < _at(
            rig.events, f"verify:{rig.integration}"
        )

    async def test_every_task_gets_its_own_merge_commit(self, rig: Rig) -> None:
        """**一个任务一个合并提交** —— 分支的来路在历史里看得见。"""
        await rig.engine.integrate(rig.request)

        commits = [event for event in rig.events if event.startswith("commit_staged_merge")]
        assert len(commits) == 2


class TestTheSymbolConflictBlocksTheMerge:
    """T1 核心 —— 符号冲突要检得出、也要挡得住。"""

    async def test_the_conflict_is_found(self, rig: Rig) -> None:
        _declare(rig, "task-a", "pkg/mod.py", "def calculate():\n    return 1\n")
        _declare(rig, "task-b", "pkg/mod.py", "def calculate():\n    return 2\n")

        outcome = await rig.engine.integrate(rig.request)

        assert outcome.stage is IntegrationStage.SYMBOL_CONFLICTED
        assert len(outcome.conflicts) == 1
        assert outcome.conflicts[0].name == "calculate"

    async def test_the_conflict_names_the_tasks(self, rig: Rig) -> None:
        """**要指得出是谁** —— 只说"有冲突"的话，人还得自己去查。"""
        _declare(rig, "task-a", "pkg/mod.py", "def calculate():\n    return 1\n")
        _declare(rig, "task-b", "pkg/mod.py", "def calculate():\n    return 2\n")

        outcome = await rig.engine.integrate(rig.request)

        assert outcome.conflicts[0].tasks == ("task-a", "task-b")
        assert "task-a" in outcome.reasons[0]
        assert "task-b" in outcome.reasons[0]

    async def test_the_merge_is_blocked(self, rig: Rig) -> None:
        """**main 一个提交都不许前进。**"""
        _declare(rig, "task-a", "pkg/mod.py", "def calculate():\n    return 1\n")
        _declare(rig, "task-b", "pkg/mod.py", "def calculate():\n    return 2\n")

        await rig.engine.integrate(rig.request)

        assert _count(rig.events, f"commit_staged_merge:{rig.main}") == 0

    async def test_the_integration_branch_is_wound_back(self, rig: Rig) -> None:
        """**失败不留半截** —— 集成分支拨回 main 原处。"""
        _declare(rig, "task-a", "pkg/mod.py", "def calculate():\n    return 1\n")
        _declare(rig, "task-b", "pkg/mod.py", "def calculate():\n    return 2\n")

        await rig.engine.integrate(rig.request)

        assert rig.git.branch_heads[BRANCH] == "main0000"

    async def test_the_identical_definitions_are_not_a_conflict(self, rig: Rig) -> None:
        """**两个任务写了同一个定义 —— 那是做了同一件事，不该挡下来。**"""
        _declare(rig, "task-a", "pkg/mod.py", "def calculate():\n    return 1\n")
        _declare(rig, "task-b", "pkg/mod.py", "def calculate():\n    return 1\n")

        outcome = await rig.engine.integrate(rig.request)

        assert outcome.stage is IntegrationStage.INTEGRATED

    async def test_the_existing_symbols_are_not_claimed(self, rig: Rig) -> None:
        """**改一个已有的函数不算声明** —— 那种情况归 git 的文本冲突管。"""
        rig.git.merge_bases[("main0000", "agent/task-a")] = "base0000"
        rig.git.merge_bases[("main0000", "agent/task-b")] = "base0000"
        rig.git.files[("base0000", "pkg/mod.py")] = "def calculate():\n    return 0\n"
        _declare(rig, "task-a", "pkg/mod.py", "def calculate():\n    return 1\n")
        _declare(rig, "task-b", "pkg/mod.py", "def calculate():\n    return 2\n")

        outcome = await rig.engine.integrate(rig.request)

        assert outcome.stage is IntegrationStage.INTEGRATED


class TestTheTextConflictBlocksTheMerge:
    """git 自己报得出来的冲突。"""

    async def test_the_conflict_is_reported(self, rig: Rig) -> None:
        rig.git.stage_error = MergeConflictError("同一个文件的同一块")

        outcome = await rig.engine.integrate(rig.request)

        assert outcome.stage is IntegrationStage.TASK_CONFLICTED
        assert outcome.blocking_task == "task-a"

    async def test_the_main_is_not_touched(self, rig: Rig) -> None:
        rig.git.stage_error = MergeConflictError("同一个文件的同一块")

        await rig.engine.integrate(rig.request)

        assert _count(rig.events, f"commit_staged_merge:{rig.main}") == 0


class TestTheJointRegressionBlocksTheMerge:
    """R-057 —— 单任务通过 ≠ 集成后通过。"""

    async def test_the_failure_is_reported(self, rig: Rig) -> None:
        rig.results[rig.integration] = CheckExecution(1, "有用例没过", "")

        outcome = await rig.engine.integrate(rig.request)

        assert outcome.stage is IntegrationStage.REGRESSION_FAILED
        assert outcome.integrated_tasks == ("task-a", "task-b")

    async def test_the_error_is_not_the_same_as_the_failure(self, rig: Rig) -> None:
        """**"没验成"与"验了没过"是两件事** —— 处置不同：前者该重跑，后者该
        返工。"""
        rig.results[rig.integration] = CheckExecution(1, "", "", started=False)

        outcome = await rig.engine.integrate(rig.request)

        assert outcome.stage is IntegrationStage.REGRESSION_ERRORED

    async def test_the_branch_is_wound_back(self, rig: Rig) -> None:
        rig.results[rig.integration] = CheckExecution(1, "有用例没过", "")

        await rig.engine.integrate(rig.request)

        assert rig.git.branch_heads[BRANCH] == "main0000"


class TestTheLocalValidationBlocksTheMerge:
    """没验过的分支不该被合进来。"""

    async def test_the_failure_blocks_the_batch(self, rig: Rig) -> None:
        rig.results[rig.task_a] = CheckExecution(1, "单测没过", "")

        outcome = await rig.engine.integrate(rig.request)

        assert outcome.stage is IntegrationStage.LOCAL_VALIDATION_FAILED
        assert outcome.blocking_task == "task-a"
        assert outcome.integrated_tasks == ()

    async def test_the_error_blocks_the_batch(self, rig: Rig) -> None:
        rig.results[rig.task_a] = CheckExecution(1, "", "", started=False)

        outcome = await rig.engine.integrate(rig.request)

        assert outcome.stage is IntegrationStage.LOCAL_VALIDATION_ERRORED

    async def test_a_missing_workspace_is_an_error_not_a_pass(self, rig: Rig) -> None:
        """**工作区没了是"没验成"，不是"过了"。**"""
        request = rig.request
        broken = TaskBranch(
            task_id="task-a",
            branch="agent/task-a",
            contract=_contract("task-a"),
            plan=_plan(),
            workspace=rig.task_a / "已经不在了",
        )

        outcome = await rig.engine.integrate(replace(request, tasks=(broken,)))

        assert outcome.stage is IntegrationStage.LOCAL_VALIDATION_ERRORED


class TestTheApprovalIsWanted:
    """审批 —— 人批的是他看到的那份内容。"""

    async def test_the_merge_waits_for_the_approval(self, rig: Rig) -> None:
        outcome = await rig.engine.integrate(rig.request)
        assert outcome.integration_commit is not None

        merge = await rig.engine.merge_to_main(
            rig.request, integration_commit=outcome.integration_commit
        )

        assert merge.stage is MergeStage.AWAITING_APPROVAL
        assert merge.approval_request_id is not None

    async def test_the_request_carries_the_commit(self, rig: Rig) -> None:
        """**审批绑在具体的提交上** —— 机器可读，人也看得见。"""
        outcome = await rig.engine.integrate(rig.request)
        assert outcome.integration_commit is not None

        await rig.engine.merge_to_main(rig.request, integration_commit=outcome.integration_commit)

        rows = await rig.gate.store.list_for_task(BATCH)
        assert rows[0].diff_summary.startswith(f"commit:{outcome.integration_commit}")

    async def test_the_old_approval_is_not_reused_for_another_commit(self, rig: Rig) -> None:
        """**集成分支变了，旧批准就不再成立。**"""
        outcome = await rig.engine.integrate(rig.request)
        assert outcome.integration_commit is not None
        first = await rig.engine.merge_to_main(
            rig.request, integration_commit=outcome.integration_commit
        )

        second = await rig.engine.merge_to_main(rig.request, integration_commit="0" * 40)

        assert second.approval_request_id != first.approval_request_id


class TestTheMergeToMain:
    """集成分支 → main。"""

    async def test_the_approved_batch_lands_in_main(self, rig: Rig) -> None:
        outcome = await rig.engine.integrate(rig.request)
        assert outcome.integration_commit is not None
        waiting = await rig.engine.merge_to_main(
            rig.request, integration_commit=outcome.integration_commit
        )
        assert waiting.approval_request_id is not None
        await rig.gate.approve(request_id=waiting.approval_request_id, approver="alice")

        merge = await rig.engine.merge_to_main(
            rig.request, integration_commit=outcome.integration_commit
        )

        assert merge.stage is MergeStage.MERGED
        assert merge.merged_commit is not None

    async def test_the_main_is_verified_before_the_commit(self, rig: Rig) -> None:
        """**先暂存再验证** —— main 上复验通过了才提交。"""
        outcome = await rig.engine.integrate(rig.request)
        assert outcome.integration_commit is not None
        waiting = await rig.engine.merge_to_main(
            rig.request, integration_commit=outcome.integration_commit
        )
        assert waiting.approval_request_id is not None
        await rig.gate.approve(request_id=waiting.approval_request_id, approver="alice")

        await rig.engine.merge_to_main(rig.request, integration_commit=outcome.integration_commit)

        assert _at(rig.events, f"verify:{rig.main}") < _at(
            rig.events, f"commit_staged_merge:{rig.main}"
        )

    async def test_the_failed_verification_undoes_the_merge(self, rig: Rig) -> None:
        """**main 上没过就撤销合并** —— main 一个提交都不许前进。"""
        outcome = await rig.engine.integrate(rig.request)
        assert outcome.integration_commit is not None
        waiting = await rig.engine.merge_to_main(
            rig.request, integration_commit=outcome.integration_commit
        )
        assert waiting.approval_request_id is not None
        await rig.gate.approve(request_id=waiting.approval_request_id, approver="alice")
        rig.results[rig.main] = CheckExecution(1, "main 上没过", "")

        merge = await rig.engine.merge_to_main(
            rig.request, integration_commit=outcome.integration_commit
        )

        assert merge.stage is MergeStage.MAIN_REGRESSION_FAILED
        assert _count(rig.events, "abort_staged_merge") == 1

    async def test_the_rejection_stops_the_merge(self, rig: Rig) -> None:
        outcome = await rig.engine.integrate(rig.request)
        assert outcome.integration_commit is not None
        waiting = await rig.engine.merge_to_main(
            rig.request, integration_commit=outcome.integration_commit
        )
        assert waiting.approval_request_id is not None
        await rig.gate.reject(
            request_id=waiting.approval_request_id, approver="alice", task_state=None
        )

        merge = await rig.engine.merge_to_main(
            rig.request, integration_commit=outcome.integration_commit
        )

        assert merge.stage is MergeStage.APPROVAL_REFUSED
