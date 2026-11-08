"""规划层 —— 演进决策链的最后一段：把决策拆成可执行的任务。

设计依据：docs/design.md §5（规划阶段：已批准的 Decisions → Evolution
Roadmap）、§3.2（七种任务类型；不同类型走不同的验收流程）；
docs/tasks.md TASK-P1-056（R-022 规划阶段产出路线图、R-023 不只是遍历待办、
R-076 七种任务类型、R-081 五阶段决策链）。

这个包是**循环 B** 的第五阶段（规划）的词汇表。目前只有一个模块：

- :mod:`core.planning.roadmap` —— 路线图、任务拆解器与七种任务类型。

**这里只有纯领域模型。** 没有 IO，没有数据库，没有 LLM 调用 —— 它描述的是
"一份路线图长什么样"与"哪些一致性必须成立"（依赖无环、任务可追溯到决策、
没放行就不拆解）。落库是仓储层的事，审查放行是角色层的事。
"""

from __future__ import annotations

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

__all__ = [
    "ACCEPTANCE_FOCUS",
    "ConflictingApprovalError",
    "DecisionApproval",
    "DependencyCycleError",
    "DependencyType",
    "DuplicateTaskKeyError",
    "EvolutionRoadmap",
    "PlannedTask",
    "PlanningError",
    "ScheduleSlot",
    "SkippedDecision",
    "TaskDecomposer",
    "TaskDependency",
    "TaskDraft",
    "TaskType",
    "UnknownDecisionError",
    "UnknownDependencyError",
]
