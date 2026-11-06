"""工作空间层的测试替身。

**为什么不用真 git 测管理器。** 管理器要回答的问题是"工作空间该怎么建、
怎么拆、怎么重置、什么时候算冲突"。这些问题的答案如果只能靠真 git 表达，
那么每一条测试都得先建一个真仓库 —— 慢，更要命的是**报错不可读**：真 git
只会说 "fatal: 'x' is already checked out"，而"管理器为什么认为这是冲突"
这个信息在那一行 stderr 里根本不存在。

所以分成两层：

- 这里 —— 假 git 与假时钟，用来精确构造场景（分支已存在、worktree 被占用、
  租约到期）；
- ``tests/integration/test_workspace_worktree.py`` —— 真 git，验行为。

**假 git 复刻了 git 最要紧的那条硬约束**：一条分支不能同时被两个 worktree
检出。这条约束是"分支冲突在结构上不可能发生"这句话的全部依据 —— 假实现
不守它，那这套测试就证明不了任何事。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.workspace.git import GitError, WorktreeEntry


class FakeClock:
    """可推进的时钟。

    租约的到期判断全靠时钟，所以"过期"这件事必须能**被制造** —— 用真实
    时间的话，测试要么等一小时，要么去 monkeypatch 时间函数。
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        """往前推。"""
        self._now += timedelta(seconds=seconds)


class FakeGitClient:
    """内存里的 git。

    只实现 :class:`~harness.workspace.git.GitClient` 协议里管理器用到的那
    几个方法，但**守住了 git 的分支/worktree 一对一约束**。
    """

    def __init__(self) -> None:
        self._worktrees: dict[Path, WorktreeEntry] = {}
        self._branches: set[str] = set()
        self.added: list[tuple[Path, Path, str]] = []
        self.removed: list[tuple[Path, Path]] = []
        self.resets: list[Path] = []
        self.cleans: list[Path] = []
        #: 让 ``add_worktree`` 失败 —— 用来测"建工作空间失败时租约要清掉"。
        self.fail_next_add = False

    # ---------- GitClient ----------

    def add_worktree(self, repo: Path, path: Path, branch: str) -> None:
        if self.fail_next_add:
            self.fail_next_add = False
            raise GitError(("git", "worktree", "add"), 128, "fatal: 模拟的失败")
        for entry in self._worktrees.values():
            if entry.branch == branch:
                raise GitError(
                    ("git", "worktree", "add"),
                    128,
                    f"fatal: '{branch}' is already checked out at '{entry.path}'",
                )
        resolved = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        self._worktrees[resolved] = WorktreeEntry(path=resolved, branch=branch, head="0" * 40)
        self._branches.add(branch)
        self.added.append((repo, path, branch))

    def remove_worktree(self, repo: Path, path: Path, *, force: bool) -> None:
        self.removed.append((repo, path))
        self._worktrees.pop(path.resolve(), None)

    def list_worktrees(self, repo: Path) -> tuple[WorktreeEntry, ...]:
        return tuple(self._worktrees.values())

    def branch_exists(self, repo: Path, branch: str) -> bool:
        return branch in self._branches

    def reset_hard(self, path: Path) -> None:
        self.resets.append(path)

    def clean_untracked(self, path: Path) -> None:
        self.cleans.append(path)

    def head_commit(self, path: Path) -> str:
        return "0" * 40

    # ---------- 测试用 ----------

    def checkout_elsewhere(self, path: Path, branch: str) -> None:
        """在别处检出某条分支 —— 用来构造"分支冲突"。"""
        resolved = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        self._worktrees[resolved] = WorktreeEntry(path=resolved, branch=branch, head="0" * 40)
        self._branches.add(branch)

    def forget(self, path: Path) -> None:
        """让一个 worktree "从 git 的登记里消失" —— 模拟崩溃遗留。"""
        self._worktrees.pop(path.resolve(), None)


__all__ = [
    "FakeClock",
    "FakeGitClient",
]
