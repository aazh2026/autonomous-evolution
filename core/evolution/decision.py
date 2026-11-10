"""决策三态与"不行动"的强制落地。

设计依据：docs/design.md §3.3（"特别建议增加一个机制：不行动也是一个合法
决策"，并给了四个"不做"的例子）、§4 循环 B；docs/tasks.md TASK-P1-054
（R-034 不行动是合法决策；R-035 Record / Defer / Reject 三态）；
TASK-P1-022（三表分离，决策是任务的唯一来源）。

**这一模块回答的是"系统会不会说'不做'"。**

一个只会说"是"的系统不是自主决策系统，它是个执行引擎。design.md §3.3 举的
四个"不做"的例子 —— 证据不足先补分析、收益太低暂不执行、有重大架构风险要求
专家审查、当前处于稳定期不引入高风险变更 —— 没有一个属于"不知道该做什么"，
全都是"知道，但这一轮不做"。**"不做"因此必须是一等公民，而不是沉默。**

**三态各自意味着什么。**

- ``record`` —— 做。理由是"值得做"。
- ``defer`` —— 这一轮不做，但**说清楚还要补什么**，下一轮重新评估。
- ``reject`` —— 不做。理由是"不值得做"。

**defer 与 reject 的区别不是语气，是后果。** defer 会**产生后续动作**（补充
分析项），reject 什么都不产生。一个"以后再说"若不带补充分析项，它既没法被
重新评估（重新评估需要知道当初缺了什么），又占着待办清单不放 —— 那是一个
不用负责的垃圾桶（R-034 / R-035）。

**只有 ``record`` 能派生任务。** 这是三表分离（TASK-P1-022）在决策层的对应
面：候选 → 决策 → 任务，中间必须有一次带理由的决策。defer 与 reject 都
**不创建 Task** —— 不是"创建了再标记为不做"，是根本不该创建。

**决策要带着它的依据。** :class:`EvolutionDecision` 记下作出决策时的那组
因子（R-082：每阶段结构化输出、下游可独立验证）—— 没有依据的决策，旁人
无从复核"这个决定该不该做"，只能选择相信或者不信。

**这个模块不碰数据库，也不碰文件系统。** 它只描述"一份决策长什么样"以及
"哪些一致性必须成立"。落库是仓储层的事（``DecisionRepository``），把决策
变成任务是规划层的事（TASK-P1-056）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.evolution.placeholders import PLACEHOLDER_DESCRIPTIONS
from core.evolution.priority import (
    PriorityFactors,
    ScoredCandidate,
    normalize_priority,
    raw_priority,
)


class DecisionState(StrEnum):
    """决策三态（R-035）。

    取值与数据库那一列的 CHECK 约束一致（``decision_state``）：``record`` /
    ``defer`` / ``reject``。**三态不多不少** —— 第四种状态（比如"挂起"）
    会让"不行动"这件事又变得含糊：它到底是 defer 还是 reject？
    """

    RECORD = "record"
    DEFER = "defer"
    REJECT = "reject"


class AnalysisKind(StrEnum):
    """补充分析的类别。

    四类，按"要动什么"分：

    - ``measurement`` —— 去量一下：跑基准、测覆盖率、统计耦合度。
    - ``investigation`` —— 去查清楚：读代码、翻历史、定位根因。
    - ``validation`` —— 去验证：找用户确认、跑实验。
    - ``review`` —— 找人看一眼：架构评审、专家审查。

    **为什么分类。** 一条"补充分析"如果不说明是"去量"还是"去问人"，下一轮
    根本不知道该派谁去 —— 而 defer 的整个意义就是把这件事**推给下一轮**，
    推得含糊等于没推。
    """

    MEASUREMENT = "measurement"
    INVESTIGATION = "investigation"
    VALIDATION = "validation"
    REVIEW = "review"


class FollowUpAnalysis(BaseModel):
    """一条补充分析项 —— defer 决策留下的"下一轮要补什么"。

    ``description`` 写的是**具体做什么**，不是"再调查一下"这种没有落点的
    话。占位写法（"待补充"、"TBD"…）会被直接拒掉：它们能通过非空检查，
    但信息量是零，而 defer 的意义恰恰在于说清楚缺什么。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AnalysisKind
    #: 具体要做的分析。
    description: str = Field(min_length=1)
    #: 为什么需要它 —— 可选。写下来是为了让下一轮读的人知道这不是拍脑袋。
    rationale: str = ""

    @field_validator("description")
    @classmethod
    def _description_is_specific(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("补充分析项要写清楚做什么")
        if text.lower() in PLACEHOLDER_DESCRIPTIONS:
            raise ValueError(
                f"补充分析项不能是占位写法：{text!r} —— 「以后再说」必须说清还要补什么"
            )
        return text


class EvolutionDecision(BaseModel):
    """一条演进决策 —— 系统为什么决定做或不做。

    **三态**：``record`` / ``defer`` / ``reject``。后两者是合法的"不行动"
    （R-034 / R-035）。

    **三态各有各的硬约束。**

    1. ``rationale`` 三态都必填 —— 放弃也要说清楚为什么（R-035）。
    2. ``defer`` 必须至少带一条 :class:`FollowUpAnalysis` —— "以后再说"要
       说清楚还要补什么，否则 defer 成了不用负责的垃圾桶。
    3. 非 ``defer`` 不带补充分析项 —— record 之后还挂着"要补的分析"是
       自相矛盾的：要么现在就做（那就别挂），要么这轮不做（那就是 defer）。

    **决策带着它的依据**（:attr:`factors`）—— 没有依据的决策，旁人无从
    复核，只能选择相信或者不信（R-082）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 决策 id。由调用方给 —— 领域模型不自己编号，省得两个地方各编一套。
    id: str
    #: 被决策的候选。
    candidate_id: str
    decision: DecisionState
    #: 决策理由。三态都必填。
    rationale: str
    #: defer 触发的补充分析项。**只有 defer 非空。**
    requested_analysis: tuple[FollowUpAnalysis, ...] = ()
    #: 作出决策时的那组因子 —— 决策的依据。
    factors: PriorityFactors

    @field_validator("id", "candidate_id", "rationale")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("决策的 id、候选 id 与理由都不能是空白")
        return value.strip()

    @model_validator(mode="after")
    def _analysis_matches_state(self) -> Self:
        if self.decision is DecisionState.DEFER:
            if not self.requested_analysis:
                raise ValueError(
                    "defer 决策必须产出至少一条补充分析项 —— 「以后再说」必须说清楚还要补什么"
                )
        elif self.requested_analysis:
            raise ValueError("只有 defer 决策才带补充分析项")
        return self

    @property
    def score(self) -> Decimal:
        """决策时的优先级分（0~1）。

        **算出来的，不是填进去的** —— 依据是 :attr:`factors`，分数是它的
        派生量，两者永远对得上。
        """
        return normalize_priority(raw_priority(self.factors))

    @property
    def produces_task(self) -> bool:
        """这条决策会不会派生任务。**只有 ``record`` 会。**

        defer 与 reject 都不创建 Task —— 不是"创建了再标记为不做"，是根本
        不该创建（TASK-P1-022 的决策层对应面）。
        """
        return self.decision is DecisionState.RECORD

    @property
    def is_no_action(self) -> bool:
        """是不是一次"不行动"。"""
        return not self.produces_task

    def analysis_payload(self) -> list[dict[str, object]]:
        """补充分析项落库时的样子 —— ``requested_analysis`` 那一列的值。

        **单独给一个方法，是因为这一列的值会被单独取用**（仓储层的
        ``record(requested_analysis=...)``）。塞在 ``as_record_fields`` 的
        字典里，取用的人就得先把它断言回 ``Sequence[object]`` 才敢往下传 ——
        而那一步断言是多余的：这里本来就是这个类型。
        """
        return [item.model_dump() for item in self.requested_analysis]

    def as_record_fields(self) -> dict[str, object]:
        """落库时要写的那几个字段 —— 键名与 ``core.entities.EvolutionDecision``
        的列名对齐。

        放在这里而不是让每个调用方自己拼，是为了让"领域模型 → 数据库"只有
        一份映射：拼错键名或漏掉某个字段，这种错在入库时往往不会报错，只会
        让那一列永远是默认值。
        """
        return {
            "decision": self.decision.value,
            "rationale": self.rationale,
            "requested_analysis": self.analysis_payload(),
        }


class DecisionPolicy(BaseModel):
    """决策策略 —— 把一份评分翻译成一个决策。

    **三个阈值、三个出口，顺序不能换。**

    1. **置信度不够 → defer。** 先确认问题是不是真的存在，再讨论怎么改。
    2. **风险太高 → defer。** 要专家看一眼才能推进。
    3. **价值太低 → reject。** 不值得投入，做了也是白做。
    4. 其余 → record。

    **为什么置信度排在最前面。** 一个候选的价值判断是建立在"它描述的问题
    确实存在"这个前提上的；前提没坐实，价值再低也只是在讨论一个不存在的问题
    的价值。所以证据不足时先 defer —— 哪怕它看着也不值钱。反过来（价值低 +
    证据足）才是 reject。

    **风险用 ``>=``，其余用 ``<``。** 风险系数是离散的（0.2 / 0.5 / 0.8），
    默认阈值 0.8 正好是"高风险"那一档的系数 —— 用 ``>`` 的话这一档永远不
    会触发专家审查，那条路径就成了死代码。其余两个是"低于门槛"的语义，用
    ``<`` 才是"刚好达标就放行"。

    **阈值是本仓库选的默认值，不是量出来的。** 它们表达的是"多疑到什么程度
    才肯先补证据、多保守才肯先找人看"。真要定这些数，得看历史上多少候选
    因为证据不足被 defer 之后又补出了结果 —— 那是阶段四的事。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 置信度低于此值 → defer（先补证据）。
    minimum_confidence: Decimal = Field(default=Decimal("0.3"), ge=0, le=1)
    #: 风险系数大于等于此值 → defer（要专家审查）。
    maximum_risk: Decimal = Field(default=Decimal("0.8"), ge=0, le=1)
    #: 价值低于此值 → reject（不值得做）。
    minimum_value: Decimal = Field(default=Decimal("0.2"), ge=0, le=1)

    def decide(self, scored: ScoredCandidate, *, decision_id: str) -> EvolutionDecision:
        """给一个已评分的候选作出决策。

        **``decision_id`` 由调用方给** —— 领域模型不自己编号：编号规则该由
        编排层统一管（跨轮次唯一、可追溯），各模块自己编一套早晚会撞。
        """
        factors = scored.factors

        if factors.confidence < self.minimum_confidence:
            return EvolutionDecision(
                id=decision_id,
                candidate_id=scored.candidate_id,
                decision=DecisionState.DEFER,
                rationale=(
                    f"证据置信度 {factors.confidence} 低于阈值 "
                    f"{self.minimum_confidence}，先补充分析再定"
                ),
                requested_analysis=(self._evidence_analysis(scored),),
                factors=factors,
            )

        if factors.risk >= self.maximum_risk:
            return EvolutionDecision(
                id=decision_id,
                candidate_id=scored.candidate_id,
                decision=DecisionState.DEFER,
                rationale=(
                    f"风险系数 {factors.risk} 达到阈值 {self.maximum_risk}，实施前需要专家评审"
                ),
                requested_analysis=(self._review_analysis(scored),),
                factors=factors,
            )

        if factors.value < self.minimum_value:
            return EvolutionDecision(
                id=decision_id,
                candidate_id=scored.candidate_id,
                decision=DecisionState.REJECT,
                rationale=(
                    f"预期价值 {factors.value} 低于阈值 {self.minimum_value}，投入产出不成比例"
                ),
                factors=factors,
            )

        return EvolutionDecision(
            id=decision_id,
            candidate_id=scored.candidate_id,
            decision=DecisionState.RECORD,
            rationale=(
                f"价值 {factors.value}、置信度 {factors.confidence}、"
                f"成本 {factors.cost}、风险 {factors.risk} 均达到门槛，按优先级推进"
            ),
            factors=factors,
        )

    def _evidence_analysis(self, scored: ScoredCandidate) -> FollowUpAnalysis:
        """证据不足时要补的分析。

        **策略只能保证"有"，保证不了"准"。** 这里给的是一条最小可执行的分析
        —— 去做一次可复现的测量，把"问题是否存在"这件事落实。Director 可以
        给出更贴题的补充项，但**策略自己欠的那一条不能空着**。
        """
        return FollowUpAnalysis(
            kind=AnalysisKind.MEASUREMENT,
            description=(
                f"对候选 {scored.candidate_id} 做一次可复现的测量并与基线对比，"
                "确认它描述的问题是否真实存在"
            ),
            rationale="证据不足时，先确认问题存在，再讨论怎么改",
        )

    def _review_analysis(self, scored: ScoredCandidate) -> FollowUpAnalysis:
        """风险过高时要做的评审。"""
        return FollowUpAnalysis(
            kind=AnalysisKind.REVIEW,
            description=(f"对候选 {scored.candidate_id} 做一次架构评审，给出是否可以实施的结论"),
            rationale="风险超出可自动推进的范围，实施前要有人评审",
        )


class DecisionRound(BaseModel):
    """一轮演进周期里的全部决策。

    **它同时是"这轮做了什么"的清单与"这轮不创建任务"的边界**：只有
    :attr:`recorded` 里的决策能派生任务，其余的都不能。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cycle_id: str
    decisions: tuple[EvolutionDecision, ...] = ()

    @field_validator("cycle_id")
    @classmethod
    def _cycle_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("周期 id 不能是空白")
        return value.strip()

    def with_decision(self, decision: EvolutionDecision) -> DecisionRound:
        """加一条决策，返回**新的一轮**。

        不可变 + 追加 —— 决策记录一旦落下就不该被改写（要改就再记一条）。
        """
        return DecisionRound(
            cycle_id=self.cycle_id,
            decisions=(*self.decisions, decision),
        )

    @property
    def total(self) -> int:
        """这轮一共作了多少个决策。"""
        return len(self.decisions)

    @property
    def no_action(self) -> tuple[EvolutionDecision, ...]:
        """这轮"不行动"的那些决策。"""
        return tuple(item for item in self.decisions if item.is_no_action)

    @property
    def no_action_ratio(self) -> Decimal:
        """不行动决策占决策总数的比例（T2 要被记录的那个数）。

        **一轮一个决策都没有时是 0，不是"没定义"** —— 比例的分母为零时，
        "比例是多少"这个问题没有答案，硬算会得到一个除零错误；记 0 更诚实：
        这一轮确实没有做出任何决策。
        """
        if not self.decisions:
            return Decimal(0)
        return (Decimal(len(self.no_action)) / Decimal(self.total)).quantize(Decimal("0.0001"))

    @property
    def recorded(self) -> tuple[EvolutionDecision, ...]:
        """**唯一可以派生任务的决策集合。**

        defer 与 reject 都不在里面 —— 规划层（TASK-P1-056）只应该看这个
        属性，不应该自己去翻全部决策然后"自己判断哪些该做"。
        """
        return tuple(item for item in self.decisions if item.produces_task)

    @property
    def pending_analysis(self) -> tuple[FollowUpAnalysis, ...]:
        """下一轮要补的分析 —— 这一轮所有 defer 决策攒下来的待办。"""
        return tuple(
            analysis for decision in self.decisions for analysis in decision.requested_analysis
        )

    def decision_for(self, candidate_id: str) -> EvolutionDecision | None:
        """按候选 id 取决策。取不到返回 ``None``。"""
        for decision in self.decisions:
            if decision.candidate_id == candidate_id:
                return decision
        return None


class NoActionSignal(BaseModel):
    """闭环健康信号 —— 这套系统到底会不会"不行动"。

    **判据只有一条：观察到的轮次里，有没有出现过不行动决策。** 一条都没有，
    就说明"不行动"这个能力**没有被真正用上** —— 要么阈值形同虚设，要么决策
    过程根本没走到 defer / reject 分支。

    **这是必要条件，不是充分条件。** 一个每轮都拒绝九成候选的系统也"健康"
    （它确实在拒绝），但那说明阈值定得太激进 —— 那是另一个问题，由这个信号
    提醒人去看比例，而不是由这个信号来判。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 观察了多少轮。
    rounds: int
    #: 一共作了多少个决策。
    total_decisions: int
    #: 其中不行动的有多少个。
    no_action_decisions: int
    #: 不行动占决策总数的比例（T2 要被记录的那个数）。
    ratio: Decimal
    #: 闭环健康信号 —— 有没有用上"不行动"。
    healthy: bool
    #: 给读的人看的一句话结论。
    explanation: str

    @classmethod
    def from_rounds(cls, rounds: Iterable[DecisionRound]) -> Self:
        """从若干轮里算出这个信号。"""
        collected = tuple(rounds)
        total = sum(item.total for item in collected)
        no_action = sum(len(item.no_action) for item in collected)
        ratio = (
            (Decimal(no_action) / Decimal(total)).quantize(Decimal("0.0001"))
            if total
            else Decimal(0)
        )
        healthy = no_action >= 1
        if healthy:
            explanation = (
                f"观察了 {len(collected)} 轮共 {total} 条决策，"
                f"其中 {no_action} 条是不行动决策（占 {ratio}）—— "
                "「不行动」这个能力确实被用上了"
            )
        else:
            explanation = (
                f"观察了 {len(collected)} 轮共 {total} 条决策，没有一条是不行动决策 —— "
                "「不行动」能力没有被真正用上：要么阈值形同虚设，"
                "要么决策过程根本没走到 defer / reject 分支"
            )
        return cls(
            rounds=len(collected),
            total_decisions=total,
            no_action_decisions=no_action,
            ratio=ratio,
            healthy=healthy,
            explanation=explanation,
        )


def decide_round(
    policy: DecisionPolicy,
    scored: Sequence[ScoredCandidate],
    *,
    cycle_id: str,
    id_prefix: str = "EVD",
) -> DecisionRound:
    """给一轮里的全部候选作决策。

    **决策 id 在这里统一生成**：``{前缀}-{周期}-{序号}``。周期号进 id 是为了
    跨轮不撞 —— 同一个候选在不同的轮次里被重新评估是常事，两轮的决策各是各的。
    """
    decisions = [
        policy.decide(item, decision_id=f"{id_prefix}-{cycle_id}-{index:03d}")
        for index, item in enumerate(scored, start=1)
    ]
    return DecisionRound(cycle_id=cycle_id, decisions=tuple(decisions))


__all__ = [
    "AnalysisKind",
    "DecisionPolicy",
    "DecisionRound",
    "DecisionState",
    "EvolutionDecision",
    "FollowUpAnalysis",
    "NoActionSignal",
    "decide_round",
]
