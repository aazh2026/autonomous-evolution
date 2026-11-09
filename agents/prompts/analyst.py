"""Project Analyst 的 Prompt —— 认知阶段：项目现状与差距。

设计依据：docs/design.md §3.2（"Project Analyst：理解项目现状，识别实现
差距、缺陷和机会"）、§5（认知阶段 → Project Understanding；``project_assessment``
的原样结构）；docs/tasks.md TASK-P1-051（认知阶段的结构化输出）、TASK-P1-057。

**分析师答的是第一个问题："这个项目现在真实的状态是什么？"**
它的产出是整条决策链的**唯一输入口径** —— 下游四阶段不再去看原始数据，只看
这份结构化结果。所以它既不能漏（漏掉的差距下游不会知道），也不能掺
（掺进来的"差距"下游会当真）。
"""

from __future__ import annotations

from pydantic import Field

from agents.prompts.base import (
    Conclusion,
    ConclusionStatus,
    PromptSet,
    Role,
    RoleOutput,
    supported,
    unsupported,
)
from core.evolution.assessment import (
    GapSeverity,
    GoalGap,
    Opportunity,
    OverallStatus,
    ProjectAssessment,
)
from core.evolution.taxonomy import OpportunityCategory, PriorityLevel

#: 分析师看到的东西 —— 一段给模型看的材料长什么样。
ANALYST_INPUT_DESCRIPTION = """项目 id 与快照、项目目标（目标、关键结果、约束、
非目标、停止条件、验收标准）、项目状态与历史。"""


class GapConclusion(Conclusion):
    """一条"项目离目标还差什么"的结论。"""

    #: 这条差距有多严重。
    severity: GapSeverity

    def to_gap(self) -> GoalGap:
        """翻译成领域层的目标差距。

        **只有有依据的结论才走到这里。** 领域模型要求差距**至少一条证据**
        —— 没有证据的差距压根不该被构造出来。
        """
        return GoalGap(
            id=self.id,
            description=self.statement,
            evidence=self.evidence,
            severity=self.severity,
        )


class OpportunityConclusion(Conclusion):
    """一条"可以做的事"的结论。"""

    #: 属于哪一类演进机会（产品 / 架构 / 工程 / 质量 / 技术）。
    category: OpportunityCategory
    #: 粗档优先级。
    priority: PriorityLevel
    #: 这条机会回应的是哪些差距（差距 id）。
    addresses: tuple[str, ...] = ()

    def to_opportunity(self, addresses: tuple[str, ...] | None = None) -> Opportunity:
        """翻译成领域层的演进机会。

        ``addresses`` 给的是**要保留的**差距 id —— 调用方负责把已经掉队的
        差距从这里剔掉（差距不在评估里时，指着它的那条引用就是断的，而领域
        模型会当场报错）。
        """
        return Opportunity(
            id=self.id,
            category=self.category,
            action=self.statement,
            priority=self.priority,
            addresses=self.addresses if addresses is None else addresses,
        )


class AnalystOutput(RoleOutput):
    """分析师的输出 —— 一份项目评估。

    **它是认知阶段产物的形状，不是领域模型本身。** 差别在于：这里可以装着
    **没有依据的结论**（模型确实有可能写出来），而领域模型
    :class:`~core.evolution.assessment.ProjectAssessment` 装不下那种东西 ——
    差距必须带证据。

    所以两者之间隔着一个 :meth:`to_assessment`：那是"有没有依据"这条契约
    真正落地的地方 —— **没有依据的差距与机会在这里被丢掉**，指向它们的推荐
    也跟着被丢掉。
    """

    #: 项目 id。
    project_id: str = Field(min_length=1)
    #: 评估所依据的项目快照。它是这份评估的**时点**。
    snapshot: str = Field(min_length=1)
    #: 项目当前的整体状态。
    overall_status: OverallStatus
    #: 目标差距。
    gaps: tuple[GapConclusion, ...] = ()
    #: 演进机会。
    opportunities: tuple[OpportunityConclusion, ...] = ()
    #: 推荐次序 —— 机会 id，**按推荐顺序排**。
    recommendations: tuple[str, ...] = ()

    @property
    def conclusions(self) -> tuple[Conclusion, ...]:
        """这个角色产出的全部结论。"""
        return (*self.gaps, *self.opportunities)

    @property
    def supported_conclusions(self) -> tuple[Conclusion, ...]:
        """有依据的那些。"""
        return supported(self.conclusions)

    @property
    def unsupported_conclusions(self) -> tuple[Conclusion, ...]:
        """没依据的那些 —— 它们不会进入下游的评估。"""
        return unsupported(self.conclusions)

    def to_assessment(self) -> ProjectAssessment:
        """翻译成领域层的项目评估。

        **丢掉没依据的结论，并收拾掉指向它们的引用。** 掉队的差距不该再被
        机会指着（那是一条断掉的引用），掉队的机会不该再被推荐（那是一条指向
        不存在的东西的推荐）。

        剩下的东西交给领域模型自己验：``at_target`` 却还有差距、推荐了不
        存在的机会 —— 那些是**这份评估自己**的毛病，领域模型会当场报错。
        """
        gaps = tuple(gap.to_gap() for gap in self.gaps if gap.status is ConclusionStatus.SUPPORTED)
        known_gaps = {gap.id for gap in gaps}

        opportunities = tuple(
            opportunity.to_opportunity(
                addresses=tuple(gap_id for gap_id in opportunity.addresses if gap_id in known_gaps)
            )
            for opportunity in self.opportunities
            if opportunity.status is ConclusionStatus.SUPPORTED
        )
        known_opportunities = {item.id for item in opportunities}

        return ProjectAssessment(
            project_id=self.project_id,
            snapshot=self.snapshot,
            overall_status=self.overall_status,
            goal_gap=gaps,
            opportunities=opportunities,
            recommendations=tuple(
                item for item in self.recommendations if item in known_opportunities
            ),
        )


ANALYST_PROMPT = """\
你是 **Project Analyst**（项目分析师）。

## 你负责什么

回答 Director 每轮要问的第一个问题：**这个项目现在真实的状态是什么？离最终目标还差什么？**

你给出三样东西：

- **差距**（gap）：项目离目标还差什么，每条有多严重（low / medium / high / critical）；
- **机会**（opportunity）：可以做的事，每条属于哪一类（product / architecture /
  engineering / quality / technology）、多优先（high / medium / low）、回应哪些差距；
- **整体状态**：`evolving`（正在演进）/ `at_target`（已达目标）/ `blocked`
  （卡住：推进不了）/ `stalled`（停滞：能推进但连续几轮没有可验证的改善）。

## 你的输入

{input_description}

## 你不负责什么

- **你不决定做不做。** 那是 Director 的活（record / defer / reject 三态）。你只负责"看见什么"。
- **你不产出解法。** 方案是 Strategist / Architect / Quality 三个角色的活 ——
  你给的是差距与机会，不是怎么做。
- **你不做质量评审。** 缺陷率、测试覆盖率、可观测性由 Quality Strategist 判断。
- **你不列任务清单。** TODO 不是差距 —— 差距是"离目标还差什么"，不是"还没做完什么"。

## 输出格式

一段 JSON，形状由随本次请求给出的 schema 决定。

- 只输出 JSON 本身：不要解释、不要前后缀说明、不要 markdown。
- **不要输出 schema 之外的字段。** 多一个字段就是一份被拒的输出。
- 字段名用英文，取值用 schema 里给的那些 —— 不要自造同义词（"medium" 不是 "mid"）。

## 证据要求（这一条最重要）

**每一条结论（差距与机会）都必须给出至少一条 evidence 引用。**

引用是**结构化的**，`kind` 与 `ref` 必须对得上：

| kind | ref 长什么样 | 例 |
| --- | --- | --- |
| `file` | 仓库里的文件路径 | `src/render/svg.py` |
| `artifact` | `artifact://` + 产物 id | `artifact://report-001` |
| `test_report` | `artifact://` + 报告 id | `artifact://report-001` |
| `issue` | 问题单 id | `ISSUE-102` |

**拿不出证据的结论照样要写出来。** 它会被系统标记成 `unsupported`，在下游被排除 ——
但那正是"这里缺证据"这件事本身值得下游知道。**因为拿不出证据就不写**，
下游就只看到一份"看起来齐整"的评估，看不出哪一处是没底气的。

**反过来，不要为了让结论好看而编造引用。** 一条指不到具体东西的引用比没有引用更糟 ——
它会让人以为这件事已经被核实过了。
""".replace("{input_description}", ANALYST_INPUT_DESCRIPTION)


ANALYST_PROMPT_SET: PromptSet[AnalystOutput] = PromptSet(
    role=Role.ANALYST,
    purpose="理解项目现状，识别实现差距、缺陷与机会。",
    boundary="不决定做不做，不产出解法，不做质量评审，不列任务清单。",
    system_prompt=ANALYST_PROMPT,
    output_schema=AnalystOutput,
)


__all__ = [
    "ANALYST_PROMPT",
    "ANALYST_PROMPT_SET",
    "AnalystOutput",
    "GapConclusion",
    "OpportunityConclusion",
]
