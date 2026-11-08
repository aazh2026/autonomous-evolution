"""工程验证 —— 验收契约与独立验证引擎。

设计依据：docs/design.md §3.5B（"不要让 Coding Agent 自己决定是否验收
通过"；Acceptance Contract）、§6（准入闸门）；docs/tasks.md TASK-P1-040、
TASK-P1-041、R-049~R-054。

- :mod:`harness.verification.contract` —— 验收契约与判定规则："什么算完成"；
- :mod:`harness.verification.engine` —— 独立验证引擎："这次够不够"。

**两者分开的理由是方向。** 契约是执行**之前**定下的验收标准，引擎是执行
**之后**给出的结论。合成一个模块，就等于让执行者自己定义验收标准 ——
而那正是 design.md §3.5B 一开始就禁止的。
"""

from __future__ import annotations

from harness.verification.contract import (
    AcceptanceContract,
    ArtifactKind,
    CheckOutcome,
    CheckResult,
    ContractEvaluation,
    ForbiddenRule,
    RequiredCheck,
    Threshold,
    evaluate_contract,
)

__all__ = [
    "AcceptanceContract",
    "ArtifactKind",
    "CheckOutcome",
    "CheckResult",
    "ContractEvaluation",
    "ForbiddenRule",
    "RequiredCheck",
    "Threshold",
    "evaluate_contract",
]
