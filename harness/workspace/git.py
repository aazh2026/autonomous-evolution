"""git worktree 的操作面 —— 协议与真实实现。

设计依据：docs/design.md §3.5A（"Git Worktree | 同一仓库的并行分支工作空间
| 必选"）、docs/tasks.md TASK-P1-032。

**为什么这里有协议而不是直接调 git。** 工作空间管理器要回答的问题是
"工作空间该怎么建、怎么拆、怎么重置"，而这些问题的答案不该被"怎么执行
git 命令"绑死 —— 一旦绑死，测管理器就得装一个真 git 仓库，而"管理器算
错了目录名"这类错误在真 git 上只会表现为一条看不懂的 stderr。

于是拆成两层：

- :class:`GitClient` —— 管理器**依赖**的东西（协议）；
- :class:`SubprocessGitClient` —— 真的去调 ``git`` 的那个实现。

管理器的逻辑用假实现测（快，且能精确构造"分支已存在""worktree 被占用"
这些场景），真 git 的行为由集成测试验（``tests/integration/``）。

**真实实现只做 git 真正支持的事。** 关键的一条：``git worktree add`` 在
分支已存在时**不能**再加 ``-b``，否则报 "branch already exists"；而分支
不存在时必须加 ``-b``，否则 git 会拿当前 HEAD 检出出一个游离分支。所以
"建分支还是复用分支"这个决定由 :meth:`SubprocessGitClient.add_worktree`
按分支是否已存在来做 —— 而不是由调用方猜。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class GitError(RuntimeError):
    """git 命令执行失败。"""

    def __init__(self, command: tuple[str, ...], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git 命令失败（退出码 {returncode}）：{' '.join(command)}\n{stderr.strip()}"
        )
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class WorktreeEntry:
    """git worktree 列表里的一项。"""

    path: Path
    branch: str | None
    head: str


@runtime_checkable
class GitClient(Protocol):
    """工作空间管理器需要 git 做的那些事。"""

    def add_worktree(self, repo: Path, path: Path, branch: str) -> None:
        """把 ``branch`` 检出到 ``path``。

        分支不存在就建，不存在就复用 —— 调用方不必先查分支。
        """

    def remove_worktree(self, repo: Path, path: Path, *, force: bool) -> None:
        """从 git 的 worktree 列表里摘掉 ``path``。"""

    def list_worktrees(self, repo: Path) -> tuple[WorktreeEntry, ...]:
        """列出这个仓库的所有 worktree。"""

    def branch_exists(self, repo: Path, branch: str) -> bool:
        """分支是否已经存在。"""

    def reset_hard(self, path: Path) -> None:
        """把工作区恢复到 HEAD（丢弃未提交的改动）。"""

    def clean_untracked(self, path: Path) -> None:
        """删掉未跟踪的文件与目录。"""

    def head_commit(self, path: Path) -> str:
        """当前 HEAD 的提交号。"""


class SubprocessGitClient:
    """真的去调 ``git`` 的实现。"""

    def __init__(self, *, git_binary: str = "git", timeout_seconds: int = 60) -> None:
        self._git = git_binary
        self._timeout = timeout_seconds

    def _run(self, args: tuple[str, ...], *, cwd: Path | None = None) -> str:
        command = (self._git, *args)
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(command, -1, f"git 命令超时（{self._timeout}s）") from exc
        if proc.returncode != 0:
            raise GitError(command, proc.returncode, proc.stderr)
        return proc.stdout

    def add_worktree(self, repo: Path, path: Path, branch: str) -> None:
        if self.branch_exists(repo, branch):
            self._run(("worktree", "add", str(path), branch), cwd=repo)
        else:
            self._run(("worktree", "add", "-b", branch, str(path)), cwd=repo)

    def remove_worktree(self, repo: Path, path: Path, *, force: bool) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        self._run(tuple(args), cwd=repo)

    def list_worktrees(self, repo: Path) -> tuple[WorktreeEntry, ...]:
        """解析 ``git worktree list --porcelain`` 的输出。"""
        raw = self._run(("worktree", "list", "--porcelain"), cwd=repo)
        entries: list[WorktreeEntry] = []
        current_path: Path | None = None
        current_branch: str | None = None
        current_head: str | None = None

        def _flush() -> None:
            nonlocal current_path, current_branch, current_head
            if current_path is not None:
                entries.append(
                    WorktreeEntry(
                        path=current_path,
                        branch=current_branch,
                        head=current_head or "",
                    )
                )
            current_path = None
            current_branch = None
            current_head = None

        for line in raw.splitlines():
            if not line.strip():
                _flush()
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                _flush()
                current_path = Path(value)
            elif key == "branch":
                current_branch = value.removeprefix("refs/heads/")
            elif key == "HEAD":
                current_head = value
        _flush()
        return tuple(entries)

    def branch_exists(self, repo: Path, branch: str) -> bool:
        proc = subprocess.run(
            (self._git, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"),
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
            timeout=self._timeout,
        )
        return proc.returncode == 0

    def reset_hard(self, path: Path) -> None:
        self._run(("reset", "--hard"), cwd=path)

    def clean_untracked(self, path: Path) -> None:
        self._run(("clean", "-fd"), cwd=path)

    def head_commit(self, path: Path) -> str:
        return self._run(("rev-parse", "HEAD"), cwd=path).strip()


__all__ = [
    "GitClient",
    "GitError",
    "SubprocessGitClient",
    "WorktreeEntry",
]
