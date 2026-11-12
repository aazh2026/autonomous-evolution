"""自主演进策略调优实体与统计检验证据 —— **改策略要有实证，改错要退得回**。

设计依据：
- docs/tasks.md ``TASK-P4-050``；
- R-033（价值模型须用实际收益校准）；
- R-121（策略调整必须有据可依并在无收益时拒绝/退回）；
- TASK-P4-070（策略参数属于核心策略，重大修改需走 SystemEvolutionProposal）。

核心不变量：
1. **参数必须有界有型**：探索步长、容差、因子指数不能任意漂移，超出安全范围建不出来。
2. **比较要有实证**：必须对比基线策略与候选调优策略实测表现，
   并给出 Welch's t-test 统计显著性凭证（p < 0.05 且效果量 Delta > 0）。
3. **显著性不足必须拒绝**：未达显著性门槛时，执行「不采纳也是合法决策」，原样退回基线。
4. **冻结不可变与审计留痕**：策略与优化报告一经生成不可篡改，变更记录具备防篡改哈希。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from core.evolution.calibration import (
    CALIBRATION_PRECISION,
    FactorVector,
)
from core.evolution.common import content_digest
from core.policy.scheduling import CONFIDENCE_Z, DEFAULT_TOLERANCE, SchedulingPolicy

#: 统计显著性水平阈值 alpha = 0.05
SIGNIFICANCE_ALPHA: Final[Decimal] = Decimal("0.05")

#: 最少样本试次（避免小样本伪显著）
MINIMUM_EVALUATION_CYCLES: Final[int] = 6

#: t-test 最小样本量
_MIN_SAMPLE_SIZE: Final[int] = 3

#: 自由度极小安全阈值
_MIN_DF_FOR_Z: Final[float] = 2.0

#: 策略参数调节步长硬边界
MIN_TOLERANCE: Final[Decimal] = Decimal("0.01")
MAX_TOLERANCE: Final[Decimal] = Decimal("0.25")
MIN_EXPLORE_EVERY: Final[int] = 1
MAX_EXPLORE_EVERY: Final[int] = 50


def default_factor_exponents() -> FactorVector:
    """默认的一组因子指数（恒等指数 1.0）。"""
    one = Decimal("1.0")
    return FactorVector(value=one, impact=one, confidence=one, urgency=one, cost=one)


@dataclass(frozen=True, slots=True)
class TunableParameters:
    """可调度的自主演进核心策略参数集。

    :param tolerance: 调度时达成度下界与最优解允许的容差。
    :param explore_every: 调度器每多少轮强制探索一次冷门组合（0 表示不探索）。
    :param factor_exponents: 六因子优先级排序的幂变换指数向量（V, I, C, U, E）。
    :param min_confidence_gate: 候选立项的置信度硬门槛（低于此值一律 Defer）。
    """

    tolerance: Decimal = DEFAULT_TOLERANCE
    explore_every: int = 5
    factor_exponents: FactorVector = field(default_factory=default_factor_exponents)
    min_confidence_gate: Decimal = Decimal("0.30")

    def __post_init__(self) -> None:
        if not (MIN_TOLERANCE <= self.tolerance <= MAX_TOLERANCE):
            raise ValueError(
                f"容差 tolerance={self.tolerance} 超出安全边界 [{MIN_TOLERANCE}, {MAX_TOLERANCE}]"
            )
        is_explore_valid = self.explore_every == 0 or (
            MIN_EXPLORE_EVERY <= self.explore_every <= MAX_EXPLORE_EVERY
        )
        if not is_explore_valid:
            raise ValueError(
                f"探索周期 explore_every={self.explore_every} "
                f"超出安全边界 [{MIN_EXPLORE_EVERY}, {MAX_EXPLORE_EVERY}]"
            )
        if not (Decimal("0.10") <= self.min_confidence_gate <= Decimal("0.90")):
            raise ValueError(
                f"立项置信度门槛 min_confidence_gate={self.min_confidence_gate} "
                "超出安全边界 [0.10, 0.90]"
            )

    def to_scheduling_policy(self) -> SchedulingPolicy:
        """把参数投影成调度器实际执行的规矩。"""
        return SchedulingPolicy(
            tolerance=self.tolerance,
            explore_every=self.explore_every,
            z=CONFIDENCE_Z,
        )

    def digest(self) -> str:
        """计算这组参数的规范化哈希摘要。"""
        payload = {
            "tolerance": str(self.tolerance),
            "explore_every": self.explore_every,
            "min_confidence_gate": str(self.min_confidence_gate),
            "exponents": {
                "value": str(self.factor_exponents.value),
                "impact": str(self.factor_exponents.impact),
                "confidence": str(self.factor_exponents.confidence),
                "urgency": str(self.factor_exponents.urgency),
                "cost": str(self.factor_exponents.cost),
            },
        }
        return content_digest(payload)


@dataclass(frozen=True, slots=True)
class CycleOutcomeMeasurement:
    """一个演进周期执行后量出的指标切片。

    :param cycle_id: 周期编号。
    :param attainment_gain: 本轮目标达成度差值（Δ Attainment），越大越好。
    :param cost_usd: 本轮总成本花费。
    :param task_success_rate: 任务通过率（0.0 ~ 1.0）。
    """

    cycle_id: str
    attainment_gain: Decimal
    cost_usd: Decimal
    task_success_rate: Decimal

    @property
    def cost_efficiency(self) -> Decimal:
        """单位成本收益比（增益 / 成本）。若成本极小，按保底下限折算。"""
        denom = max(self.cost_usd, Decimal("0.0001"))
        return (self.attainment_gain / denom).quantize(CALIBRATION_PRECISION)


@dataclass(frozen=True, slots=True)
class StatisticalComparison:
    """策略对比的统计显著性报告。

    :param baseline_mean: 基线均值。
    :param candidate_mean: 调优候选均值。
    :param absolute_gain: 绝对增益（candidate - baseline）。
    :param relative_gain: 相对提升比例（%）。
    :param t_statistic: t 检验统计量。
    :param p_value: 双侧 p-value（显著性水平）。
    :param is_significant: 是否达到统计显著且为正收益（p < 0.05 且 gain > 0）。
    :param details: 文字解释。
    """

    baseline_mean: Decimal
    candidate_mean: Decimal
    absolute_gain: Decimal
    relative_gain: Decimal
    t_statistic: Decimal
    p_value: Decimal
    is_significant: bool
    details: str


def compare_measurements(
    baseline: Sequence[Decimal],
    candidate: Sequence[Decimal],
    *,
    alpha: Decimal = SIGNIFICANCE_ALPHA,
) -> StatisticalComparison:
    """对比两组独立实测样本，进行 Welch's t-test 显著性检验。"""
    n1 = len(baseline)
    n2 = len(candidate)
    if n1 < _MIN_SAMPLE_SIZE or n2 < _MIN_SAMPLE_SIZE:
        return StatisticalComparison(
            baseline_mean=Decimal("0"),
            candidate_mean=Decimal("0"),
            absolute_gain=Decimal("0"),
            relative_gain=Decimal("0"),
            t_statistic=Decimal("0"),
            p_value=Decimal("1.0"),
            is_significant=False,
            details=f"样本量过少 (n1={n1}, n2={n2} < {_MIN_SAMPLE_SIZE})，不具备统计推断依据",
        )

    mean1 = sum(baseline, Decimal("0")) / Decimal(n1)
    mean2 = sum(candidate, Decimal("0")) / Decimal(n2)
    gain = mean2 - mean1

    rel_gain = (
        (gain / abs(mean1) * Decimal(100)).quantize(CALIBRATION_PRECISION)
        if mean1 != Decimal(0)
        else Decimal("0")
    )

    var1 = sum(((x - mean1) ** 2 for x in baseline), Decimal("0")) / Decimal(n1 - 1)
    var2 = sum(((x - mean2) ** 2 for x in candidate), Decimal("0")) / Decimal(n2 - 1)

    se1 = var1 / Decimal(n1)
    se2 = var2 / Decimal(n2)
    se_diff = (se1 + se2).sqrt()

    if se_diff == Decimal(0):
        is_sig = gain > Decimal(0)
        p_val = Decimal("0.0001") if is_sig else Decimal("1.0")
        return StatisticalComparison(
            baseline_mean=mean1.quantize(CALIBRATION_PRECISION),
            candidate_mean=mean2.quantize(CALIBRATION_PRECISION),
            absolute_gain=gain.quantize(CALIBRATION_PRECISION),
            relative_gain=rel_gain,
            t_statistic=Decimal("0"),
            p_value=p_val,
            is_significant=is_sig,
            details="两组内部方差为零，直接根据确定性均值判定",
        )

    t_stat = gain / se_diff
    num_df = (se1 + se2) ** 2
    den_df = ((se1**2) / Decimal(n1 - 1)) + ((se2**2) / Decimal(n2 - 1))
    df = float(num_df / den_df) if den_df > Decimal(0) else float(n1 + n2 - 2)

    t_val = float(t_stat)
    p_val_float = _approx_two_tailed_p(t_val, df)
    p_value = Decimal(str(round(p_val_float, 6)))

    is_significant = (p_value <= alpha) and (gain > Decimal(0))
    details = (
        f"t={t_stat:.4f}, p={p_value:.6f}, 均值从 {mean1:.4f} 提升至 {mean2:.4f} "
        f"(Δ={gain:.4f}, {rel_gain}%); {'显著提升' if is_significant else '未达显著性或为负增益'}"
    )

    return StatisticalComparison(
        baseline_mean=mean1.quantize(CALIBRATION_PRECISION),
        candidate_mean=mean2.quantize(CALIBRATION_PRECISION),
        absolute_gain=gain.quantize(CALIBRATION_PRECISION),
        relative_gain=rel_gain,
        t_statistic=t_stat.quantize(CALIBRATION_PRECISION),
        p_value=p_value,
        is_significant=is_significant,
        details=details,
    )


def _approx_two_tailed_p(t: float, df: float) -> float:
    """近似计算 Student-t 的双侧 p 值。"""
    abs_t = abs(t)
    if abs_t == 0.0:
        return 1.0
    z = abs_t / math.sqrt(df / (df - _MIN_DF_FOR_Z)) if df > _MIN_DF_FOR_Z else abs_t
    norm_cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    two_tailed = 2.0 * (1.0 - norm_cdf)
    return max(min(two_tailed, 1.0), 0.000001)


@dataclass(frozen=True, slots=True)
class OptimizationReport:
    """策略优化调优整体决策报告。

    :param baseline_parameters: 原基线参数。
    :param candidate_parameters: 调优后候选参数。
    :param accepted: 是否决定采纳并切换策略。
    :param comparison: 统计对比证据。
    :param rationale: 人话解释与审计声明。
    """

    baseline_parameters: TunableParameters
    candidate_parameters: TunableParameters
    accepted: bool
    comparison: StatisticalComparison
    rationale: str

    @property
    def baseline_digest(self) -> str:
        return self.baseline_parameters.digest()

    @property
    def candidate_digest(self) -> str:
        return self.candidate_parameters.digest()


__all__ = [
    "MAX_EXPLORE_EVERY",
    "MAX_TOLERANCE",
    "MINIMUM_EVALUATION_CYCLES",
    "MIN_EXPLORE_EVERY",
    "MIN_TOLERANCE",
    "SIGNIFICANCE_ALPHA",
    "CycleOutcomeMeasurement",
    "OptimizationReport",
    "StatisticalComparison",
    "TunableParameters",
    "compare_measurements",
    "default_factor_exponents",
]
