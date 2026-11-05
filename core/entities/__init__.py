"""十二个核心持久化实体。

设计依据：docs/design.md §3.6「核心数据模型」表；docs/tasks.md TASK-P1-020。

    Project              项目定义
    ProjectSnapshot      项目状态快照
    EvolutionCandidate   演进机会
    EvolutionDecision    演进决策
    Task                 研发任务
    TaskDependency       任务依赖
    AgentSession         执行会话
    ExecutionEvent       执行轨迹
    VerificationRun      验收记录
    Artifact             交付证据
    ArchitectureDecision 架构决策
    EvolutionCycle       演进周期

导入本模块会把全部实体注册到同一个 ``Base.metadata`` —— Alembic 的
autogenerate 与 ``create_all`` 都依赖这一点。少导入一个实体，迁移就会
悄悄漏掉那张表。
"""

from __future__ import annotations

from core.entities.architecture import ArchitectureDecision
from core.entities.base import Base, JSONDocument, TimestampMixin, UUIDPrimaryKeyMixin
from core.entities.evidence import Artifact, VerificationRun
from core.entities.evolution import EvolutionCandidate, EvolutionCycle, EvolutionDecision
from core.entities.execution import AgentSession, ExecutionEvent
from core.entities.project import Project, ProjectSnapshot
from core.entities.task import Task, TaskDependency

#: 十二个实体。顺序与 design.md §3.6 的表格一致，便于逐条核对。
ALL_ENTITIES = (
    Project,
    ProjectSnapshot,
    EvolutionCandidate,
    EvolutionDecision,
    Task,
    TaskDependency,
    AgentSession,
    ExecutionEvent,
    VerificationRun,
    Artifact,
    ArchitectureDecision,
    EvolutionCycle,
)

__all__ = [
    "ALL_ENTITIES",
    "AgentSession",
    "ArchitectureDecision",
    "Artifact",
    "Base",
    "EvolutionCandidate",
    "EvolutionCycle",
    "EvolutionDecision",
    "ExecutionEvent",
    "JSONDocument",
    "Project",
    "ProjectSnapshot",
    "Task",
    "TaskDependency",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "VerificationRun",
]
