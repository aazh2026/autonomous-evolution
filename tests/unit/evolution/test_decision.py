"""TASK-P1-054 —— 决策三态与"不行动"的强制落地。

四条 T1 对应四组用例：

1. **``confidence = 0.1`` 的候选 → defer 决策 + 理由 + 不创建 Task**
   —— "系统说得出不做"，而且不做的当轮不留下任何任务。
2. **defer 决策包含至少一条具体的补充分析项** —— "以后再说"要说清楚
   还要补什么，否则它就是个不用负责的垃圾桶。
3. **reject 必须带 rationale** —— 放弃也要说清楚为什么。
4. **T2**：不行动决策占决策总数的比例被记录下来，且"一条都没有"会被
   明确报成闭环不健康。
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Table

from core.entities.evolution import EvolutionDecision as DecisionEntity
from core.evolution.candidate import EvolutionCandidate
from core.evolution.decision import (
    AnalysisKind,
    DecisionPolicy,
    DecisionRound,
    DecisionState,
    EvolutionDecision,
    FollowUpAnalysis,
    NoActionSignal,
    decide_round,
)
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel

#: 决策实体的表 —— 用来核对领域枚举与数据库 CHECK 约束有没有漂移。
_DECISION_TABLE = cast("Table", DecisionEntity.__table__)


def _check_constraints() -> list[str]:
    """决策实体上的 CHECK 约束文本。"""
    return [
        str(constraint.sqltext)
        for constraint in _DECISION_TABLE.constraints
        if isinstance(constraint, CheckConstraint)
    ]


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


def _factors(**overrides: Decimal | str) -> PriorityFactors:
    """一组默认因子 —— 全部 0.5，除特别说明外。"""
    data: dict[str, Decimal | str] = {
        "value": Decimal("0.8"),
        "impact": Decimal("0.6"),
        "confidence": Decimal("0.5"),
        "urgency": Decimal("0.5"),
        "cost": Decimal("0.5"),
        "risk": Decimal("0.5"),
    }
    data.update(overrides)
    return PriorityFactors.model_validate(data)


def _scored(candidate_id: str = "EVO-1024", **overrides: Decimal | str) -> ScoredCandidate:
    return ScoredCandidate(candidate_id=candidate_id, factors=_factors(**overrides))


def _decision(**overrides: object) -> EvolutionDecision:
    data: dict[str, object] = {
        "id": "EVD-1",
        "candidate_id": "EVO-1024",
        "decision": DecisionState.RECORD,
        "rationale": "证据充分，成本可控",
        "factors": _factors(),
    }
    data.update(overrides)
    if data["decision"] is DecisionState.DEFER and "requested_analysis" not in data:
        data["requested_analysis"] = (
            FollowUpAnalysis(
                kind=AnalysisKind.MEASUREMENT,
                description="跑一次依赖分析并与基线对比",
            ),
        )
    return EvolutionDecision.model_validate(data)


class TestTheThreeStates:
    """R-035 —— Record / Defer / Reject 三态。"""

    def test_there_are_exactly_three_states(self) -> None:
        assert len(DecisionState) == 3

    def test_the_state_values_match_the_database_constraint(self) -> None:
        """领域枚举与数据库 CHECK 约束必须对得上 —— 两边漂移会有一边收不住。"""
        texts = _check_constraints()

        state_constraint = next(text for text in texts if text.startswith("decision IN"))
        for state in DecisionState:
            assert f"'{state.value}'" in state_constraint

    def test_the_database_also_requires_analysis_for_defer(self) -> None:
        """领域模型与数据库都要求 defer 带补充分析 —— 两边都拦，才叫强制。"""
        texts = _check_constraints()

        assert any("jsonb_array_length(requested_analysis) > 0" in text for text in texts)


class TestRationaleIsAlwaysRequired:
    """三态都必填理由 —— 放弃也要说清楚为什么。"""

    @pytest.mark.parametrize("state", list(DecisionState))
    def test_a_blank_rationale_is_rejected(self, state: DecisionState) -> None:
        with pytest.raises(ValidationError, match="理由"):
            _decision(decision=state, rationale="   ")

    @pytest.mark.parametrize("state", list(DecisionState))
    def test_a_rationale_is_kept(self, state: DecisionState) -> None:
        decision = _decision(decision=state, rationale="  理由在这儿  ")

        assert decision.rationale == "理由在这儿"

    def test_a_blank_decision_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(id="   ")

    def test_a_blank_candidate_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(candidate_id="   ")


class TestDeferMustProduceFollowUpAnalysis:
    """T1 ② —— defer 必须带至少一条补充分析项。"""

    def test_a_defer_without_analysis_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="defer 决策必须产出至少一条补充分析项"):
            EvolutionDecision(
                id="EVD-1",
                candidate_id="EVO-1024",
                decision=DecisionState.DEFER,
                rationale="证据不足",
                factors=_factors(),
            )

    def test_a_defer_with_analysis_is_accepted(self) -> None:
        decision = _decision(
            decision=DecisionState.DEFER,
            rationale="证据不足",
            requested_analysis=(
                FollowUpAnalysis(
                    kind=AnalysisKind.MEASUREMENT,
                    description="跑一次依赖分析并与基线对比",
                ),
            ),
        )

        assert len(decision.requested_analysis) == 1

    @pytest.mark.parametrize("state", [DecisionState.RECORD, DecisionState.REJECT])
    def test_a_non_defer_with_analysis_is_rejected(self, state: DecisionState) -> None:
        """record 之后还挂着"要补的分析"是自相矛盾的。"""
        with pytest.raises(ValidationError, match="只有 defer 决策才带补充分析项"):
            _decision(
                decision=state,
                requested_analysis=(
                    FollowUpAnalysis(
                        kind=AnalysisKind.MEASUREMENT,
                        description="跑一次依赖分析并与基线对比",
                    ),
                ),
            )

    @pytest.mark.parametrize("description", ["待补充", "TBD", "  tbd  ", "n/a", "-", "..."])
    def test_a_placeholder_description_is_rejected(self, description: str) -> None:
        """占位写法能通过非空检查，但信息量是零。"""
        with pytest.raises(ValidationError, match="占位写法"):
            FollowUpAnalysis(kind=AnalysisKind.MEASUREMENT, description=description)

    def test_a_blank_description_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FollowUpAnalysis(kind=AnalysisKind.MEASUREMENT, description="   ")

    def test_the_description_keeps_its_inner_spaces(self) -> None:
        analysis = FollowUpAnalysis(
            kind=AnalysisKind.MEASUREMENT,
            description="  跑一次依赖分析并与基线对比  ",
        )

        assert analysis.description == "跑一次依赖分析并与基线对比"

    def test_the_rationale_of_an_analysis_is_optional(self) -> None:
        analysis = FollowUpAnalysis(kind=AnalysisKind.REVIEW, description="做一次架构评审")

        assert analysis.rationale == ""


class TestOnlyRecordProducesATask:
    """T1 ① —— defer 与 reject 都不创建 Task。"""

    @pytest.mark.parametrize(
        ("state", "produces"),
        [
            (DecisionState.RECORD, True),
            (DecisionState.DEFER, False),
            (DecisionState.REJECT, False),
        ],
    )
    def test_only_record_produces_a_task(self, state: DecisionState, produces: bool) -> None:
        analysis = (
            (FollowUpAnalysis(kind=AnalysisKind.REVIEW, description="做一次架构评审"),)
            if state is DecisionState.DEFER
            else ()
        )
        decision = _decision(decision=state, requested_analysis=analysis)

        assert decision.produces_task is produces

    @pytest.mark.parametrize(
        ("state", "no_action"),
        [
            (DecisionState.RECORD, False),
            (DecisionState.DEFER, True),
            (DecisionState.REJECT, True),
        ],
    )
    def test_the_no_action_flag(self, state: DecisionState, no_action: bool) -> None:
        analysis = (
            (FollowUpAnalysis(kind=AnalysisKind.REVIEW, description="做一次架构评审"),)
            if state is DecisionState.DEFER
            else ()
        )
        decision = _decision(decision=state, requested_analysis=analysis)

        assert decision.is_no_action is no_action


class TestLowConfidenceProducesADefer:
    """T1 ① —— confidence = 0.1 的候选 → defer + 理由 + 不派生任务。"""

    def test_a_low_confidence_candidate_is_deferred(self) -> None:
        candidate = _candidate(confidence="0.1")
        factors = PriorityFactors.from_candidate(
            candidate,
            value=Decimal("0.8"),
            impact=Decimal("0.6"),
            urgency=Decimal("0.5"),
            cost=Decimal("0.5"),
        )
        scored = ScoredCandidate(candidate_id=candidate.id, factors=factors)

        decision = DecisionPolicy().decide(scored, decision_id="EVD-1")

        assert decision.decision is DecisionState.DEFER
        assert decision.rationale.strip()
        assert decision.produces_task is False

    def test_the_defer_names_the_candidate(self) -> None:
        decision = DecisionPolicy().decide(
            _scored("EVO-2048", confidence="0.1"), decision_id="EVD-1"
        )

        assert "EVO-2048" in decision.requested_analysis[0].description

    def test_the_defer_carries_at_least_one_follow_up_analysis(self) -> None:
        decision = DecisionPolicy().decide(_scored(confidence="0.1"), decision_id="EVD-1")

        assert len(decision.requested_analysis) >= 1
        assert decision.requested_analysis[0].description.strip()

    def test_the_defer_says_what_is_missing(self) -> None:
        """理由要落在"缺什么"上，而不是"再看看"。"""
        decision = DecisionPolicy().decide(_scored(confidence="0.1"), decision_id="EVD-1")

        assert "置信度" in decision.rationale


class TestRejectMustCarryARationale:
    """T1 ③ —— reject 必须带 rationale。"""

    def test_a_low_value_candidate_is_rejected(self) -> None:
        decision = DecisionPolicy().decide(_scored(value="0.05"), decision_id="EVD-1")

        assert decision.decision is DecisionState.REJECT
        assert decision.rationale.strip()
        assert decision.produces_task is False

    def test_a_reject_produces_no_follow_up_analysis(self) -> None:
        decision = DecisionPolicy().decide(_scored(value="0.05"), decision_id="EVD-1")

        assert decision.requested_analysis == ()

    def test_a_reject_without_a_rationale_cannot_be_built(self) -> None:
        with pytest.raises(ValidationError):
            EvolutionDecision(
                id="EVD-1",
                candidate_id="EVO-1024",
                decision=DecisionState.REJECT,
                rationale="",
                factors=_factors(),
            )


class TestTheThresholds:
    """三个阈值、三个出口，顺序不能换。"""

    def test_a_healthy_candidate_is_recorded(self) -> None:
        decision = DecisionPolicy().decide(_scored(), decision_id="EVD-1")

        assert decision.decision is DecisionState.RECORD
        assert decision.produces_task is True

    def test_a_high_risk_candidate_is_deferred_for_review(self) -> None:
        decision = DecisionPolicy().decide(_scored(risk="0.8"), decision_id="EVD-1")

        assert decision.decision is DecisionState.DEFER
        assert decision.requested_analysis[0].kind is AnalysisKind.REVIEW

    def test_the_risk_threshold_is_inclusive(self) -> None:
        """恰好等于阈值的那一档也要触发 —— 否则高风险档永远走不到评审。"""
        policy = DecisionPolicy()

        assert policy.decide(_scored(risk="0.8"), decision_id="EVD-1").decision is (
            DecisionState.DEFER
        )
        assert policy.decide(_scored(risk="0.7"), decision_id="EVD-2").decision is (
            DecisionState.RECORD
        )

    def test_the_confidence_threshold_is_exclusive(self) -> None:
        """刚好达标就放行。"""
        policy = DecisionPolicy()

        assert policy.decide(_scored(confidence="0.3"), decision_id="EVD-1").decision is (
            DecisionState.RECORD
        )
        assert policy.decide(_scored(confidence="0.29"), decision_id="EVD-2").decision is (
            DecisionState.DEFER
        )

    def test_the_value_threshold_is_exclusive(self) -> None:
        policy = DecisionPolicy()

        assert policy.decide(_scored(value="0.2"), decision_id="EVD-1").decision is (
            DecisionState.RECORD
        )
        assert policy.decide(_scored(value="0.19"), decision_id="EVD-2").decision is (
            DecisionState.REJECT
        )

    def test_low_confidence_wins_over_low_value(self) -> None:
        """前提没坐实的时候，先补证据 —— 而不是先讨论一个可能不存在的问题。"""
        decision = DecisionPolicy().decide(
            _scored(confidence="0.1", value="0.05"), decision_id="EVD-1"
        )

        assert decision.decision is DecisionState.DEFER

    def test_low_confidence_wins_over_high_risk(self) -> None:
        decision = DecisionPolicy().decide(
            _scored(confidence="0.1", risk="0.9"), decision_id="EVD-1"
        )

        assert decision.decision is DecisionState.DEFER
        assert decision.requested_analysis[0].kind is AnalysisKind.MEASUREMENT

    def test_the_thresholds_can_be_configured(self) -> None:
        policy = DecisionPolicy(minimum_confidence=Decimal("0.9"))

        assert policy.decide(_scored(confidence="0.5"), decision_id="EVD-1").decision is (
            DecisionState.DEFER
        )

    @pytest.mark.parametrize("field", ["minimum_confidence", "maximum_risk", "minimum_value"])
    def test_a_threshold_outside_the_unit_interval_is_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            DecisionPolicy.model_validate({field: Decimal("1.5")})


class TestTheDecisionIdComesFromTheCaller:
    """决策 id 由调用方给 —— 领域模型不自己编号。

    **四个出口都得守这条。** 编号规则该由编排层统一管（跨轮次唯一、可追溯），
    各模块自己编一套早晚会撞。策略要是偷偷改了这个 id，调用方拿它去写库时
    写进去的就是另一条决策 —— 追溯链当场断掉，而且不报错。
    """

    @pytest.mark.parametrize(
        ("overrides", "state"),
        [
            ({"confidence": "0.1"}, DecisionState.DEFER),
            ({"risk": "0.9"}, DecisionState.DEFER),
            ({"value": "0.05"}, DecisionState.REJECT),
            ({}, DecisionState.RECORD),
        ],
    )
    def test_the_given_decision_id_is_kept(
        self, overrides: dict[str, str], state: DecisionState
    ) -> None:
        decision = DecisionPolicy().decide(_scored(**overrides), decision_id="EVD-2026-0001")

        assert decision.decision is state
        assert decision.id == "EVD-2026-0001"


class TestTheDecisionCarriesItsBasis:
    """决策带着依据 —— 分数是算出来的。"""

    def test_the_score_is_derived_from_the_factors(self) -> None:
        decision = _decision(factors=_factors())

        assert decision.score == Decimal("0.016")

    def test_the_score_follows_the_factors(self) -> None:
        low = _decision(factors=_factors(value="0.2"))
        high = _decision(factors=_factors(value="0.9"))

        assert high.score > low.score

    def test_the_factors_are_required(self) -> None:
        """没有依据的决策构造不出来。"""
        with pytest.raises(ValidationError):
            EvolutionDecision.model_validate(
                {
                    "id": "EVD-1",
                    "candidate_id": "EVO-1",
                    "decision": DecisionState.RECORD,
                    "rationale": "理由",
                }
            )


class TestAsRecordFields:
    """落库字段 —— 键名要与实体列名对齐。"""

    def test_the_field_names_match_the_entity_columns(self) -> None:
        columns = set(DecisionEntity.__table__.columns.keys())

        assert set(_decision().as_record_fields()) <= columns

    def test_the_analysis_is_dumped_as_a_list_of_dicts(self) -> None:
        decision = _decision(
            decision=DecisionState.DEFER,
            requested_analysis=(
                FollowUpAnalysis(
                    kind=AnalysisKind.MEASUREMENT,
                    description="跑一次依赖分析并与基线对比",
                ),
            ),
        )

        payload = decision.as_record_fields()

        assert payload["decision"] == "defer"
        assert payload["requested_analysis"] == [
            {
                "kind": "measurement",
                "description": "跑一次依赖分析并与基线对比",
                "rationale": "",
            }
        ]

    def test_a_record_decision_has_no_analysis(self) -> None:
        assert _decision().as_record_fields()["requested_analysis"] == []


class TestDecisionRound:
    """一轮里的决策集合。"""

    def _round(self) -> DecisionRound:
        decisions = (
            _decision(id="EVD-1", candidate_id="EVO-1", decision=DecisionState.RECORD),
            _decision(
                id="EVD-2",
                candidate_id="EVO-2",
                decision=DecisionState.DEFER,
                rationale="证据不足",
                requested_analysis=(
                    FollowUpAnalysis(
                        kind=AnalysisKind.MEASUREMENT,
                        description="跑一次依赖分析并与基线对比",
                    ),
                ),
            ),
            _decision(
                id="EVD-3",
                candidate_id="EVO-3",
                decision=DecisionState.REJECT,
                rationale="收益太低",
            ),
        )
        return DecisionRound(cycle_id="cycle-0007", decisions=decisions)

    def test_the_total_is_counted(self) -> None:
        assert self._round().total == 3

    def test_the_no_action_decisions_are_counted(self) -> None:
        assert len(self._round().no_action) == 2

    def test_the_ratio_is_recorded(self) -> None:
        """T2 —— 不行动占决策总数的比例。"""
        assert self._round().no_action_ratio == Decimal("0.6667")

    def test_an_empty_round_has_a_ratio_of_zero(self) -> None:
        """分母为零时记 0，不记"没定义"。"""
        assert DecisionRound(cycle_id="cycle-0001").no_action_ratio == Decimal(0)

    def test_only_record_decisions_are_task_eligible(self) -> None:
        """只有 record 能派生任务 —— 规划层只看这个。"""
        recorded = self._round().recorded

        assert [item.id for item in recorded] == ["EVD-1"]

    def test_the_pending_analysis_collects_every_defer(self) -> None:
        pending = self._round().pending_analysis

        assert len(pending) == 1
        assert "依赖分析" in pending[0].description

    def test_a_decision_can_be_found_by_candidate(self) -> None:
        found = self._round().decision_for("EVO-2")

        assert found is not None
        assert found.id == "EVD-2"

    def test_an_unknown_candidate_has_no_decision(self) -> None:
        assert self._round().decision_for("EVO-999") is None

    def test_adding_a_decision_returns_a_new_round(self) -> None:
        """不可变 —— 记下的决策不改写，要改就再记一条。"""
        first = DecisionRound(cycle_id="cycle-0007")
        second = first.with_decision(_decision())

        assert first.total == 0
        assert second.total == 1
        assert second.cycle_id == "cycle-0007"

    def test_a_blank_cycle_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DecisionRound(cycle_id="   ")


class TestNoActionSignal:
    """T2 —— 闭环健康信号。"""

    def _rounds(self, *states: DecisionState) -> tuple[DecisionRound, ...]:
        decisions = tuple(
            _decision(
                id=f"EVD-{index}",
                candidate_id=f"EVO-{index}",
                decision=state,
                rationale="理由",
                requested_analysis=(
                    (
                        FollowUpAnalysis(
                            kind=AnalysisKind.MEASUREMENT,
                            description="跑一次依赖分析并与基线对比",
                        ),
                    )
                    if state is DecisionState.DEFER
                    else ()
                ),
            )
            for index, state in enumerate(states, start=1)
        )
        return (DecisionRound(cycle_id="cycle-0007", decisions=decisions),)

    def test_a_round_with_a_no_action_decision_is_healthy(self) -> None:
        signal = NoActionSignal.from_rounds(self._rounds(DecisionState.RECORD, DecisionState.DEFER))

        assert signal.healthy is True
        assert signal.no_action_decisions == 1
        assert signal.total_decisions == 2

    def test_a_round_without_any_no_action_decision_is_unhealthy(self) -> None:
        """一条都没有 —— "不行动"能力没有被真正用上。"""
        signal = NoActionSignal.from_rounds(
            self._rounds(DecisionState.RECORD, DecisionState.RECORD)
        )

        assert signal.healthy is False
        assert signal.no_action_decisions == 0

    def test_the_ratio_is_recorded(self) -> None:
        signal = NoActionSignal.from_rounds(
            self._rounds(DecisionState.RECORD, DecisionState.DEFER, DecisionState.REJECT)
        )

        assert signal.ratio == Decimal("0.6667")

    def test_the_rounds_are_counted(self) -> None:
        signal = NoActionSignal.from_rounds(
            self._rounds(DecisionState.RECORD) + self._rounds(DecisionState.DEFER)
        )

        assert signal.rounds == 2
        assert signal.total_decisions == 2

    def test_an_unhealthy_signal_says_so(self) -> None:
        signal = NoActionSignal.from_rounds(self._rounds(DecisionState.RECORD))

        assert "没有被真正用上" in signal.explanation

    def test_a_healthy_signal_says_so(self) -> None:
        signal = NoActionSignal.from_rounds(self._rounds(DecisionState.DEFER))

        assert "被用上了" in signal.explanation

    def test_no_rounds_at_all_is_not_a_crash(self) -> None:
        signal = NoActionSignal.from_rounds(())

        assert signal.rounds == 0
        assert signal.total_decisions == 0
        assert signal.ratio == Decimal(0)
        assert signal.healthy is False


class TestDecideRound:
    """一轮里的全部候选一起决策。"""

    def test_every_candidate_gets_a_decision(self) -> None:
        scored = [_scored("EVO-1"), _scored("EVO-2", confidence="0.1")]

        round_ = decide_round(DecisionPolicy(), scored, cycle_id="cycle-0007")

        assert round_.total == 2
        assert round_.decision_for("EVO-2") is not None

    def test_the_decision_ids_carry_the_cycle_number(self) -> None:
        """跨轮不撞 —— 同一个候选在两轮里被重新评估是常事。"""
        round_ = decide_round(DecisionPolicy(), [_scored("EVO-1")], cycle_id="cycle-0007")

        assert round_.decisions[0].id == "EVD-cycle-0007-001"

    def test_the_decision_ids_are_unique_within_a_round(self) -> None:
        scored = [_scored("EVO-1"), _scored("EVO-2"), _scored("EVO-3")]

        round_ = decide_round(DecisionPolicy(), scored, cycle_id="cycle-0007")

        ids = [item.id for item in round_.decisions]
        assert len(set(ids)) == len(ids)

    def test_an_empty_round_is_allowed(self) -> None:
        round_ = decide_round(DecisionPolicy(), [], cycle_id="cycle-0007")

        assert round_.total == 0
