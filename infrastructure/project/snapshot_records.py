"""快照记录落库 —— **落的是当时那几个数，不是"到时再查"**。

设计依据：docs/tasks.md TASK-P1-023（§3.6 Snapshot = version + commit +
metrics、R-068、R-069）、docs/design.md §3.6（PostgreSQL 保存项目的结构化状态）。

**为什么指标要跟着记录一起落。** 快照的用途是回答"那会儿是什么样"。如果
快照只存 commit，答案就得靠"回到那个 commit 再跑一遍" —— 那时候的依赖、
环境、测试数据都可能不一样，跑出来的是**另一个时点**的数。**落下来的数才是
那个时点的数。**

**复用已有的仓储，不另起一套。** ``SnapshotRepository`` 已经有 ``version`` /
``commit`` / ``branch`` 三列与一个 ``metrics`` JSON 字段 —— 一一对应，不需要
新的迁移。**没有为新字段改表结构** —— 那会牵动迁移链，而这个任务的全部
要求用现有的四样东西就够。
"""

from __future__ import annotations

from core.project.snapshot import ProjectSnapshotRecord
from infrastructure.database.repositories import SnapshotRepository


class SnapshotRecordStore:
    """把快照记录落进库里 —— **凭库里那一行就能重建当时的快照**。

    **读回来的是"当时的数"，不是"现在的数"。** 库里存着多少，就还回来多少
    —— 不重跑测试、不查覆盖率文件。
    """

    __slots__ = ("_repository",)

    def __init__(self, repository: SnapshotRepository) -> None:
        self._repository = repository

    async def save(self, record: ProjectSnapshotRecord) -> None:
        """落一份快照。

        **版本号由调用方给** —— 仓储层自己去查"当前最大版本 + 1"，两次并发
        落库就会拿到同一个版本号。
        """
        await self._repository.create(
            project_id=record.project_id,
            version=record.version,
            commit=record.commit,
            branch=record.branch,
            metrics=record.metrics_document(),
        )

    async def latest(self, project_id: str) -> ProjectSnapshotRecord | None:
        """这个项目最新的一份快照 —— **没有就是 ``None``**。

        **"没有快照"与"有一份空快照"是两回事**：前者是"还没采过"，后者是
        "采了，什么都没采到"。达成度要拿上一份快照来比，拿不到就是**没有可比
        对的基线**（那不是"没变"）。
        """
        row = await self._repository.latest(project_id)
        if row is None:
            return None
        return ProjectSnapshotRecord.from_document(
            {
                "project_id": row.project_id,
                "version": row.version,
                "commit": row.commit,
                "branch": row.branch,
                **row.metrics,
            }
        )


__all__ = ["SnapshotRecordStore"]
