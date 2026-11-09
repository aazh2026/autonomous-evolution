"""项目记忆 —— 跨轮次留下来的事实。

设计依据：docs/tasks.md TASK-P2-016（项目知识库）、R-064、R-067。

事实是**结构化的**：一条事实有一个键、一段摘要、一个记下时间。键是跨轮
对照时唯一能用的东西 —— 摘要会改，键不会。"PILOT-001 已修复"这句话的
全部意义就在那个键上。
"""

from knowledge.project_memory.memory import (
    KIND_CONSTRAINT,
    KIND_DECISION,
    KIND_ISSUE_FIXED,
    KIND_OBSERVATION,
    Fact,
    FactKind,
    FactStoreError,
    ProjectMemory,
    parse_fact,
    render_fact,
)

__all__ = [
    "KIND_CONSTRAINT",
    "KIND_DECISION",
    "KIND_ISSUE_FIXED",
    "KIND_OBSERVATION",
    "Fact",
    "FactKind",
    "FactStoreError",
    "ProjectMemory",
    "parse_fact",
    "render_fact",
]
