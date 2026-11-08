"""人工审批门。

设计依据：docs/tasks.md TASK-P1-046（DB-backed 审批门）、docs/design.md §7。

这一层是**适配层**：领域模型与持久化端口在 ``core/policy/approval.py``，数据
库实现在 ``infrastructure/database/approval.py``。门本身把两者接起来，并把
"审批被拒 → 任务进 BLOCKED"这条规则放在一处。
"""

from __future__ import annotations

from harness.approval.gate import (
    DEFAULT_TTL,
    DEFAULT_WAIT_SECONDS,
    ApprovalGate,
    ApprovalRejection,
    block_after_rejection,
)

__all__ = [
    "DEFAULT_TTL",
    "DEFAULT_WAIT_SECONDS",
    "ApprovalGate",
    "ApprovalRejection",
    "block_after_rejection",
]
