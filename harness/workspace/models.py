"""工作空间与租约 —— 记录长什么样。

设计依据：docs/design.md §3.5A（每个开发任务都应该有独立的工作环境；
"不要让 Coding Agent 直接访问宿主机的 SSH 凭据、云平台管理员密钥和
生产数据库"）、docs/tasks.md TASK-P1-032（``WorkspaceLease``，带 TTL
与 GC；I-14 工作空间需要租约与 GC）。

**为什么工作空间需要"租约"而不是"用完就删"。** 一个工作空间不只属于
"正在跑的那个任务" —— 它还占着磁盘、还挂着一条 git 分支、可能还有一个
没退出的容器。谁负责在什么时候回收它，得有个明确的凭据。租约就是这个
凭据：一张有期限的持有凭证，到期没人续，就该被回收。

**租约到期不等于"立刻删"。** 回收动作会先看工作空间**有没有主人**：
- 有租约但过期 → 回收（``LEASE_EXPIRED``）—— 主人忘了续期，或者它死了；
- 连租约记录都没有 → 回收（``ORPHANED``）—— 崩在半路的工作空间。

第二种情况是崩溃遗留：进程在建工作空间的过程中死了，留下一个既没有租约
也没有人认领的目录。这种东西必须能被识别出来 —— 否则磁盘会被一次次崩溃
慢慢吃光，而系统自己毫无察觉。

**回收是不可逆的。** 所以它只做两件事：删工作空间目录、从 git 的
worktree 列表里摘掉它。**分支留着** —— 分支是任务的产出，删了就没了；
工作空间只是一块临时地盘。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class WorkspaceError(RuntimeError):
    """工作空间操作失败。"""


class WorkspaceBusyError(WorkspaceError):
    """工作空间正被别人占着。

    两个执行者抢同一个工作空间不是"谁后到谁赢" —— 那会让它们的产出混在
    一起，谁也说不清哪个提交是谁做的。所以这里直接拒绝。
    """

    def __init__(self, workspace_id: str, holder: str) -> None:
        super().__init__(f"工作空间 {workspace_id!r} 正被 {holder!r} 持有")
        self.workspace_id = workspace_id
        self.holder = holder


class WorkspaceNotFoundError(WorkspaceError):
    """工作空间不存在。"""

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"工作空间 {workspace_id!r} 不存在")
        self.workspace_id = workspace_id


@runtime_checkable
class Clock(Protocol):
    """时钟 —— 租约的到期判断全靠它，所以它必须可替换。"""

    def now(self) -> datetime:
        """当前时间（带时区）。"""


class SystemClock:
    """真实时钟。"""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Workspace:
    """一个任务的工作空间。

    ``path`` 是工作目录（git worktree 的检出位置），``branch`` 是它检出的
    分支。两者都由 :mod:`harness.workspace.naming` 按约定算出来 —— 不是
    调用方随便给的。
    """

    workspace_id: str
    task_id: str
    branch: str
    path: Path
    repo_path: Path
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    """工作空间的持有凭证。

    ``expires_at`` 是这张凭证的到期时刻。持有者可以续期（:meth:`renew`），
    续期就是换一张更晚到期的凭证 —— 而不是修改旧凭证，因为旧凭证可能
    正被另一个人拿着看。
    """

    workspace_id: str
    task_id: str
    branch: str
    path: Path
    repo_path: Path
    holder: str
    created_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        """这张凭证是不是已经到期。"""
        return now >= self.expires_at

    def remaining(self, now: datetime) -> timedelta:
        """还剩多久到期。已到期就是负数 —— 负数与零是不同的状态：
        零是"恰好到期"，负数是"早就过期了"。"""
        return self.expires_at - now

    def renew(self, now: datetime, ttl_seconds: int) -> WorkspaceLease:
        """续期 —— 换一张更晚到期的凭证。

        返回**新凭证**而不是就地修改：租约是要被别人读的（GC 读它判断能不能
        回收），一张被就地改掉的凭证会让读的人看到半新半旧的状态。
        """
        if ttl_seconds <= 0:
            raise ValueError(f"租约时长必须为正，得到 {ttl_seconds}")
        return WorkspaceLease(
            workspace_id=self.workspace_id,
            task_id=self.task_id,
            branch=self.branch,
            path=self.path,
            repo_path=self.repo_path,
            holder=self.holder,
            created_at=self.created_at,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )


class ReclaimReason(StrEnum):
    """回收的原因 —— 回收是不可逆的，所以必须能说清为什么。"""

    #: 持有者显式释放。
    EXPLICIT = "explicit"
    #: 租约到期，没人续期。
    LEASE_EXPIRED = "lease_expired"
    #: 崩溃遗留 —— 没有租约记录的工作空间。
    ORPHANED = "orphaned"


@dataclass(frozen=True, slots=True)
class ReclaimedWorkspace:
    """一次回收的结果。"""

    workspace_id: str
    path: Path
    reason: ReclaimReason
    reclaimed_at: datetime


__all__ = [
    "Clock",
    "ReclaimReason",
    "ReclaimedWorkspace",
    "SystemClock",
    "Workspace",
    "WorkspaceBusyError",
    "WorkspaceError",
    "WorkspaceLease",
    "WorkspaceNotFoundError",
]
