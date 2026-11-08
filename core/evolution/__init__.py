"""演进决策链的领域模型。

设计依据：docs/design.md §5（Director 五阶段决策链：认知 → 诊断 → 探索 →
决策 → 规划）、§3.2（五类演进机会）、§3.3（Candidate 结构与优先级公式）；
docs/tasks.md TASK-P1-051 ~ TASK-P1-056。

这个包是**循环 B**（项目演进循环）的词汇表。四个模块各对应决策链上的一段：

- :mod:`core.evolution.evidence` —— 证据引用的形态与解析规则；
- :mod:`core.evolution.taxonomy` —— 五类演进机会与粗档优先级；
- :mod:`core.evolution.assessment` —— 认知阶段的输出（项目评估）；
- :mod:`core.evolution.candidate` —— 探索阶段的输出（演进候选）；
- :mod:`core.evolution.priority` —— 六因子优先级评分；
- :mod:`core.evolution.decision` —— 决策三态与"不行动"的落地。

**这里只有纯领域模型。** 没有 IO，没有数据库，没有 LLM 调用 —— 这些模型
描述的是"这些东西长什么样、哪些一致性必须成立"，让它们能脱离运行环境
被单独校验与比较。取证据、存决策、跑编排都在外面。
"""

from __future__ import annotations

from core.evolution.assessment import (
    DeferredOpportunity,
    GapSeverity,
    GoalGap,
    Opportunity,
    OverallStatus,
    ProjectAssessment,
)
from core.evolution.candidate import (
    CandidateDiscoverer,
    CandidateStatus,
    DiscoveryContext,
    DiscoveryResult,
    DroppedTrend,
    EvolutionCandidate,
    ProjectBenefit,
    TechnologyTrend,
    TechnologyTrendDiscoverer,
)
from core.evolution.evidence import ARTIFACT_SCHEME, ISSUE_SCHEME, Evidence, EvidenceKind
from core.evolution.taxonomy import OpportunityCategory, PriorityLevel, RiskLevel

__all__ = [
    "ARTIFACT_SCHEME",
    "ISSUE_SCHEME",
    "CandidateDiscoverer",
    "CandidateStatus",
    "DeferredOpportunity",
    "DiscoveryContext",
    "DiscoveryResult",
    "DroppedTrend",
    "Evidence",
    "EvidenceKind",
    "EvolutionCandidate",
    "GapSeverity",
    "GoalGap",
    "Opportunity",
    "OpportunityCategory",
    "OverallStatus",
    "PriorityLevel",
    "ProjectAssessment",
    "ProjectBenefit",
    "RiskLevel",
    "TechnologyTrend",
    "TechnologyTrendDiscoverer",
]
