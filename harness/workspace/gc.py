"""工作空间 GC —— 回收没人要的工作空间。

设计依据：docs/tasks.md TASK-P1-032（"租约过期后 workspace 被 GC 回收"；
"崩溃遗留的 workspace 可被 GC 识别并清理"）；I-14 工作空间需要租约与 GC。

**GC 凭什么判断一个工作空间可以回收。** 只有两条依据，而且都必须来自
**磁盘上**的记录 —— GC 常常是在系统重启之后才跑的，内存里那份租约早没了：

- **租约存在但已过期** → ``LEASE_EXPIRED``。主人忘了续期，或者它死了。
- **租约记录不存在（或读不出来）** → ``ORPHANED``。崩在半路的工作空间。

第二种是崩溃遗留。它必须能被认出来，否则磁盘会被一次次崩溃慢慢吃光，而
系统自己毫无察觉 —— 每一次崩溃留下的目录看起来都"很正常"。

**"读不出来"与"没有记录"是同一件事。** 理由：租约记录是"这个工作空间有主
人"的**唯一证据**。证据读不出来，等于没有证据；而没有证据的工作空间，按
无人认领处理。这不是"宁可错杀" —— 回收只删**目录**与 git 的登记，**分支留
着**，任务的产出一点没丢；真回收错了，重新 acquire 一次就能拿回来。

**回收只动两样东西：工作空间目录、git 的 worktree 登记。** 分支留着 ——
分支是任务的产出，删了就没了；工作空间只是一块临时地盘。
"""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from harness.workspace.git import GitClient, GitError
from harness.workspace.leases import LeaseStore
from harness.workspace.models import (
    Clock,
    ReclaimedWorkspace,
    ReclaimReason,
)

#: 工作空间根目录下存放元数据的目录名 —— GC 扫描时**跳过**它。
#:
#: 它是租约记录的存放处，不是工作空间本身。把它当成工作空间回收掉，等于
#: 把所有租约记录一起删了 —— 然后所有还活着的工作空间都变成"孤儿"。
METADATA_DIRNAME: str = ".ases"


@dataclass(frozen=True, slots=True)
class ReclaimCandidate:
    """一个待回收的工作空间。

    ``expires_at`` 只在 ``LEASE_EXPIRED`` 时有意义（它是那份租约的到期
    时刻）；``ORPHANED`` 时是 ``None`` —— 没有租约，也就没有到期时刻。
    """

    workspace_id: str
    path: Path
    reason: ReclaimReason
    expires_at: datetime | None


class WorkspaceGC:
    """回收没人要的工作空间。

    :param repo_path: 主仓库路径。
    :param workspace_root: 工作空间根目录。
    :param git: git 操作面。
    :param clock: 时钟。
    :param lease_store: 租约的磁盘存储。默认在 ``workspace_root`` 下建一个。
    """

    def __init__(
        self,
        *,
        repo_path: Path,
        workspace_root: Path,
        git: GitClient,
        clock: Clock,
        lease_store: LeaseStore | None = None,
    ) -> None:
        self._repo_path = Path(repo_path)
        self._workspace_root = Path(workspace_root)
        self._git = git
        self._clock = clock
        self._lease_store = lease_store or LeaseStore(self._workspace_root)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def lease_store(self) -> LeaseStore:
        return self._lease_store

    # ---------- 扫描 ----------

    def candidates(self) -> tuple[ReclaimCandidate, ...]:
        """当前可以回收的工作空间。

        判定只看两件事：**目录是不是一个工作空间**（工作空间根目录下的
        一级目录，跳过元数据目录与符号链接），以及**它有没有还活着的租约**。
        """
        now = self._clock.now()
        found: list[ReclaimCandidate] = []
        for entry in self._workspace_directories():
            lease = self._lease_store.load(entry.name)
            if lease is None:
                found.append(
                    ReclaimCandidate(
                        workspace_id=entry.name,
                        path=entry,
                        reason=ReclaimReason.ORPHANED,
                        expires_at=None,
                    )
                )
            elif lease.is_expired(now):
                found.append(
                    ReclaimCandidate(
                        workspace_id=entry.name,
                        path=entry,
                        reason=ReclaimReason.LEASE_EXPIRED,
                        expires_at=lease.expires_at,
                    )
                )
        return tuple(sorted(found, key=lambda candidate: candidate.workspace_id))

    def _workspace_directories(self) -> tuple[Path, ...]:
        if not self._workspace_root.exists():
            return ()
        entries = [
            entry
            for entry in sorted(self._workspace_root.iterdir())
            if entry.name != METADATA_DIRNAME and entry.is_dir() and not entry.is_symlink()
        ]
        return tuple(entries)

    # ---------- 回收 ----------

    def collect(self) -> tuple[ReclaimedWorkspace, ...]:
        """回收所有可以回收的工作空间。

        回收是**不可逆**的：目录删掉、git 的登记摘掉。**分支留着**。
        """
        now = self._clock.now()
        reclaimed: list[ReclaimedWorkspace] = []
        for candidate in self.candidates():
            self._reclaim_directory(candidate.path)
            self._lease_store.remove(candidate.workspace_id)
            reclaimed.append(
                ReclaimedWorkspace(
                    workspace_id=candidate.workspace_id,
                    path=candidate.path,
                    reason=candidate.reason,
                    reclaimed_at=now,
                )
            )
        return tuple(reclaimed)

    def prune_stale_records(self) -> tuple[str, ...]:
        """删掉"工作空间已经不在了"的租约记录。

        这类记录是崩溃留下的残渣：租约先落盘、worktree 后建（见
        :meth:`~harness.workspace.manager.WorkspaceManager.acquire` 的顺序
        理由），崩在中间就留下一份指向空处的记录。它不影响回收判定，但会
        让"这个工作空间正被谁持有"这个问题得到一个**过期**的答案。

        返回被删掉的工作空间 id。
        """
        removed: list[str] = []
        for lease in self._lease_store.all():
            if not lease.path.exists():
                self._lease_store.remove(lease.workspace_id)
                removed.append(lease.workspace_id)
        return tuple(sorted(removed))

    def run(self) -> tuple[ReclaimedWorkspace, ...]:
        """先清残渣，再回收。返回被回收的工作空间。"""
        self.prune_stale_records()
        return self.collect()

    # ---------- 内部 ----------

    def _reclaim_directory(self, path: Path) -> None:
        """删掉工作空间目录，并从 git 的 worktree 列表里摘掉它。"""
        # git 的登记可能已经没了（崩溃遗留）。那种情况下目录照样得删 ——
        # 留着就是一个占着磁盘的孤儿。所以这里吞掉错误，继续删目录。
        with contextlib.suppress(GitError):
            self._git.remove_worktree(self._repo_path, path, force=True)
        shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "METADATA_DIRNAME",
    "ReclaimCandidate",
    "WorkspaceGC",
]
