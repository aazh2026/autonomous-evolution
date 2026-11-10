"""四个因子的判断 —— 价值 / 影响 / 紧迫 / 成本。

设计依据：docs/design.md §3.3 第三层"演进价值评估（Prioritize）"；docs/tasks.md
TASK-P3-014（交付物：价值模型 + 用实际收益校准；验收条件"校准后模型的排序与
'事后实际收益'有正相关（Spearman ρ > 0.3），否则退回启发式模型并记录"；验证
T2）、TASK-P1-053（六因子公式 ``V×I×C×U / (E×(1+R))``）；R-031（跨方案优先级
判断）、R-033（价值模型须用实际收益校准）。

**六个因子里有四个没有观察来源。** ``C``（证据置信度）与 ``R``（风险系数）取
候选自己的 —— 候选存的就是"这份证据有多硬"与"这件事有多危险"，那两样是
**证据的属性**。剩下的 ``V`` / ``I`` / ``U`` / ``E`` 回答的是"这件事值多少、
影响多大、有多急、要花多少" —— **这些看不出来，只能判断。**

**判断要有出处。** 这一层给的是一个**端口**（:class:`FactorAssessor`），不是
一个具体算法 —— 判断从哪儿来是上面那层的事（Director 的评估阶段，或者人）。
**端口不定义"怎么判断"，只定义"判断长什么样"**，于是换一个判断来源时，下游
的评分与校准流程一行都不用改。

**"判不出来"必须与"判成 0"分开。** 端口允许返回 ``None``：那是**判不出来**
—— 要补信息；而 ``0`` 是**判成"这件事不值"** —— 那是结论。**两者的补救动作
完全相反**（一个是去补数据，一个是照做），混成一件就等于没记。

**越界就报错，不夹紧。** 与 :class:`~core.evolution.priority.PriorityFactors`
同一条规矩：夹紧会让"我填了 1.5"与"我填了 1"变成同一件事，而那件事正是错
的 —— 填错的人不会知道自己填错了，模型也不会知道。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from core.evolution.candidate import EvolutionCandidate

#: 判断出来的四个因子。**置信度与风险不在其中** —— 那两个取候选自己的。
JUDGED_FACTORS: tuple[str, ...] = ("value", "impact", "urgency", "cost")


@dataclass(frozen=True, slots=True)
class FactorJudgement:
    """``V`` / ``I`` / ``U`` / ``E`` —— 四个判断出来的因子。

    :param value: ``V`` 预期价值，0~1。
    :param impact: ``I`` 影响范围，0~1。
    :param urgency: ``U`` 紧迫程度，0~1。
    :param cost: ``E`` 实施成本，0~1。**0 合法** —— 它会被按下限算
        （见 :data:`~core.evolution.priority.MINIMUM_COST`）。

    **四个数一组，不是六个** —— 另两个取候选自己的，装配的时候才知道。
    """

    value: Decimal
    impact: Decimal
    urgency: Decimal
    cost: Decimal

    def __post_init__(self) -> None:
        """**四个数都得落在 0~1 之间。**"""
        for name in JUDGED_FACTORS:
            number = getattr(self, name)
            if not Decimal(0) <= number <= Decimal(1):
                raise ValueError(f"因子 {name} 必须落在 0~1 之间 —— 收到 {number}")


@runtime_checkable
class FactorAssessor(Protocol):
    """判断四个因子的端口。

    **它只回答"这件事值多少、影响多大、有多急、要花多少"。** 不回答"要不要
    做" —— 那是决策层的事（P1-054 的 record / defer / reject）。
    """

    def assess(self, candidate: EvolutionCandidate) -> FactorJudgement | None:
        """判断一个候选。**返回 ``None`` 表示判不出来** —— 与"判成 0"是两
        件事。
        """
        ...


__all__ = ["JUDGED_FACTORS", "FactorAssessor", "FactorJudgement"]
