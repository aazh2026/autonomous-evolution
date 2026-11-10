"""规划层 —— 演进决策链的最后一段：把决策拆成可执行的任务。

设计依据：docs/design.md §5（规划阶段：已批准的 Decisions → Evolution
Roadmap）、§3.2（七种任务类型；不同类型走不同的验收流程）；
docs/tasks.md TASK-P1-056（R-022 规划阶段产出路线图、R-023 不只是遍历待办、
R-076 七种任务类型、R-081 五阶段决策链）；TASK-P2-012（多任务依赖图与 DAG 调度）。

这个包是**循环 B** 的第五阶段（规划）的词汇表。目前有三个模块：

- :mod:`core.planning.roadmap` —— 路线图、任务拆解器与七种任务类型。
- :mod:`core.planning.stages` —— 五阶段（认知 → 诊断 → 探索 → 决策 → 规划）
  的**契约**：每一步的输入、产出与一致性要求。
- :mod:`core.planning.dag` —— 多任务有向无环依赖图（DAG）、拓扑排序与分层执行波次。
"""

from __future__ import annotations

from core.planning.dag import (
    DAGEdge,
    DAGError,
    DAGTaskNode,
    DuplicateNodeError,
    TaskDAG,
    UnknownNodeError,
)
from core.planning.roadmap import (
    ACCEPTANCE_FOCUS,
    ConflictingApprovalError,
    DecisionApproval,
    DependencyCycleError,
    DependencyType,
    DuplicateTaskKeyError,
    EvolutionRoadmap,
    PlannedTask,
    PlanningError,
    ScheduleSlot,
    SkippedDecision,
    TaskDecomposer,
    TaskDependency,
    TaskDraft,
    TaskType,
    UnknownDecisionError,
    UnknownDependencyError,
)
from core.planning.stages import (
    STAGE_ORDER,
    Decider,
    DecisionOutcome,
    Diagnoser,
    Explorer,
    GapAnalysis,
    Planner,
    ProjectContext,
    Stage,
    StageContractError,
    StageHandlers,
    Understander,
    advance,
    draft_keys,
    next_stage,
    stage_field,
)

__all__ = [
    "ACCEPTANCE_FOCUS",
    "STAGE_ORDER",
    "ConflictingApprovalError",
    "DAGEdge",
    "DAGError",
    "DAGTaskNode",
    "Decider",
    "DecisionApproval",
    "DecisionOutcome",
    "DependencyCycleError",
    "DependencyType",
    "Diagnoser",
    "DuplicateNodeError",
    "DuplicateTaskKeyError",
    "EvolutionRoadmap",
    "Explorer",
    "GapAnalysis",
    "PlannedTask",
    "Planner",
    "PlanningError",
    "ProjectContext",
    "ScheduleSlot",
    "SkippedDecision",
    "Stage",
    "StageContractError",
    "StageHandlers",
    "TaskDAG",
    "TaskDecomposer",
    "TaskDependency",
    "TaskDraft",
    "TaskType",
    "Understander",
    "UnknownDecisionError",
    "UnknownDependencyError",
    "UnknownNodeError",
    "advance",
    "draft_keys",
    "next_stage",
    "stage_field",
]
