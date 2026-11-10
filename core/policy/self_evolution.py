"""自我改进的规矩 —— **风险越高，要求越严**。

设计依据：docs/tasks.md ``TASK-P4-070``（``SystemEvolutionProposal`` 实体 +
独立测试 + 审批后发布）、R-102（不得自主修改控制自身的系统）、R-104（防止系统
自改验收标准）、``TASK-P4-060``（高风险变更治理：标记为高风险的变更 100% 经过
独立评审后才可执行）。

**这一层回答的是"这类改动要多严"。** 领域层（:mod:`core.evolution.proposal`）
定死了**下限** —— 每类提案天生至少是什么风险、至少几个人批；这一层放的是
**项目可以在下限之上再抬多少**：某个项目可以要求所有代码改动都要两个人批。

**为什么只许往上抬、不许往下压。** 下限是这类改动**内在**的性质：改验收标准
在任何项目里都是高风险。把下限当成可以由配置调低的参数，就等于给了一个口子
—— 而这个口子的名字恰好是"把标准改松，好让自己更容易通过"（R-104）。所以这
里的校验是**只许更严**：写一个比下限更松的值，**策略压根建不出来**。

**这一层不存验收标准版本。** 版本是**治理外置**的（``TASK-P1-011``），它住在
治理存储里，由 :class:`orchestration.self_evolution.CriteriaPort` 读写。把它
塞进策略对象，等于让系统自己手里握着"现在是第几版标准"这个答案 —— 而那正是
R-104 要防的事。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from core.evolution.proposal import (
    MINIMUM_APPROVALS_BY_KIND,
    MINIMUM_RISK_BY_KIND,
    ProposalKind,
)
from core.evolution.taxonomy import RiskLevel

#: 风险档的次序 —— **用来判"只能更严"**。
_RISK_ORDER: Final[Mapping[RiskLevel, int]] = MappingProxyType(
    {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.HIGH: 2,
    }
)


@dataclass(frozen=True, slots=True)
class SelfEvolutionPolicy:
    """自我改进的规矩 —— **一个项目对自我改进的额外要求**。

    :param required_approvals: 每类提案要几份独立审批 —— **只能比下限更严**。
    :param risk_overrides: 每类提案的风险档 —— **只能比下限更严**。

    **这两张表都是"往严里加"的旋钮。** 表里没写的类别就按天生下限来；表里写
    了的，只在**更严**的时候才算数 —— 写一个更松的值会直接**报错**，而不是
    被悄悄忽略。
    """

    #: 每类提案要几份独立审批。
    required_approvals: Mapping[ProposalKind, int] = field(default_factory=dict)
    #: 每类提案的风险档。
    risk_overrides: Mapping[ProposalKind, RiskLevel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """**只能比下限更严 —— 写松了就是策略建不出来。**

        顺带把两张表**冻成只读** —— 规矩本身不该在运行中被谁改掉。
        """
        for kind, count in self.required_approvals.items():
            approval_floor = MINIMUM_APPROVALS_BY_KIND[kind]
            if count < approval_floor:
                raise ValueError(
                    f"{kind.value} 类提案至少要 {approval_floor} 份独立审批，写了 {count} —— "
                    "规矩只能往严里加"
                )
        for kind, risk in self.risk_overrides.items():
            risk_floor = MINIMUM_RISK_BY_KIND[kind]
            if _RISK_ORDER[risk] < _RISK_ORDER[risk_floor]:
                raise ValueError(
                    f"{kind.value} 类提案的风险不得低于 {risk_floor.value}，写了 {risk.value} —— "
                    "规矩只能往严里加"
                )
        object.__setattr__(
            self, "required_approvals", MappingProxyType(dict(self.required_approvals))
        )
        object.__setattr__(self, "risk_overrides", MappingProxyType(dict(self.risk_overrides)))

    def risk_for(self, kind: ProposalKind) -> RiskLevel:
        """这类提案的风险档 —— **下限与项目加严取更严的那个**。"""
        override = self.risk_overrides.get(kind)
        if override is None:
            return MINIMUM_RISK_BY_KIND[kind]
        return override

    def approvals_for(self, kind: ProposalKind) -> int:
        """这类提案要几份独立审批 —— **下限与项目加严取更大的那个**。"""
        return max(MINIMUM_APPROVALS_BY_KIND[kind], self.required_approvals.get(kind, 0))


__all__ = ["SelfEvolutionPolicy"]
