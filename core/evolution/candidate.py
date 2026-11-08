"""演进机会 —— 统一结构、五类分类、以及"技术趋势必须先证明有收益"。

设计依据：docs/design.md §3.2（五类演进机会；"技术趋势不应直接转化为升级
任务，必须先证明它对当前项目有实际收益"）、§3.3（Candidate 结构）；
docs/tasks.md TASK-P1-052（R-024 五类演进机会分类法；R-029 Candidate 统一
结构；R-030 Candidate ≠ Task；R-025 技术趋势须先证明对本项目有收益）。

**候选不是任务**（R-030）。它只是"一个待评估的改进机会" —— 没有被批准，
没有成本约束，没有验收标准。把候选直接变成任务，等于把"发现"和"决定"
压缩成一步，而那正是 §3.6 要防的事。所以候选里**没有**任务才有的那些
字段：没有状态机、没有验收契约、没有分支、没有执行器。

**``risk`` 是档，DB 里那一列是系数 —— 两件事，别混。** 本任务的规格写的是
``risk ∈ {low, medium, high}``，而 :class:`~core.entities.evolution.EvolutionCandidate`
的 ``risk`` 列是 ``Numeric(12, 4)``、带 ``0 ≤ risk ≤ 1`` 的检查约束，存的是
六因子公式里的**风险系数 R**。一个是给人看的定性判断，一个是喂给公式的
0~1 的数，档到系数的换算在 :mod:`core.evolution.priority` 里做（那里才有
"低档对应多少系数"的依据）。**落库时不能把 ``RiskLevel`` 直接塞进那一列** ——
`low` 不是 0.3。词表与换算分开，是为了让"这个候选风险有多高"和"这个候选
在公式里排第几"各自能被独立核对。

**证据必须是引用，不是描述**（R-029）。design.md §3.3 的示例里写的证据是
"文件耦合度过高"这样一句话 —— 那份示例是**说明性**的，落地时不能照抄：
一句话没法被独立核验，说"有证据"就成了一句没有内容的话。所以这里的
``evidence`` 用的是 :mod:`core.evolution.evidence` 里的那套结构化引用
（``artifact://`` / ``issue:`` / 文件路径），与
:class:`~core.entities.evolution.EvolutionCandidate` 那一列**存的东西
一样**（那一列的注释已经明写"存结构化的证据引用，而不是一段自然语言
描述"），也与 TASK-P1-051 的 ``goal_gap[].evidence`` 同一套词表 —— 三处
说的是同一件事，抄本迟早对不上。

**技术趋势是最容易变成"无意义的升级"的一类**（R-025）。一条技术趋势
（"Rust 又快了""又出了个新框架"）跟本项目有没有关系，是一件**可以核对**
的事 —— 它必须落在本项目的某个具体东西上（技术栈里的一个条目、某个目标
差距）。所以收益论证不是一句"这个框架很好"，而是**说得出落点**：

    benefits: 对本项目的实际收益论证（summary + applies_to）

``applies_to`` 里的每一项都要能在**这个项目**里找到对应的东西（技术栈
条目或目标差距 id）；找不到，这条趋势就被**丢弃**并留下理由。

**为什么"丢弃"要留痕。** 一条被丢掉的趋势如果什么都不说，读的人分不清
"它被判断为不适用"和"它压根没被看见"—— 而这两种情况的补救动作完全相反
（前者是记录一个判断，后者是补一次扫描）。丢弃和暂缓一样：**要说得清
为什么**。
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.evolution.assessment import ProjectAssessment
from core.evolution.evidence import Evidence
from core.evolution.taxonomy import OpportunityCategory, RiskLevel

#: 落点匹配的**最短长度**。两个词都要够长才允许做包含匹配 ——
#: 否则 ``"py"`` 能装进任何长词里，任何落点都会"命中"。
_MINIMUM_MATCH_LENGTH: Final[int] = 3


class CandidateStatus(StrEnum):
    """候选的处理状态。

    **"候选"本身没有状态机** —— 状态是决策层的事（record / defer /
    reject 见 TASK-P1-054）。这里留一个取值域，是为了与
    :class:`~core.entities.evolution.EvolutionCandidate` 的 ``status`` 列
    对得上：那个列的取值就是这四个。词表只有一份，两边才不会各写各的。
    """

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class ProjectBenefit(BaseModel):
    """一条技术趋势对本项目的**实际收益**论证。

    **两件事都要说清**：收益是什么（``summary``），以及它落在本项目的
    哪一处（``applies_to``）。少一件都算不上论证 —— 只有 ``summary`` 的
    那种（"这个框架性能更好"）对**任何**项目都成立，而对任何项目都成立
    的说法，恰恰说明它没有针对这个项目论证过。

    ``applies_to`` 里的每一项都要能在**这个项目**里找到对应的东西
    （技术栈里的一个条目，或某个目标差距的 id）。找不到就说明这条趋势
    与本项目无关 —— 那它就不该成为候选。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1)
    #: 本项目的具体落点：技术栈条目或目标差距 id。至少一条。
    applies_to: tuple[str, ...] = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def _summary_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("收益论证要说清收益是什么")
        return value.strip()

    @field_validator("applies_to")
    @classmethod
    def _applies_to_not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not any(item.strip() for item in value):
            raise ValueError("收益论证要指得出它落在本项目的哪一处")
        return value


class EvolutionCandidate(BaseModel):
    """一个演进机会。

    **候选 ≠ 任务**（R-030）：这里没有状态机、没有验收契约、没有分支、
    没有执行器 —— 那些是任务才有的东西，候选一件都不带。

    ``confidence`` 是**证据的置信度**，不是"我觉得这事有把握"。它要能被
    独立核验，所以落在 0~1 之间并由证据支撑；P1-054 的"证据不足就 defer"
    就是拿它当判据的。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    category: OpportunityCategory
    title: str = Field(min_length=1)
    #: 支撑这个候选的证据。**至少一条**，且每条都指向可解析的引用。
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    #: 假设：做了这件事会怎样。
    hypothesis: str = Field(min_length=1)
    #: 预期结果。**至少一条** —— 事后拿它跟"实际收益"比，才知道这个
    #: 候选当初的判断准不准（R-033：价值模型要用实际收益校准）。
    expected_outcome: tuple[str, ...] = Field(min_length=1)
    #: 风险档。
    risk: RiskLevel
    #: 证据置信度，0~1。
    confidence: Decimal = Field(ge=0, le=1)
    #: 预估成本，如 ``"2d"``。自由字符串 —— 成本单位不统一是常态
    #: （人天 / token / 机器时）。
    estimated_effort: str = ""
    #: 对本项目的实际收益论证。**技术类候选必填**（R-025）。
    benefit: ProjectBenefit | None = None

    @field_validator("id", "title", "hypothesis")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("候选的 id、标题与假设都不能是空白")
        return value.strip()

    @field_validator("expected_outcome")
    @classmethod
    def _expected_outcome_not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not any(item.strip() for item in value):
            raise ValueError("候选必须说得出预期结果 —— 没有预期结果就无法事后核验")
        return value

    @model_validator(mode="after")
    def _technology_needs_a_proven_benefit(self) -> Self:
        """技术类候选必须先证明对本项目有实际收益（R-025）。

        **"这个技术更新更好"不是理由。** 理由是"它在本项目的这一处解决了
        这个问题" —— 没有落点的技术候选，就是一次没有理由的升级。
        """
        if self.category is OpportunityCategory.TECHNOLOGY and self.benefit is None:
            raise ValueError(
                "技术类候选必须先证明对本项目有实际收益 —— "
                "换技术不是理由，落在本项目哪一处、解决什么问题才是"
            )
        return self


class TechnologyTrend(BaseModel):
    """一条技术趋势。

    **趋势本身还不是候选。** 它是"外面出现了这么个东西"，而候选是"这
    个东西对本项目值得做"。中间隔着收益论证 —— 所以趋势可以没有收益论证
    （它只是被观察到了），而技术类**候选**不能。

    **发现器不替趋势编数字。** 风险与置信度都在趋势上写着：趋势的来源
    知道它有多可信，而"发现器随手给个 0.5"会让那对数字看起来像是有依据
    的判断 —— 其实是编的。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: 趋势本身的证据 —— "外面确实有这回事"的依据。
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    #: 如果引入它，本项目会怎样。
    hypothesis: str = Field(min_length=1)
    risk: RiskLevel
    confidence: Decimal = Field(ge=0, le=1)
    #: 对本项目的实际收益论证。**可以没有** —— 那时这条趋势会被丢弃。
    benefit: ProjectBenefit | None = None
    estimated_effort: str = ""

    @field_validator("id", "name", "description", "hypothesis")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("趋势的 id、名称、描述与假设都不能是空白")
        return value.strip()


class DiscoveryContext(BaseModel):
    """发现候选时的上下文 —— 认知与诊断阶段的产物。

    **发现器不重新看世界。** 它看的是这份上下文：项目评估（差距在哪）
    与技术栈（这个项目是什么做的）。让发现器自己去扫仓库、翻 issue，
    是 P3-013 的事（演进机会发现流水线）；阶段的边界在这里划清楚。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    #: 认知与诊断阶段的产物。候选要回应的是这里的差距。
    assessment: ProjectAssessment | None = None
    #: 项目的技术栈。技术趋势的收益论证必须落在这些东西上。
    tech_stack: tuple[str, ...] = ()

    @field_validator("project_id", "snapshot")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("项目 id 与快照不能是空白")
        return value.strip()

    def known_targets(self) -> frozenset[str]:
        """这个项目里"可以指着说"的东西 —— 技术栈条目与目标差距 id。

        全部转成小写去空白 —— 比对时不该被大小写与空格绊住。
        """
        targets = {item.strip().lower() for item in self.tech_stack if item.strip()}
        if self.assessment is not None:
            targets.update(gap.id.strip().lower() for gap in self.assessment.goal_gap)
        return frozenset(targets)


class DroppedTrend(BaseModel):
    """一条被丢弃的趋势 —— 连同丢弃的理由。

    **丢弃也要留痕。** 什么都不留的话，读的人分不清"判断为不适用"与
    "压根没看见"，而这两种情况的补救动作完全相反。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trend: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("trend", "reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("被丢弃的趋势与理由都不能是空白")
        return value.strip()


class DiscoveryResult(BaseModel):
    """一次发现的结果。

    **产出与丢弃都在结果里。** 只返回产出会让"这轮什么都没发现"与"这轮
    发现的东西都被否了"变成同一个结果 —— 而后者是需要有人看的。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[EvolutionCandidate, ...] = ()
    dropped: tuple[DroppedTrend, ...] = ()


@runtime_checkable
class CandidateDiscoverer(Protocol):
    """演进机会发现器。

    **发现器只产出候选，不做决策。** 它没有权力决定"这件事要不要做" ——
    那是决策层的事（R-030：Candidate ≠ Task）。发现器唯一有权力拒绝的
    东西是**不成立**的候选 —— 比如一条没有收益论证的技术趋势。

    发现器**不抛异常**给"没发现东西"这件事用：空结果是
    ``DiscoveryResult(candidates=())``。异常留给真正出了岔子的情况。
    """

    def discover(self, context: DiscoveryContext) -> DiscoveryResult:
        """发现这个项目的演进机会。"""
        ...


class TechnologyTrendDiscoverer:
    """把技术趋势变成候选 —— **但必须先证明它对本项目有收益**。

    **这是 R-025 的落点。** design.md §3.2 明写"技术趋势不应直接转化为
    升级任务，必须先证明它对当前项目有实际收益"。所以这个发现器做两件事：

    1. **没有收益论证的趋势直接丢弃** —— 不是降级、不是"先记下来以后再说"
       （那等于把升级任务记在了待办里）；
    2. **收益论证的落点必须在本项目里** —— ``applies_to`` 里的每一项都要
       在这个项目的技术栈或目标差距里找得到。找不到，说明这条趋势与本项目
       无关。

    **丢弃的顺序是有讲究的**：先看有没有论证，再看落点 —— 没论证是"连
    想法都没对项目"，有论证但落点对不上是"想法有但用不上"。两条理由
    写进 :class:`DroppedTrend`，读的人知道下一步该做什么。
    """

    def __init__(self, trends: Iterable[TechnologyTrend]) -> None:
        self._trends = tuple(trends)

    def discover(self, context: DiscoveryContext) -> DiscoveryResult:
        candidates: list[EvolutionCandidate] = []
        dropped: list[DroppedTrend] = []
        targets = context.known_targets()

        for trend in self._trends:
            benefit = trend.benefit
            if benefit is None:
                dropped.append(
                    DroppedTrend(
                        trend=trend.name,
                        reason="技术趋势没有针对本项目的收益论证 —— 不能直接变成升级任务",
                    )
                )
                continue
            unmatched = [item for item in benefit.applies_to if not _matches(item, targets)]
            if unmatched:
                reason = f"收益论证的落点不在本项目里：{'、'.join(unmatched)}"
                dropped.append(DroppedTrend(trend=trend.name, reason=reason))
                continue
            candidates.append(
                EvolutionCandidate(
                    id=trend.id,
                    category=OpportunityCategory.TECHNOLOGY,
                    title=trend.name,
                    evidence=trend.evidence,
                    hypothesis=trend.hypothesis,
                    expected_outcome=(benefit.summary,),
                    risk=trend.risk,
                    confidence=trend.confidence,
                    estimated_effort=trend.estimated_effort,
                    benefit=benefit,
                )
            )

        return DiscoveryResult(candidates=tuple(candidates), dropped=tuple(dropped))


def _matches(item: str, targets: frozenset[str]) -> bool:
    """一个落点是不是这个项目里的东西。

    **双向包含**而不是精确相等 —— 技术栈里写的是 ``"python 3.12"`` 而收益
    论证写的是 ``"python"`` 时，那显然说的是同一件事。但包含关系也不能
    太松：一个足够短的词能装进任何长词里，所以两边都得够长
    （至少三个字符），免得 ``"py"`` 命中一切。
    """
    needle = item.strip().lower()
    if not needle:
        return False
    for target in targets:
        if needle == target:
            return True
        if (
            len(needle) >= _MINIMUM_MATCH_LENGTH
            and len(target) >= _MINIMUM_MATCH_LENGTH
            and (needle in target or target in needle)
        ):
            return True
    return False


__all__ = [
    "CandidateDiscoverer",
    "CandidateStatus",
    "DiscoveryContext",
    "DiscoveryResult",
    "DroppedTrend",
    "EvolutionCandidate",
    "ProjectBenefit",
    "TechnologyTrend",
    "TechnologyTrendDiscoverer",
]
