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

此外有两张**预算账本**表，不在 design.md §3.6 的清单里：

    BudgetAccount        预算累计额
    BudgetEntry          预算流水（append-only）

它们是设计文档的一处缺口，已在 docs/tasks.md 第 6 节列为待裁决项 8。补它们的
理由很直接：TASK-P1-012 要求"用真实并发写库验证"，而十二个实体里没有任何
一张表能承载预算累计额。

还有一张**审批请求**表：

    ApprovalRequest      人工审批请求（谁在什么时候批了什么）

它同样不在 design.md §3.6 的清单里，但 §7 的初始自主权限里明确写着"代码合并
= L2 需要人工审批" —— 人工审批必须落到某张表上，否则那条权限只是一句文档。

导入本模块会把全部实体注册到同一个 ``Base.metadata`` —— Alembic 的
autogenerate 与 ``create_all`` 都依赖这一点。少导入一个实体，迁移就会
悄悄漏掉那张表。
"""

from __future__ import annotations

from core.entities.approval import ApprovalRequest
from core.entities.architecture import ArchitectureDecision
from core.entities.base import Base, JSONDocument, TimestampMixin, UUIDPrimaryKeyMixin
from core.entities.budget import BudgetAccount, BudgetEntry
from core.entities.evidence import Artifact, VerificationRun
from core.entities.evolution import EvolutionCandidate, EvolutionCycle, EvolutionDecision
from core.entities.execution import AgentSession, ExecutionEvent
from core.entities.project import Project, ProjectSnapshot
from core.entities.task import Task, TaskDependency

#: 十二个核心实体 + 两张预算账本表 + 一张审批请求表。
#:
#: 前十二个的顺序与 design.md §3.6 的表格一致，便于逐条核对；后面三个是
#: 预算账本（见模块文档与 tasks.md 第 6 节待裁决项 8）与审批请求。
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
    BudgetAccount,
    BudgetEntry,
    ApprovalRequest,
)

__all__ = [
    "ALL_ENTITIES",
    "AgentSession",
    "ApprovalRequest",
    "ArchitectureDecision",
    "Artifact",
    "Base",
    "BudgetAccount",
    "BudgetEntry",
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
