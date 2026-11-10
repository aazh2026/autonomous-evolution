"""风险的词汇与下限 —— **风险越高，要几份独立评审；改架构，还得有人有资格**。

设计依据：docs/tasks.md ``TASK-P4-060``（高风险变更治理：架构级变更的独立评审
与放行流程；验收条件"**标记为高风险的变更 100% 经过独立评审后才可执行**"）、
R-061（高风险操作独立授权）、R-102（不得自主修改控制自身的系统）、
``TASK-P4-070``（``SystemEvolutionProposal`` 实体 + 独立测试 + 审批后发布）。

**这一层只放"下限"与"词汇"。** 它不判任何一份具体的评审 —— 那是
:mod:`core.evolution.proposal`（实体）与 :mod:`orchestration.high_risk_review`
（流程）的事。

**为什么下限钉在风险档上，而不是钉在"改动的种类"上。** 验收条件说的是"**标记
为高风险**的变更" —— 关键词是"标记为"，不是"是哪一类改动"。同一类改动，这一
次风险高、下一次风险低，都是常事。把要求钉在种类上，就会留下"这一类只要一份
审批"的口子 —— 而风险恰恰是**逐次判断**的：**尺子必须对着每一次的标记，不能对
着类目。**

**为什么高风险要两份。** 一份是**自证**（提的人自己批）。两份意味着另一个人也看
了 —— 高风险改动的影响面大到一个人的判断不够。而两份必须是**两个人**：一个人
点两次批准，那还是一个人的意见。

**评审资格为什么是独立的一层。** "有几个人批了"回答不了"这几个人看得懂吗"。让
一个只懂测试的人去批改架构的改动，等于没批 —— 所以还要求**至少有一位具备该类
评审资格的人**。

**这一层是叶子。** 它不认识提案、不认识流程、不认识存储 —— 所以
:mod:`core.evolution.proposal` 与 :mod:`core.policy.high_risk` 都能引它，
而它们之间不会因此形成环。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from core.evolution.taxonomy import RiskLevel


class ReviewerScope(StrEnum):
    """评审资格 —— **"你能审什么"**。

    **这是个闭集。** 评审资格只有这几种：新增一种就是新增一条治理规则，该走
    外部配置 + 审批那条路，而不是在代码里随手加一个字符串 —— 后者会让"谁能审
    这类改动"这个问题没有确定答案。

    - ``architecture`` —— 架构评审资格（改分层、改依赖方向、改模块划分）。
    - ``security`` —— 安全评审资格（权限、密钥、攻击面）。
    - ``governance`` —— 治理评审资格（改规则、改验收标准、改权限策略）。
    - ``general`` —— 通用评审资格。**它不顶替上面三种** —— 否则"至少有一位
      架构评审人"这条要求就等于没有。
    """

    ARCHITECTURE = "architecture"
    SECURITY = "security"
    GOVERNANCE = "governance"
    GENERAL = "general"


#: 每档风险**天生**要几份独立评审 —— **规矩只能往上抬，不能往下压**。
#:
#: **为什么低风险是 0 而不是 1。** 零评审不等于"随便改" —— 低风险改动走的是
#: 常规路径（该有的验收、该有的测试一样不少），只是**不再额外要求一份人工评审**。
#: 而**中风险要 1 份**：那是"至少有人看过一眼"的最低保证。
MINIMUM_REVIEWS_BY_RISK: Final[Mapping[RiskLevel, int]] = MappingProxyType(
    {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.HIGH: 2,
    }
)


#: 风险档的次序 —— **用来判"不得低于这一档"**。
#:
#: **为什么不给 :class:`~core.evolution.taxonomy.RiskLevel` 加序。** 那个枚举是
#: 全仓库共用的粗档，给它加序就是给它加语义 —— 而"低 < 中 < 高"这件事在本仓
#: 库里只有风险这一处需要判，留在这里更诚实。
RISK_ORDER: Final[Mapping[RiskLevel, int]] = MappingProxyType(
    {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.HIGH: 2,
    }
)


def reviews_for_risk(risk: RiskLevel) -> int:
    """这一档风险**天生**要几份独立评审。"""
    return MINIMUM_REVIEWS_BY_RISK[risk]


def max_risk(*risks: RiskLevel) -> RiskLevel:
    """几档风险里**最高**的那一档。

    **为什么需要它。** 一份变更的档位有两个来源 —— **它是什么操作**
    （合并到主干 = 高）与**它是什么种类的改动**（改验收标准 = 高）。这两个来源
    可能给出不同的档位，而**档位只能取更高的那个**：低的那个是下限，不是上限。
    """
    if not risks:
        raise ValueError("至少要有一档风险")
    return max(risks, key=lambda risk: RISK_ORDER[risk])


__all__ = [
    "MINIMUM_REVIEWS_BY_RISK",
    "RISK_ORDER",
    "ReviewerScope",
    "max_risk",
    "reviews_for_risk",
]
