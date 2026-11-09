"""架构认知 —— 从索引推导出的结构，以及那些结构背后的决定。

设计依据：docs/tasks.md TASK-P2-016（架构知识图谱、ADR 存储）。

三块东西：

1. :mod:`knowledge.architecture.model` —— **架构模型**：分层、依赖边、
   循环依赖、没人碰的公开接口。全部从仓库索引推导，不读仓库。
2. :mod:`knowledge.architecture.decisions` —— **架构决策**：端口 + 人读的
   那一份（Git + Markdown/YAML）。
3. 数据库那一份由 :mod:`infrastructure.database.repositories` 实现 ——
   领域层不该认识 SQL。
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
from knowledge.architecture.decisions import (
    DecisionError,
    DecisionSet,
    FileDecisionStore,
    decisions_as_set,
    parse_decision,
    render_decision,
)
from knowledge.architecture.model import (
    ENTRY_POINT_NAMES,
    ArchitectureModel,
    DependencyCycle,
    LayerViolation,
    ViolationKind,
    build_architecture,
)

__all__ = [
    "DECISION_STATUSES",
    "ENTRY_POINT_NAMES",
    "STATUS_ACCEPTED",
    "STATUS_PROPOSED",
    "STATUS_REJECTED",
    "STATUS_SUPERSEDED",
    "ArchitectureDecisionError",
    "ArchitectureModel",
    "DecisionError",
    "DecisionRecord",
    "DecisionSet",
    "DecisionStore",
    "DependencyCycle",
    "FileDecisionStore",
    "LayerViolation",
    "ViolationKind",
    "build_architecture",
    "decisions_as_set",
    "parse_decision",
    "render_decision",
]
