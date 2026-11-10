"""价值模型的校准 —— 用实际收益把"该先做哪件"这件事学出来。

设计依据：docs/tasks.md TASK-P3-014（交付物：价值模型 + 用实际收益校准；验收
条件"校准后模型的排序与'事后实际收益'有正相关（Spearman ρ > 0.3），否则退回
启发式模型并记录"；验证 T2）、R-031（跨方案优先级判断）、R-033（价值模型须用
实际收益校准）、P1-053（`core/evolution/priority.py` 留了一个校准口子的
Protocol + 恒等实现）。

> **P1-053 留了一个口子，那一刻是有意为之的。**
>
> `ValueCalibrator` 是个 Protocol，默认实现是**恒等**的 —— 因为那时候确实
> 没有实际收益数据可言。**留口子，不留假实现**：等有了数据，换一个实现进来，
> 评分流程一行都不用改。
>
> **这一份就是那个实现。**

**校准学的是"哪个因子值得信"。** 启发式模型把六个因子一律乘起来、权重都
是 1 —— 那是一个**先验**，不是量出来的。本模块用实际兑现的收益去回答一件
事：**当初那六个数里，哪几个真的predicts结果，哪几个是噪声。**

**为什么不是"直接乘一个系数"。** 因为分数是六个因子的**乘积** —— 给任何一个
因子乘一个**常数**，等价于给所有候选的分数乘同一个常数，**次序一点都不
变**。所以校准必须改的是**指数**：``V^α`` 而不是 ``V·k``。指数变了，因子之间
的相对轻重才真的变了。

**指数怎么用。** 因子不是直接取幂，而是**围绕样本均值**做幂变换：

``x' = 均值^(1-α) · x^α``

``α = 1`` 是恒等（就是启发式模型）；``α = 0`` 表示"这个因子完全不该参与排序"
（所有候选都被压成同一个均值）；``α = 2`` 表示"这个因子比启发式模型以为的
更要紧"。变换后夹在 0~1 之间 —— 因子有值域，出了值域就是非法数据。

**风险系数不参与校准。** 风险只有三档（低/中/高），样本里每一档的样本数天
然不均，拿它当连续因子去拟合指数会过拟合；而它的作用是**惩罚**，不是"值不
值得"。它固定为 1。

**测相关性要用留出法，不能用同一个数据既训练又验收。** 拿拟合过的那批数据
去量相关性，得到的数一定好看 —— 那叫自我验证。**所以这里做 k 折交叉验
证**：每一折都用"另外几折"去拟合，再拿拟合出来的模型给这一折打分，最后把
所有折的分数汇总算一次 ρ。**这个 ρ 才是"这套校准能不能用"的证据。**

**量不出来就说量不出来。** 样本里的实际收益全一样、或者样本只有一条时，相
关性**不存在** —— 那是 ``None``，不是 0。**"算不出"与"算出来是 0"是两件事**，
而这一层要是把它们混成一件，门槛就形同虚设。

**这个模块没有 IO。** 它只做算术：给定样本，给出模型、给出相关性、给出报告。
**"样本从哪来"是上面那一层的事。**
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final, cast

from core.evolution.priority import (
    PriorityFactors,
    raw_priority,
)

#: 校准后的模型与实际收益的相关系数门槛。**仓库默认值。**
#:
#: **为什么是 0.3。** 六个因子的乘积本身就带了不少噪声，而候选之间的差别
#: 往往没那么大 —— 排序与实际收益只要**弱正相关**，就说明"这个模型比随机
#: 排强"。**这个数是拍的**：真实的门槛要看真实项目上"启发式排序"本身的
#: 相关性是多少，而那个基线本仓库还没量过。
SPEARMAN_THRESHOLD: Final[Decimal] = Decimal("0.3")

#: 至少要多少个样本才尝试校准。**仓库默认值。**
#:
#: **为什么要有下限。** 样本少的时候，指数拟合出来的是噪声 —— 而噪声拟合
#: 出来的模型**照样能算出一个 ρ**。**样本不足时最好的做法是不校准**，用启
#: 发式模型，并把这件事记下来。
MINIMUM_CALIBRATION_SAMPLES: Final[int] = 8

#: 指数的取值范围与步长。**网格搜索，不是连续优化。**
#:
#: **为什么用网格。** 网格是确定的、可复现的、能说清"我们搜过哪些值"的。
#: 连续优化（梯度、随机搜索）在这个尺度上收益很小，代价是"同一批数据两
#: 次跑出两个结果" —— 而排序是要给人看的，**同样输入给出同样次序**才敢用。
MIN_EXPONENT: Final[Decimal] = Decimal("0")
MAX_EXPONENT: Final[Decimal] = Decimal("2")
EXPONENT_STEP: Final[Decimal] = Decimal("0.25")

#: 坐标搜索最多跑几轮。**仓库默认值。**
#:
#: 每一轮把五个因子各扫一遍网格。轮数再多收益就看不见了 —— 指数之间的相
#: 互影响没那么大。
MAX_FIT_ROUNDS: Final[int] = 4

#: 交叉验证的折数。**仓库默认值。**
#:
#: 折数多了，每折的样本少，**每折量出来的 ρ 就更抖**；折数少了，训练集小，
#: 拟合出来的指数就更容易过拟合。4 折是这两头之间一个说得过去的折中。
CROSS_VALIDATION_FOLDS: Final[int] = 4

#: 相关性保留几位小数。
CALIBRATION_PRECISION: Final[Decimal] = Decimal("0.000001")

#: 算相关性至少要几个点。**少一个点就没有相关性。**
MINIMUM_CORRELATION_POINTS: Final[int] = 2

#: 交叉验证至少要几折。**一折没法留出** —— 那样就成了自我验证。
MINIMUM_FOLDS: Final[int] = 2

#: 参与校准的五个因子。**风险不在其中。**
CALIBRATED_FACTORS: Final[tuple[str, ...]] = (
    "value",
    "impact",
    "confidence",
    "urgency",
    "cost",
)


@dataclass(frozen=True, slots=True)
class FactorVector:
    """五个因子的一个取值。

    **五个数一组，不是六个** —— 风险系数不参与校准（理由见模块文档）。

    **为什么用同一个形状装"指数"与"均值"。** 它们在算法里总是成对出现
    （"按这个指数、围绕这个均值变换"），形状一样就少一处"哪个是哪个"的
    笔误机会。
    """

    value: Decimal
    impact: Decimal
    confidence: Decimal
    urgency: Decimal
    cost: Decimal

    def get(self, name: str) -> Decimal:
        """取一个因子的值。

        **名字只从 :data:`CALIBRATED_FACTORS` 里来** —— 那是本模块自己定的
        五个因子名，与这里的字段一一对应。``getattr`` 的返回值是动态的，所以
        这里显式断言类型 —— **五个字段都是 ``Decimal``，这一点由构造保证**。
        """
        return cast(Decimal, getattr(self, name))

    def replaced(self, name: str, number: Decimal) -> FactorVector:
        """换掉一个因子，其余不动。"""
        return replace(self, **{name: number})


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    """一条校准样本 —— **当初的判断 + 事后的收益**。

    **两者必须成对。** 只有判断没有收益，那不是样本（还没发生）；只有收益
    没有判断，那也不知道当初是怎么想的。
    """

    candidate_id: str
    factors: PriorityFactors
    realized_value: Decimal

    def __post_init__(self) -> None:
        """**空白 id 与越界的收益都要拦住。**"""
        if not self.candidate_id.strip():
            raise ValueError("校准样本的候选 id 不能是空白")
        if not Decimal(0) <= self.realized_value <= Decimal(1):
            raise ValueError("实际收益必须落在 0~1 之间 —— 它与 V 是同一量纲")


@dataclass(frozen=True, slots=True)
class CalibrationModel:
    """校准出来的模型。

    :param exponents: 五个因子的指数。**1 就是启发式模型。**
    :param means: 五个因子在样本里的均值 —— 幂变换围绕它做。
    :param samples: 拟合用了多少个样本。
    """

    exponents: FactorVector
    means: FactorVector
    samples: int

    def apply(self, factors: PriorityFactors) -> PriorityFactors:
        """把一组因子按这个模型变换一遍。

        **风险系数原样带走** —— 它不参与校准。
        """
        return PriorityFactors(
            value=_transform(factors.value, self.means.value, self.exponents.value),
            impact=_transform(factors.impact, self.means.impact, self.exponents.impact),
            confidence=_transform(
                factors.confidence,
                self.means.confidence,
                self.exponents.confidence,
            ),
            urgency=_transform(factors.urgency, self.means.urgency, self.exponents.urgency),
            cost=_transform(factors.cost, self.means.cost, self.exponents.cost),
            risk=factors.risk,
        )


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """一次校准的结论。

    :param samples: 用了多少个样本。
    :param rho_before: **留出法**量出来的、启发式模型的 ρ。
    :param rho_after: **留出法**量出来的、校准后模型的 ρ。
    :param threshold: 门槛。
    :param accepted: 校准结果被采纳了吗。
    :param reason: **为什么**。**采纳与退回都要说清理由。**

    **两个 ρ 都是 ``None`` 的可能：** 样本不足、或者相关性算不出来。**"算不
    出"不是 0** —— 门槛是拿来比大小的，不是拿来当默认值的。
    """

    samples: int
    rho_before: Decimal | None
    rho_after: Decimal | None
    threshold: Decimal
    accepted: bool
    reason: str


def spearman_rho(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal:
    """两组数之间的 Spearman 秩相关。

    **秩相关比皮尔逊相关适合这里**：排序只关心**次序**，不关心数值本身差多
    少 —— 而实际收益与因子都常常偏斜、分布不成正态。

    :param xs: 第一组数。
    :param ys: 第二组数。
    :raises ValueError: **两组数长度对不上、或者少于两个点、或者某一侧全都
        一样** —— 那三种情况下相关性**不存在**，不是 0。

    **"算不出"与"算出来是 0"必须分开。** 混成一件，门槛就形同虚设：门槛是
    "相关性 > 0.3"，而"算不出"被当成 0 的话，结论会变成"不算好" —— 听起来
    一样，但**补救动作完全不同**（一个是"再攒点数据"，一个是"换模型"）。
    """
    if len(xs) != len(ys):
        raise ValueError("算相关性要两组一样长的数")
    if len(xs) < MINIMUM_CORRELATION_POINTS:
        raise ValueError("少于两个点算不出相关性")
    ranks_x = _ranks(xs)
    ranks_y = _ranks(ys)
    return _pearson(ranks_x, ranks_y)


def fit_model(samples: Sequence[CalibrationSample]) -> CalibrationModel:
    """用实际收益拟合五个因子的指数。

    **坐标搜索**：每一轮把五个因子各扫一遍网格，取让相关性最大的那个指数。
    起点是全 1（也就是启发式模型）—— **所以拟合结果不会比启发式模型差**，
    除非网格里根本没有更好的点。

    :param samples: 校准样本。
    """
    if not samples:
        raise ValueError("没有样本就没法拟合")
    means = _means(samples)
    exponents = FactorVector(
        value=Decimal(1),
        impact=Decimal(1),
        confidence=Decimal(1),
        urgency=Decimal(1),
        cost=Decimal(1),
    )
    best = _rho_for(samples, exponents, means)
    for _ in range(MAX_FIT_ROUNDS):
        improved = False
        for name in CALIBRATED_FACTORS:
            current = exponents.get(name)
            for candidate in _exponent_grid():
                trial = exponents.replaced(name, candidate)
                rho = _rho_for(samples, trial, means)
                if not _is_better(rho, candidate, best, current):
                    continue
                best = rho
                exponents = trial
                current = candidate
                improved = True
        if not improved:
            break
    return CalibrationModel(exponents=exponents, means=means, samples=len(samples))


def cross_validated_rho(
    samples: Sequence[CalibrationSample],
    folds: int = CROSS_VALIDATION_FOLDS,
) -> tuple[Decimal | None, Decimal | None]:
    """**留出法**量出来的相关性 —— 启发式的与校准后的各一个。

    **每一折都用"另外几折"去拟合**，再拿拟合出来的模型给这一折打分；所有
    折的分数汇总起来算一次 ρ。**用同一批数据既训练又验收，量出来的数一定
    好看** —— 那是自我验证，不是证据。

    :param samples: 校准样本。
    :param folds: 折数。
    :return: (启发式的 ρ, 校准后的 ρ)。**算不出来就是 ``None``。**
    """
    if folds < MINIMUM_FOLDS:
        raise ValueError("交叉验证至少要两折 —— 一折没法留出")
    ordered = sorted(samples, key=lambda item: item.candidate_id)
    if len(ordered) < MINIMUM_CORRELATION_POINTS:
        return None, None
    heuristic_scores: list[Decimal] = []
    calibrated_scores: list[Decimal] = []
    realized: list[Decimal] = []
    for fold in range(folds):
        held_out = [item for index, item in enumerate(ordered) if index % folds == fold]
        training = [item for index, item in enumerate(ordered) if index % folds != fold]
        if not held_out or not training:
            continue
        model = fit_model(training)
        for item in held_out:
            heuristic_scores.append(raw_priority(item.factors))
            calibrated_scores.append(raw_priority(model.apply(item.factors)))
            realized.append(item.realized_value)
    if len(realized) < MINIMUM_CORRELATION_POINTS:
        return None, None
    try:
        before = spearman_rho(heuristic_scores, realized)
    except ValueError:
        before = None
    try:
        after = spearman_rho(calibrated_scores, realized)
    except ValueError:
        after = None
    return before, after


def calibrate(
    samples: Sequence[CalibrationSample],
    *,
    threshold: Decimal = SPEARMAN_THRESHOLD,
    minimum_samples: int = MINIMUM_CALIBRATION_SAMPLES,
    folds: int = CROSS_VALIDATION_FOLDS,
) -> tuple[CalibrationModel | None, CalibrationReport]:
    """校准一次，给出模型与结论。

    **三条不通的路，三条都要说清理由：**

    1. **样本不够** —— 不校准，用启发式模型（"信息不足，暂不参与校准"）；
    2. **相关性算不出来** —— 不校准（"算不出相关性，暂不参与校准"）；
    3. **相关性没到门槛** —— **退回启发式模型**（"校准后的排序与实际收益的
       相关性没到门槛"）。

    :param samples: 校准样本。
    :return: (模型, 报告)。**模型是 ``None`` 表示不该用校准后的模型** ——
        退回启发式模型时，调用方要拿原样的因子重新排，而不是拿一个空模型。
    """
    if len(samples) < minimum_samples:
        report = CalibrationReport(
            samples=len(samples),
            rho_before=None,
            rho_after=None,
            threshold=threshold,
            accepted=False,
            reason=(
                f"样本不足（{len(samples)} 条，少于 {minimum_samples} 条）—— "
                "这一轮不参与校准，用启发式模型"
            ),
        )
        return None, report
    before, after = cross_validated_rho(samples, folds=folds)
    if after is None:
        report = CalibrationReport(
            samples=len(samples),
            rho_before=before,
            rho_after=None,
            threshold=threshold,
            accepted=False,
            reason="实际收益算不出相关性（样本太集中或太少）—— 这一轮不参与校准",
        )
        return None, report
    if after <= threshold:
        report = CalibrationReport(
            samples=len(samples),
            rho_before=before,
            rho_after=after,
            threshold=threshold,
            accepted=False,
            reason=(
                f"校准后的排序与实际收益的相关性 {after} 没到门槛 {threshold} —— "
                "退回启发式模型，并把这件事记下来"
            ),
        )
        return None, report
    model = fit_model(samples)
    report = CalibrationReport(
        samples=len(samples),
        rho_before=before,
        rho_after=after,
        threshold=threshold,
        accepted=True,
        reason=f"校准后的排序与实际收益的相关性 {after} 过了门槛 {threshold}",
    )
    return model, report


def _is_better(
    rho: Decimal,
    new_exponent: Decimal,
    best: Decimal,
    current: Decimal,
) -> bool:
    """这个指数比现在这个好吗？

    **打平时偏向更保守的那个** —— 也就是离启发式模型（指数 1）更近的那个。

    **为什么。** 有些数据上多个指数给出同样的相关性 —— 比如某个因子的取值在
    样本里本来就只有一种，其余因子全都一样，那么这个因子取什么指数，次序都
    一样。**这时候数据没有给出偏好**，拟合却得挑一个。**挑更保守的那个**，
    理由是"证据不足时少改一点"：离启发式模型越近，改动越小，而启发式模型
    是一直都在的那个。

    **不挑保守的那个会怎样。** 拟合会落在网格里**先扫到**的那个 —— 也就是
    任意的一个。**一个任意的改动看起来像是有依据的改动**，而它其实没有。
    """
    if rho > best:
        return True
    if rho < best:
        return False
    return abs(new_exponent - Decimal(1)) < abs(current - Decimal(1))


def _transform(value: Decimal, mean: Decimal, exponent: Decimal) -> Decimal:
    """把一个因子围绕均值做幂变换。

    ``x' = 均值^(1-α) · x^α``，结果夹在 0~1 之间。

    **三种退让的情况**（都要有说法，不能悄悄算错）：

    - ``α = 1`` —— 恒等，就是启发式模型；
    - **均值是 0** —— 没有围绕它变换的余地，原样带走（均值全是 0 说明这个因
      子在样本里根本没有区分度）；
    - **值是 0** —— ``0^α = 0``（``α > 0`` 时），按 0 算。
    """
    if exponent == 1:
        return value
    if mean <= 0:
        return value
    if exponent == 0:
        return mean
    if value <= 0:
        return Decimal(0)
    scaled = ((value.ln() * exponent) + (mean.ln() * (1 - exponent))).exp()
    clipped = min(max(scaled, Decimal(0)), Decimal(1))
    return clipped.quantize(CALIBRATION_PRECISION)


def _exponent_grid() -> tuple[Decimal, ...]:
    """指数的候选值 —— 从 0 到 2，步长 0.25。"""
    values: list[Decimal] = []
    steps = int((MAX_EXPONENT - MIN_EXPONENT) / EXPONENT_STEP)
    for index in range(steps + 1):
        values.append((MIN_EXPONENT + EXPONENT_STEP * index).quantize(CALIBRATION_PRECISION))
    return tuple(values)


def _means(samples: Sequence[CalibrationSample]) -> FactorVector:
    """样本里五个因子的均值。"""
    count = Decimal(len(samples))
    return FactorVector(
        value=_mean_of([item.factors.value for item in samples], count),
        impact=_mean_of([item.factors.impact for item in samples], count),
        confidence=_mean_of([item.factors.confidence for item in samples], count),
        urgency=_mean_of([item.factors.urgency for item in samples], count),
        cost=_mean_of([item.factors.cost for item in samples], count),
    )


def _mean_of(values: Sequence[Decimal], count: Decimal) -> Decimal:
    """一组数的均值。"""
    total = sum(values, Decimal(0))
    return (total / count).quantize(CALIBRATION_PRECISION)


def _rho_for(
    samples: Sequence[CalibrationSample],
    exponents: FactorVector,
    means: FactorVector,
) -> Decimal:
    """按这组指数给样本打分，再看与实际收益的相关性。

    **算不出相关性时当作"没有相关性"**（返回 -1）—— 拟合这一步只是用来挑
    指数，挑不出更好的就保持原样，**不因为"算不出"就把已有的好结果扔掉**。
    """
    scores: list[Decimal] = []
    realized: list[Decimal] = []
    for item in samples:
        transformed = _transformed_factors(item.factors, exponents, means)
        scores.append(raw_priority(transformed))
        realized.append(item.realized_value)
    try:
        return spearman_rho(scores, realized)
    except ValueError:
        return Decimal(-1)


def _transformed_factors(
    factors: PriorityFactors,
    exponents: FactorVector,
    means: FactorVector,
) -> PriorityFactors:
    """按一组指数与均值变换因子。"""
    return PriorityFactors(
        value=_transform(factors.value, means.value, exponents.value),
        impact=_transform(factors.impact, means.impact, exponents.impact),
        confidence=_transform(factors.confidence, means.confidence, exponents.confidence),
        urgency=_transform(factors.urgency, means.urgency, exponents.urgency),
        cost=_transform(factors.cost, means.cost, exponents.cost),
        risk=factors.risk,
    )


def _ranks(values: Sequence[Decimal]) -> list[Decimal]:
    """秩 —— **并列取平均秩**。

    **为什么并列要取平均。** 两个候选分数一样时，它们的先后**没有区别**；给
    它们不同的秩，等于凭空造出一个次序来，算出来的相关性会偏高。
    """
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [Decimal(0)] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = Decimal(position + end + 2) / Decimal(2)
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def _pearson(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal:
    """皮尔逊相关 —— **在秩上算，就是 Spearman**。

    **分母为 0 算不出来**（某一侧全是同一个数）—— 抛错，交给调用方按"算不
    出"处理。
    """
    count = Decimal(len(xs))
    mean_x = sum(xs, Decimal(0)) / count
    mean_y = sum(ys, Decimal(0)) / count
    covariance = sum(
        ((xs[index] - mean_x) * (ys[index] - mean_y) for index in range(len(xs))),
        Decimal(0),
    )
    variance_x = sum(((value - mean_x) ** 2 for value in xs), Decimal(0))
    variance_y = sum(((value - mean_y) ** 2 for value in ys), Decimal(0))
    if variance_x <= 0 or variance_y <= 0:
        raise ValueError("某一侧全是同一个数，相关性算不出来")
    rho = covariance / (variance_x * variance_y).sqrt()
    return rho.quantize(CALIBRATION_PRECISION)


__all__ = [
    "CALIBRATED_FACTORS",
    "CALIBRATION_PRECISION",
    "CROSS_VALIDATION_FOLDS",
    "EXPONENT_STEP",
    "MAX_EXPONENT",
    "MAX_FIT_ROUNDS",
    "MINIMUM_CALIBRATION_SAMPLES",
    "MINIMUM_CORRELATION_POINTS",
    "MINIMUM_FOLDS",
    "MIN_EXPONENT",
    "SPEARMAN_THRESHOLD",
    "CalibrationModel",
    "CalibrationReport",
    "CalibrationSample",
    "FactorVector",
    "calibrate",
    "cross_validated_rho",
    "fit_model",
    "spearman_rho",
]
