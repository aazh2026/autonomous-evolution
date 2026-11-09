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

还有两张**编排状态**表：

    GraphCheckpoint      决策图的一个 checkpoint（某个时刻的完整状态）
    GraphCheckpointWrite checkpoint 期间写下的中间结果

它们记的是**编排器自己的运行状态**，不是项目的业务事实 —— 决策图跑到哪一步
了、中间写下了什么。放在这儿是因为 Alembic 的 autogenerate 与 ``create_all``
只看 ``Base.metadata``：实体散落在别处，迁移就会悄悄漏掉那几张表。

还有两张**事件**表：

    OutboxEvent         待发布的事件（状态写入与事件发布同事务）
    ConsumedEvent       某个消费者已经占用过的消息（幂等消费）

它们同样不在 design.md §3.6 的清单里 —— 但 TASK-P1-025 的"状态写入与事件
发布**同事务**"（I-06）必须落到某张表上，否则那条要求只是一句文档：一个
可靠的 outbox 需要一张自己的表，而不是靠调用方自觉地把"改状态"和"发消息"
放在同一个事务里。

导入本模块会把全部实体注册到同一个 ``Base.metadata`` —— Alembic 的
autogenerate 与 ``create_all`` 都依赖这一点。少导入一个实体，迁移就会
悄悄漏掉那张表。
"""

from __future__ import annotations

from core.entities.approval import ApprovalRequest
from core.entities.architecture import ArchitectureDecision
from core.entities.base import Base, JSONDocument, TimestampMixin, UUIDPrimaryKeyMixin
from core.entities.budget import BudgetAccount, BudgetEntry
from core.entities.checkpoint import GraphCheckpoint, GraphCheckpointWrite
from core.entities.evidence import Artifact, VerificationRun
from core.entities.evolution import EvolutionCandidate, EvolutionCycle, EvolutionDecision
from core.entities.execution import AgentSession, ExecutionEvent
from core.entities.outbox import ConsumedEvent, OutboxEvent
from core.entities.project import CoverageBaseline, Project, ProjectSnapshot
from core.entities.task import Task, TaskDependency

#: 十二个核心实体 + 两张预算账本表 + 一张审批请求表 + 两张编排状态表
#: + 两张事件表。
#:
#: 前十二个的顺序与 design.md §3.6 的表格一致，便于逐条核对；后面三个是
#: 预算账本（见模块文档与 tasks.md 第 6 节待裁决项 8）与审批请求；再后面两个
#: 是决策图的 checkpoint；最后两个是 outbox 与幂等消费标记。
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
    GraphCheckpoint,
    GraphCheckpointWrite,
    OutboxEvent,
    ConsumedEvent,
    CoverageBaseline,
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
    "ConsumedEvent",
    "CoverageBaseline",
    "EvolutionCandidate",
    "EvolutionCycle",
    "EvolutionDecision",
    "ExecutionEvent",
    "GraphCheckpoint",
    "GraphCheckpointWrite",
    "JSONDocument",
    "OutboxEvent",
    "Project",
    "ProjectSnapshot",
    "Task",
    "TaskDependency",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "VerificationRun",
]
