"""独立 Critic —— 九项检查、事实核验工具与结构化审查报告。

设计依据：docs/design.md §5（"建议增加一个独立的 Critic"，七问；"应该调用代码
仓库、Git 历史、测试报告和项目状态进行事实核验"）；docs/tasks.md
TASK-P1-055（R-085 / R-086 / R-087 / I-18）。

**Critic 不给分数，给一份可复核的报告。** 分数会把"查过六处"压成一个数，
读的人没法复核那个数背后查了什么。这个包分四层：

- :mod:`agents.critic.checks` —— 九问是哪九问、结论的三态、否决的资格；
- :mod:`agents.critic.tools` —— 事实核验工具的接口与调用账本；
- :mod:`agents.critic.sources` —— 工具的具体实现（**这一层做 IO**）；
- :mod:`agents.critic.report` —— 把九项检查跑起来，产出 :class:`CriticReport`；
- :mod:`agents.critic.matching` —— 词面匹配（判定"是否触碰非目标"）。

**为什么 IO 在 ``agents/`` 而不是 ``core/``。** ``core/`` 是领域层，一行 IO
都不做；这些工具唯一的职责就是做 IO。它们按接口取事实，不拥有事实 ——
所以 ``TaskTableTool`` 收的是 :class:`~agents.critic.tools.TaskLookup` 端口，
不是数据库连接。
"""

from __future__ import annotations

from agents.critic.checks import (
    CHECK_QUESTIONS,
    VETOING_CHECKS,
    CheckOutcome,
    CriticCheck,
    CriticEvidence,
    EvidenceKind,
    Verdict,
)
from agents.critic.report import Critic, CriticReport, CriticVetoError, require_no_veto
from agents.critic.tools import (
    FactChecker,
    FactTool,
    ProjectStatusFact,
    TaskLookup,
    TaskSummary,
    ToolCall,
    ToolKind,
    ToolResult,
    ToolUnavailableError,
)

__all__ = [
    "CHECK_QUESTIONS",
    "VETOING_CHECKS",
    "CheckOutcome",
    "Critic",
    "CriticCheck",
    "CriticEvidence",
    "CriticReport",
    "CriticVetoError",
    "EvidenceKind",
    "FactChecker",
    "FactTool",
    "ProjectStatusFact",
    "TaskLookup",
    "TaskSummary",
    "ToolCall",
    "ToolKind",
    "ToolResult",
    "ToolUnavailableError",
    "Verdict",
    "require_no_veto",
]
