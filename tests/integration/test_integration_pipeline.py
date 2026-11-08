"""TASK-P1-047 —— 真 git 上的最小集成路径。

**单元测试用假 git，是因为"次序错了"在假 git 上看不出来。** 假合并口会
老老实实按你写的顺序调方法；真 git 才会在"暂存了但没提交"的时候，让
main 的工作区真的停在半路 —— 而那正是这条路径最要紧的一处：合并后跑不过
时，main 必须**一个提交都没前进**。

所以这一组用真的 ``git`` 命令跑一遍，验三件事：

- **T1 ③**：合并之后 main 的测试仍然通过 —— 合并动作本身不破坏基线；
- **R-057**：任务分支上验过了，合进 main 之后可能就不过 —— 而 main 会回到
  合并前的样子（HEAD 不动、文件逐字不变、工作区干净）；
- **T1 ②**：没验过的分支进不了合并流程 —— 连审批单都不会有。

需要真的装了 git。不需要 Docker 或数据库，所以不依赖那些夹具。

**R-057 的复现靠一个刻意设计的仓库。** 每个分支各自有一份"期望文件"，
写明依赖该返回什么；自检脚本收集**所有**期望文件并要求它们一致：

    base 提交：dep.py 返回 1，base_expected.txt 写着 1     → 绿
    main 提交：dep.py 改成返回 2，期望文件同步改成 2      → 绿
    任务分支：  从 base 拉出，加一份自己的期望文件写着 1   → 绿

三个都绿。合到一起就红了：``dep.py`` 取 main 的（返回 2），而任务分支
带来的那份期望文件写着 1 —— **两个期望对不上**。这正是"单任务通过 ≠ 集成
后通过"的样子，而且合并本身**没有冲突**（没有一份文件被两边同时改过），
所以它考验的是集成路径，不是 git 的冲突处理。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.policy.approval import InMemoryApprovalStore
from core.policy.autonomy import Operation
from core.task.state_machine import TaskState
from harness.approval.gate import ApprovalGate
from harness.integration.pipeline import (
    IntegrationPipeline,
    PipelineRequest,
    PipelineStage,
    SubprocessBranchMerger,
)
from harness.verification.contract import AcceptanceContract, RequiredCheck
from harness.verification.engine import CheckSpec, VerificationEngine, VerificationPlan

TASK_ID = "TASK-1"
BRANCH = "task/TASK-1"

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


def _git_exit(cwd: Path, *args: str) -> int:
    """跑一条 git 命令，只返回退出码。

    **给"这条命令本来就该失败"的断言用。** 比如"撤销之后没有半个合并留在
    原地"要问的是 ``MERGE_HEAD`` 在不在 —— 问法是"git 找不到它"，也就是
    非零退出码。让 :func:`_git` 来问，它会把"问到了否定答案"当成命令失败
    抛出来。
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def _snapshot(repo: Path) -> dict[str, str]:
    """仓库里每个受版本控制的文件的内容。

    **按内容比，不按 mtime 比。** "没动过"的判据是文件逐字相同 —— 时间戳
    会因为 checkout、换页、文件系统精度而变，拿它当判据只会得到假失败。
    """
    names = _git(repo, "ls-files").split()
    return {name: (repo / name).read_text(encoding="utf-8") for name in names}


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
    (path / "check.py").write_text(_CHECK_SCRIPT, encoding="utf-8")
    (path / "base_expected.txt").write_text("1\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "初始提交")
    return path


def _pipeline() -> IntegrationPipeline:
    """一条真的集成路径 —— 真的 git、真的子进程、真的审批门。"""
    return IntegrationPipeline(
        engine=VerificationEngine(),
        gate=ApprovalGate(InMemoryApprovalStore()),
        merger=SubprocessBranchMerger(),
    )


def _request(task_workspace: Path, main_workspace: Path) -> PipelineRequest:
    """一次集成请求 —— 自检命令就是在工作区里跑 ``check.py``。

    ``-B`` 是刻意的：不写字节码，工作区才不会多出 ``__pycache__`` ——
    那会让"合并后工作区是干净的"这条断言变成在测 gitignore。
    """
    return PipelineRequest(
        task_id=TASK_ID,
        agent_id="agent-1",
        project_id="proj-1",
        branch=BRANCH,
        task_workspace=task_workspace,
        main_workspace=main_workspace,
        contract=AcceptanceContract(
            task_id=TASK_ID,
            required=(RequiredCheck.UNIT_TESTS_PASS,),
        ),
        plan=VerificationPlan(
            checks=(
                CheckSpec(
                    check=RequiredCheck.UNIT_TESTS_PASS,
                    command=(sys.executable, "-B", "check.py"),
                ),
            )
        ),
        diff_summary="改一行依赖与期望",
        requested_by="agent-1",
    )


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-m", message)


def _task_worktree(repo: Path, tmp_path: Path) -> Path:
    """从 ``repo`` 当前 HEAD 拉一个任务分支，并检出成独立工作区。"""
    _git(repo, "branch", BRANCH)
    worktree = tmp_path / "task"
    _git(repo, "worktree", "add", str(worktree), BRANCH)
    return worktree


class TestTheMergedMainStillPasses:
    """T1 ③ —— 合并之后 main 的测试仍然通过。"""

    async def test_the_task_branch_lands_in_main_and_main_stays_green(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """合并动作本身不破坏基线。

        任务分支从 main 的当前位置拉出，只加一份新文件 —— 合进 main 之后，
        main 上那份自检必须照样通过，而且 main 真的前进了一个合并提交。
        """
        task = _task_worktree(repo, tmp_path)
        (task / "feature.txt").write_text("新功能\n", encoding="utf-8")
        _commit(task, "任务分支加功能")

        pipeline = _pipeline()
        request = _request(task, repo)

        waiting = await pipeline.run(request)
        assert waiting.stage is PipelineStage.AWAITING_APPROVAL
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        merged = await pipeline.run(request)

        assert merged.stage is PipelineStage.MERGED
        assert merged.task_state is TaskState.COMPLETED
        assert (repo / "feature.txt").read_text(encoding="utf-8") == "新功能\n"
        # main 真的产生了一个**合并**提交（--no-ff：合过一次就要在历史里
        # 留得下痕迹）。
        parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
        assert len(parents) == 3
        # main 上的自检仍然通过 —— 这是 T1 ③ 的正脸。
        check = subprocess.run(
            [sys.executable, "-B", "check.py"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        assert check.returncode == 0, check.stdout + check.stderr

    async def test_the_merged_commit_lands_the_whole_branch(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """合进 main 的是**整个分支**，不是最后一次改动。"""
        task = _task_worktree(repo, tmp_path)
        (task / "one.txt").write_text("一\n", encoding="utf-8")
        _commit(task, "第一处")
        (task / "two.txt").write_text("二\n", encoding="utf-8")
        _commit(task, "第二处")

        pipeline = _pipeline()
        request = _request(task, repo)
        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        await pipeline.run(request)

        assert (repo / "one.txt").exists()
        assert (repo / "two.txt").exists()


class TestTheStaleBranch:
    """R-057 —— 单任务通过 ≠ 集成后通过。"""

    @pytest.fixture
    def stale(self, repo: Path, tmp_path: Path) -> Path:
        """一个"生锈"的任务分支：它是从旧基线拉出来的。

        main 后来把依赖改了（并同步了期望），任务分支还停在旧基线上 ——
        它自己那份期望文件说的是旧值。三个分支各自都是绿的，合起来不是。
        """
        _git(repo, "branch", BRANCH)
        # main 先走一步：依赖改成返回 2，期望文件同步跟上。
        (repo / "dep.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        (repo / "base_expected.txt").write_text("2\n", encoding="utf-8")
        (repo / "main_expected.txt").write_text("2\n", encoding="utf-8")
        _commit(repo, "main 改依赖")

        task = tmp_path / "task"
        _git(repo, "worktree", "add", str(task), BRANCH)
        (task / "task_expected.txt").write_text("1\n", encoding="utf-8")
        _commit(task, "任务分支加自己的期望")
        return task

    async def test_the_task_branch_passes_on_its_own(self, stale: Path) -> None:
        """**先确认它自己确实是绿的。**

        不然"合并后跑不过"这句话就没有对照 —— 可能是任务分支本来就没通过，
        那跟集成没关系。
        """
        check = subprocess.run(
            [sys.executable, "-B", "check.py"],
            cwd=str(stale),
            capture_output=True,
            text=True,
            check=False,
        )
        assert check.returncode == 0, check.stdout + check.stderr

    async def test_the_merge_is_rolled_back_when_main_stops_passing(
        self, stale: Path, repo: Path
    ) -> None:
        """**main 一个提交都没前进。**

        合并后的内容跑不过 → 撤销合并 → main 回到合并前的样子：HEAD 不动、
        受版本控制的文件逐字不变、工作区干净。坏的东西进不去。
        """
        head_before = _git(repo, "rev-parse", "HEAD").strip()
        files_before = _snapshot(repo)

        pipeline = _pipeline()
        request = _request(stale, repo)
        waiting = await pipeline.run(request)
        assert waiting.stage is PipelineStage.AWAITING_APPROVAL
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGED_CONTENT_FAILED
        assert result.task_state is TaskState.REPAIRING
        # 失败的理由是"合并之后两个期望对不上" —— 不是"脚本没跑起来"之类
        # 别的什么。理由写错了，后面的人会照着错的方向去查。
        assert any("矛盾" in reason for reason in result.reasons)
        assert _git(repo, "rev-parse", "HEAD").strip() == head_before
        assert _snapshot(repo) == files_before
        assert _git(repo, "status", "--porcelain").strip() == ""
        # 撤销之后没有半个合并留在原地。
        assert _git_exit(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD") != 0

    async def test_main_is_still_green_after_the_rollback(self, stale: Path, repo: Path) -> None:
        """撤销之后 main 还是原来那个绿的样子 —— 不是"变成了红"。"""
        pipeline = _pipeline()
        request = _request(stale, repo)
        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        await pipeline.run(request)

        check = subprocess.run(
            [sys.executable, "-B", "check.py"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        assert check.returncode == 0, check.stdout + check.stderr


class TestTheMergeThatCannotHappen:
    """合并这一步的两种"没成" —— 都必须把 main 原样留下。"""

    async def test_a_real_conflict_leaves_main_untouched(self, repo: Path, tmp_path: Path) -> None:
        """**撞上冲突时 main 不能停在"合并进行中"。**

        冲突是**半完成**的：MERGE_HEAD 已经写下了，索引里留着冲突标记。
        不撤销的话，main 会一直停在这个状态 —— 那比这次合并失败严重得多。
        """
        task = _task_worktree(repo, tmp_path)
        (task / "dep.py").write_text("def f():\n    return 3\n", encoding="utf-8")
        (task / "base_expected.txt").write_text("3\n", encoding="utf-8")
        _commit(task, "任务分支改依赖")
        # main 改同一处 —— 合并必撞。
        (repo / "dep.py").write_text("def f():\n    return 4\n", encoding="utf-8")
        (repo / "base_expected.txt").write_text("4\n", encoding="utf-8")
        _commit(repo, "main 也改依赖")

        head_before = _git(repo, "rev-parse", "HEAD").strip()
        files_before = _snapshot(repo)

        pipeline = _pipeline()
        request = _request(task, repo)
        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGE_CONFLICTED
        assert result.task_state is TaskState.REPAIRING
        assert _git(repo, "rev-parse", "HEAD").strip() == head_before
        assert _snapshot(repo) == files_before
        assert _git(repo, "status", "--porcelain").strip() == ""
        assert _git_exit(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD") != 0

    async def test_a_merge_git_refuses_leaves_main_untouched(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """**合并这一步自己没干成，main 也不能停在半路。**

        这里失败的原因与内容无关：main 的工作区里有没提交的改动，而任务
        分支要改的正是那个文件 —— git 拒绝合并。

        **这是环境问题，不是代码问题。** 处置是**阻塞**（等人看一眼），不是
        返工（让 Agent 去改代码）—— 代码没错，改代码也解决不了"main 上有人
        留了没提交的改动"。
        """
        task = _task_worktree(repo, tmp_path)
        # 任务分支改依赖 —— 它自己的那份自检仍然是绿的。
        (task / "dep.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        (task / "base_expected.txt").write_text("2\n", encoding="utf-8")
        _commit(task, "任务分支改依赖")
        # main 的工作区有没提交的改动，改的正是那个文件 —— 合并必然被拒。
        (repo / "dep.py").write_text("def f():\n    return 99\n", encoding="utf-8")

        head_before = _git(repo, "rev-parse", "HEAD").strip()
        files_before = _snapshot(repo)

        pipeline = _pipeline()
        request = _request(task, repo)
        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGE_FAILED
        assert result.task_state is TaskState.BLOCKED
        assert _git(repo, "rev-parse", "HEAD").strip() == head_before
        assert _snapshot(repo) == files_before
        # 那份没提交的改动还在原处，而且**没有被暂存**（第二列是空格）——
        # git 拒绝合并时一个字节都没碰过它。
        assert _git(repo, "status", "--porcelain") == " M dep.py\n"
        assert _git_exit(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD") != 0

    async def test_a_failed_commit_still_rolls_the_merge_back(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**提交失败也必须撤销合并 —— 这一处最容易漏。**

        合并成功、提交失败，是这条路径上最不对称的一种情形：暂存已经把
        MERGE_HEAD 写进 main 了，索引里也躺着合并结果。这时提交没干成，
        main 就停在"合并进行中"—— 不撤销的话，它会一直停在那儿，下一个
        人接手时看到的是个坏掉的仓库。

        **用"作者日期不合法"让提交失败，而不是"没配身份"。** 因为
        ``git merge --no-ff --no-commit`` 自己也认提交者 —— 拿身份问题去试，
        失败会发生在**合并**那一步，根本走不到提交，测的就不是这里了。而
        ``GIT_AUTHOR_DATE`` 只有真要写下提交对象时才被解析。
        """
        task = _task_worktree(repo, tmp_path)
        (task / "feature.txt").write_text("新功能\n", encoding="utf-8")
        _commit(task, "任务分支加功能")

        monkeypatch.setenv("GIT_AUTHOR_DATE", "not-a-date")

        head_before = _git(repo, "rev-parse", "HEAD").strip()
        files_before = _snapshot(repo)

        pipeline = _pipeline()
        request = _request(task, repo)
        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGE_FAILED
        assert result.task_state is TaskState.BLOCKED
        # 钉住"失败的是提交那一步"—— 万一哪天 git 提前在合并那步就失败
        # 了，这条断言会响，而不是让这条用例悄悄变成测别的东西。
        assert any("提交合并失败" in reason for reason in result.reasons), result.reasons
        assert _git(repo, "rev-parse", "HEAD").strip() == head_before
        assert _snapshot(repo) == files_before
        assert _git(repo, "status", "--porcelain").strip() == ""
        assert _git_exit(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD") != 0

    async def test_the_merge_commit_runs_no_hook_at_all(self, repo: Path, tmp_path: Path) -> None:
        """**合并提交一个钩子都不跑。**

        ``--no-verify`` 只跳过 ``pre-commit`` 与 ``commit-msg``；
        ``prepare-commit-msg`` 照样跑 —— 而钩子能改工作区里的文件。改过的
        东西没有被验过，"验过的就是合进去的"这条就不成立了。

        所以这条用例盯的是"一个都没跑"，不是"常见的两个没跑"：钩子往仓库
        里写一个标记文件，合并之后那个文件不该存在。

        钩子目录放在仓库外面 —— 放里面的话，仓库就会多出一个没被跟踪的
        目录，"工作区是干净的"那条断言就变成在测 gitignore 了。
        """
        task = _task_worktree(repo, tmp_path)
        (task / "feature.txt").write_text("新功能\n", encoding="utf-8")
        _commit(task, "任务分支加功能")

        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "prepare-commit-msg"
        hook.write_text("#!/bin/sh\necho ran > hook-ran.txt\n", encoding="utf-8")
        hook.chmod(0o755)
        _git(repo, "config", "core.hooksPath", str(hooks))

        pipeline = _pipeline()
        request = _request(task, repo)
        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGED
        assert not (repo / "hook-ran.txt").exists()


class TestTheUnverifiedBranch:
    """T1 ② —— 没验过的分支进不了合并流程。"""

    async def test_a_branch_that_fails_its_own_check_never_reaches_an_approval(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """没验过的改动不该出现在人的审批清单里。

        人看到一张审批单，会以为"这东西已经验过了" —— 审批单上写着验证证据
        引用，它就该真的是。
        """
        task = _task_worktree(repo, tmp_path)
        (task / "base_expected.txt").write_text("9\n", encoding="utf-8")
        _commit(task, "写错期望")

        pipeline = _pipeline()
        head_before = _git(repo, "rev-parse", "HEAD").strip()

        result = await pipeline.run(_request(task, repo))

        assert result.stage is PipelineStage.LOCAL_VALIDATION_FAILED
        assert result.task_state is TaskState.REPAIRING
        assert await pipeline.gate.store.list_for_task(TASK_ID) == ()
        assert _git(repo, "rev-parse", "HEAD").strip() == head_before
        assert _git(repo, "status", "--porcelain").strip() == ""

    async def test_a_branch_that_fails_its_own_check_is_never_merged(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """就算有人硬批了，验不过的分支也合不进去。

        **审批不是绕过验证的开关。** 人批的是"这份改动可以进 main"，而
        "这份改动验得过"是另一件事 —— 合并前那一次验证不是给人看的仪式。
        """
        task = _task_worktree(repo, tmp_path)
        (task / "base_expected.txt").write_text("9\n", encoding="utf-8")
        _commit(task, "写错期望")

        pipeline = _pipeline()
        request = _request(task, repo)

        # 手工发一条请求并立刻批掉 —— 模拟有人绕过路径直接批了。
        head = _git(task, "rev-parse", "HEAD").strip()
        created = await pipeline.gate.request(
            task_id=TASK_ID,
            operation=Operation.MERGE_TO_MAIN,
            diff_summary=f"commit:{head}\n写错期望",
            evidence_ref="local-verification:手动",
            requested_by="agent-1",
        )
        await pipeline.gate.approve(request_id=created.request_id, approver="alice")

        result = await pipeline.run(request)

        assert result.stage is PipelineStage.LOCAL_VALIDATION_FAILED
        assert result.merged_commit is None
        assert _git(repo, "status", "--porcelain").strip() == ""
