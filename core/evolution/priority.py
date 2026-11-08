"""六因子优先级评分 —— ``Priority = V×I×C×U / (E×(1+R))``。

设计依据：docs/design.md §3.3 第三层"演进价值评估（Prioritize）"、§5
（Director 决策链的决策阶段）；docs/tasks.md TASK-P1-053（R-031 六因子优先级
公式；R-032 归一化 0~1、成本设下限；R-033 价值模型须用实际收益校准；
R-084 跨方案优先级判断能力；R-026 综合目标/风险/成本/收益决策）。

**这是一个启发式模型，而且必须被当成启发式模型看待。** design.md 原话：
"这不是成熟的价值模型，后续应该用实际任务收益、失败率和返工成本校准。"
所以这里的每一个数字都有三档性质：公式的**形状**来自设计文档，系数与下限
是**本仓库选的默认值**，而"这些默认值靠不靠谱"要等阶段四用实际收益校准
（:class:`ValueCalibrator` 就是那个口子）。

**六个因子各是什么。**

============  ==========================  ==========================
符号          因子                         含义
============  ==========================  ==========================
``V``         :attr:`PriorityFactors.value`         预期价值
``I``         :attr:`PriorityFactors.impact`        影响范围
``C``         :attr:`PriorityFactors.confidence`    证据置信度
``U``         :attr:`PriorityFactors.urgency`       紧迫程度
``E``         :attr:`PriorityFactors.cost`          实施成本
``R``         :attr:`PriorityFactors.risk`          风险系数
============  ==========================  ==========================

**为什么 ``C`` 在分子里。** 证据置信度低的东西排在前面，等于让"没查清的事"
先做 —— 那是拿不确定性当紧迫性。分子里带着 ``C``，意味着**证据越硬、越靠
前**；证据不足的候选自然沉底，于是决策层更可能选择 defer（补证据）而不是
record（先做）。这与 TASK-P1-054 的"证据不足就暂缓"是同一件事的两种说法。

**为什么 ``R`` 在分母里、而且是 ``1+R`` 而不是 ``R``。** 风险不为零的候选
得分要打折，但 ``R=0``（零风险）不该得分**额外加成** —— 那等于奖励"没有
风险"这件事，而零风险的候选往往是没有被想过风险的候选。写成 ``1+R``，风
险的惩罚从"1 倍"起算，连续、单调，没有那个断点。

**成本下限为什么必须存在。** ``E`` 在分母上，``E=0`` 会让分数发散 —— 而且
现实里 ``E=0`` 极少意味着"这件事不要钱"，多数时候意味着"没人估过成本"。
所以 :data:`MINIMUM_COST` 给成本一个下限：``E`` 低于下限时按下限算。这既
避免了除零，也避免了"没估过成本的事"霸榜。

**归一化是单调缩放，不是重排。** 原始分的理论上限是 ``1 / 成本下限``（分子
最大 1、分母最小），把原始分乘以成本下限就落回 0~1。**这个缩放不改变任何
次序** —— 它只是把分数放进一个能存进 DB（``Numeric(12, 4)`` 且带
``0 ≤ value ≤ 1`` 检查约束）、能跟别的量纲并排看的刻度里。次序由
:func:`rank` 给，不由归一化给。

**排序为什么要显式的次级键。** "因子全相等时排序稳定"不等于"保持输入次序"
—— 那是 :func:`sorted` 的默认行为，结果会随输入次序变化，同样的候选换个
顺序进来就换个名次。:func:`rank` 用 ``(分数降序, id 升序)`` 做键，于是名次
只由**内容**决定，与输入次序无关。

**校准的口子留在哪。** :class:`ValueCalibrator` 是阶段四（P3-014 / P4-050）
的接口：拿"实际收益"回头调"当初的判断"。**此刻它只有一个恒等实现**
（:class:`HeuristicCalibrator`）—— 校准这件事现在确实没做，而不是被假装
做了。是否信任校准结果（相关系数门槛）是 P3-014 的事，不在这里。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.evolution.candidate import EvolutionCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel

#: **成本下限**。``E`` 低于它时按它算（同时也是除零的兜底）。
#:
#: 为什么是 0.1：它把"零成本"与"最贵的真成本"之间的差距限制在 10 倍以内
#: —— 够让真便宜的事排在前面，又不至于让"没人估过成本"的事霸榜。它是**本
#: 仓库选的默认值**，不是量出来的；阶段四校准的时候会连它一起看。
MINIMUM_COST: Final[Decimal] = Decimal("0.1")

#: **分数精度**。原始分与归一化分都量化到这个刻度上（ROUND_HALF_UP）。
#:
#: 为什么不留更多位：DB 那一列是 ``Numeric(12, 4)``，留 12 位小数只是让
#: 存进去时被悄悄舍掉。留 6 位已经足够分辨"两个候选谁更值得先做"，又不会
#: 假装精确到小数点后第六位。
SCORE_PRECISION: Final[Decimal] = Decimal("0.000001")

#: 风险档 → 风险系数 ``R``。**启发式默认值，不是量出来的。**
#:
#: 三档之间的跨度刻意压得小：分母倍数分别是 1.2 / 1.5 / 1.8，最高与最低
#: 相差 1.5 倍。风险该影响次序，但不该把次序整个翻过来 —— 一个高价值高
#: 风险的事该排在前面（只是要小心），不该被风险因子一路压到末尾。要真正
#: 校准它，得用实际失败率（阶段四）。
RISK_COEFFICIENT_LOW: Final[Decimal] = Decimal("0.2")
RISK_COEFFICIENT_MEDIUM: Final[Decimal] = Decimal("0.5")
RISK_COEFFICIENT_HIGH: Final[Decimal] = Decimal("0.8")


def risk_coefficient(level: RiskLevel) -> Decimal:
    """风险档 → 风险系数 ``R``（0~1）。

    **这里做的是"档 → 数"的换算，不是在评价风险。** 档是给人看的定性判断
    （见 :class:`~core.evolution.taxonomy.RiskLevel`），系数是喂给公式的数；
    两者的换算放在这里，是因为公式在这、公式需要它。
    """
    match level:
        case RiskLevel.LOW:
            return RISK_COEFFICIENT_LOW
        case RiskLevel.MEDIUM:
            return RISK_COEFFICIENT_MEDIUM
        case RiskLevel.HIGH:
            return RISK_COEFFICIENT_HIGH


class PriorityFactors(BaseModel):
    """六个因子，全部归一化到 0~1。

    **越界就报错，不夹紧。** 夹紧会让"我填了 1.5"和"我填了 1"变成同一件事
    —— 而那件事正是错的：填错的人不会知道自己填错了，模型也不会知道。报错
    让它当场露出来。唯一被"夹"的是成本，因为成本的下限有**业务含义**
    （"没有免费的候选"），见 :data:`MINIMUM_COST`。

    六个因子都是 ``Decimal`` 而不是 ``float``：它们要进 DB 的
    ``Numeric(12, 4)`` 列，也要做手算核对 —— 二进制浮点数在这两件事上都会
    咬人。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``V`` 预期价值。
    value: Decimal = Field(ge=0, le=1)
    #: ``I`` 影响范围。
    impact: Decimal = Field(ge=0, le=1)
    #: ``C`` 证据置信度。
    confidence: Decimal = Field(ge=0, le=1)
    #: ``U`` 紧迫程度。
    urgency: Decimal = Field(ge=0, le=1)
    #: ``E`` 实施成本。0 合法（会被按下限算），大于 1 不合法。
    cost: Decimal = Field(ge=0, le=1)
    #: ``R`` 风险系数。**这是数，不是档** —— 档用 :func:`risk_coefficient` 换。
    risk: Decimal = Field(ge=0, le=1)

    @classmethod
    def from_candidate(
        cls,
        candidate: EvolutionCandidate,
        *,
        value: Decimal,
        impact: Decimal,
        urgency: Decimal,
        cost: Decimal,
    ) -> Self:
        """从候选装配因子。

        **置信度直接取候选自己的** —— 候选里那个 ``confidence`` 说的就是
        "这份证据有多硬"（R-029），再另填一个数就是自己跟自己打架。**风险
        档换算成系数** —— 候选存的是档，公式要的是数。

        剩下的 ``V`` / ``I`` / ``U`` / ``E`` 没有来源：它们是判断，不是观察
        —— 装配的人得自己给，或者走 :func:`normalize_measure` 把原始量纲压
        过来。
        """
        return cls(
            value=value,
            impact=impact,
            confidence=candidate.confidence,
            urgency=urgency,
            cost=cost,
            risk=risk_coefficient(candidate.risk),
        )


class ScoredCandidate(BaseModel):
    """一个已评分的候选。

    **分数是算出来的，不是填进去的** —— ``raw`` 与 ``score`` 都是
    :class:`PriorityFactors` 的派生量，做成 :attr:`factors` 的计算属性。
    存成字段的话，总有一份数据能让"分数"和"因子"对不上（多写了一个字段、
    改了一个因子忘了重算），而对不上的分数比没有分数更坏 —— 它看起来是算
    出来的。**能构造出来就一定自洽。**
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    #: 分类。**排名不看它** —— 跨方案比较就是这一条的意义（一个产品机会
    #: 该能排在架构机会前面，反之亦然）。留着它是为了让排完的结果能自己
    #: 说明自己是什么。
    category: OpportunityCategory | None = None
    factors: PriorityFactors

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("候选 id 不能是空白")
        return value.strip()

    @property
    def raw(self) -> Decimal:
        """原始优先级分 —— **不归一化**，可以大于 1。"""
        return raw_priority(self.factors)

    @property
    def score(self) -> Decimal:
        """归一化后的优先级分（0~1）。**排次序用这个。**"""
        return normalize_priority(self.raw)


class ActualOutcome(BaseModel):
    """一件已经发生的事 —— 阶段四拿它跟当初的判断比。

    **"实际收益"必须是一个数，而不是一句评价。** "做完了，感觉不错"没法用来
    校准任何东西；``realized_value`` 落在 0~1，与 ``V`` 同一量纲，才能把
    "当初说值 0.8"和"实际拿到 0.3"摆在一起看。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    #: 实际兑现的价值，0~1，与 ``V`` 同一量纲。
    realized_value: Decimal = Field(ge=0, le=1)
    note: str = ""

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("候选 id 不能是空白")
        return value.strip()


@runtime_checkable
class ValueCalibrator(Protocol):
    """用实际收益校准价值判断 —— **阶段四的接口，现在只有一个空实现**。

     **留一个口子，而不是留一个假实现。** 校准要用真实收益数据，而那些数据
     要等系统真的跑过几轮才有。所以此刻的默认实现是**恒等**的（原样返回），
     不是"用某个假数据算出来的校准"。等 P3-014 有数据了，换一个实现进来，
     评分流程一行都不用改。

     ``calibrate`` 返回 ``None`` 表示**这次结果不该参与校准**（比如信息不足、
     或者这次任务的规模与模型见过的完全不是一回事）—— 这与"校准后是原值"
    是两回事：前者是"不参与"，后者是"参与了，结论是不用改"。
    """

    def calibrate(self, factors: PriorityFactors, outcome: ActualOutcome) -> PriorityFactors | None:
        """用一次实际结果校准这组因子。返回 ``None`` 表示这次不参与校准。"""
        ...


class HeuristicCalibrator:
    """阶段四之前的默认校准器 —— **恒等**。

    **它不假装校准过。** 返回的因子与传进来的一模一样，因为此刻确实没有可
    用的实际收益数据可言。这个类存在的意义是让"接上真校准器"这件事在阶段
    四只是换一个对象，而不是改评分流程。
    """

    def calibrate(
        self,
        factors: PriorityFactors,
        outcome: ActualOutcome,  # noqa: ARG002 - 恒等实现不看实际结果
    ) -> PriorityFactors | None:
        return factors


def normalize_measure(raw: Decimal, maximum: Decimal) -> Decimal:
    """把一个原始量纲压到 0~1 —— **指标归一化**。

    ``raw`` 是原始量（人天、影响人数、待办条数），``maximum`` 是这个量纲在
    本项目里的上限。超出上限的一律算 1（"顶格了"），负的算 0。

    **上限必须是正数。** 用 0 当除数会让归一化失去意义 —— 那是输入错了，
    不是"结果算不出来"，所以这里报错而不是返回 0。

    :func:`normalize_priority` 是另一回事：它归一化的是**分数**，不是指标。
    两者名字相近、作用不同，故分作两个函数而不是一个带开关的。
    """
    if maximum <= 0:
        raise ValueError("归一化的上限必须为正 —— 用 0 当除数会让归一化失去意义")
    if raw <= 0:
        return Decimal(0)
    if raw >= maximum:
        return Decimal(1)
    return (raw / maximum).quantize(SCORE_PRECISION, rounding=ROUND_HALF_UP)


def raw_priority(factors: PriorityFactors) -> Decimal:
    """原始优先级分 —— ``V×I×C×U / (E×(1+R))``。

    **成本按下限算。** ``E`` 为 0 时除以 0 会得到 ``NaN`` 或 ``Infinity``，
    而这两种值一旦流进排序就足以让整个次序失去意义（NaN 的比较结果是 False，
    于是"NaN 排哪儿"由实现细节决定）。所以成本低于 :data:`MINIMUM_COST`
    时按下限算 —— 既不除零，也不让"没估过成本"霸榜。

    **原始分可以大于 1** —— 它的上限是 ``1 / 成本下限``。归一化是
    :func:`normalize_priority` 的事。
    """
    cost = max(factors.cost, MINIMUM_COST)
    numerator = factors.value * factors.impact * factors.confidence * factors.urgency
    denominator = cost * (Decimal(1) + factors.risk)
    return (numerator / denominator).quantize(SCORE_PRECISION, rounding=ROUND_HALF_UP)


def normalize_priority(raw: Decimal) -> Decimal:
    """把原始优先级分归一化到 0~1。

    **为什么是乘以成本下限而不是除以某个上限。** 原始分的理论最大值是
    ``1 / 成本下限``（分子最大 1，分母最小），所以"除以理论最大值"等价于
    "乘以成本下限" —— 后者没有除法，没有舍入误差，而且上下界严丝合缝：
    0 映射到 0，理论最大值映射到 1。

    **它是单调的，所以不改变任何次序。** 归一化只负责把分数放进一个能存进
    DB、能跟别的量纲并排看的刻度；名次由 :func:`rank` 给。
    """
    if raw <= 0:
        return Decimal(0)
    maximum = Decimal(1) / MINIMUM_COST
    if raw >= maximum:
        return Decimal(1)
    return (raw * MINIMUM_COST).quantize(SCORE_PRECISION, rounding=ROUND_HALF_UP)


def score_candidate(candidate_id: str, factors: PriorityFactors) -> ScoredCandidate:
    """给一个候选打分。"""
    return ScoredCandidate(candidate_id=candidate_id, factors=factors)


def rank(scored: Iterable[ScoredCandidate]) -> tuple[ScoredCandidate, ...]:
    """按优先级分**降序**排 —— 并列时按 id 升序。

    **次级键是 id，不是输入次序。** :func:`sorted` 本身是稳定的，也就是说
    分数相同时会保持输入次序 —— 但那样一来，同样的候选换个顺序进来就换个
    名次，而名次是要进 DB、要给人看的。用 id 做**显式**的次级键，名次就只
    由（分数, id）决定，与输入次序无关。

    **排名不看分类。** 五类演进机会放在同一个序列里比 —— 这正是"跨方案
    优先级判断"（R-084）的样子：一个产品机会该能排在架构机会前面，反之亦然。
    """
    return tuple(sorted(scored, key=lambda item: (-item.score, item.candidate_id)))


def apply_calibration(
    calibrator: ValueCalibrator,
    scored: Iterable[ScoredCandidate],
    outcomes: Mapping[str, ActualOutcome],
) -> tuple[ScoredCandidate, ...]:
    """用实际收益校准之后的排序 —— **阶段四的入口**。

    拿每个候选对应的实际结果去校准它的因子，再重新排序。校准器返回
    ``None`` 的候选**保留原因子**（这次结果不参与校准，与"校准后仍是原值"
    不是一回事）。

    **是否该信任校准结果，不在这里判断。** 门槛是"校准后的排序与实际收益
    的相关性够不够"（P3-014：Spearman ρ > 0.3，否则退回启发式模型并记录）
    —— 那需要一批结果与一套统计口径，是 P3-014 的活。这里只负责"接上校准
    器 → 重新排序"这条路径本身。
    """
    calibrated: list[ScoredCandidate] = []
    for item in scored:
        outcome = outcomes.get(item.candidate_id)
        if outcome is None:
            calibrated.append(item)
            continue
        adjusted = calibrator.calibrate(item.factors, outcome)
        if adjusted is None:
            calibrated.append(item)
            continue
        calibrated.append(
            ScoredCandidate(
                candidate_id=item.candidate_id,
                category=item.category,
                factors=adjusted,
            )
        )
    return rank(calibrated)


__all__ = [
    "MINIMUM_COST",
    "RISK_COEFFICIENT_HIGH",
    "RISK_COEFFICIENT_LOW",
    "RISK_COEFFICIENT_MEDIUM",
    "SCORE_PRECISION",
    "ActualOutcome",
    "HeuristicCalibrator",
    "PriorityFactors",
    "ScoredCandidate",
    "ValueCalibrator",
    "apply_calibration",
    "normalize_measure",
    "normalize_priority",
    "rank",
    "raw_priority",
    "risk_coefficient",
    "score_candidate",
]
