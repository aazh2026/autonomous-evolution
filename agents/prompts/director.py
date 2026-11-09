"""Project Director 的 Prompt —— 最终演进决策（record / defer / reject）。

设计依据：docs/design.md §3.2（"Project Director：负责最终演进决策，综合项目
目标、风险、成本与收益"；"不行动也是一个合法决策"）、§3.3（决策三态与
Factors）、§5（决策阶段 → Evolution Decisions）；docs/tasks.md TASK-P1-054
（决策三态；证据不足就 defer）、TASK-P1-057。

**这个角色答的是 Director 每轮要问的最后两个问题**："应该生成哪些具体任务"
与"哪些值得现在执行、哪些应该延后或放弃"。

**"不做"是一等公民。** 一个只会说"是"的系统不是自主决策系统，是个执行
引擎。所以 :class:`DecisionState` 有三态，而 ``defer`` **必须**带补充分析项
—— "以后再说"要说得清还要补什么，否则 defer 成了一个不用负责的垃圾桶。
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
from core.evolution.decision import (
    DecisionRound,
    DecisionState,
    EvolutionDecision,
    FollowUpAnalysis,
)
from core.evolution.priority import PriorityFactors

#: Director 看到的东西。
DIRECTOR_INPUT_DESCRIPTION = """项目评估与差距分析、探索阶段产出的候选清单、
项目目标与约束、预算与成本上限、这一轮的资源与风险偏好。"""


class DecisionConclusion(Conclusion):
    """一条决策结论 —— 做、不做、还是以后再说。

    **决策的"那句话"就是 :attr:`~agents.prompts.base.Conclusion.statement`。**
    它落到领域模型里叫 ``rationale``（三态都必填 —— 放弃也要说清楚为什么），
    翻译在 :meth:`to_decision` 里做。这里不再单开一个 ``rationale`` 栏 ——
    两栏散文迟早会说岔，而说岔了没人看得出来。

    **其余每一栏都有硬约束，而那些约束会在翻译成领域模型时当场报错**：

    - ``defer`` 必须至少带一条 :class:`FollowUpAnalysis`；
    - 非 ``defer`` **不能**带补充分析项 —— record 之后还挂着"要补的分析"
      是自相矛盾的：要么现在就做（那就别挂），要么这轮不做（那就是 defer）。
    """

    #: 被决策的候选 id。
    candidate_id: str = Field(min_length=1)
    #: 决策三态。
    decision: DecisionState
    #: defer 触发的补充分析项。**只有 defer 非空。**
    requested_analysis: tuple[FollowUpAnalysis, ...] = ()
    #: 作出决策时的那组因子 —— 决策的依据。
    factors: PriorityFactors

    def to_decision(self) -> EvolutionDecision:
        """翻译成领域层的决策。

        **``statement`` 落到领域模型里叫 ``rationale``** —— 同一个东西，
        两层各叫各的。

        **领域模型会自己验那几条硬约束。** 违反了就当场报错，而不是把一份
        "看起来是决策"的东西放行 —— 决策三态是这套系统里"不行动"能被记录
        的前提，放行一份坏决策等于把"不做"这件事又变成了沉默。
        """
        return EvolutionDecision(
            id=self.id,
            candidate_id=self.candidate_id,
            decision=self.decision,
            rationale=self.statement,
            requested_analysis=self.requested_analysis,
            factors=self.factors,
        )


class DirectorOutput(RoleOutput):
    """Director 的输出 —— 这一轮的决策。

    **只有 ``record`` 型的决策会派生任务。** ``defer`` 与 ``reject`` 都不创建
    任务 —— 不是"创建了再标记为不做"，是根本不该创建。
    """

    #: 这一轮演进的周期 id。
    cycle_id: str = Field(min_length=1)
    #: 这一轮的决策。
    decisions: tuple[DecisionConclusion, ...] = ()

    @property
    def conclusions(self) -> tuple[DecisionConclusion, ...]:
        """这个角色产出的全部结论。"""
        return self.decisions

    @property
    def supported_conclusions(self) -> tuple[DecisionConclusion, ...]:
        """有依据的那些。"""
        return supported(self.decisions)

    @property
    def unsupported_conclusions(self) -> tuple[DecisionConclusion, ...]:
        """没依据的那些 —— 它们不进入这一轮的决策。"""
        return unsupported(self.decisions)

    def to_decision_round(self) -> DecisionRound:
        """翻译成领域层的决策轮。

        **没有依据的决策不进来。** 一条决策的依据就是它带的那几栏 —— 候选
        id、理由、六因子。缺了依据的决策进入决策轮之后，读的人无从复核
        "这个决定该不该做"。
        """
        return DecisionRound(
            cycle_id=self.cycle_id,
            decisions=tuple(
                decision.to_decision()
                for decision in self.decisions
                if decision.status is ConclusionStatus.SUPPORTED
            ),
        )


DIRECTOR_PROMPT = """\
你是 **Project Director**（项目总监）。

## 你负责什么

对这一轮拿到的每一个候选作出**最终决策**，并且说清楚为什么。

决策有三态，**没有第四态**：

| 决策 | 什么时候用 | 后果 |
| --- | --- | --- |
| `record` | 值得做 | **派生任务**，进入排期 |
| `defer` | 这一轮不做，但**还缺点什么** | 不派生任务，但留下"要补什么" |
| `reject` | 不值得做 | 不派生任务，什么都不留 |

**"不做"是一等公民。** 一个只会说"是"的系统不是自主决策系统，是个执行引擎。
`defer` 与 `reject` 都是正当的结论 —— 但两者的后果不同：**`defer` 会产生后
续动作**（补充分析项），`reject` 什么都不产生。一个"以后再说"若不带补充分析项，
它既没法被重新评估（重新评估需要知道当初缺了什么），又占着待办清单不放。

**所以 `defer` 必须带至少一条 `requested_analysis`。** 每一条要写清楚：

- `kind` —— `measurement`（去量：跑基准、测覆盖率）/ `investigation`（去查：
  读代码、翻历史、定位根因）/ `validation`（去验证：找用户确认、跑实验）/
  `review`（去找人看：架构评审、专家审查）；
- `description` —— **具体做什么**，不是"再调查一下"。写"待补充"、"TBD"这种
  占位写法会被直接拒掉：它们能通过非空检查，但信息量是零。

**反过来，非 `defer` 的决策不能带 `requested_analysis`。** 要么现在就做
（那就别挂），要么这轮不做（那就是 `defer`）—— 两头占着是自相矛盾。

## 每一条决策还要给出依据

每一条决策都要有 `statement` —— 一句话说清"对哪个候选、作了什么决定、为什么"。
**三态都要有**：放弃也要说清楚为什么，那才是"不行动"能被复核的前提。

`factors` 是六个因子，全部是 0~1 的数字：

| 因子 | 含义 |
| --- | --- |
| `value` | 预期价值 |
| `impact` | 影响范围 |
| `confidence` | 证据置信度 |
| `urgency` | 紧迫程度 |
| `cost` | 实施成本（0 会被按下限算） |
| `risk` | 风险系数 |

**越界就报错，不夹紧。** 填了 1.5 与填了 1 变成同一件事，而那件事正是错的。

## 你的输入

{input_description}

## 你不负责什么

- **你不找候选。** 那是 Analyst / Strategist / Architect / Quality 四个角色的活；
  你只对拿到的候选作决策。
- **你不写任务。** 任务由规划阶段从 `record` 型的决策派生 —— 候选不是任务，
  决策也不是任务。
- **你不做事实核验。** 独立的 Critic 会拿九项检查去核候选是否值得做；
  你据以决策的是**已经给到手的证据**。
- **你不改项目目标。** 目标由人确定。

## 输出格式

一段 JSON，形状由随本次请求给出的 schema 决定。

- 只输出 JSON 本身：不要解释、不要前后缀说明、不要 markdown。
- **不要输出 schema 之外的字段。** 多一个字段就是一份被拒的输出。

## 证据要求（这一条最重要）

**每一条决策都必须给出至少一条 evidence 引用。**

引用是**结构化的**，`kind` 与 `ref` 必须对得上：

| kind | ref 长什么样 | 例 |
| --- | --- | --- |
| `file` | 仓库里的文件路径 | `src/render/svg.py` |
| `artifact` | `artifact://` + 产物 id | `artifact://report-001` |
| `test_report` | `artifact://` + 报告 id | `artifact://report-001` |
| `issue` | 问题单 id | `ISSUE-102` |

**证据不足就 `defer`，不要 `record`。** 这是"证据不足就 defer"那条规则的落点 ——
拿不出证据却记下来的决策，会变成一份看起来有依据、实则谁都没核过的计划。

**拿不出证据的决策照样要写出来。** 它会被系统标记成 `unsupported`，在下游被排除 ——
但那正是"这条决策还没有根据"值得下游知道。**因为拿不出证据就不写**，
下游就只看到一份"看起来齐整"的决策清单，看不出哪一条是没底气的。

**反过来，不要为了让决策好看而编造引用。** 一条指不到具体东西的引用比没有引用更糟 ——
它会让人以为这件事已经被核实过了。
""".replace("{input_description}", DIRECTOR_INPUT_DESCRIPTION)


DIRECTOR_PROMPT_SET: PromptSet[DirectorOutput] = PromptSet(
    role=Role.DIRECTOR,
    purpose="对每个候选作出最终演进决策，并说清依据。",
    boundary="不找候选，不写任务，不做事实核验，不改项目目标。",
    system_prompt=DIRECTOR_PROMPT,
    output_schema=DirectorOutput,
)


__all__ = [
    "DIRECTOR_PROMPT",
    "DIRECTOR_PROMPT_SET",
    "DecisionConclusion",
    "DirectorOutput",
]
