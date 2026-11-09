"""TASK-P2-017 —— 真 git 上的多任务集成。

**单元测试用假 git，是因为"次序错了"在假 git 上看不出来。** 假合并口会老实
实按你写的顺序调方法；真 git 才会在"暂存了但没提交"的时候，让 main 的工作
区真的停在半路 —— 而那正是这一层最要紧的一处：**符号冲突要在合并之后才
看得出来，而 main 一个提交都不许前进。**

所以这一组用真的 ``git`` 命令跑一遍，验四件事：

- **T1**：两个任务各自单测通过，但联合运行时产生符号冲突 —— 引擎精准检出，
  并且**阻断合并**（main 原地不动）；
- **R-057**：两个任务各自都绿，合在一起不绿 —— 联合回归挡下；
- **先验后合**：某个任务自己就没验过 —— 连集成分支都不许碰；
- **合进去了**：都通的时候，一批任务确实能落到 main 上。

需要真的装了 git。不需要 Docker 或数据库，所以不依赖那些夹具。

**符号冲突的复现靠一个刻意设计的仓库。** 两个任务都在 ``mod.py`` 里新增一
个**同名**的 ``calculate``，但**加在不同位置** —— 于是 git 合得干干净净，
Python 也照跑不误（后一个定义把前一个盖掉），而合出来的代码已经不是人写的
那一份了。**git 挡不住的，就是这一层要挡的。**
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.policy.approval import InMemoryApprovalStore
from harness.approval.gate import ApprovalGate
from harness.integration.multi_task import (
    IntegrationStage,
    MergeStage,
    MultiTaskIntegrationEngine,
    MultiTaskRequest,
    SubprocessIntegrationGit,
    TaskBranch,
)
from harness.verification.contract import AcceptanceContract, RequiredCheck
from harness.verification.engine import CheckSpec, VerificationEngine, VerificationPlan

BATCH = "batch-1"
BRANCH = "integration/batch-1"

#: 仓库里的自检脚本。
#:
#: **它收集所有 ``*_expected.txt``，而不是只读一份。** 合并前每个分支各自
#: 只有一份期望文件；合并后多出来的那几份就是"别的分支带来的期望"。两个
#: 期望不一致，就是"合并之后跑不过"的最小复现 —— 而且复现出来的报错是
#: 人话（"期望文件互相矛盾"），不是 git 的英文。
_CHECK_SCRIPT = """\
import glob

from dep import f

files = sorted(glob.glob("*_expected.txt"))
if not files:
    raise SystemExit("没有任何期望文件 —— 无从判断")

values = {path: open(path, encoding="utf-8").read().strip() for path in files}
if len(set(values.values())) != 1:
    raise SystemExit(f"期望文件互相矛盾：{values}")

expected = next(iter(values.values()))
actual = str(f())
if actual != expected:
    raise SystemExit(f"dep.f() == {actual}，期望 {expected}")
print("ok")
"""


def _git(cwd: Path, *args: str) -> str:
    """跑一条 git 命令，返回 stdout。"""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：\n{result.stdout}\n{result.stderr}")
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个真 git 仓库，有一个初始提交。

    初始提交是必需的：``git worktree add`` 要从 HEAD 检出，没有 HEAD 的
    仓库建不出 worktree。
    """
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "ases@example.invalid")
    _git(path, "config", "user.name", "ASES")
    (path / "dep.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (path / "mod.py").write_text("def base():\n    return 0\n", encoding="utf-8")
    (path / "check.py").write_text(_CHECK_SCRIPT, encoding="utf-8")
    (path / "base_expected.txt").write_text("1\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "初始提交")
    return path


def _worktree(repo: Path, tmp_path: Path, branch: str, name: str) -> Path:
    """从 ``repo`` 拉一个分支，并检出成独立工作区。"""
    _git(repo, "branch", branch)
    worktree = tmp_path / name
    _git(repo, "worktree", "add", str(worktree), branch)
    return worktree


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-m", message)


def _engine() -> MultiTaskIntegrationEngine:
    """真的集成引擎 —— 真的 git、真的子进程、真的审批门。"""
    return MultiTaskIntegrationEngine(
        git=SubprocessIntegrationGit(),
        engine=VerificationEngine(),
        gate=ApprovalGate(InMemoryApprovalStore()),
    )


def _plan() -> VerificationPlan:
    """验证计划 —— 自检命令就是在工作区里跑 ``check.py``。

    ``-B`` 是刻意的：不写字节码，工作区才不会多出 ``__pycache__`` ——
    那会让"合并后工作区是干净的"这条断言变成在测 gitignore。
    """
    return VerificationPlan(
        checks=(
            CheckSpec(
                check=RequiredCheck.UNIT_TESTS_PASS,
                command=(sys.executable, "-B", "check.py"),
            ),
        )
    )


def _task(task_id: str, workspace: Path) -> TaskBranch:
    """一个待集成的任务分支。"""
    return TaskBranch(
        task_id=task_id,
        branch=f"agent/{task_id}",
        contract=AcceptanceContract(task_id=task_id, required=(RequiredCheck.UNIT_TESTS_PASS,)),
        plan=_plan(),
        workspace=workspace,
    )


def _request(
    repo: Path,
    integration: Path,
    tasks: tuple[TaskBranch, ...],
) -> MultiTaskRequest:
    """一次多任务集成请求。"""
    return MultiTaskRequest(
        batch_id=BATCH,
        project_id="proj-1",
        agent_id="agent-1",
        integration_branch=BRANCH,
        integration_workspace=integration,
        main_workspace=repo,
        tasks=tasks,
        joint_contract=AcceptanceContract(task_id=BATCH, required=(RequiredCheck.UNIT_TESTS_PASS,)),
        joint_plan=_plan(),
        requested_by="agent-1",
        diff_summary="两个任务合到一起",
    )


def _green(workspace: Path) -> bool:
    """工作区里的自检过不过。"""
    result = subprocess.run(
        [sys.executable, "-B", "check.py"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


class TestTheSymbolConflictBlocksTheMerge:
    """T1 —— 符号冲突要检得出，也要挡得住。"""

    @pytest.fixture
    def conflicted(self, repo: Path, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        """两个任务各自新增一个同名的 ``calculate``，但加在不同位置。

        **git 合得干干净净** —— 冲突不在同一段文本上。而合出来的 ``mod.py``
        里有两个 ``calculate``，后一个把前一个盖掉：代码"跑得起来"，但已经
        不是人写的那份了。**这正是 git 挡不住、这一层要挡的那一类。**
        """
        task_a = _worktree(repo, tmp_path, "agent/task-a", "task-a")
        task_b = _worktree(repo, tmp_path, "agent/task-b", "task-b")
        (task_a / "mod.py").write_text(
            "def calculate():\n    return 1\n\n\ndef base():\n    return 0\n", encoding="utf-8"
        )
        _commit(task_a, "任务 A 加 calculate")
        (task_b / "mod.py").write_text(
            "def base():\n    return 0\n\n\ndef calculate():\n    return 2\n", encoding="utf-8"
        )
        _commit(task_b, "任务 B 加 calculate")
        integration = _worktree(repo, tmp_path, BRANCH, "integration")
        return repo, integration, task_a, task_b

    async def test_each_task_passes_on_its_own(self, conflicted: tuple[Path, ...]) -> None:
        """**先确认它们自己确实是绿的。**

        不然"联合运行才出问题"这句话就没有对照 —— 可能是某个任务本来就没通过，
        那跟集成没关系。
        """
        _repo, _integration, task_a, task_b = conflicted

        assert _green(task_a)
        assert _green(task_b)

    async def test_the_conflict_is_detected(self, conflicted: tuple[Path, ...]) -> None:
        """**精准检出** —— 说得出是哪个文件、哪个符号、哪两个任务。"""
        repo, integration, task_a, task_b = conflicted
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

        outcome = await engine.integrate(request)

        assert outcome.stage is IntegrationStage.SYMBOL_CONFLICTED
        assert len(outcome.conflicts) == 1
        assert outcome.conflicts[0].name == "calculate"
        assert outcome.conflicts[0].path == "mod.py"
        assert outcome.conflicts[0].tasks == ("task-a", "task-b")

    async def test_the_main_does_not_move(self, conflicted: tuple[Path, ...]) -> None:
        """**main 一个提交都不许前进。**"""
        repo, integration, task_a, task_b = conflicted
        before = _git(repo, "rev-parse", "HEAD")
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

        await engine.integrate(request)

        assert _git(repo, "rev-parse", "HEAD") == before

    async def test_the_integration_branch_is_wound_back(self, conflicted: tuple[Path, ...]) -> None:
        """**失败不留半截** —— 集成分支拨回 main 原处。"""
        repo, integration, task_a, task_b = conflicted
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

        await engine.integrate(request)

        assert _git(integration, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")

    async def test_the_batch_produces_nothing_to_merge(self, conflicted: tuple[Path, ...]) -> None:
        """**冲突没过就没有可合并的东西** —— 编排层拿不到集成分支的提交，
        人也不该被叫来批一个已知有问题的批次。"""
        repo, integration, task_a, task_b = conflicted
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

        outcome = await engine.integrate(request)

        assert outcome.integrated is False
        assert outcome.integration_commit is None


class TestTheJointRegressionBlocksTheMerge:
    """R-057 —— 两个任务各自都绿，合在一起不绿。"""

    @pytest.fixture
    def stale(self, repo: Path, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        """两个"生锈"的任务分支：它们是从旧基线拉出来的。

        main 后来把依赖改了（并同步了期望），任务分支还停在旧基线上 ——
        它们各自那份期望文件说的是旧值。**每个分支单独看都是绿的，合起来
        不是。**
        """
        _git(repo, "branch", "agent/task-a")
        _git(repo, "branch", "agent/task-b")
        # main 先走一步：依赖改成返回 2，期望文件同步跟上。
        (repo / "dep.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        (repo / "base_expected.txt").write_text("2\n", encoding="utf-8")
        _commit(repo, "main 改依赖")

        task_a = tmp_path / "task-a"
        _git(repo, "worktree", "add", str(task_a), "agent/task-a")
        (task_a / "a_expected.txt").write_text("1\n", encoding="utf-8")
        _commit(task_a, "任务 A 加自己的期望")

        task_b = tmp_path / "task-b"
        _git(repo, "worktree", "add", str(task_b), "agent/task-b")
        (task_b / "b_expected.txt").write_text("1\n", encoding="utf-8")
        _commit(task_b, "任务 B 加自己的期望")

        integration = _worktree(repo, tmp_path, BRANCH, "integration")
        return repo, integration, task_a, task_b

    async def test_each_task_passes_on_its_own(self, stale: tuple[Path, ...]) -> None:
        _repo, _integration, task_a, task_b = stale

        assert _green(task_a)
        assert _green(task_b)

    async def test_the_joint_regression_fails(self, stale: tuple[Path, ...]) -> None:
        """**联合回归专门看这一件事** —— 合在一起才出的问题。"""
        repo, integration, task_a, task_b = stale
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

        outcome = await engine.integrate(request)

        assert outcome.stage is IntegrationStage.REGRESSION_FAILED
        assert outcome.integrated_tasks == ("task-a", "task-b")

    async def test_the_main_does_not_move(self, stale: tuple[Path, ...]) -> None:
        repo, integration, task_a, task_b = stale
        before = _git(repo, "rev-parse", "HEAD")
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

        await engine.integrate(request)

        assert _git(repo, "rev-parse", "HEAD") == before


class TestTheUnverifiedTaskIsNotMerged:
    """先验后合 —— 没验过的分支不该被合进来。"""

    async def test_the_local_validation_failure_blocks_the_batch(
        self, repo: Path, tmp_path: Path
    ) -> None:
        task_a = _worktree(repo, tmp_path, "agent/task-a", "task-a")
        task_b = _worktree(repo, tmp_path, "agent/task-b", "task-b")
        # 任务 A 自己就红：它带来的期望文件与依赖对不上。
        (task_a / "a_expected.txt").write_text("2\n", encoding="utf-8")
        _commit(task_a, "任务 A 带了个错的期望")
        (task_b / "b_expected.txt").write_text("1\n", encoding="utf-8")
        _commit(task_b, "任务 B 加期望")
        integration = _worktree(repo, tmp_path, BRANCH, "integration")
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

        outcome = await engine.integrate(request)

        assert outcome.stage is IntegrationStage.LOCAL_VALIDATION_FAILED
        assert outcome.blocking_task == "task-a"
        assert outcome.integrated_tasks == ()

    async def test_the_integration_branch_was_not_touched(self, repo: Path, tmp_path: Path) -> None:
        """**没验过的分支，一个提交都不许进集成分支。**"""
        task_a = _worktree(repo, tmp_path, "agent/task-a", "task-a")
        task_b = _worktree(repo, tmp_path, "agent/task-b", "task-b")
        (task_a / "a_expected.txt").write_text("2\n", encoding="utf-8")
        _commit(task_a, "任务 A 带了个错的期望")
        integration = _worktree(repo, tmp_path, BRANCH, "integration")
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

        await engine.integrate(request)

        assert _git(integration, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")


class TestTheBatchLandsInMain:
    """都通的时候，一批任务确实能落到 main 上。"""

    async def test_the_batch_is_integrated_and_merged(self, repo: Path, tmp_path: Path) -> None:
        task_a = _worktree(repo, tmp_path, "agent/task-a", "task-a")
        task_b = _worktree(repo, tmp_path, "agent/task-b", "task-b")
        (task_a / "a_feature.txt").write_text("任务 A 的功能\n", encoding="utf-8")
        _commit(task_a, "任务 A 加功能")
        (task_b / "b_feature.txt").write_text("任务 B 的功能\n", encoding="utf-8")
        _commit(task_b, "任务 B 加功能")
        integration = _worktree(repo, tmp_path, BRANCH, "integration")
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

        outcome = await engine.integrate(request)

        assert outcome.stage is IntegrationStage.INTEGRATED
        assert outcome.integrated_tasks == ("task-a", "task-b")
        assert outcome.integration_commit is not None

        waiting = await engine.merge_to_main(request, integration_commit=outcome.integration_commit)
        assert waiting.stage is MergeStage.AWAITING_APPROVAL
        await engine.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        merge = await engine.merge_to_main(request, integration_commit=outcome.integration_commit)

        assert merge.stage is MergeStage.MERGED
        assert (repo / "a_feature.txt").read_text(encoding="utf-8") == "任务 A 的功能\n"
        assert (repo / "b_feature.txt").read_text(encoding="utf-8") == "任务 B 的功能\n"

    async def test_the_main_stays_green(self, repo: Path, tmp_path: Path) -> None:
        """**合进去的东西自己还是过的** —— 合并动作不破坏基线。"""
        task_a = _worktree(repo, tmp_path, "agent/task-a", "task-a")
        task_b = _worktree(repo, tmp_path, "agent/task-b", "task-b")
        (task_a / "a_expected.txt").write_text("1\n", encoding="utf-8")
        _commit(task_a, "任务 A 加期望")
        (task_b / "b_expected.txt").write_text("1\n", encoding="utf-8")
        _commit(task_b, "任务 B 加期望")
        integration = _worktree(repo, tmp_path, BRANCH, "integration")
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))
        outcome = await engine.integrate(request)
        assert outcome.integration_commit is not None
        waiting = await engine.merge_to_main(request, integration_commit=outcome.integration_commit)
        await engine.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        await engine.merge_to_main(request, integration_commit=outcome.integration_commit)

        assert _green(repo)

    async def test_the_main_has_a_merge_commit(self, repo: Path, tmp_path: Path) -> None:
        """**合过一次就在历史里留得下痕迹** —— main 上是一个合并提交。"""
        task_a = _worktree(repo, tmp_path, "agent/task-a", "task-a")
        task_b = _worktree(repo, tmp_path, "agent/task-b", "task-b")
        (task_a / "a_feature.txt").write_text("任务 A 的功能\n", encoding="utf-8")
        _commit(task_a, "任务 A 加功能")
        (task_b / "b_feature.txt").write_text("任务 B 的功能\n", encoding="utf-8")
        _commit(task_b, "任务 B 加功能")
        integration = _worktree(repo, tmp_path, BRANCH, "integration")
        engine = _engine()
        request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))
        outcome = await engine.integrate(request)
        assert outcome.integration_commit is not None
        waiting = await engine.merge_to_main(request, integration_commit=outcome.integration_commit)
        await engine.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        await engine.merge_to_main(request, integration_commit=outcome.integration_commit)

        parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
        assert len(parents) == 3
