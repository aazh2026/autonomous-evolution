"""租约的磁盘存储。

设计依据：docs/tasks.md TASK-P1-032（``WorkspaceLease``，带 TTL 与 GC）；
I-14 工作空间需要租约与 GC。

**为什么租约有"内存"和"磁盘"两份。** 两份各有一件对方办不了的事：

- **内存那份**（:attr:`WorkspaceManager.leases`）是**活着的租约** —— 进程还
  活着时它说了算，谁正占着哪个工作空间，内存里一清二楚。
- **磁盘那份**（这个模块）是**给 GC 用的** —— 进程重启之后内存那份没了，
  磁盘那份还能告诉 GC"这个工作空间的租约什么时候到期"。没有它，GC 只能
  靠"目录在不在"来判断，而那会把"正在跑的任务"和"崩溃遗留"混成一类。

**磁盘那份放在工作空间目录之外。** 位置是 ``<workspace_root>/.ases/leases/``，
不在 worktree 里面。两个理由：

1. worktree 是 Agent 的工作区。往里塞一个未跟踪文件，Agent 可能顺手把它
   一起提交进去 —— 然后下一轮 GC 看到的就是"工作区里多了个奇怪的 json"。
2. 崩溃遗留的 worktree（目录还在，git 的登记没了）必须能被识别成"没有
   主人"。元数据在外面，"有没有主人"就是"外面那份记录在不在、过期了没有"，
   判断不会因为工作区里多了个文件而变得含糊。

**格式是版本化的最小 JSON。** 它不是为了好看，是为了"进程崩了之后，磁盘上
这份东西还能不能被读懂"。所以字段名用 snake_case、时间用 ISO-8601 字符串，
读写都显式转换 —— 不依赖任何运行时的默认序列化行为。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from harness.workspace.models import WorkspaceLease

#: 租约记录目录（相对于工作空间根目录）。
LEASES_DIRNAME: str = ".ases/leases"

#: 租约记录的文件名后缀。
LEASE_SUFFIX: str = ".json"


class LeaseStore:
    """租约记录的磁盘存储。

    :param workspace_root: 工作空间根目录。租约记录写在它下面的
        ``.ases/leases/`` 里 —— **不在**任何工作空间目录里面。
    """

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    @property
    def root(self) -> Path:
        """租约记录目录。"""
        return self._root / LEASES_DIRNAME

    def path_for(self, workspace_id: str) -> Path:
        """某个工作空间的租约记录文件。"""
        return self.root / f"{workspace_id}{LEASE_SUFFIX}"

    def save(self, lease: WorkspaceLease) -> Path:
        """写入一份租约。返回落盘路径。"""
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "workspace_id": lease.workspace_id,
            "task_id": lease.task_id,
            "branch": lease.branch,
            "path": str(lease.path),
            "repo_path": str(lease.repo_path),
            "holder": lease.holder,
            "created_at": lease.created_at.isoformat(),
            "expires_at": lease.expires_at.isoformat(),
        }
        target = self.path_for(lease.workspace_id)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def load(self, workspace_id: str) -> WorkspaceLease | None:
        """读一份租约。没有记录就是 ``None``。"""
        target = self.path_for(workspace_id)
        if not target.exists():
            return None
        return self._parse(target)

    def all(self) -> tuple[WorkspaceLease, ...]:
        """所有租约记录。"""
        if not self.root.exists():
            return ()
        leases: list[WorkspaceLease] = []
        for target in sorted(self.root.glob(f"*{LEASE_SUFFIX}")):
            lease = self._parse(target)
            if lease is not None:
                leases.append(lease)
        return tuple(leases)

    def remove(self, workspace_id: str) -> bool:
        """删掉一份租约记录。返回是否真的删掉了什么。"""
        target = self.path_for(workspace_id)
        if not target.exists():
            return False
        target.unlink()
        return True

    def _parse(self, target: Path) -> WorkspaceLease | None:
        """解析一份租约记录。

        解析不了就当**没有记录**（返回 ``None``），而不是抛异常：一份写坏
        的记录不应该让 GC 整个跑不起来。但它也不会被静默丢掉 —— GC 见到
        "目录在、记录读不出来"时会把它判成孤儿工作空间（没有可用的主人
        证据），照样回收。

        所以"读不出来"和"没有记录"在这里是**同一件事**：都没有主人。
        """
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return WorkspaceLease(
                workspace_id=str(payload["workspace_id"]),
                task_id=str(payload["task_id"]),
                branch=str(payload["branch"]),
                path=Path(str(payload["path"])),
                repo_path=Path(str(payload["repo_path"])),
                holder=str(payload["holder"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
                expires_at=datetime.fromisoformat(str(payload["expires_at"])),
            )
        except (KeyError, TypeError, ValueError):
            return None


__all__ = [
    "LEASES_DIRNAME",
    "LEASE_SUFFIX",
    "LeaseStore",
]
