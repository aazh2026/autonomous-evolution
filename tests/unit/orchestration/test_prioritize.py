"""TASK-P3-014 —— 优先级决策：排次序、退回启发式模型、并把这件事记下来。

**测的是"决策"这件事，不是"打分"。** 打分在 ``tests/unit/evolution/
test_priority.py`` 里。

这一份测试守住三件事：

1. **次序只由内容决定** —— 与输入次序无关，与校准无关的部分也不该变；
2. **排不掉的候选不静默消失** —— 判不出来与 id 撞车各有各的理由；
3. **退回启发式模型时，那次尝试被记下来了** —— 不记的话，下一次会有人以为
   这个模型是校准过的。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from core.evolution.candidate import EvolutionCandidate
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.judgement import FactorJudgement
from core.evolution.priority import ActualOutcome
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from orchestration.prioritize import PriorityDecider


def _candidate(
    candidate_id: str,
    *,
    category: OpportunityCategory = OpportunityCategory.ENGINEERING,
    confidence: Decimal = Decimal("0.80"),
    risk: RiskLevel = RiskLevel.MEDIUM,
) -> EvolutionCandidate:
    """造一个候选。"""
    return EvolutionCandidate(
        id=candidate_id,
        category=category,
        title=f"候选 {candidate_id}",
        evidence=(Evidence(kind=EvidenceKind.FILE, ref="src/pkg/a.py"),),
        hypothesis="做了这件事会变好",
        expected_outcome=("变好了",),
        risk=risk,
        confidence=confidence,
    )


def _judgement(
    *,
    value: Decimal = Decimal("0.50"),
    impact: Decimal = Decimal("0.50"),
    urgency: Decimal = Decimal("0.50"),
    cost: Decimal = Decimal("0.30"),
) -> FactorJudgement:
    """造一组判断。"""
    return FactorJudgement(value=value, impact=impact, urgency=urgency, cost=cost)


class _Assessor:
    """一个可控的判断来源 —— 说不清就没法排序，所以它可以答"不知道"。"""

    def __init__(self, judgements: Mapping[str, FactorJudgement]) -> None:
        self._judgements = judgements

    def assess(self, candidate: EvolutionCandidate) -> FactorJudgement | None:
        """按 id 给判断。**没给就是判不出来。**"""
        return self._judgements.get(candidate.id)


def _misled() -> tuple[
    tuple[EvolutionCandidate, ...],
    dict[str, FactorJudgement],
    dict[str, ActualOutcome],
]:
    """一批被启发式模型骗了的候选 —— 紧迫程度与实际收益反着来。

    **实际收益跟着价值走，紧迫程度反着走。** 启发式模型把六个因子相乘，于是
    "越急越该先做"把次序带偏；校准该学到的正是"紧迫程度不该参与排序"。
    """
    candidates: list[EvolutionCandidate] = []
    judgements: dict[str, FactorJudgement] = {}
    outcomes: dict[str, ActualOutcome] = {}
    for index, text in enumerate(("0.20", "0.30", "0.40", "0.50", "0.60", "0.70", "0.80", "0.90")):
        candidate_id = f"c{index:02d}"
        value = Decimal(text)
        candidates.append(_candidate(candidate_id))
        judgements[candidate_id] = _judgement(value=value, urgency=Decimal(1) - value)
        outcomes[candidate_id] = ActualOutcome(candidate_id=candidate_id, realized_value=value)
    return tuple(candidates), judgements, outcomes


def _noise() -> dict[str, ActualOutcome]:
    """一批**没有关系**的实际收益 —— 排序救不回来的那种。"""
    realized = ("0.40", "0.70", "0.10", "0.80", "0.20", "0.50", "0.30", "0.60")
    return {
        f"c{index:02d}": ActualOutcome(candidate_id=f"c{index:02d}", realized_value=Decimal(text))
        for index, text in enumerate(realized)
    }


class TestTheRanking:
    """次序。"""

    def test_the_candidates_are_ranked_by_score(self) -> None:
        """**值高的排前面。**"""
        candidates = (_candidate("low"), _candidate("high"))
        assessor = _Assessor(
            {
                "low": _judgement(value=Decimal("0.20")),
                "high": _judgement(value=Decimal("0.80")),
            }
        )
        decision = PriorityDecider().decide(candidates, assessor=assessor)
        assert [item.candidate_id for item in decision.ranked] == ["high", "low"]

    def test_the_order_does_not_depend_on_the_input_order(self) -> None:
        """**同样的候选换个顺序进来，次序不变** —— 次级键是 id，不是输入次序。"""
        assessor = _Assessor(
            {
                "a": _judgement(value=Decimal("0.40")),
                "b": _judgement(value=Decimal("0.40")),
            }
        )
        forward = PriorityDecider().decide((_candidate("a"), _candidate("b")), assessor=assessor)
        backward = PriorityDecider().decide((_candidate("b"), _candidate("a")), assessor=assessor)
        assert [item.candidate_id for item in forward.ranked] == ["a", "b"]
        assert [item.candidate_id for item in backward.ranked] == ["a", "b"]

    def test_a_product_opportunity_can_outrank_an_architecture_one(self) -> None:
        """**排名不看分类** —— 跨方案比较就是这一条的意义（R-031）。

        一个产品机会该能排在架构机会前面，反之亦然。
        """
        candidates = (
            _candidate("arch", category=OpportunityCategory.ARCHITECTURE),
            _candidate("prod", category=OpportunityCategory.PRODUCT),
        )
        assessor = _Assessor(
            {
                "arch": _judgement(value=Decimal("0.30"), impact=Decimal("0.30")),
                "prod": _judgement(value=Decimal("0.90"), impact=Decimal("0.90")),
            }
        )
        decision = PriorityDecider().decide(candidates, assessor=assessor)
        assert [item.candidate_id for item in decision.ranked] == ["prod", "arch"]

    def test_the_category_travels_with_the_result(self) -> None:
        """**排完的结果能自己说明自己是什么** —— 分类跟着走。"""
        decision = PriorityDecider().decide(
            (_candidate("arch", category=OpportunityCategory.ARCHITECTURE),),
            assessor=_Assessor({"arch": _judgement()}),
        )
        assert decision.ranked[0].category is OpportunityCategory.ARCHITECTURE


class TestTheUnrankable:
    """排不进次序的候选。"""

    def test_a_candidate_without_a_judgement_is_not_ranked(self) -> None:
        """**判不出来就排不了序** —— 它不能悄悄消失。"""
        decision = PriorityDecider().decide(
            (_candidate("known"), _candidate("unknown")),
            assessor=_Assessor({"known": _judgement()}),
        )
        assert [item.candidate_id for item in decision.ranked] == ["known"]

    def test_it_is_recorded_with_a_reason(self) -> None:
        """**理由要能分辨"判断不出来"与"id 撞车"** —— 补救动作不同。"""
        decision = PriorityDecider().decide(
            (_candidate("unknown"),),
            assessor=_Assessor({}),
        )
        assert decision.unranked[0].candidate_id == "unknown"
        assert "判断不出" in decision.unranked[0].reason

    def test_a_duplicated_id_is_recorded(self) -> None:
        """**id 是主键** —— 撞车的排不了序，也不能悄悄合并。"""
        decision = PriorityDecider().decide(
            (_candidate("same"), _candidate("same")),
            assessor=_Assessor({"same": _judgement()}),
        )
        assert len(decision.ranked) == 1
        assert "重复" in decision.unranked[0].reason


class TestTheFallback:
    """退回启发式模型。"""

    def test_without_outcomes_it_falls_back(self) -> None:
        """**没有实际收益就没有可校准的东西** —— 用启发式模型，并记下这件事。

        **"没有收益数据"不是"收益是 0"。** 前者要等数据，后者是一次结论。
        """
        candidates = (_candidate("a"), _candidate("b"))
        assessor = _Assessor({"a": _judgement(), "b": _judgement(value=Decimal("0.8"))})
        decision = PriorityDecider().decide(candidates, assessor=assessor)
        assert decision.report.accepted is False
        assert "样本不足" in decision.report.reason

    def test_the_ranking_is_still_produced(self) -> None:
        """**退回不是失败** —— 启发式模型一直都在，它仍然给得出一个次序。"""
        candidates = (_candidate("a"), _candidate("b"))
        assessor = _Assessor({"a": _judgement(), "b": _judgement(value=Decimal("0.8"))})
        decision = PriorityDecider().decide(candidates, assessor=assessor)
        assert [item.candidate_id for item in decision.ranked] == ["b", "a"]

    def test_a_useless_outcome_falls_back_to_the_same_ranking(self) -> None:
        """**没有关系的收益不该改变次序** —— 校准不过就一个字都不改。

        与"完全没有收益"的结果逐条相同：**退回启发式模型的意思就是用原样
        的因子排。**
        """
        candidates, judgements, _ = _misled()
        plain = PriorityDecider().decide(candidates, assessor=_Assessor(judgements))
        noise = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=_noise()
        )
        assert noise.report.accepted is False
        assert [item.candidate_id for item in noise.ranked] == [
            item.candidate_id for item in plain.ranked
        ]

    def test_the_fallback_is_recorded(self) -> None:
        """**"我们试过校准，没成"要留下来** —— 不记的话，下次会有人以为
        这个模型是校准过的。
        """
        candidates, judgements, _ = _misled()
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=_noise()
        )
        assert decision.report.accepted is False
        assert "退回启发式模型" in decision.report.reason


class TestTheCalibration:
    """校准被采纳的时候。"""

    def test_the_calibration_is_used_when_there_is_a_real_relation(self) -> None:
        """**实际收益与价值同向、跟紧迫程度反向时，校准该被采纳。**"""
        candidates, judgements, outcomes = _misled()
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=outcomes
        )
        assert decision.report.accepted is True
        assert decision.report.rho_after is not None
        assert decision.report.rho_after > decision.report.threshold

    def test_the_calibrated_ranking_is_not_the_heuristic_one(self) -> None:
        """**采纳了就该换个次序** —— 不然采纳没有意义。"""
        candidates, judgements, outcomes = _misled()
        plain = PriorityDecider().decide(candidates, assessor=_Assessor(judgements))
        tuned = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=outcomes
        )
        assert [item.candidate_id for item in tuned.ranked] != [
            item.candidate_id for item in plain.ranked
        ]

    def test_the_calibrated_ranking_follows_the_realised_value(self) -> None:
        """**校准后的次序该跟着实际收益走** —— 那正是校准的目的。

        这批数据里实际收益跟着价值走，所以次序该按价值排。
        """
        candidates, judgements, outcomes = _misled()
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=outcomes
        )
        ordered = [item.candidate_id for item in decision.ranked]
        assert ordered == [f"c{index:02d}" for index in range(7, -1, -1)]

    def test_candidates_without_outcomes_still_get_ranked(self) -> None:
        """**没有实际收益的候选照样排** —— 校准只改因子，不改候选的集合。"""
        candidates, judgements, outcomes = _misled()
        extra = _candidate("c99")
        candidates = (*candidates, extra)
        judgements = dict(judgements)
        judgements["c99"] = _judgement(value=Decimal("0.95"))
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=outcomes
        )
        assert "c99" in [item.candidate_id for item in decision.ranked]
