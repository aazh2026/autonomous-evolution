"""TASK-P1-052 —— 演进候选的统一结构、五类分类与技术趋势的收益论证。

三条 T1 对应三组用例：

1. **缺 ``evidence`` 或 ``hypothesis`` 的候选被拒绝** —— 候选存在的理由
   就是证据；没有证据的候选是猜测，没有假设的候选是愿望。
2. **``confidence ∈ [0,1]``、``risk ∈ {low,medium,high}`` 校验生效** ——
   置信度与风险档是**要被拿去比大小**的数与档，值域外的值会让排序悄悄
   失去意义。
3. **技术趋势类候选若无"对本项目的实际收益"论证则被丢弃** —— 这条是
   R-025 的落点：技术趋势不能直接变成升级任务。

另外还守着一件容易漏的事：**丢弃也要留痕**。被丢掉的趋势如果什么都不说，
读的人分不清"判断为不适用"与"压根没看见"。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.evolution.assessment import (
    GapSeverity,
    GoalGap,
    OverallStatus,
    ProjectAssessment,
)
from core.evolution.candidate import (
    CandidateDiscoverer,
    CandidateStatus,
    DiscoveryContext,
    DiscoveryResult,
    DroppedTrend,
    EvolutionCandidate,
    ProjectBenefit,
    TechnologyTrend,
    TechnologyTrendDiscoverer,
)
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.taxonomy import OpportunityCategory, RiskLevel


def _evidence(ref: str = "artifact://report-001") -> Evidence:
    return Evidence(kind=EvidenceKind.TEST_REPORT, ref=ref)


def _benefit(
    *,
    summary: str = "把渲染层换掉之后，这个项目的页面渲染耗时能降一半",
    applies_to: tuple[str, ...] = ("svg",),
) -> ProjectBenefit:
    return ProjectBenefit(summary=summary, applies_to=applies_to)


def _candidate(**overrides: object) -> EvolutionCandidate:
    """一个合法的演进候选（非技术类）。"""
    data: dict[str, object] = {
        "id": "EVO-1024",
        "category": OpportunityCategory.ARCHITECTURE,
        "title": "拆分用户模块中的权限逻辑",
        "evidence": (_evidence(),),
        "hypothesis": "拆分权限领域服务可以降低模块耦合，提高独立测试能力。",
        "expected_outcome": ("降低模块依赖数量", "提高测试覆盖率"),
        "risk": RiskLevel.MEDIUM,
        "confidence": Decimal("0.82"),
        "estimated_effort": "2d",
    }
    data.update(overrides)
    return EvolutionCandidate.model_validate(data)


def _context(**overrides: object) -> DiscoveryContext:
    """一份发现上下文：项目用 svg 技术栈，有一个目标差距。"""
    assessment = ProjectAssessment(
        project_id="my-product",
        snapshot="commit-abc123",
        overall_status=OverallStatus.EVOLVING,
        goal_gap=(
            GoalGap(
                id="GAP-001",
                description="SVG 页面渲染质量未达到目标",
                evidence=(_evidence(),),
                severity=GapSeverity.HIGH,
            ),
        ),
    )
    data: dict[str, object] = {
        "project_id": "my-product",
        "snapshot": "commit-abc123",
        "assessment": assessment,
        "tech_stack": ("svg", "python"),
    }
    data.update(overrides)
    return DiscoveryContext.model_validate(data)


def _trend(**overrides: object) -> TechnologyTrend:
    """一条技术趋势（默认带着对本项目的收益论证）。"""
    data: dict[str, object] = {
        "id": "EVO-2001",
        "name": "resvg 渲染库",
        "description": "比现在的渲染路径快一个数量级",
        "evidence": (_evidence("artifact://benchmark-001"),),
        "hypothesis": "换用 resvg 之后，本项目的页面渲染耗时能降一半。",
        "risk": RiskLevel.MEDIUM,
        "confidence": Decimal("0.6"),
        "benefit": _benefit(),
        "estimated_effort": "3d",
    }
    data.update(overrides)
    return TechnologyTrend.model_validate(data)


class TestCandidateRequiresEvidenceAndHypothesis:
    """T1 ① —— 缺证据或缺假设的候选被拒绝。"""

    def test_a_candidate_without_evidence_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(evidence=())

    def test_a_candidate_without_the_evidence_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvolutionCandidate.model_validate(
                {
                    "id": "EVO-1024",
                    "category": "architecture",
                    "title": "拆分用户模块中的权限逻辑",
                    "hypothesis": "拆了就能解耦",
                    "expected_outcome": ("降低耦合",),
                    "risk": "medium",
                    "confidence": "0.82",
                }
            )

    def test_a_candidate_without_a_hypothesis_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(hypothesis="")

    def test_a_candidate_without_the_hypothesis_field_is_rejected(self) -> None:
        """字段整个不存在也不行 —— 不能给个缺省值蒙混过去。"""
        with pytest.raises(ValidationError):
            EvolutionCandidate.model_validate(
                {
                    "id": "EVO-1024",
                    "category": "architecture",
                    "title": "拆分用户模块中的权限逻辑",
                    "evidence": [_evidence()],
                    "expected_outcome": ["降低耦合"],
                    "risk": "medium",
                    "confidence": "0.82",
                }
            )

    def test_a_candidate_with_a_blank_hypothesis_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="候选的 id、标题与假设都不能是空白"):
            _candidate(hypothesis="   ")

    def test_a_candidate_without_an_expected_outcome_is_rejected(self) -> None:
        """没有预期结果就无法事后核验这个候选当初的判断准不准。"""
        with pytest.raises(ValidationError):
            _candidate(expected_outcome=())

    def test_a_valid_candidate_is_accepted(self) -> None:
        candidate = _candidate()

        assert candidate.category is OpportunityCategory.ARCHITECTURE
        assert candidate.confidence == Decimal("0.82")


class TestConfidenceAndRiskAreValidated:
    """T1 ② —— 置信度与风险档的值域。"""

    @pytest.mark.parametrize("value", ["1.01", "-0.01", "1.5"])
    def test_a_confidence_above_one_is_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError):
            _candidate(confidence=Decimal(value))

    @pytest.mark.parametrize("value", ["0", "1", "0.5"])
    def test_a_confidence_inside_the_range_is_accepted(self, value: str) -> None:
        candidate = _candidate(confidence=Decimal(value))

        assert candidate.confidence == Decimal(value)

    def test_an_unknown_risk_level_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(risk="catastrophic")

    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    def test_the_three_risk_levels_are_accepted(self, level: str) -> None:
        candidate = _candidate(risk=level)

        assert candidate.risk.value == level


class TestTechnologyNeedsAProvenBenefit:
    """T1 ③ —— 技术类候选必须先证明对本项目有实际收益（R-025）。"""

    def test_a_technology_candidate_without_a_benefit_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="技术类候选必须先证明对本项目有实际收益"):
            _candidate(category=OpportunityCategory.TECHNOLOGY, benefit=None)

    def test_a_technology_candidate_with_a_benefit_is_accepted(self) -> None:
        candidate = _candidate(category=OpportunityCategory.TECHNOLOGY, benefit=_benefit())

        assert candidate.benefit is not None

    def test_a_non_technology_candidate_needs_no_benefit(self) -> None:
        candidate = _candidate()

        assert candidate.benefit is None

    def test_a_benefit_without_a_target_is_rejected(self) -> None:
        """收益论证要指得出落点 —— 否则它对任何项目都成立。"""
        with pytest.raises(ValidationError):
            _benefit(applies_to=("   ",))

    def test_a_benefit_without_a_summary_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="收益论证要说清收益是什么"):
            _benefit(summary="  ")


class TestTechnologyTrendDiscoverer:
    """技术趋势发现器 —— 先证明收益，再产出候选。"""

    def test_a_trend_without_a_benefit_is_dropped(self) -> None:
        """没有收益论证的趋势**直接丢弃** —— 不是"先记下来以后再说"。"""
        discoverer = TechnologyTrendDiscoverer([_trend(benefit=None)])

        result = discoverer.discover(_context())

        assert result.candidates == ()
        assert len(result.dropped) == 1
        assert "收益论证" in result.dropped[0].reason

    def test_a_trend_whose_target_is_not_in_this_project_is_dropped(self) -> None:
        """收益论证的落点必须在本项目里 —— 否则它与本项目无关。"""
        trend = _trend(benefit=_benefit(applies_to=("kubernetes",)))
        discoverer = TechnologyTrendDiscoverer([trend])

        result = discoverer.discover(_context())

        assert result.candidates == ()
        assert "kubernetes" in result.dropped[0].reason

    def test_a_trend_whose_target_is_a_goal_gap_is_accepted(self) -> None:
        discoverer = TechnologyTrendDiscoverer([_trend(benefit=_benefit(applies_to=("GAP-001",)))])

        result = discoverer.discover(_context())

        assert len(result.candidates) == 1
        assert result.dropped == ()

    def test_a_trend_whose_target_is_in_the_tech_stack_is_accepted(self) -> None:
        discoverer = TechnologyTrendDiscoverer([_trend()])

        result = discoverer.discover(_context())

        assert len(result.candidates) == 1

    def test_a_trend_with_a_too_short_target_is_dropped(self) -> None:
        """太短的落点谁都能命中 —— 'py' 能装进任何长词里。"""
        trend = _trend(benefit=_benefit(applies_to=("py",)))
        discoverer = TechnologyTrendDiscoverer([trend])

        result = discoverer.discover(_context())

        assert result.candidates == ()
        assert len(result.dropped) == 1

    def test_the_produced_candidate_carries_the_benefit(self) -> None:
        discoverer = TechnologyTrendDiscoverer([_trend()])

        result = discoverer.discover(_context())

        candidate = result.candidates[0]
        assert candidate.category is OpportunityCategory.TECHNOLOGY
        assert candidate.benefit is not None
        assert candidate.benefit.applies_to == ("svg",)

    def test_the_produced_candidate_carries_the_trend_fields(self) -> None:
        """候选是趋势的搬运，不是重写 —— 改写就多了一个能走样的地方。"""
        trend = _trend()
        discoverer = TechnologyTrendDiscoverer([trend])

        result = discoverer.discover(_context())

        candidate = result.candidates[0]
        assert candidate.id == trend.id
        assert candidate.title == trend.name
        assert candidate.evidence == trend.evidence
        assert candidate.hypothesis == trend.hypothesis
        assert candidate.estimated_effort == trend.estimated_effort
        assert candidate.expected_outcome == (trend.benefit.summary,)  # type: ignore[union-attr]

    def test_the_produced_candidate_does_not_invent_the_numbers(self) -> None:
        """发现器不替趋势编置信度与风险 —— 编出来的数字看起来像有依据。"""
        trend = _trend(confidence=Decimal("0.31"), risk=RiskLevel.HIGH)
        discoverer = TechnologyTrendDiscoverer([trend])

        result = discoverer.discover(_context())

        candidate = result.candidates[0]
        assert candidate.confidence == Decimal("0.31")
        assert candidate.risk is RiskLevel.HIGH

    def test_a_dropped_trend_is_recorded_with_its_reason(self) -> None:
        """丢弃也要留痕 —— 分不清"判断为不适用"与"压根没看见"。"""
        discoverer = TechnologyTrendDiscoverer([_trend(name="resvg 渲染库", benefit=None)])

        result = discoverer.discover(_context())

        assert result.dropped[0].trend == "resvg 渲染库"

    def test_the_discoverer_satisfies_the_interface(self) -> None:
        discoverer = TechnologyTrendDiscoverer([_trend()])

        assert isinstance(discoverer, CandidateDiscoverer)

    def test_an_empty_trend_list_produces_nothing(self) -> None:
        discoverer = TechnologyTrendDiscoverer([])

        result = discoverer.discover(_context())

        assert result == DiscoveryResult()


class TestTheFiveCategories:
    """R-024 —— 五类演进机会，分类法定死。"""

    def test_there_are_exactly_five_categories(self) -> None:
        assert [category.value for category in OpportunityCategory] == [
            "product",
            "architecture",
            "engineering",
            "quality",
            "technology",
        ]

    def test_an_unknown_category_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(category="documentation")

    def test_a_candidate_without_a_category_is_rejected(self) -> None:
        """分类可以缺省的话，缺分类的候选就被悄悄归到某一类去了。"""
        with pytest.raises(ValidationError):
            EvolutionCandidate.model_validate(
                {
                    "id": "EVO-1024",
                    "title": "拆分用户模块中的权限逻辑",
                    "evidence": [_evidence()],
                    "hypothesis": "拆了就能解耦",
                    "expected_outcome": ["降低耦合"],
                    "risk": "medium",
                    "confidence": "0.82",
                }
            )


class TestTheStatusVocabulary:
    """候选状态的取值域 —— 与 DB 那一列对得上。"""

    def test_the_four_statuses_are_proposed_accepted_rejected_deferred(self) -> None:
        assert [status.value for status in CandidateStatus] == [
            "proposed",
            "accepted",
            "rejected",
            "deferred",
        ]

    def test_an_unknown_status_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            CandidateStatus("in_flight")


class TestBlankFieldsAreRejected:
    """空白等于没有内容。"""

    def test_a_blank_candidate_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="候选的 id、标题与假设都不能是空白"):
            _candidate(id="   ")

    def test_a_blank_title_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="候选的 id、标题与假设都不能是空白"):
            _candidate(title="   ")

    def test_a_blank_expected_outcome_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="候选必须说得出预期结果"):
            _candidate(expected_outcome=("   ",))

    def test_a_blank_trend_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="趋势的 id、名称、描述与假设都不能是空白"):
            _trend(name="   ")

    def test_a_benefit_without_any_target_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _benefit(applies_to=())

    def test_a_dropped_trend_without_a_reason_is_rejected(self) -> None:
        """丢弃也要留痕 —— 什么都不说等于没发生过。"""
        with pytest.raises(ValidationError, match="被丢弃的趋势与理由都不能是空白"):
            DroppedTrend(trend="resvg", reason="   ")


class TestDiscoveryContext:
    """发现上下文 —— 候选要回应的是这个项目的差距。"""

    def test_the_known_targets_include_the_tech_stack(self) -> None:
        context = _context()

        assert "svg" in context.known_targets()

    def test_the_known_targets_include_the_goal_gaps(self) -> None:
        context = _context()

        assert "gap-001" in context.known_targets()

    def test_a_context_without_an_assessment_still_works(self) -> None:
        context = _context(assessment=None)

        assert context.known_targets() == frozenset({"svg", "python"})
