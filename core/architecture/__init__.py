"""架构认知的领域层 —— 架构决策的模型与存取端口。

设计依据：docs/tasks.md TASK-P2-016（ADR 存储）。

这一层只有**模型与端口**，没有实现。文件那份实现放在
:mod:`knowledge.architecture.decisions`（Git + Markdown/YAML），数据库
那份放在 :mod:`infrastructure.database.repositories`（PostgreSQL）。
"""

from core.architecture.decisions import (
    DECISION_STATUSES,
    STATUS_ACCEPTED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    ArchitectureDecisionError,
    DecisionRecord,
    DecisionStore,
)

__all__ = [
    "DECISION_STATUSES",
    "STATUS_ACCEPTED",
    "STATUS_PROPOSED",
    "STATUS_REJECTED",
    "STATUS_SUPERSEDED",
    "ArchitectureDecisionError",
    "DecisionRecord",
    "DecisionStore",
]
