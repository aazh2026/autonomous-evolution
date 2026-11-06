"""工作空间与沙箱执行环境。

设计依据：docs/design.md §3.5A（R-045~R-048：每个开发任务独立工作环境；
"不要让 Coding Agent 直接访问宿主机的 SSH 凭据、云平台管理员密钥和
生产数据库"）、docs/tasks.md TASK-P1-032。

这一层管的是"Agent 在哪儿干活、干活时看得见什么"：

- :mod:`harness.workspace.naming` —— 分支名与目录名的约定（``agent/<task_id>``）；
- :mod:`harness.workspace.git` —— git worktree 的操作面；
- :mod:`harness.workspace.manager` —— 工作空间的分配、续期、重置、释放；
- :mod:`harness.workspace.leases` —— 租约的磁盘存储；
- :mod:`harness.workspace.gc` —— 回收没人要的工作空间。

容器的部分在 :mod:`harness.sandbox` —— 工作空间生命周期与容器生命周期
是两件事，分开放。
"""

from __future__ import annotations

from harness.workspace.gc import ReclaimCandidate, WorkspaceGC
from harness.workspace.git import GitClient, GitError, SubprocessGitClient, WorktreeEntry
from harness.workspace.leases import LeaseStore
from harness.workspace.manager import WorkspaceManager
from harness.workspace.models import (
    Clock,
    ReclaimedWorkspace,
    ReclaimReason,
    SystemClock,
    Workspace,
    WorkspaceBusyError,
    WorkspaceError,
    WorkspaceLease,
    WorkspaceNotFoundError,
)
from harness.workspace.naming import (
    branch_for_task,
    validate_task_id,
    workspace_id_for_task,
    workspace_path_for_task,
)

__all__ = [
    "Clock",
    "GitClient",
    "GitError",
    "LeaseStore",
    "ReclaimCandidate",
    "ReclaimReason",
    "ReclaimedWorkspace",
    "SubprocessGitClient",
    "SystemClock",
    "Workspace",
    "WorkspaceBusyError",
    "WorkspaceError",
    "WorkspaceGC",
    "WorkspaceLease",
    "WorkspaceManager",
    "WorkspaceNotFoundError",
    "WorktreeEntry",
    "branch_for_task",
    "validate_task_id",
    "workspace_id_for_task",
    "workspace_path_for_task",
]
