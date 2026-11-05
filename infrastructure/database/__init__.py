"""数据访问基础设施。

设计依据：docs/design.md §3.6（PostgreSQL 为项目状态库）、docs/tasks.md
TASK-P1-003 / TASK-P1-020 / TASK-P1-022。

分层：

- ``core/entities/`` —— 十二个持久化实体与声明式基类（领域侧）。
- ``infrastructure/database/`` —— 引擎、会话、仓储（基础设施侧）。

依赖方向永远是 ``infrastructure → core``。
"""

from __future__ import annotations

from infrastructure.database.engine import (
    build_engine,
    build_session_factory,
    describe,
    dispose_engine,
    get_engine,
    get_session_factory,
    session_scope,
)
from infrastructure.database.repositories import (
    DECISION_DEFER,
    DECISION_RECORD,
    DECISION_REJECT,
    DECISION_STATES,
    AcceptanceContractError,
    AgentSessionRepository,
    CandidateRepository,
    DecisionRepository,
    InvalidDecisionError,
    NonExecutableDecisionError,
    NotFoundError,
    RepositoryError,
    TaskDependencyRepository,
    TaskRepository,
)

__all__ = [
    "DECISION_DEFER",
    "DECISION_RECORD",
    "DECISION_REJECT",
    "DECISION_STATES",
    "AcceptanceContractError",
    "AgentSessionRepository",
    "CandidateRepository",
    "DecisionRepository",
    "InvalidDecisionError",
    "NonExecutableDecisionError",
    "NotFoundError",
    "RepositoryError",
    "TaskDependencyRepository",
    "TaskRepository",
    "build_engine",
    "build_session_factory",
    "describe",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "session_scope",
]
