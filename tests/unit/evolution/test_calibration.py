"""TASK-P3-014 —— 价值模型的校准：把"哪个因子值得信"学出来。

**测的是三件事**：算得对（秩相关与幂变换）、退得对（三条不通的路各有各的
理由）、以及**"算不出"与"算出来是 0"分得开**。

**最后那一条是这一层最要紧的。** 门槛是拿来比大小的 —— "算不出"被当成 0 的
话，结论会变成"不算好"，听起来一样，**但补救动作完全相反**：一个是"再攒点
数据"，一个是"换模型"。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.evolution.calibration import (
    CROSS_VALIDATION_FOLDS,
    MINIMUM_CALIBRATION_SAMPLES,
    SPEARMAN_THRESHOLD,
    CalibrationModel,
    CalibrationSample,
    FactorVector,
    calibrate,
    cross_validated_rho,
    fit_model,
    spearman_rho,
)
from core.evolution.priority import PriorityFactors, raw_priority


def _factors(**overrides: Decimal) -> PriorityFactors:
    """造一组因子 —— 没给的就用中间值。"""
    base = {
        "value": Decimal("0.50"),
        "impact": Decimal("0.50"),
        "confidence": Decimal("0.80"),
        "urgency": Decimal("0.50"),
        "cost": Decimal("0.30"),
        "risk": Decimal("0.20"),
    }
    return PriorityFactors(**{**base, **overrides})


def _sample(index: int, realized: Decimal, **overrides: Decimal) -> CalibrationSample:
    """造一条样本 —— 候选 id 按序号排。"""
    return CalibrationSample(
        candidate_id=f"c{index:02d}",
        factors=_factors(**overrides),
        realized_value=realized,
    )


def _misled_samples() -> tuple[CalibrationSample, ...]:
    """一批样本 —— **实际收益跟着价值走，紧迫程度反着走**。

    **启发式模型在这里一定会被骗。** 六个因子相乘，而紧迫程度与收益反着来 ——
    于是"越急的事越该先做"这条直觉把次序带偏。校准该学到的正是"紧迫程度
    不该参与排序"（指数 0）。
    """
    values = ("0.20", "0.30", "0.40", "0.50", "0.60", "0.70", "0.80", "0.90")
    samples = []
    for index, text in enumerate(values):
        value = Decimal(text)
        samples.append(
            _sample(
                index,
                realized=value,
                value=value,
                urgency=Decimal(1) - value,
            )
        )
    return tuple(samples)


class TestTheRankCorrelation:
    """秩相关算得对。"""

    def test_a_perfect_order_gives_one(self) -> None:
        """**完全同序 → ρ = 1。**"""
        rho = spearman_rho(
            [Decimal(1), Decimal(2), Decimal(3)],
            [Decimal(7), Decimal(8), Decimal(9)],
        )
        assert rho == Decimal("1.000000")

    def test_a_reversed_order_gives_minus_one(self) -> None:
        """**完全反序 → ρ = -1。**"""
        rho = spearman_rho(
            [Decimal(1), Decimal(2), Decimal(3)],
            [Decimal(9), Decimal(8), Decimal(7)],
        )
        assert rho == Decimal("-1.000000")

    def test_ties_get_the_average_rank(self) -> None:
        """**并列取平均秩** —— 两个候选分数一样时，先后没有区别。

        给它们不同的秩，等于凭空造出一个次序来，算出来的相关性会偏高。
        """
        rho = spearman_rho(
            [Decimal(1), Decimal(2), Decimal(2), Decimal(3)],
            [Decimal(1), Decimal(2), Decimal(3), Decimal(4)],
        )
        assert rho == Decimal("0.948683")

    def test_two_different_lengths_are_refused(self) -> None:
        """**两组数不一样长就没法算** —— 报错，不是算出一个数。"""
        with pytest.raises(ValueError, match="一样长"):
            spearman_rho([Decimal(1)], [Decimal(1), Decimal(2)])

    def test_one_point_is_refused(self) -> None:
        """**一个点算不出相关性。**"""
        with pytest.raises(ValueError, match="少于两个点"):
            spearman_rho([Decimal(1)], [Decimal(1)])

    def test_a_flat_side_is_refused(self) -> None:
        """**某一侧全是同一个数 → 相关性不存在**，不是 0。

        这是本层最要紧的一条：**"算不出"与"算出来是 0"必须分开**。
        """
        with pytest.raises(ValueError, match="同一个数"):
            spearman_rho(
                [Decimal(1), Decimal(1), Decimal(1)],
                [Decimal(1), Decimal(2), Decimal(3)],
            )


class TestTheModel:
    """校准出来的模型。"""

    def test_the_identity_model_changes_nothing(self) -> None:
        """**指数全是 1 就是启发式模型** —— 一个因子都不许动。"""
        model = CalibrationModel(
            exponents=FactorVector(*(Decimal(1) for _ in range(5))),
            means=FactorVector(*(Decimal("0.5") for _ in range(5))),
            samples=8,
        )
        factors = _factors(value=Decimal("0.7"), urgency=Decimal("0.2"))
        assert model.apply(factors) == factors

    def test_the_risk_coefficient_is_carried_through(self) -> None:
        """**风险不参与校准** —— 它原样带走。

        风险只有三档，拿它当连续因子去拟合指数会过拟合；而且它的作用是**惩
        罚**，不是"值不值得"。
        """
        model = CalibrationModel(
            exponents=FactorVector(*(Decimal(1) for _ in range(5))),
            means=FactorVector(*(Decimal("0.5") for _ in range(5))),
            samples=8,
        )
        factors = _factors(risk=Decimal("0.8"))
        assert model.apply(factors).risk == Decimal("0.8")

    def test_an_exponent_of_zero_flattens_the_factor(self) -> None:
        """**指数 0 → 这个因子不参与排序** —— 所有人都被压成同一个均值。"""
        model = CalibrationModel(
            exponents=FactorVector(
                value=Decimal(1),
                impact=Decimal(1),
                confidence=Decimal(1),
                urgency=Decimal(0),
                cost=Decimal(1),
            ),
            means=FactorVector(*(Decimal("0.5") for _ in range(5))),
            samples=8,
        )
        low = model.apply(_factors(urgency=Decimal("0.1")))
        high = model.apply(_factors(urgency=Decimal("0.9")))
        assert low.urgency == high.urgency == Decimal("0.5")

    def test_the_transformed_factor_stays_in_range(self) -> None:
        """**变换后的因子还在 0~1 之间** —— 因子有值域，出了值域就是非法数据。"""
        model = CalibrationModel(
            exponents=FactorVector(
                value=Decimal("1.75"),
                impact=Decimal(1),
                confidence=Decimal(1),
                urgency=Decimal(1),
                cost=Decimal(1),
            ),
            means=FactorVector(*(Decimal("0.5") for _ in range(5))),
            samples=8,
        )
        for text in ("0.01", "0.5", "0.99"):
            transformed = model.apply(_factors(value=Decimal(text)))
            assert Decimal(0) <= transformed.value <= Decimal(1), text


class TestTheFit:
    """拟合出来的指数。"""

    def test_it_finds_the_factor_that_lies(self) -> None:
        """**骗人的那个因子该被学出来** —— 它的指数该降到 0。

        紧迫程度与实际收益反着来，那么"这个因子不该参与排序"就是学到的结论。
        """
        model = fit_model(_misled_samples())
        assert model.exponents.urgency == Decimal(0)

    def test_it_never_fits_worse_than_the_heuristic(self) -> None:
        """**拟合的起点就是启发式模型** —— 所以结果不会比它差。

        除非网格里根本没有更好的点。
        """
        samples = _misled_samples()
        heuristic = spearman_rho(
            [raw_priority(item.factors) for item in samples],
            [item.realized_value for item in samples],
        )
        model = fit_model(samples)
        fitted = spearman_rho(
            [raw_priority(model.apply(item.factors)) for item in samples],
            [item.realized_value for item in samples],
        )
        assert fitted >= heuristic

    def test_it_refuses_to_fit_without_samples(self) -> None:
        """**没有样本就没法拟合** —— 报错，不是返回一个"大概"的模型。"""
        with pytest.raises(ValueError, match="没有样本"):
            fit_model(())

    def test_a_tie_prefers_the_more_conservative_exponent(self) -> None:
        """**数据没给出偏好时，一个因子都不该动。**

        这批样本里只有价值在变，其余因子都是常数 —— 于是价值取什么指数，次序
        都一样，**数据没有给出偏好**。拟合却得挑一个：**挑更保守的那个**，
        也就是指数 1（就是启发式模型）。

        **不挑保守的那个会怎样**：拟合落在网格里先扫到的那个 —— **一个任意的
        改动看起来像是有依据的改动，而它其实没有。**
        """
        samples = tuple(
            _sample(
                index,
                realized=Decimal("0.20") + Decimal("0.10") * index,
                value=Decimal("0.20") + Decimal("0.10") * index,
                urgency=Decimal("0.50"),
            )
            for index in range(8)
        )
        model = fit_model(samples)
        assert model.exponents.value == Decimal(1)
        assert model.exponents.urgency == Decimal(1)
        assert model.exponents.cost == Decimal(1)


class TestTheHoldOut:
    """留出法量出来的相关性。"""

    def test_both_sides_are_measured(self) -> None:
        """**启发式的与校准后的各量一个** —— 没有"校准前"就无从比较。"""
        before, after = cross_validated_rho(_misled_samples())
        assert before is not None
        assert after is not None

    def test_the_calibrated_side_is_the_better_one(self) -> None:
        """**被骗的数据上，校准后的相关性更高** —— 这就是它存在的理由。"""
        before, after = cross_validated_rho(_misled_samples())
        assert after is not None and before is not None
        assert after > before

    def test_one_fold_is_refused(self) -> None:
        """**一折没法留出** —— 报错。"""
        with pytest.raises(ValueError, match="两折"):
            cross_validated_rho(_misled_samples(), folds=1)

    def test_too_few_samples_measure_nothing(self) -> None:
        """**样本太少就量不出** —— 是 ``None``，不是 0。"""
        before, after = cross_validated_rho(_misled_samples()[:1])
        assert before is None
        assert after is None


class TestTheGate:
    """三条不通的路，三条都要说清理由。"""

    def test_too_few_samples_are_refused(self) -> None:
        """**样本不足 → 不校准** —— 用启发式模型。"""
        model, report = calibrate(_misled_samples()[:3])
        assert model is None
        assert report.accepted is False
        assert "样本不足" in report.reason

    def test_a_flat_outcome_cannot_be_measured(self) -> None:
        """**实际收益全一样 → 相关性算不出来** → 不校准。

        这是"算不出"与"算出来是 0"分开的落点：这里 ρ 是 ``None``。
        """
        samples = tuple(
            _sample(index, realized=Decimal("0.5"), value=Decimal("0.2") + Decimal("0.1") * index)
            for index in range(MINIMUM_CALIBRATION_SAMPLES)
        )
        model, report = calibrate(samples)
        assert model is None
        assert report.accepted is False
        assert report.rho_after is None
        assert "算不出" in report.reason

    def test_a_weak_correlation_falls_back(self) -> None:
        """**相关性没到门槛 → 退回启发式模型，并记下这件事。**

        这批样本里，实际收益的秩与两个因子的秩**都不相关** —— 无论把价值
        还是紧迫程度放大，排序都救不回来。**所以该退回。**
        """
        realized = ("0.40", "0.70", "0.10", "0.80", "0.20", "0.50", "0.30", "0.60")
        samples = tuple(
            _sample(
                index,
                realized=Decimal(realized[index]),
                value=Decimal("0.20") + Decimal("0.05") * index,
                urgency=Decimal("0.90") - Decimal("0.05") * index,
            )
            for index in range(MINIMUM_CALIBRATION_SAMPLES)
        )
        model, report = calibrate(samples)
        assert model is None
        assert report.accepted is False
        assert report.rho_after is not None
        assert report.rho_after <= SPEARMAN_THRESHOLD
        assert "退回启发式模型" in report.reason

    def test_a_strong_correlation_is_accepted(self) -> None:
        """**过了门槛就用校准后的模型。**"""
        model, report = calibrate(_misled_samples())
        assert model is not None
        assert report.accepted is True
        assert report.rho_after is not None
        assert report.rho_after > SPEARMAN_THRESHOLD
        assert "过了门槛" in report.reason

    def test_the_threshold_is_the_one_we_said(self) -> None:
        """**门槛就是声明的那个数** —— 不是别的数。"""
        _model, report = calibrate(_misled_samples())
        assert report.threshold == SPEARMAN_THRESHOLD

    def test_the_fold_count_is_the_one_we_said(self) -> None:
        """**折数也是声明的那个数** —— 4 折。"""
        assert CROSS_VALIDATION_FOLDS == 4
