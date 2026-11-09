"""产出候选的三个角色共用的那一份 —— Strategist / Architect / Quality。

设计依据：docs/design.md §3.2（五类演进机会；"技术趋势不应直接转化为升级
任务，必须先证明它对当前项目有实际收益"）、§3.3（Candidate 的统一结构）；
docs/tasks.md TASK-P1-052（五类演进机会分类法）、TASK-P1-057。

**这三个角色的输出形状是一样的，差别只在两处**：看的角度不同（正文），
产出的候选归哪一类不同（:attr:`CandidateRoleOutput.category`）。

**为什么类别由角色定，不由模型定。**
:mod:`core.evolution.taxonomy` 把机会分成产品 / 架构 / 工程 / 质量 / 技术
五类，为的是让它们**互相可比** —— 而可比的前提是每一类都由同一双眼睛看
出来的。如果模型自己填 ``category``，那么"质量策略师报了一条产品机会"就会
发生，而那两类机会的排期口径根本不同（产品的依据是用户价值，质量的依据是
缺陷率），放在一起比就是拿两把尺子量同一张桌子。

所以 :class:`CandidateConclusion` **没有** ``category`` 字段，类别由角色钉死。
模型多说一句 ``category`` 会被丢掉（见 :mod:`agents.prompts.base` 里对
"系统说了算的字段"的处理），不影响结果 —— 但它也不作数。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import ClassVar

from pydantic import Field, model_validator

from agents.prompts.base import Conclusion, ConclusionStatus, RoleOutput
from core.evolution.candidate import EvolutionCandidate, ProjectBenefit
from core.evolution.taxonomy import OpportunityCategory, RiskLevel


class CandidateConclusion(Conclusion):
    """一条"值得做的事"的结论。

    **它比别的结论多几栏，因为"值得做"这句话本身信息量太小。**
    一条只有主张的候选没法排期：不知道假设是什么、不知道做完会怎样、不知道
    代价多大、也不知道凭什么说它有把握。所以这里把 :class:`EvolutionCandidate`
    要求的那几栏都摆出来，让模型**在给结论的同时给出依据**。

    **注意这里没有 ``category``。** 类别由角色钉死（见本模块的说明）。
    """

    #: 假设：做了这件事会怎样。
    hypothesis: str = Field(min_length=1)
    #: 预期结果。**至少一条** —— 事后拿它跟实际收益比，才知道当初的判断准不准。
    expected_outcome: tuple[str, ...] = Field(min_length=1)
    #: 风险档。风险说的是"做了可能出什么事"，与优先级不是一回事。
    risk: RiskLevel
    #: 证据置信度，0~1。**这是证据的置信度**，不是"我觉得有把握"。
    confidence: Decimal = Field(ge=0, le=1)
    #: 预估成本。自由字符串 —— 成本单位不统一是常态（人天 / token / 机器时）。
    estimated_effort: str = ""
    #: 对本项目的实际收益论证。技术类候选必填；这三个角色产出的不是技术类，
    #: 所以它是可选的。
    benefit: ProjectBenefit | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_the_category(cls, data: object) -> object:
        """丢掉模型多说的 ``category``。

        **收下但不采用。** 类别是角色的事 —— 模型说了不算。但为这一句话把整份
        输出毙掉太贵：一次调用的钱、一次跑偏的分析，还有那几段其实写得对的
        结论。

        真正的类别由 :attr:`CandidateRoleOutput.category` 给出。
        """
        if isinstance(data, Mapping) and "category" in data:
            return {key: value for key, value in data.items() if key != "category"}
        return data


class CandidateRoleOutput(RoleOutput):
    """产出候选的角色（Strategist / Architect / Quality）的输出形状。

    **三个角色共用这一份输出形状。** 它们的差别在正文（看哪个角度）与钉死
    的类别，不在结构 —— 结构一样，才能把三个角色的候选放进同一份清单里比。
    """

    #: 这个角色产出的候选属于哪一类机会。**由角色定，不由模型定。**
    category: ClassVar[OpportunityCategory]

    #: 候选清单。
    candidates: tuple[CandidateConclusion, ...] = ()

    @property
    def conclusions(self) -> tuple[CandidateConclusion, ...]:
        """这个角色产出的全部结论。"""
        return self.candidates

    def to_candidates(self) -> tuple[EvolutionCandidate, ...]:
        """翻译成领域层的演进候选。

        **没有依据的候选不进来。** 一条不带证据的候选在下游是"看起来完整"
        的东西 —— 它有标题、有假设、有预期结果，唯独没有依据，而那恰恰是
        最容易被当成事实用的地方。

        **类别由角色钉死，不看模型说了什么。**
        """
        return tuple(
            EvolutionCandidate(
                id=item.id,
                category=self.category,
                title=item.statement,
                evidence=item.evidence,
                hypothesis=item.hypothesis,
                expected_outcome=item.expected_outcome,
                risk=item.risk,
                confidence=item.confidence,
                estimated_effort=item.estimated_effort,
                benefit=item.benefit,
            )
            for item in self.candidates
            if item.status is ConclusionStatus.SUPPORTED
        )


__all__ = [
    "CandidateConclusion",
    "CandidateRoleOutput",
]
