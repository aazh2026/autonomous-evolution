"""快照记录落成文件 —— **一份 JSON 一份快照**。

设计依据：docs/tasks.md TASK-P1-023（"指标采集结果落盘（不实时查），使'当时
的快照'可被事后重建"）、R-069。

**为什么还要一份文件形态的。** 库里的那一份适合被程序查；文件的那一份适合
被**人**拿走 —— 交给别人、归档、跟别的产物放在一起。两者记的是同一份文档，
所以从哪一份重建出来的快照是一样的。

**文件里存的是数，不是"到哪儿去查"。** 快照文档里写的就是当时那几个数；
产物 URI 指向原始报告，是为了**核对**（那个数是不是真的），不是为了**重算**
（重算得到的是另一个时点的数）。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.project.snapshot import ProjectSnapshotRecord


class SnapshotDocumentStore:
    """把快照落成 JSON 文件。

    **一份快照一个文件** —— 文件名里带着版本号与 commit，所以"这是哪一份"
    一眼看得出来，也不用解开 JSON 才知道。
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, record: ProjectSnapshotRecord) -> Path:
        """这份快照落在哪个文件上。"""
        safe_project = record.project_id.replace("/", "_")
        short_commit = record.commit[:12]
        return self._root / safe_project / f"v{record.version}-{short_commit}.json"

    async def save(self, record: ProjectSnapshotRecord) -> str:
        """落一份快照，返回它的 URI。"""
        path = self._path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record.to_document(), ensure_ascii=False, indent=2, sort_keys=True)
        path.write_text(payload + "\n", encoding="utf-8")
        return path.as_uri()

    async def load(self, project_id: str, version: int) -> ProjectSnapshotRecord | None:
        """取回第几版 —— **取不到是 ``None``**。"""
        safe_project = project_id.replace("/", "_")
        directory = self._root / safe_project
        if not directory.is_dir():
            return None
        for path in sorted(directory.glob(f"v{version}-*.json")):
            return self._read(path)
        return None

    async def latest(self, project_id: str) -> ProjectSnapshotRecord | None:
        """这个项目最新的一份 —— **没有就是 ``None``**。"""
        safe_project = project_id.replace("/", "_")
        directory = self._root / safe_project
        if not directory.is_dir():
            return None
        versions: list[tuple[int, Path]] = []
        for path in directory.glob("v*-*.json"):
            version = _version_of(path)
            if version is not None:
                versions.append((version, path))
        if not versions:
            return None
        _, newest = max(versions, key=lambda item: item[0])
        return self._read(newest)

    def _read(self, path: Path) -> ProjectSnapshotRecord:
        """读一份快照 —— **读不出来就报错，不返回半份**。

        一份读坏了的快照如果被当成"没有快照"，那这次采集就白做了，而且没人
        知道它坏过。
        """
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"快照文件不是一份对象：{path}")
        return ProjectSnapshotRecord.from_document(raw)


def _version_of(path: Path) -> int | None:
    """从文件名里读出版本号 —— **读不出来是 ``None``**。

    文件名是 ``v<版本号>-<commit 前 12 位>.json``。
    """
    stem = path.stem
    if not stem.startswith("v"):
        return None
    head, _, _ = stem[1:].partition("-")
    try:
        return int(head)
    except ValueError:
        return None


__all__ = ["SnapshotDocumentStore"]
