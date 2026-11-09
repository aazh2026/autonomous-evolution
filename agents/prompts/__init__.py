"""角色化 Prompt 集 —— 六个角色，六套正文，六种输出形状。

设计依据：docs/design.md §3.2（"我建议不要只使用一个通用 Director Prompt，
而是构建四个逻辑角色"）、§5（"不要简单地让 Agent 遍历 TODO，然后挑一个执行"；
"每一阶段都应该有结构化输出，并且允许下游独立验证"）；docs/tasks.md
TASK-P1-057（六套 Prompt；每套输出绑定对应 Pydantic schema；每套含证据引用
要求；模型与温度可外部配置）。

**这个包回答三个问题。**

1. **每个角色负责什么、不负责什么** —— 写在正文里，不只写在文档里；
2. **模型的输出长什么样** —— 六个 Pydantic 模型，**每个角色一个**，
   六套输出互相套不上（``extra="forbid"`` 会当场挡下来）；
3. **没有依据的结论怎么办** —— 标记成 ``unsupported``，**在下游被排除**。

**"没有依据"是系统算出来的，不是模型说的。** 见
:attr:`agents.prompts.base.Conclusion.status`。

**这个包不做 IO。** 它不调模型、不读配置、不写库。调用模型是
:class:`~agents.prompts.base.PromptClient` 那一头的事；把运行配置接上来是
:mod:`orchestration.prompt_runtime` 的事 —— 那样 ``agents/`` 就不必依赖
``infrastructure/``，依赖方向是干净的。
"""

from __future__ import annotations

from agents.prompts.analyst import (
    ANALYST_PROMPT,
    ANALYST_PROMPT_SET,
    AnalystOutput,
    GapConclusion,
    OpportunityConclusion,
)
from agents.prompts.architect import (
    ARCHITECT_PROMPT,
    ARCHITECT_PROMPT_SET,
    ArchitectOutput,
)
from agents.prompts.base import (
    Conclusion,
    ConclusionStatus,
    HasStatus,
    ModelSettings,
    PromptClient,
    PromptParseError,
    PromptRequest,
    PromptSet,
    Role,
    RoleOutput,
    conclusion_status,
    parse_output,
    supported,
    unsupported,
)
from agents.prompts.candidates import CandidateConclusion, CandidateRoleOutput
from agents.prompts.critic import (
    CRITIC_PROMPT,
    CRITIC_PROMPT_SET,
    CriticConclusion,
    CriticOutput,
)
from agents.prompts.director import (
    DIRECTOR_PROMPT,
    DIRECTOR_PROMPT_SET,
    DecisionConclusion,
    DirectorOutput,
)
from agents.prompts.quality import (
    QUALITY_PROMPT,
    QUALITY_PROMPT_SET,
    QualityOutput,
)
from agents.prompts.strategist import (
    STRATEGIST_PROMPT,
    STRATEGIST_PROMPT_SET,
    StrategistOutput,
)

#: 六套 Prompt —— 按角色的固定次序排。
#:
#: **次序是固定的，不是随手排的。** 它跟五阶段决策链的次序对齐：分析师先看
#: 现状，策略师 / 架构师 / 质量三个平行提候选，Director 最后决策，Critic 审
#: 决策。
PROMPT_SETS: tuple[PromptSet[RoleOutput], ...] = (
    ANALYST_PROMPT_SET,
    STRATEGIST_PROMPT_SET,
    ARCHITECT_PROMPT_SET,
    QUALITY_PROMPT_SET,
    DIRECTOR_PROMPT_SET,
    CRITIC_PROMPT_SET,
)


def prompt_set_for(role: Role) -> PromptSet[RoleOutput]:
    """取某个角色的那套 Prompt。取不到就是 ``KeyError`` —— 没有就是没有。"""
    for prompt_set in PROMPT_SETS:
        if prompt_set.role is role:
            return prompt_set
    raise KeyError(role.value)


__all__ = [
    "ANALYST_PROMPT",
    "ANALYST_PROMPT_SET",
    "ARCHITECT_PROMPT",
    "ARCHITECT_PROMPT_SET",
    "CRITIC_PROMPT",
    "CRITIC_PROMPT_SET",
    "DIRECTOR_PROMPT",
    "DIRECTOR_PROMPT_SET",
    "PROMPT_SETS",
    "QUALITY_PROMPT",
    "QUALITY_PROMPT_SET",
    "STRATEGIST_PROMPT",
    "STRATEGIST_PROMPT_SET",
    "AnalystOutput",
    "ArchitectOutput",
    "CandidateConclusion",
    "CandidateRoleOutput",
    "Conclusion",
    "ConclusionStatus",
    "CriticConclusion",
    "CriticOutput",
    "DecisionConclusion",
    "DirectorOutput",
    "GapConclusion",
    "HasStatus",
    "ModelSettings",
    "OpportunityConclusion",
    "PromptClient",
    "PromptParseError",
    "PromptRequest",
    "PromptSet",
    "QualityOutput",
    "Role",
    "RoleOutput",
    "StrategistOutput",
    "conclusion_status",
    "parse_output",
    "prompt_set_for",
    "supported",
    "unsupported",
]
