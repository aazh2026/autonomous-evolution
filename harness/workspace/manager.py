"""工作空间管理器 —— 建、续、重置、释放。

设计依据：docs/design.md §3.5A（每个开发任务独立工作空间；Git Worktree
必选）；docs/tasks.md TASK-P1-032（分支 ``agent/<task_id>``；租约与 GC；
I-15 分支冲突约定）。

**这个管理器保证三件事同时成立**，而它们合起来就是"分支冲突不可能发生"：

1. 一个任务对应**一个**工作空间目录；
2. 一个任务对应**一条**分支（``agent/<task_id>``）；
3. 一个分支在同一个仓库里**只能**被一个 worktree 检出 —— 这是 git 自己的
   硬约束，不遵守它的 ``worktree add`` 会直接失败。

所以"两个任务并行建工作空间"必然互不干扰（目录不同、分支不同），而
"重试同一个任务"必然复用同一个目录与同一条分支（不会撞车）。

**租约记在两处，因为两处各有用处。**

- **内存里**的那份是活着的租约 —— 进程还活着时，它说了算；
- **磁盘上**的那份（``<workspace_root>/.ases/leases/<id>.json``）是给 GC 用的
  —— 进程重启之后内存那份没了，磁盘那份还能告诉 GC"这个工作空间的租约
  什么时候到期"。

磁盘那份**不放进 worktree 目录里**：worktree 是 Agent 的工作区，往里塞
一个未跟踪文件，Agent 可能把它一起提交进去 —— 然后下一轮 GC 看到的就是
"工作区里有个奇怪的 json"。放在工作区外面，Agent 看不见它。

**元数据不放在 worktree 里还有一个理由**：崩溃遗留的 worktree（目录还在，
git 的登记没了）必须能被识别成"没有主人"。元数据在外面，"有没有主人"
就是"外面那份记录在不在、过期了没有"，判断不会因为工作区里多了个文件而
变得含糊。
"""

from __future__ import annotations

import contextlib
import shutil
from datetime import timedelta
from pathlib import Path

from harness.workspace.git import GitClient, GitError
from harness.workspace.leases import LeaseStore
from harness.workspace.models import (
    Clock,
    Workspace,
    WorkspaceBusyError,
    WorkspaceError,
    WorkspaceLease,
    WorkspaceNotFoundError,
)
from harness.workspace.naming import (
    branch_for_task,
    workspace_id_for_task,
    workspace_path_for_task,
)


class WorkspaceManager:
    """工作空间的分配与回收。

    :param repo_path: 主仓库路径。
    :param workspace_root: 工作空间根目录。**必须在仓库之外** —— 仓库里的
        worktree 会被 git 当成未跟踪内容，而且主仓库的 ``git status`` 会
        永远不干净。
    :param git: git 操作面。
    :param clock: 时钟。租约的到期判断全靠它，所以它必须可替换 ——
        否则"过期"这件事没法测。
    :param lease_ttl_seconds: 租约时长。
    """

    def __init__(
        self,
        *,
        repo_path: Path,
        workspace_root: Path,
        git: GitClient,
        clock: Clock,
        lease_ttl_seconds: int,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError(f"租约时长必须为正，得到 {lease_ttl_seconds}")
        repo = Path(repo_path)
        root = Path(workspace_root)
        if not repo.exists():
            raise WorkspaceError(f"仓库路径不存在：{repo}")
        if not (repo / ".git").exists():
            raise WorkspaceError(f"{repo} 不是一个 git 仓库")
        if root == repo or repo in root.parents:
            raise WorkspaceError(f"工作空间根目录 {root} 不能位于仓库 {repo} 之内")

        self._repo_path = repo
        self._workspace_root = root
        self._git = git
        self._clock = clock
        self._lease_ttl = lease_ttl_seconds
        self._leases: dict[str, WorkspaceLease] = {}
        self._lease_store = LeaseStore(root)

    # ---------- 只读属性 ----------

    @property
    def repo_path(self) -> Path:
        return self._repo_path

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def lease_store(self) -> LeaseStore:
        """租约的磁盘存储。"""
        return self._lease_store

    @property
    def lease_dir(self) -> Path:
        """租约记录目录。"""
        return self._lease_store.root

    def path_of(self, task_id: str) -> Path:
        """这个任务的工作空间目录。"""
        return workspace_path_for_task(self._workspace_root, task_id)

    def branch_of(self, task_id: str) -> str:
        """这个任务的分支名。"""
        return branch_for_task(task_id)

    def lease_of(self, task_id: str) -> WorkspaceLease | None:
        """当前内存里的租约（没有就是 ``None``）。"""
        return self._leases.get(workspace_id_for_task(task_id))

    def leases(self) -> tuple[WorkspaceLease, ...]:
        """所有在手里的租约。"""
        return tuple(self._leases.values())

    def exists(self, task_id: str) -> bool:
        """工作空间目录是否已经存在。"""
        return self.path_of(task_id).exists()

    # ---------- 分配 ----------

    def acquire(self, task_id: str, *, holder: str) -> Workspace:
        """拿到这个任务的工作空间，没有就建一个。

        同一个持有者重复调用 = 续用（并续期租约）；**别人**拿着没到期的租约
        来拿 = 直接拒绝（:class:`WorkspaceBusyError`）—— 两个执行者共用一个
        工作空间，它们的产出会混在一起，谁也说不清哪个提交是谁做的。
        """
        workspace_id = workspace_id_for_task(task_id)
        now = self._clock.now()
        path = self.path_of(task_id)
        branch = self.branch_of(task_id)

        current = self._leases.get(workspace_id)
        if current is not None and not current.is_expired(now) and current.holder != holder:
            raise WorkspaceBusyError(workspace_id, current.holder)

        lease = WorkspaceLease(
            workspace_id=workspace_id,
            task_id=task_id,
            branch=branch,
            path=path,
            repo_path=self._repo_path,
            holder=holder,
            created_at=current.created_at if current is not None else now,
            expires_at=now + timedelta(seconds=self._lease_ttl),
        )

        if path.exists():
            self._assert_usable(path, branch)
            self._leases[workspace_id] = lease
            self._write_lease(lease)
        else:
            # **先落租约，再建 worktree** —— 顺序反过来的话，崩在两步中间
            # 会留下一个"目录在、租约不在"的工作空间。GC 认不出它有主人，
            # 就把它当成崩溃遗留回收掉 —— 而它其实刚刚才被分配出去。
            self._leases[workspace_id] = lease
            self._write_lease(lease)
            try:
                self._git.add_worktree(self._repo_path, path, branch)
            except Exception:
                self._leases.pop(workspace_id, None)
                self._remove_lease_file(workspace_id)
                raise

        return Workspace(
            workspace_id=workspace_id,
            task_id=task_id,
            branch=branch,
            path=path,
            repo_path=self._repo_path,
            created_at=lease.created_at,
        )

    def renew(self, task_id: str, *, holder: str) -> WorkspaceLease:
        """续期。"""
        workspace_id = workspace_id_for_task(task_id)
        current = self._leases.get(workspace_id)
        if current is None:
            raise WorkspaceNotFoundError(workspace_id)
        if current.holder != holder:
            raise WorkspaceBusyError(workspace_id, current.holder)
        renewed = current.renew(self._clock.now(), self._lease_ttl)
        self._leases[workspace_id] = renewed
        self._write_lease(renewed)
        return renewed

    def reset(self, task_id: str) -> None:
        """把工作空间恢复到 HEAD —— **重试**用的那一步。

        丢弃上一次尝试**未提交**的改动，**已提交**的产出留着：那是上一次真正
        交付过的东西，扔掉它等于假装那次尝试什么都没做。

        续跑（恢复语义，TASK-P1-034）**不**调这个方法 —— 续跑要的是原样
        接着来，不是清干净重来。
        """
        workspace_id = workspace_id_for_task(task_id)
        path = self.path_of(task_id)
        if not path.exists():
            raise WorkspaceNotFoundError(workspace_id)
        self._git.reset_hard(path)
        self._git.clean_untracked(path)

    def release(self, task_id: str) -> None:
        """显式释放：删掉工作空间目录，从 git 的登记里摘掉它。

        **分支留着** —— 分支是任务的产出，删了就没了；工作空间只是一块
        临时地盘。
        """
        workspace_id = workspace_id_for_task(task_id)
        path = self.path_of(task_id)
        if path.exists():
            # git 的登记可能已经没了（崩溃遗留）。目录还得删 ——
            # 留着就是一个占着磁盘的孤儿。所以这里吞掉错误，继续删目录。
            with contextlib.suppress(GitError):
                self._git.remove_worktree(self._repo_path, path, force=True)
            shutil.rmtree(path, ignore_errors=True)
        self._leases.pop(workspace_id, None)
        self._remove_lease_file(workspace_id)

    # ---------- 内部 ----------

    def _assert_usable(self, path: Path, branch: str) -> None:
        """工作空间目录已存在时，确认它确实是**这个分支**的工作空间。

        两种情况要拦下来：

        - 目录在，但不是 git 认得的 worktree → 崩溃遗留，没有主人；
        - 目录在，git 认得，但检出的是别的分支 → 分支冲突。
        """
        entries = {
            entry.path.resolve(): entry for entry in self._git.list_worktrees(self._repo_path)
        }
        entry = entries.get(path.resolve())
        if entry is None:
            raise WorkspaceError(
                f"{path} 已存在，但不是 git 认得的 worktree —— "
                "这是崩溃遗留的工作空间，请先运行 GC 回收它"
            )
        if entry.branch != branch:
            raise WorkspaceError(
                f"{path} 当前检出的是分支 {entry.branch!r}，与约定的 {branch!r} 不符"
            )
        for other_path, other in entries.items():
            if other_path == path.resolve():
                continue
            if other.branch == branch:
                raise WorkspaceError(
                    f"分支 {branch!r} 已经在 {other.path} 上被检出 —— "
                    "同一条分支不能同时被两个 worktree 检出"
                )

    def _lease_file(self, workspace_id: str) -> Path:
        return self._lease_store.path_for(workspace_id)

    def _write_lease(self, lease: WorkspaceLease) -> None:
        self._lease_store.save(lease)

    def _remove_lease_file(self, workspace_id: str) -> None:
        self._lease_store.remove(workspace_id)


__all__ = ["WorkspaceManager"]
