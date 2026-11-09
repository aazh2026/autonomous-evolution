"""项目状态的落盘 —— **快照记录存在哪儿**。

设计依据：docs/tasks.md TASK-P1-023（"指标采集结果落盘（不实时查），使'当时
的快照'可被事后重建"）。

- :mod:`infrastructure.project.snapshot_records` —— 落进数据库（复用已有的
  ``SnapshotRepository``）。
- :mod:`infrastructure.project.snapshot_documents` —— 落成文件（一份 JSON
  一份快照，凭它就能重建）。
"""

from __future__ import annotations

from infrastructure.project.snapshot_documents import SnapshotDocumentStore
from infrastructure.project.snapshot_records import SnapshotRecordStore

__all__ = ["SnapshotDocumentStore", "SnapshotRecordStore"]
