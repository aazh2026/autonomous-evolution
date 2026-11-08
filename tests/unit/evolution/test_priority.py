"""TASK-P1-053 —— 六因子优先级评分。

四条 T1 对应四组用例：

1. **各因子输入 ∈ [0,1]，越界报错** —— 夹紧会让"我填了 1.5"和"我填了 1"
   变成同一件事，而那件事正是错的。
2. **``E`` 取 0 时不除零（走下限）** —— 除零的结果一旦流进排序，"NaN 排
   哪儿"就由实现细节决定了。
3. **因子全相等时排序稳定** —— 名次要由内容决定，不能由输入次序决定。
4. **已知输入的评分与手算值一致** —— 公式本身要能被手算复核。

外加两条**方向性断言**：提高 ``C``（置信度）排名上升，提高 ``E``（成本）
排名下降 —— 这两条比"分数等于某个常数"更能说明公式是活的。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.evolution.candidate import EvolutionCandidate
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.priority import (
    MINIMUM_COST,
    ActualOutcome,
    HeuristicCalibrator,
    PriorityFactors,
    ScoredCandidate,
    ValueCalibrator,
    apply_calibration,
    normalize_measure,
    normalize_priority,
    rank,
    raw_priority,
    risk_coefficient,
    score_candidate,
)
from core.evolution.taxonomy import OpportunityCategory, RiskLevel


def _factors(**overrides: Decimal | int | str) -> PriorityFactors:
    """一组默认因子 —— 全部 0.5，除特别说明外。"""
    data: dict[str, Decimal | int | str] = {
        "value": Decimal("0.8"),
        "impact": Decimal("0.6"),
        "confidence": Decimal("0.5"),
        "urgency": Decimal("0.5"),
        "cost": Decimal("0.5"),
        "risk": Decimal("0.5"),
    }
    data.update(overrides)
    return PriorityFactors.model_validate(data)


def _candidate(confidence: str = "0.82", risk: RiskLevel = RiskLevel.MEDIUM) -> EvolutionCandidate:
    return EvolutionCandidate(
        id="EVO-1024",
        category=OpportunityCategory.ARCHITECTURE,
        title="拆分用户模块中的权限逻辑",
        evidence=(Evidence(kind=EvidenceKind.TEST_REPORT, ref="artifact://report-001"),),
        hypothesis="拆分权限领域服务可以降低模块耦合。",
        expected_outcome=("降低模块依赖数量",),
        risk=risk,
        confidence=Decimal(confidence),
    )


class TestFactorsAreValidated:
    """T1 ① —— 六个因子都必须在 0~1，越界报错。"""

    @pytest.mark.parametrize("field", ["value", "impact", "confidence", "urgency", "cost", "risk"])
    @pytest.mark.parametrize("value", ["-0.01", "1.01"])
    def test_a_factor_outside_the_range_is_rejected(self, field: str, value: str) -> None:
        with pytest.raises(ValidationError):
            _factors(**{field: Decimal(value)})

    @pytest.mark.parametrize("field", ["value", "impact", "confidence", "urgency", "cost", "risk"])
    @pytest.mark.parametrize("value", ["0", "1", "0.5"])
    def test_a_factor_inside_the_range_is_accepted(self, field: str, value: str) -> None:
        factors = _factors(**{field: Decimal(value)})

        assert getattr(factors, field) == Decimal(value)

    def test_an_unknown_factor_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PriorityFactors.model_validate(
                {
                    "value": "0.8",
                    "impact": "0.6",
                    "confidence": "0.5",
                    "urgency": "0.5",
                    "cost": "0.5",
                    "risk": "0.5",
                    "effort": "2d",
                }
            )


class TestCostFloor:
    """T1 ② —— E 取 0 时不除零，走下限。"""

    def test_a_zero_cost_does_not_divide_by_zero(self) -> None:
        factors = _factors(cost=Decimal("0"))

        score = raw_priority(factors)

        assert score.is_finite()
        assert score == raw_priority(_factors(cost=MINIMUM_COST))

    def test_a_cost_below_the_floor_is_raised_to_the_floor(self) -> None:
        factors = _factors(cost=Decimal("0.01"))

        assert raw_priority(factors) == raw_priority(_factors(cost=MINIMUM_COST))

    def test_a_cost_above_the_floor_is_left_alone(self) -> None:
        factors = _factors(cost=Decimal("0.9"))

        assert raw_priority(factors) != raw_priority(_factors(cost=MINIMUM_COST))

    def test_the_normalized_score_of_a_zero_cost_is_still_within_the_unit_interval(
        self,
    ) -> None:
        factors = _factors(cost=Decimal("0"))

        score = score_candidate("EVO-1", factors).score

        assert Decimal("0") <= score <= Decimal("1")


class TestTheScoreMatchesTheHandCalculation:
    """T1 ④ —— 已知输入的评分与手算值一致。"""

    def test_the_raw_score_matches_the_hand_calculation(self) -> None:
        """0.8×0.6×0.5×0.5 = 0.12；0.5×(1+0.5) = 0.75；0.12 ÷ 0.75 = 0.16。"""
        factors = _factors()

        assert raw_priority(factors) == Decimal("0.16")

    def test_the_normalized_score_matches_the_hand_calculation(self) -> None:
        """归一化是乘以成本下限：0.16 × 0.1 = 0.016。"""
        factors = _factors()

        score = score_candidate("EVO-1", factors).score

        assert score == Decimal("0.016")

    def test_a_perfect_candidate_scores_one(self) -> None:
        """理论上限：分子 1、分母是成本下限 × 1 → 归一化后正好是 1。"""
        factors = PriorityFactors(
            value=Decimal("1"),
            impact=Decimal("1"),
            confidence=Decimal("1"),
            urgency=Decimal("1"),
            cost=Decimal("0"),
            risk=Decimal("0"),
        )

        assert score_candidate("EVO-1", factors).score == Decimal("1")

    def test_a_worthless_candidate_scores_zero(self) -> None:
        factors = _factors(value=Decimal("0"))

        assert score_candidate("EVO-1", factors).score == Decimal("0")

    def test_the_score_is_always_within_the_unit_interval(self) -> None:
        factors = _factors(value=Decimal("1"), impact=Decimal("1"), confidence=Decimal("1"))

        score = score_candidate("EVO-1", factors).score

        assert Decimal("0") <= score <= Decimal("1")

    def test_the_raw_score_may_exceed_one(self) -> None:
        """原始分**不**归一化 —— 归一化是另一件事。"""
        factors = _factors(
            value=Decimal("1"),
            impact=Decimal("1"),
            confidence=Decimal("1"),
            urgency=Decimal("1"),
            cost=Decimal("0"),
            risk=Decimal("0"),
        )

        assert raw_priority(factors) > Decimal("1")

    def test_a_non_terminating_score_is_kept_to_six_decimal_places(self) -> None:
        """1 ÷ 0.3 是无限小数 —— 28 位小数装进 Numeric(12,4) 时会被悄悄改掉。"""
        factors = PriorityFactors(
            value=Decimal("1"),
            impact=Decimal("1"),
            confidence=Decimal("1"),
            urgency=Decimal("1"),
            cost=Decimal("0.3"),
            risk=Decimal("0"),
        )

        assert score_candidate("EVO-1", factors).score == Decimal("0.333333")


class TestRankingIsStable:
    """T1 ③ —— 因子全相等时排序稳定。"""

    def test_tied_candidates_are_ordered_by_id(self) -> None:
        scored = [
            score_candidate("EVO-3", _factors()),
            score_candidate("EVO-1", _factors()),
            score_candidate("EVO-2", _factors()),
        ]

        assert [item.candidate_id for item in rank(scored)] == ["EVO-1", "EVO-2", "EVO-3"]

    def test_the_order_does_not_depend_on_the_input_order(self) -> None:
        """名次要由内容决定，不能由输入次序决定 —— 否则换个顺序就换个名次。"""
        first = [
            score_candidate("EVO-3", _factors()),
            score_candidate("EVO-1", _factors()),
            score_candidate("EVO-2", _factors()),
        ]
        second = list(reversed(first))

        assert [item.candidate_id for item in rank(first)] == [
            item.candidate_id for item in rank(second)
        ]

    def test_ranking_twice_gives_the_same_answer(self) -> None:
        scored = [score_candidate("EVO-2", _factors()), score_candidate("EVO-1", _factors())]

        assert rank(scored) == rank(scored)

    def test_a_higher_score_comes_first(self) -> None:
        scored = [
            score_candidate("EVO-low", _factors(value=Decimal("0.1"))),
            score_candidate("EVO-high", _factors(value=Decimal("0.9"))),
        ]

        assert [item.candidate_id for item in rank(scored)] == ["EVO-high", "EVO-low"]

    def test_the_ranking_spans_categories(self) -> None:
        """跨方案优先级判断（R-084）—— 排名不看分类。"""
        scored = [
            ScoredCandidate(
                candidate_id="product-a",
                category=OpportunityCategory.PRODUCT,
                factors=_factors(value=Decimal("0.9")),
            ),
            ScoredCandidate(
                candidate_id="architecture-a",
                category=OpportunityCategory.ARCHITECTURE,
                factors=_factors(value=Decimal("0.3")),
            ),
        ]

        assert [item.candidate_id for item in rank(scored)] == [
            "product-a",
            "architecture-a",
        ]


class TestDirectionalAssertions:
    """方向性断言 —— 提高置信度排名上升，提高成本排名下降。"""

    def test_a_higher_confidence_ranks_higher(self) -> None:
        low = score_candidate("EVO-low", _factors(confidence=Decimal("0.2")))
        high = score_candidate("EVO-high", _factors(confidence=Decimal("0.9")))

        assert [item.candidate_id for item in rank([low, high])] == ["EVO-high", "EVO-low"]

    def test_a_higher_cost_ranks_lower(self) -> None:
        cheap = score_candidate("EVO-cheap", _factors(cost=Decimal("0.2")))
        pricey = score_candidate("EVO-pricey", _factors(cost=Decimal("0.9")))

        assert [item.candidate_id for item in rank([cheap, pricey])] == ["EVO-cheap", "EVO-pricey"]

    def test_a_higher_value_ranks_higher(self) -> None:
        low = score_candidate("EVO-low", _factors(value=Decimal("0.2")))
        high = score_candidate("EVO-high", _factors(value=Decimal("0.9")))

        assert [item.candidate_id for item in rank([low, high])] == ["EVO-high", "EVO-low"]

    def test_a_higher_risk_ranks_lower(self) -> None:
        safe = score_candidate("EVO-safe", _factors(risk=Decimal("0.1")))
        risky = score_candidate("EVO-risky", _factors(risk=Decimal("0.9")))

        assert [item.candidate_id for item in rank([safe, risky])] == ["EVO-safe", "EVO-risky"]


class TestRiskCoefficients:
    """档 → 系数的换算。"""

    def test_the_three_levels_map_to_three_coefficients(self) -> None:
        assert risk_coefficient(RiskLevel.LOW) == Decimal("0.2")
        assert risk_coefficient(RiskLevel.MEDIUM) == Decimal("0.5")
        assert risk_coefficient(RiskLevel.HIGH) == Decimal("0.8")

    def test_the_coefficients_stay_within_the_unit_interval(self) -> None:
        for level in RiskLevel:
            assert Decimal("0") <= risk_coefficient(level) <= Decimal("1")

    def test_the_risk_spread_is_deliberately_narrow(self) -> None:
        """风险该影响次序，不该把次序整个翻过来 —— 最高与最低差 1.5 倍。"""
        low = risk_coefficient(RiskLevel.LOW)
        high = risk_coefficient(RiskLevel.HIGH)

        assert high / low == Decimal("4")


class TestFactorsFromACandidate:
    """因子可以从候选装配 —— 置信度取候选自己的，风险档换成系数。"""

    def test_the_confidence_comes_from_the_candidate(self) -> None:
        factors = PriorityFactors.from_candidate(
            _candidate(confidence="0.82"),
            value=Decimal("0.8"),
            impact=Decimal("0.6"),
            urgency=Decimal("0.5"),
            cost=Decimal("0.5"),
        )

        assert factors.confidence == Decimal("0.82")

    def test_the_risk_level_becomes_a_coefficient(self) -> None:
        factors = PriorityFactors.from_candidate(
            _candidate(risk=RiskLevel.HIGH),
            value=Decimal("0.8"),
            impact=Decimal("0.6"),
            urgency=Decimal("0.5"),
            cost=Decimal("0.5"),
        )

        assert factors.risk == Decimal("0.8")

    def test_an_out_of_range_assembly_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PriorityFactors.from_candidate(
                _candidate(),
                value=Decimal("1.5"),
                impact=Decimal("0.6"),
                urgency=Decimal("0.5"),
                cost=Decimal("0.5"),
            )


class TestNormalizeMeasure:
    """指标归一化 —— 把原始量纲压到 0~1。"""

    def test_a_value_at_the_maximum_maps_to_one(self) -> None:
        assert normalize_measure(Decimal("10"), Decimal("10")) == Decimal("1")

    def test_a_value_beyond_the_maximum_is_capped(self) -> None:
        assert normalize_measure(Decimal("20"), Decimal("10")) == Decimal("1")

    def test_a_value_below_zero_maps_to_zero(self) -> None:
        assert normalize_measure(Decimal("-1"), Decimal("10")) == Decimal("0")

    def test_a_half_of_the_maximum_maps_to_a_half(self) -> None:
        assert normalize_measure(Decimal("5"), Decimal("10")) == Decimal("0.5")

    def test_a_zero_maximum_is_rejected(self) -> None:
        """用 0 当除数会让归一化失去意义 —— 那是输入错了，不是算不出来。"""
        with pytest.raises(ValueError, match="归一化的上限必须为正"):
            normalize_measure(Decimal("1"), Decimal("0"))

    def test_a_negative_maximum_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="归一化的上限必须为正"):
            normalize_measure(Decimal("1"), Decimal("-1"))


class TestNormalizePriority:
    """分数归一化 —— 单调缩放，不改次序。"""

    def test_the_theoretical_maximum_maps_to_one(self) -> None:
        assert normalize_priority(Decimal("10")) == Decimal("1")

    def test_a_negative_raw_maps_to_zero(self) -> None:
        assert normalize_priority(Decimal("-1")) == Decimal("0")

    def test_the_normalization_does_not_change_the_order(self) -> None:
        """归一化是单调的 —— 名次不能被它动。"""
        low = normalize_priority(Decimal("0.1"))
        high = normalize_priority(Decimal("0.5"))

        assert low < high

    def test_the_normalization_is_multiplied_by_the_cost_floor(self) -> None:
        assert normalize_priority(Decimal("0.32")) == Decimal("0.32") * MINIMUM_COST

    def test_a_raw_score_beyond_the_maximum_is_capped(self) -> None:
        """原始分理论上到不了 10 以上 —— 但这个函数是公开的，得自己兜住。"""
        assert normalize_priority(Decimal("20")) == Decimal("1")


class TestTheCalibrationSeam:
    """阶段四的校准口子 —— 现在只有一个恒等实现。"""

    def test_a_blank_candidate_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="候选 id 不能是空白"):
            ScoredCandidate(candidate_id="   ", factors=_factors())

    def test_an_out_of_range_realized_value_is_rejected(self) -> None:
        """实际收益要与 V 同一量纲 —— 超出 0~1 就没法跟当初的判断比。"""
        with pytest.raises(ValidationError):
            ActualOutcome(candidate_id="EVO-1", realized_value=Decimal("1.5"))

    def test_a_blank_outcome_candidate_id_is_rejected(self) -> None:
        """校准样本的 id 也要非空 —— 否则对不上是哪个候选的结果。"""
        with pytest.raises(ValidationError, match="候选 id 不能是空白"):
            ActualOutcome(candidate_id="   ", realized_value=Decimal("0.3"))

    def test_the_default_calibrator_changes_nothing(self) -> None:
        calibrator = HeuristicCalibrator()
        factors = _factors()
        outcome = ActualOutcome(candidate_id="EVO-1", realized_value=Decimal("0.3"))

        assert calibrator.calibrate(factors, outcome) == factors

    def test_the_default_calibrator_satisfies_the_interface(self) -> None:
        assert isinstance(HeuristicCalibrator(), ValueCalibrator)

    def test_calibrating_keeps_the_order_when_nothing_changes(self) -> None:
        scored = [score_candidate("EVO-1", _factors()), score_candidate("EVO-2", _factors())]
        outcomes = {"EVO-1": ActualOutcome(candidate_id="EVO-1", realized_value=Decimal("0.3"))}

        result = apply_calibration(HeuristicCalibrator(), scored, outcomes)

        assert [item.candidate_id for item in result] == ["EVO-1", "EVO-2"]

    def test_a_candidate_without_an_outcome_keeps_its_factors(self) -> None:
        scored = [score_candidate("EVO-1", _factors())]

        result = apply_calibration(HeuristicCalibrator(), scored, {})

        assert result[0].factors == scored[0].factors

    def test_a_calibrator_that_skips_a_sample_leaves_the_factors_alone(self) -> None:
        """返回 None 是"这次不参与校准"，与"校准后仍是原值"不是一回事。"""

        class SkippingCalibrator:
            def calibrate(
                self, factors: PriorityFactors, outcome: ActualOutcome
            ) -> PriorityFactors | None:
                # 没有记录说明，判为信息不足，不参与校准。
                if not outcome.note:
                    return None
                return factors

        scored = [score_candidate("EVO-1", _factors())]
        outcomes = {"EVO-1": ActualOutcome(candidate_id="EVO-1", realized_value=Decimal("0.3"))}

        result = apply_calibration(SkippingCalibrator(), scored, outcomes)

        assert result[0].factors == scored[0].factors

    def test_a_calibrator_that_changes_the_value_changes_the_order(self) -> None:
        """口子是真的通的 —— 换个校准器，名次就会变。"""

        class RewardingCalibrator:
            def calibrate(
                self, factors: PriorityFactors, outcome: ActualOutcome
            ) -> PriorityFactors | None:
                # 把"实际拿到了多少"直接当成价值 —— 于是名次跟着实际结果走。
                return factors.model_copy(update={"value": outcome.realized_value})

        scored = [
            score_candidate("EVO-1", _factors(value=Decimal("0.2"))),
            score_candidate("EVO-2", _factors(value=Decimal("0.3"))),
        ]
        outcomes = {
            "EVO-1": ActualOutcome(candidate_id="EVO-1", realized_value=Decimal("0.9")),
        }

        result = apply_calibration(RewardingCalibrator(), scored, outcomes)

        assert [item.candidate_id for item in result] == ["EVO-1", "EVO-2"]
