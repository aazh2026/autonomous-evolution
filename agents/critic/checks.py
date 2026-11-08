"""九项检查 —— Critic 到底问哪九句话。

设计依据：docs/design.md §5「建议增加一个独立的 Critic」（七问）、§3.1（项目
目标六要素里的 Non-goals 与 Stop Conditions）；docs/tasks.md TASK-P1-055
（R-085 / R-086 / R-087 / I-18）。

**九问从哪来。** 设计文档给了七问，本清单补了两问：

- **第八问「是否触碰项目 Non-goals」** —— 六要素里的 Non-goals 说的是"明确
  不做什么"。七问里第 4 问只管"目标与架构约束"，**管不到非目标** —— 而非
  目标恰恰是最容易被演进循环踩的：一条候选完全可以既不违反目标、也有充分
  证据，同时做的正是项目明说不做的事。
- **第九问「是否触发 Stop Conditions」** —— 停止条件是"满足了就该停"。演进
  循环不查它，就会在项目该停下来的时候继续提候选 —— 那正是 §4 循环 B 最需要
  克制的地方。

**哪些检查能否决，写死在 :data:`VETOING_CHECKS` 里。** 不让各检查临场决定，
是因为"能不能决"是**政策**不是判断：同一个检查在这一轮否决、在下一轮不否决，
读的人就说不清这一轮的标准是什么了。

**为什么有四问不否决。** 「问题是否真实存在」「有没有更低成本的方案」「是否
引入不必要的复杂性」「是否已经有其他任务解决」这四问的答案都**没有硬事实**
支撑 —— 仓库里搜不到东西，证明不了问题不存在；"更便宜"是一个要跟别的方案
比出来的判断；"复杂"要看改动的影响面，而影响面在动手前只能估计；任务表能
给出的只是"**标题像是**" —— 像不等于同一件事。所以这四问的产出是**给人看的
线索**，不是拦路虎。真正拦得住的是另外五问：证据不足、违反约束、不值得现在
做、触碰非目标、触发停止条件 —— 它们背后都有**硬事实**。

**第三问为什么也不否决，是评审逼出来的。** TASK-P1-055 的 T3 评审抽了五条
历史候选去审，**五条全部被"任务表里已有相关任务"否决** —— 而其中至少三条
的匹配是误报（一条关于端到端成功率的候选，匹配上了三条讲别的事的已完成
任务）。**相似度当成"已解决"，误报率高到这条规则没法用**：候选会被成批
挡下，而挡下的理由读的人一眼就知道站不住。所以第三问改成**检出并列出任务
id**，把"是不是同一件事"交给人判断 —— 报告里那几条 id 正是给人判断用的材料。
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CriticCheck(StrEnum):
    """九项检查。"""

    #: 一、这个问题是否真实存在？
    PROBLEM_EXISTS = "problem_exists"
    #: 二、是否有充分证据？
    EVIDENCE_SUFFICIENT = "evidence_sufficient"
    #: 三、是否已经有其他任务解决？
    NOT_ALREADY_SOLVED = "not_already_solved"
    #: 四、是否违反项目目标或架构约束？
    RESPECTS_GOALS_AND_CONSTRAINTS = "respects_goals_and_constraints"
    #: 五、有没有更低成本的解决方案？
    HAS_LOWER_COST_ALTERNATIVE = "has_lower_cost_alternative"
    #: 六、是否引入不必要的复杂性？
    NO_UNNECESSARY_COMPLEXITY = "no_unnecessary_complexity"
    #: 七、是否值得现在执行？
    WORTH_DOING_NOW = "worth_doing_now"
    #: 八、是否触碰项目 Non-goals？
    RESPECTS_NON_GOALS = "respects_non_goals"
    #: 九、是否触发 Stop Conditions？
    DOES_NOT_TRIGGER_STOP_CONDITIONS = "does_not_trigger_stop_conditions"


#: 九问的原文 —— 报告里逐条回显，读的人能对上 design.md §5。
CHECK_QUESTIONS: Final[MappingProxyType[CriticCheck, str]] = MappingProxyType(
    {
        CriticCheck.PROBLEM_EXISTS: "这个问题是否真实存在？",
        CriticCheck.EVIDENCE_SUFFICIENT: "是否有充分证据？",
        CriticCheck.NOT_ALREADY_SOLVED: "是否已经有其他任务解决？",
        CriticCheck.RESPECTS_GOALS_AND_CONSTRAINTS: "是否违反项目目标或架构约束？",
        CriticCheck.HAS_LOWER_COST_ALTERNATIVE: "有没有更低成本的解决方案？",
        CriticCheck.NO_UNNECESSARY_COMPLEXITY: "是否引入不必要的复杂性？",
        CriticCheck.WORTH_DOING_NOW: "是否值得现在执行？",
        CriticCheck.RESPECTS_NON_GOALS: "是否触碰项目 Non-goals？",
        CriticCheck.DOES_NOT_TRIGGER_STOP_CONDITIONS: "是否触发 Stop Conditions？",
    }
)

#: 失败时能否决的检查。
#:
#: **刻意只有五项。** 剩下四问（问题是否存在、有没有更便宜的方案、是不是太复杂、
#: 是不是已经有别的任务解决）拿不出硬事实 —— 用它们否决，等于让否决理由经不起
#: 复核。
VETOING_CHECKS: Final[frozenset[CriticCheck]] = frozenset(
    {
        CriticCheck.EVIDENCE_SUFFICIENT,
        CriticCheck.RESPECTS_GOALS_AND_CONSTRAINTS,
        CriticCheck.WORTH_DOING_NOW,
        CriticCheck.RESPECTS_NON_GOALS,
        CriticCheck.DOES_NOT_TRIGGER_STOP_CONDITIONS,
    }
)


class Verdict(StrEnum):
    """一项检查的结论。

    **三态而不是两态。** 只有"通过/不通过"的话，"没查"和"查了没有"会被迫
    挤成同一格 —— 而这两件事必须分得开：前者是**能力缺失**（工具没配），
    后者是**事实结论**（查过了，确实没有）。把"没查"报成"没问题"，Critic 就
    成了一台会点头的机器。
    """

    #: 查过了，结论是这条候选没问题。
    PASSED = "passed"
    #: 查过了，结论是有问题。
    FAILED = "failed"
    #: 没查成 —— 工具不可用或事实不足以判断。
    UNVERIFIED = "unverified"


class EvidenceKind(StrEnum):
    """证据引用的类别 —— 报告里"凭什么这么说"的分类。"""

    #: 仓库里的文件路径。
    REPOSITORY = "repository"
    #: Git 历史里的提交。
    GIT_HISTORY = "git_history"
    #: 测试报告里的用例。
    TEST_REPORT = "test_report"
    #: 项目状态查询的结果。
    PROJECT_STATUS = "project_status"
    #: 任务表里的任务。
    TASK = "task"
    #: 产物登记处里的证据。
    ARTIFACT = "artifact"
    #: 项目 Non-goals 里的一条。
    NON_GOAL = "non_goal"
    #: 项目约束里的一条。
    CONSTRAINT = "constraint"
    #: 停止条件里的一条。
    STOP_CONDITION = "stop_condition"
    #: 关键结果 / 目标里的一条。
    GOAL = "goal"
    #: 候选自己的字段。
    CANDIDATE = "candidate"


class CriticEvidence(BaseModel):
    """一条证据引用。

    ``reference`` 必须是**具体的东西** —— 一个 id、一个路径、一段原文。
    "查了一下"不是证据引用。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvidenceKind
    #: 具体指向 —— id、路径，或**对上的那一段文字**。
    reference: str = Field(min_length=1)
    #: 这条引用说明了什么。
    detail: str = ""

    @model_validator(mode="after")
    def _reference_not_blank(self) -> CriticEvidence:
        if not self.reference.strip():
            raise ValueError("证据引用不能是空白")
        return self


class CheckOutcome(BaseModel):
    """一项检查的结论。

    **否决必须带证据。** 没有证据引用的否决，读的人无从复核，只能选择相信或
    不信 —— 而那正是 Critic 这个角色要消除的东西。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: CriticCheck
    verdict: Verdict
    #: 这一项的结论。一句话说清楚"查了什么、结论是什么"。
    summary: str = Field(min_length=1)
    #: 支撑结论的证据引用。
    evidence: tuple[CriticEvidence, ...] = ()
    #: 这一项是否构成否决。
    vetoes: bool = False

    @model_validator(mode="after")
    def _veto_needs_ground(self) -> CheckOutcome:
        if not self.summary.strip():
            raise ValueError("检查结论不能是空白")
        if self.vetoes:
            if self.verdict is not Verdict.FAILED:
                raise ValueError("只有失败的检查才能否决 —— 未核实不构成否决")
            if self.check not in VETOING_CHECKS:
                raise ValueError(f"检查 {self.check.value} 不是可否决的检查")
            if not self.evidence:
                raise ValueError("否决必须给出至少一条证据引用")
        return self


__all__ = [
    "CHECK_QUESTIONS",
    "VETOING_CHECKS",
    "CheckOutcome",
    "CriticCheck",
    "CriticEvidence",
    "EvidenceKind",
    "Verdict",
]
