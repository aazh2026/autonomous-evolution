"""演进机会 —— 从观察到候选的那本账。

设计依据：docs/design.md §3 第二层（演进机会发现 → 统一 Evolution Candidate；
"Candidate 不等于任务"）、§3.2（五类演进机会）、§3.3（Candidate 结构与证据）；
docs/tasks.md TASK-P3-013（Observe 层六类数据源接入 + Discover 层；验收条件
"六类数据源各接入 ≥1 个真实来源；候选的 evidence 指向真实可解析的证据"；
R-024 五类演进机会分类法；R-028 候选要有真实证据；R-029 证据必须可独立核验；
R-030 Candidate ≠ Task）。

**这一层是"观察"与"候选"之间唯一的翻译表。** 观察说的是事实（"这个文件
被改了 5 次"），候选说的是机会（"这块该理一理"）。中间这一步不能藏在某个
函数里 —— 一旦藏起来，"哪种观察会变成哪类候选"就没人说得清了，而那正是
验收条件要问的第一个问题。

**规则表有六行，五行有规则。** 文档那一类（:attr:`ObservationKind.DOCS`）
**故意不产出候选** —— 一篇架构决策记录写的是"当初决定怎么做"，那是已经做
过的决定，不是待办。把它变成候选，等于让系统去"实现"一个早就实现了的东
西。**六类观察都接上了，但只有五类会变成候选** —— 这两件事不一样。

**风险与置信度是仓库默认值，不是实测值。** 它们回答的是"这一类观察通常有
多可信"，不是"这一件事有多确定"。真正的校准要等候选跑过一轮、拿到实际
收益之后回头做（R-033）—— 那时候这两个数才有依据。**在此之前它们只是默
认值，所以这里写死它们，不让它们看起来像测量结果。**

**丢弃也要留痕。** 一条被丢掉的候选如果什么都不说，读的人分不清"它被判断
为不成立"与"它压根没被看见"—— 而这两种情况的补救动作完全相反。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from core.evolution.candidate import EvolutionCandidate
from core.evolution.evidence import Evidence
from core.evolution.observe import ObservationKind
from core.evolution.taxonomy import OpportunityCategory, RiskLevel


class ResolutionStatus(StrEnum):
    """一条证据引用的核验结果 —— **三态**。

    **"没找到"与"查不了"必须分开。** 一个指向仓库里某个文件的引用找不到，
    是**证据指错了**；一个指向对象存储里产物的引用查不了，是**本机没有产
    物存储**。前者要丢掉候选（证据是假的），后者不能丢（证据可能是真的，
    只是这儿看不到）。
    """

    #: 找到了 —— 引用指向的东西真的在这儿。
    RESOLVED = "resolved"
    #: 找过了，不在 —— 引用指错了地方。
    MISSING = "missing"
    #: 查不了 —— 本机没有核验它的办法。
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Resolution:
    """一次核验的结果。

    :param status: 三态之一。
    :param detail: 说清为什么。**"没找到"与"查不了"必须能一眼分开。**
    """

    status: ResolutionStatus
    detail: str = ""


@runtime_checkable
class EvidenceResolver(Protocol):
    """证据核验器。

    **它只回答"这个引用指的东西在这儿吗"。** 不回答"这件事重不重要" ——
    那是发现层的事。
    """

    def resolve(self, evidence: Evidence) -> Resolution:
        """核验一条证据引用。"""
        ...


@dataclass(frozen=True, slots=True)
class OpportunityRule:
    """一条规则 —— **一类观察变成一类候选**。

    :param kind: 哪一类观察。
    :param category: 变成哪一类候选（五类之一）。
    :param title_prefix: 候选标题的前缀。**标题 = 前缀 + 主体** —— 主体是
        具体的东西，前缀说清它是什么性质的问题。
    :param hypothesis_template: 假设的模板。**里面可以写 ``{subject}``** ——
        假设必须说得清是"这一件"还是"这一类"。
    :param expected_outcome: 预期结果。**事后拿它跟实际收益比**，才知道这
        个候选当初的判断准不准（R-033）。
    :param risk: 风险档。
    :param confidence: 证据置信度，0~1。**仓库默认值，不是实测值。**
    :param estimated_effort: 预估成本。
    """

    kind: ObservationKind
    category: OpportunityCategory
    title_prefix: str
    hypothesis_template: str
    expected_outcome: tuple[str, ...]
    risk: RiskLevel
    confidence: Decimal
    estimated_effort: str


#: 观察 → 候选的规则表。
#:
#: **六类观察，五条规则。** 文档那一类故意不在表里 —— 决策记录写的是已经
#: 做过的决定，不是待办。**缺一行和少一类是两回事**：这里是**有意**缺一行。
#:
#: **分类的依据是"这个候选要去做什么"**：
#:
#: - 改动频繁 → **工程实现**（可维护性：这块反复改，说明它难以稳定）；
#: - 用例没过 → **质量**（缺陷）；
#: - 依赖方向不对 → **架构**（怎么搭）；
#: - 跑起来出事了 → **工程实现**（运行时表现）；
#: - 人报上来了 → **产品**（往哪儿走）。
#:
#: **技术类不在这张表里。** 技术候选要的是"这个趋势对本项目有收益"的论证
#: （R-025），那是 :class:`~core.evolution.candidate.TechnologyTrendDiscoverer`
#: 的活 —— 它需要一条**趋势**，观察给不出趋势。
#:
#: **置信度是仓库默认值，不是实测值。** 它们回答的是"这一类观察通常有多可
#: 信"；真正的校准要等候选跑过一轮、拿到实际收益之后回头做。
OPPORTUNITY_RULES: Final[dict[ObservationKind, OpportunityRule]] = {
    ObservationKind.CHANGES: OpportunityRule(
        kind=ObservationKind.CHANGES,
        category=OpportunityCategory.ENGINEERING,
        title_prefix="反复改动的文件",
        hypothesis_template="把 {subject} 的职责理顺，能降低反复改动带来的理解成本",
        expected_outcome=("改动集中在职责明确的模块上",),
        risk=RiskLevel.LOW,
        confidence=Decimal("0.60"),
        estimated_effort="1d",
    ),
    ObservationKind.TESTS: OpportunityRule(
        kind=ObservationKind.TESTS,
        category=OpportunityCategory.QUALITY,
        title_prefix="没通过的用例",
        hypothesis_template="把 {subject} 修过，缺陷就不再复现",
        expected_outcome=("这条用例重新通过", "同类缺陷不再复现"),
        risk=RiskLevel.LOW,
        confidence=Decimal("0.90"),
        estimated_effort="1d",
    ),
    ObservationKind.STATIC: OpportunityRule(
        kind=ObservationKind.STATIC,
        category=OpportunityCategory.ARCHITECTURE,
        title_prefix="依赖方向不对",
        hypothesis_template="把 {subject} 里那条依赖掰直，分层就守住了",
        expected_outcome=("产品代码不再依赖测试或脚本",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.80"),
        estimated_effort="2d",
    ),
    ObservationKind.RUNTIME: OpportunityRule(
        kind=ObservationKind.RUNTIME,
        category=OpportunityCategory.ENGINEERING,
        title_prefix="跑起来出事了",
        hypothesis_template="把 {subject} 跑通，运行这条路就通了",
        expected_outcome=("这条命令退出码为 0",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.90"),
        estimated_effort="1d",
    ),
    ObservationKind.FEEDBACK: OpportunityRule(
        kind=ObservationKind.FEEDBACK,
        category=OpportunityCategory.PRODUCT,
        title_prefix="人报上来的问题",
        hypothesis_template="{subject} 说的是真问题，补上就能少一次投诉",
        expected_outcome=("这条反馈有对应的处理",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.70"),
        estimated_effort="2d",
    ),
}


@dataclass(frozen=True, slots=True)
class DroppedOpportunity:
    """一条被丢弃的机会 —— 连同丢弃的理由。

    :param subject: 丢弃的是哪一条观察的主体。
    :param reason: 为什么丢。**理由必须能区分"证据指错了"与"这一类本来就不
        产出候选"** —— 前者要改证据，后者什么都不用改。
    """

    subject: str
    reason: str


@dataclass(frozen=True, slots=True)
class OpportunityDiscovery:
    """一次机会发现的结果。

    :param candidates: 产出的候选。
    :param dropped: 被丢弃的，连同理由。

    **产出与丢弃都在结果里。** 只返回产出会让"这轮什么都没发现"与"这轮发现
    的东西都被否了"变成同一个结果 —— 而后者是需要有人看的。
    """

    candidates: tuple[EvolutionCandidate, ...] = ()
    dropped: tuple[DroppedOpportunity, ...] = ()


__all__ = [
    "OPPORTUNITY_RULES",
    "DroppedOpportunity",
    "EvidenceResolver",
    "OpportunityDiscovery",
    "OpportunityRule",
    "Resolution",
    "ResolutionStatus",
]
