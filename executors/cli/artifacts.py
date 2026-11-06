"""产出物的事实来源 —— 工作空间里的 git 状态。

**执行器不产出代码，代码是 Agent 改出来的。** 所以"这次执行改了什么"这件事
**只能从工作空间里读**，不能从 CLI 的输出里猜：CLI 说的是"我做了什么"，
git 记的是"仓库现在是什么样"。后者才是事实 —— 前者只是自我报告
（design.md §3.4："self_assessment 只是 Agent 的自我报告，不是验收结果"）。

于是这一层只做一件事：把工作空间的 git 状态读成一份
:class:`WorkspaceState`。

**分支从哪来。** 优先用**事实**（工作空间当前检出的分支）；读不出来时退回
**约定**（``agent/<task_id>``）。约定取自
:func:`harness.workspace.naming.branch_for_task` —— **引用它而不是复述它**：
复述一遍就会漂移，而漂移的分支名会让两个执行器报出不同的产出位置。

为什么需要这个退路：执行器可能拿到一个还不是 git 仓库的目录（第一次接入
时的工作空间、只读挂载的目录）。那时候没有事实可说，但按约定报出分支名
仍然比报 ``None`` 有用 —— 调用方能知道"产出应该在 ``agent/<task_id>`` 上"。
而 commit 仍然是 ``None``：**没有提交就是没有提交**，不拿别的东西顶替。

**改了哪些文件，比"提交了几次"更接近产出。** 所以基线取会话开始时的
``HEAD``，之后比的是"工作树相对基线变了什么"—— 提交了的和没提交的都算。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from harness.workspace.naming import InvalidTaskIdError, branch_for_task

#: 读 git 时的超时（秒）。git 在正常情况下是毫秒级的；给它一个上限，
#: 是为了不让"工作空间卡住"变成"整个执行卡住"。
_GIT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """工作空间在某一刻的状态。"""

    branch: str | None
    commit: str | None
    changed_files: int


class WorkspaceInspector(Protocol):
    """读工作空间状态的接口。

    **为什么是接口而不是直接调 git。** 执行器这一层不该假设工作空间一定是
    git 仓库 —— 换了别的版本控制系统、或者测试里用一个假的，都不该要求改
    执行器。同时，测试要能**断言**"执行器到底查了哪些 git 命令"，而那需要
    一个能被替换的实现。
    """

    def baseline(self, workspace: Path) -> str | None:
        """会话开始时的基线提交 —— 用来算"这次改了什么"。"""
        ...

    def snapshot(self, workspace: Path, baseline: str | None) -> WorkspaceState:
        """当前的状态。"""
        ...


def fallback_branch(task_id: str) -> str | None:
    """工作空间读不出分支时，按约定回退。

    读不出分支有两种情况：工作空间不是 git 仓库，或者 ``task_id`` 本身不能
    用来拼分支名（路径穿越风险）。后者一律不给分支 —— 宁可少报一个字段，
    也不报一个可能是 ``agent/../../etc`` 的分支名。
    """
    try:
        return branch_for_task(task_id)
    except InvalidTaskIdError:
        return None


class GitWorkspaceInspector:
    """用真的 git 读工作空间状态。"""

    def __init__(self, git_binary: str = "git") -> None:
        self._git = git_binary

    def baseline(self, workspace: Path) -> str | None:
        return self._run(workspace, "rev-parse", "HEAD")

    def snapshot(self, workspace: Path, baseline: str | None) -> WorkspaceState:
        branch = self._run(workspace, "symbolic-ref", "--short", "HEAD")
        commit = self._run(workspace, "rev-parse", "HEAD")
        changed = 0
        if baseline is not None:
            changed = self._changed_files(workspace, baseline)
        return WorkspaceState(branch=branch, commit=commit, changed_files=changed)

    def _changed_files(self, workspace: Path, baseline: str) -> int:
        """相对基线变了多少个文件。

        两个来源取并集：``diff`` 给出已跟踪文件的改动（含已提交与未提交），
        ``ls-files --others`` 给出新建的未跟踪文件 —— 只看 diff 会漏掉
        "Agent 新建了一个文件但还没提交"这种情况。
        """
        names = set(self._run_lines(workspace, "diff", "--name-only", baseline))
        names.update(self._run_lines(workspace, "ls-files", "--others", "--exclude-standard"))
        return len(names)

    def _run(self, workspace: Path, *args: str) -> str | None:
        result = self._call(workspace, *args)
        if result is None:
            return None
        text = result.stdout.strip()
        return text or None

    def _run_lines(self, workspace: Path, *args: str) -> list[str]:
        result = self._call(workspace, *args)
        if result is None:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _call(self, workspace: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            completed = subprocess.run(
                [self._git, *args],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            # git 不在、目录不是仓库、命令超时 —— 一律当作"读不出来"。
            # 读不出来不是错误：工作空间未必是 git 仓库。
            return None
        if completed.returncode != 0:
            return None
        return completed


__all__ = [
    "GitWorkspaceInspector",
    "WorkspaceInspector",
    "WorkspaceState",
    "fallback_branch",
]
