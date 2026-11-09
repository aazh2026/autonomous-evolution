"""独立 Critic 的 Prompt —— 九项检查，一份可复核的报告。

设计依据：docs/design.md §5（"建议增加一个独立的 Critic"，七问；"Critic 不能只
给出一个 LLM 评分，还应该调用代码仓库、Git 历史、测试报告和项目状态进行
事实核验"）；docs/tasks.md TASK-P1-055（九项检查；否决必须带证据）、TASK-P1-057。

**Critic 不给分数，给一份可复核的报告。** 分数会把"查过六处"压成一个数，
读的人没法复核那个数背后查了什么。所以它的输出是**九项检查各一条结论**，
每条结论都带着它查到的事实。

**它的证据词表与别的角色不同。** Critic 要引用**非目标、约束、停止条件、
关键结果** —— 那些东西在 :mod:`core.evolution.evidence` 的词表里压根没有
（那儿只有产物、测试报告、问题单、文件路径四种）。所以这里用的是
:class:`~agents.critic.checks.CriticEvidence` 那一套，字段是 ``kind`` /
``reference`` / ``detail``。

**"没查成"与"查了没问题"必须分开。** 所以结论有**三态**：``passed``（查过了，
没问题）/ ``failed``（查过了，有问题）/ ``unverified``（没查成 —— 工具不可用
或事实不足以判断）。把"没查"报成"没问题"，Critic 就成了一台会点头的机器。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.critic.checks import (
    CHECK_QUESTIONS,
    VETOING_CHECKS,
    CheckOutcome,
    CriticCheck,
    CriticEvidence,
    Verdict,
)
from agents.critic.report import CriticReport
from agents.prompts.base import (
    ConclusionStatus,
    PromptSet,
    Role,
    RoleOutput,
    conclusion_status,
    supported,
    unsupported,
)

#: Critic 看到的东西 —— 工具查回来的事实。
CRITIC_INPUT_DESCRIPTION = """被审的候选（标题、假设、预期结果、证据）、
仓库与 Git 历史、测试报告与失败用例、任务表里已有的任务、产物登记处里已登记
的证据、项目目标与关键结果、约束、Non-goals、停止条件。"""


class CriticConclusion(BaseModel):
    """一项检查的结论。

    **它不继承 :class:`~agents.prompts.base.Conclusion`** —— 证据词表不一样
    （见本模块的说明）。但"有没有依据"这件事是一样的，所以
    :attr:`status` 照样是**算出来的**：模型填不了，也填不掉。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 这一项检查是哪一问。
    check: CriticCheck
    #: 结论三态：通过 / 不通过 / 未核实。
    verdict: Verdict
    #: 这一项的结论。一句话说清"查了什么、结论是什么"。
    summary: str = Field(min_length=1)
    #: 支撑结论的证据引用。**空了就意味着"没查到东西"。**
    evidence: tuple[CriticEvidence, ...] = ()
    #: 这一项是否构成否决。
    vetoes: bool = False

    @field_validator("summary")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("检查结论不能是空白")
        return value.strip()

    @property
    def status(self) -> ConclusionStatus:
        """这一项有没有依据。**算出来的。**"""
        return conclusion_status(self.evidence)

    def to_outcome(self) -> CheckOutcome:
        """翻译成领域层的检查结论。

        **没有证据的结论不否决。** 它被记成 ``unverified``（"未核实"）——
        没查成**不等于**没问题，也**不等于**有问题。这正是三态存在的理由。

        **"排除"排除的是它的作用，不是它的存在。** 这一项仍然出现在报告里
        （读者要看得见"这里没查成"），但它不否决任何东西。
        """
        if self.status is ConclusionStatus.UNSUPPORTED:
            return CheckOutcome(
                check=self.check,
                verdict=Verdict.UNVERIFIED,
                summary=self.summary,
                evidence=(),
                vetoes=False,
            )
        return CheckOutcome(
            check=self.check,
            verdict=self.verdict,
            summary=self.summary,
            evidence=self.evidence,
            vetoes=self.vetoes,
        )


class CriticOutput(RoleOutput):
    """Critic 的输出 —— 九项检查各一条结论。"""

    #: 被审的候选 id。
    candidate_id: str = Field(min_length=1)
    #: 九项检查的结论。
    outcomes: tuple[CriticConclusion, ...] = ()

    @property
    def conclusions(self) -> tuple[CriticConclusion, ...]:
        """这个角色产出的全部结论。"""
        return self.outcomes

    @property
    def supported_conclusions(self) -> tuple[CriticConclusion, ...]:
        """查到了东西的那些。"""
        return supported(self.outcomes)

    @property
    def unsupported_conclusions(self) -> tuple[CriticConclusion, ...]:
        """没查到东西的那些 —— 它们不否决任何东西。"""
        return unsupported(self.outcomes)

    def to_report(self) -> CriticReport:
        """翻译成领域层的审查报告。

        **九项一项都不能少。** 少一项的报告不是"少查了一项"，是"读的人会以为
        查过了" —— 那比明说"没查"更坏。少了哪一项，领域模型会当场报错并
        把缺的那几项列出来。
        """
        return CriticReport(
            candidate_id=self.candidate_id,
            outcomes=tuple(outcome.to_outcome() for outcome in self.outcomes),
        )


def _check_list() -> str:
    """九问的清单 —— 报告里逐条回显，读的人能对上 design.md §5。"""
    return "\n".join(
        f"{index}. **{CHECK_QUESTIONS[check]}**（`check: {check.value}`）"
        for index, check in enumerate(CriticCheck, start=1)
    )


def _veto_list() -> str:
    """能否决的那五项。"""
    return "、".join(f"`{check.value}`" for check in sorted(VETOING_CHECKS, key=lambda c: c.value))


CRITIC_PROMPT = (
    """\
你是**独立 Critic**。

## 你负责什么

对**一个**候选逐项做**九项检查**，给出一份读者能逐条复核的报告。

**你不给分数。** 分数会把"查过六处"压成一个数，读的人没法复核那个数背后
查了什么。你给的是九项结论，每项带着你查到的事实。

## 九项检查

{check_list}

**九项都要答。** 少一项的报告不是"少查了一项"，是"读的人会以为查过了"——
那比明说"没查"更坏。

## 结论三态

| 结论 | 什么时候用 |
| --- | --- |
| `passed` | 查过了，结论是这条候选没问题 |
| `failed` | 查过了，结论是有问题 |
| `unverified` | **没查成** —— 工具不可用，或事实不足以判断 |

**把"没查"报成"没问题"，你就成了一台会点头的机器。** 拿不准的时候用
`unverified` —— 那是一个诚实的答案，而"没问题"不是。

## 谁能否决

**只有这五项能否决**：{veto_list}

**其余四项不否决。** 「问题是否真实存在」「有没有更低成本的方案」「是否引入
不必要的复杂性」「是否已经有其他任务解决」—— 这四问的答案**没有硬事实**
支撑：仓库里搜不到东西，证明不了问题不存在；"更便宜"要跟别的方案比出来；
"复杂"要看改动的影响面，而影响面在动手前只能估计。所以它们的产出是**给人
看的线索**，不是拦路虎 —— 检出了、列出来了，判不判由人看。

**否决必须带证据。** `vetoes: true` 的那一项，`verdict` 必须是 `failed`，
而且**至少一条 evidence 引用**。没有证据引用的否决，读的人无从复核，只能
选择相信或不信 —— 而那正是这个角色要消除的东西。

## 你的输入

{input_description}

## 你不负责什么

- **你不打分、不排序、不给优先级。** 你只回答那九问。
- **你不改候选。** 你不改它的标题、假设或预期结果 —— 你是审它的。
- **你不决定最终做不做。** 你的报告是 Director 的输入。
- **你不编造。** 没查到就说 `unverified`，不要猜。

## 输出格式

一段 JSON，形状由随本次请求给出的 schema 决定。

- 只输出 JSON 本身：不要解释、不要前后缀说明、不要 markdown。
- **不要输出 schema 之外的字段。** 多一个字段就是一份被拒的输出。

## 证据要求（这一条最重要）

**每一项检查都必须给出至少一条 evidence 引用。**

引用是**结构化的**，`kind` 与 `reference` 必须对得上：

| kind | reference 长什么样 | 例 |
| --- | --- | --- |
| `repository` | 仓库里的文件路径 | `src/render/svg.py` |
| `git_history` | 提交哈希或提交标题 | `a1b2c3d` |
| `test_report` | 测试报告里的用例 | `test_svg_render_complex` |
| `project_status` | 项目状态查询的结果 | `coverage=0.62` |
| `task` | 任务表里的任务 id | `T-42` |
| `artifact` | 产物登记处里的证据 uri | `artifact://report-001` |
| `non_goal` | 项目 Non-goals 里的一条 | `不支持多人实时协作` |
| `constraint` | 项目约束里的一条 | `不允许未经授权修改生产环境` |
| `stop_condition` | 停止条件里的一条 | `KR-001 连续两轮达标` |
| `goal` | 关键结果 / 目标里的一条 | `KR-001` |
| `candidate` | 候选自己的字段 | `confidence=0.82` |

**拿不出证据的检查照样要写出来** —— `verdict` 给 `unverified`。它会被系统标记
成 `unsupported`，**不否决任何东西**（但仍然出现在报告里，让读者看得见
"这一项没查成"）。**因为拿不出证据就不写**，读者就只看到一份"看起来齐整"
的报告，看不出哪几项是没底气的。

**反过来，不要为了让报告好看而编造引用。** 一条指不到具体东西的引用比没有
引用更糟 —— 它会让人以为这件事已经被核实过了。
""".replace("{check_list}", _check_list())
    .replace("{veto_list}", _veto_list())
    .replace("{input_description}", CRITIC_INPUT_DESCRIPTION)
)


CRITIC_PROMPT_SET: PromptSet[CriticOutput] = PromptSet(
    role=Role.CRITIC,
    purpose="逐项做九项检查，给出一份读者能复核的报告。",
    boundary="不打分，不排序，不改候选，不决定最终做不做。",
    system_prompt=CRITIC_PROMPT,
    output_schema=CriticOutput,
)


__all__ = [
    "CRITIC_PROMPT",
    "CRITIC_PROMPT_SET",
    "CriticConclusion",
    "CriticOutput",
]
