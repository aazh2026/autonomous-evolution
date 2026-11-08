"""项目理解与差距分析的结构化模型。

设计依据：docs/design.md §5（Director 五阶段决策链的第一阶段 —— 认知，
输出 ``project_assessment``；"每一阶段都应该有结构化输出，并且允许下游独立
验证"）、§3.2；docs/tasks.md TASK-P1-051（R-022 Director 每轮回答五问；
R-082 每阶段结构化输出且下游可独立验证；R-083 project_assessment 结构）。

**这份模型回答的是"项目现在怎么样"，不是"下一步做什么"。**
``goal_gap`` 是**目标差距** —— 当前状态离 Key Results 还差多少；它不是缺陷
清单。缺陷、体验、技术债那类东西属于"机会"（:class:`Opportunity`），因为
它们不对应某个 Key Result。这条区分不是洁癖：它决定了 ``overall_status``
能不能与 ``goal_gap`` 自洽（见 :meth:`ProjectAssessment._check_consistency`）。

**三条硬约束**（对应 TASK-P1-051 的三条 T1）：

1. **每个差距都要有证据。** 没证据的差距是猜测，而猜测一旦流到决策层，
   就会变成一个没人能复核的结论。
2. **证据必须指向可解析的引用。** 一句"这里很慢"撑不起一个差距。
3. **每条"暂缓"都要说清为什么。** 没有理由的暂缓是一个不用负责的垃圾桶。

**"暂缓"是一等公民。** design.md §3.3 明写"不行动也是一个合法决策"。
:class:`DeferredOpportunity` 让"这轮不做"成为**结构化产物**而不是沉默 ——
沉默看不出"是判断不做"还是"压根没看见"，而这两种情况的补救动作完全相反。

**这个模块不碰数据库，也不碰文件系统。** 它只描述"一份项目评估长什么样"
以及"哪些一致性必须成立"。落库是仓储层的事，取证据是知识层的事。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.evolution.evidence import Evidence
from core.evolution.taxonomy import OpportunityCategory, PriorityLevel


class GapSeverity(StrEnum):
    """差距的严重度。

    四档而不是两档："高"与"低"之间那一片是最需要区分的 —— 中等的差距
    可以排期，高危的差距不能等。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OverallStatus(StrEnum):
    """项目当前的整体状态。

    - ``evolving`` —— 正在演进：有目标差距，有明确的下一步。
    - ``at_target`` —— 已达目标：Key Results 都达标了。
    - ``blocked`` —— 卡住：推进不了（预算、权限、外部依赖）。
    - ``stalled`` —— 停滞：能推进，但连续几轮没有可验证的改善。

    **"卡住"与"停滞"要分开。** 两者的补救完全不同：卡住要解阻塞
    （要授权、要预算、要等别人），停滞要换方向（继续按老路走只会原地
    打转）。合成一个"有问题"就等于把两种处方混成一种。
    """

    EVOLVING = "evolving"
    AT_TARGET = "at_target"
    BLOCKED = "blocked"
    STALLED = "stalled"


class GoalGap(BaseModel):
    """一个目标差距。

    ``evidence`` **至少一条** —— 这是差距能成立的全部理由。没有证据的
    差距不该被构造出来。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: 支撑这条差距的证据。**至少一条**，且每条都指向可解析的引用。
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    severity: GapSeverity

    @field_validator("id", "description")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("差距的 id 与描述都不能是空白")
        return value.strip()


class Opportunity(BaseModel):
    """一个演进机会。

    ``action`` 是"打算怎么做"，不是"发现了什么" —— 发现的东西在
    :class:`GoalGap` 与证据里，这里说的是针对它要做的事。

    ``addresses`` 指向它回应的是哪些差距（:class:`GoalGap` 的 id）。留空
    表示"这是从别处冒出来的机会"（比如一条技术趋势）；填了的话，下游可以
    独立核验"这个动作确实对应上面说的那个差距"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    category: OpportunityCategory
    action: str = Field(min_length=1)
    priority: PriorityLevel
    #: 本机会回应的差距 id。必须在同一份评估的 ``goal_gap`` 里存在。
    addresses: tuple[str, ...] = ()

    @field_validator("id", "action")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("机会的 id 与动作不能是空白")
        return value.strip()


class DeferredOpportunity(BaseModel):
    """一条"这轮不做"。

    ``reason`` 是**必填**的 —— 没有理由的暂缓等于没暂缓：它既不能让读的人
    知道为什么，也没法在下一轮被重新评估（重新评估需要知道当初缺了什么）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(min_length=1)
    #: 被暂缓的候选标识。
    candidate: str = Field(min_length=1)

    @field_validator("reason", "candidate")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("暂缓的理由与候选标识都不能是空白")
        return value.strip()


class ProjectAssessment(BaseModel):
    """一份项目评估（认知阶段的结构化输出）。

    **五问里的第一问的答案。** design.md §5 的决策链有五阶段，这份模型是
    第一阶段（认知 → Project Understanding）的产物，也是下游四阶段的
    **唯一输入口径** —— 下游不再去看原始数据，只看这份结构化结果。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    #: 评估所依据的项目快照（通常是 commit 或发布号）。它是这份评估的
    #: **时点** —— 隔天再读这份评估，它描述的是快照那一刻的项目。
    snapshot: str = Field(min_length=1)
    overall_status: OverallStatus
    #: 目标差距（离 Key Results 还差什么）。
    goal_gap: tuple[GoalGap, ...] = ()
    #: 演进机会（可以做什么）。
    opportunities: tuple[Opportunity, ...] = ()
    #: 推荐次序 —— 机会 id 的列表，**按推荐顺序排**。
    recommendations: tuple[str, ...] = ()
    #: 这轮不做的事。
    deferred: tuple[DeferredOpportunity, ...] = ()

    @field_validator("project_id", "snapshot")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("项目 id 与快照不能是空白")
        return value.strip()

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        """整体一致性 —— 引用必须指得到，暂缓不能被推荐。"""
        _reject_duplicates([gap.id for gap in self.goal_gap], "目标差距的 id")
        _reject_duplicates([opportunity.id for opportunity in self.opportunities], "机会的 id")
        _reject_duplicates(list(self.recommendations), "推荐项")

        known_gaps = {gap.id for gap in self.goal_gap}
        known_opportunities = {opportunity.id for opportunity in self.opportunities}

        for opportunity in self.opportunities:
            unknown = [gap_id for gap_id in opportunity.addresses if gap_id not in known_gaps]
            if unknown:
                raise ValueError(
                    f"机会 {opportunity.id} 声称回应了不存在的差距：{'、'.join(unknown)}"
                )

        unknown_recommendations = [
            item for item in self.recommendations if item not in known_opportunities
        ]
        if unknown_recommendations:
            raise ValueError(f"推荐项指向不存在的机会：{'、'.join(unknown_recommendations)}")

        recommended = set(self.recommendations)
        for item in self.deferred:
            if item.candidate in recommended:
                raise ValueError(
                    f"候选 {item.candidate} 既被暂缓又在推荐里 —— "
                    "暂缓必须是真暂缓，不能既不做又要做"
                )

        if self.overall_status is OverallStatus.AT_TARGET and self.goal_gap:
            raise ValueError(
                "整体状态是 at_target 就不该还有目标差距 —— "
                "目标差距量的是离 Key Results 的距离，达标了就没有距离"
            )
        return self

    def opportunity(self, opportunity_id: str) -> Opportunity | None:
        """按 id 取一个机会。没有返回 ``None``。"""
        for opportunity in self.opportunities:
            if opportunity.id == opportunity_id:
                return opportunity
        return None

    def gap(self, gap_id: str) -> GoalGap | None:
        """按 id 取一个目标差距。没有返回 ``None``。"""
        for gap in self.goal_gap:
            if gap.id == gap_id:
                return gap
        return None

    @classmethod
    def from_yaml(cls, text: str) -> ProjectAssessment:
        """从 YAML 文本构造。

        认 design.md §5 的原样写法 —— 顶层是 ``project_assessment:`` 包装。
        """
        raw = yaml.safe_load(text) or {}
        if not isinstance(raw, Mapping):
            raise ValueError("项目评估必须是 YAML 映射")
        inner = raw.get("project_assessment", raw)
        if not isinstance(inner, Mapping):
            raise ValueError("project_assessment 段必须是 YAML 映射")
        return cls.model_validate(inner)


def _reject_duplicates(items: Sequence[str], what: str) -> None:
    """同一个标识出现两次 —— 引用会指向哪一个？"""
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        if item in seen:
            duplicates.append(item)
        seen.add(item)
    if duplicates:
        raise ValueError(f"{what} 不能重复：{'、'.join(duplicates)}")


__all__ = [
    "DeferredOpportunity",
    "GapSeverity",
    "GoalGap",
    "Opportunity",
    "OverallStatus",
    "ProjectAssessment",
]
