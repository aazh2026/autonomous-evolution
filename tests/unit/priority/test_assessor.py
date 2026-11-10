"""TASK-P3-014 —— 默认的判断来源：按分类给的先验。

**测的是"先验"这件事的两条性质**：它覆盖了全部五类候选（不然有一类候选永远
排不了序），以及它**是仓库默认值而不是实测值**（这一点没法用断言证明，只能
靠文档与注释写清楚 —— 所以这里至少断言它不是从别处"测"出来的）。

**先验给的是"这一类通常长什么样"，不是"这一件事值多少"。** 同一分类里的
候选拿到同一组数 —— 这是先验的固有代价，写在这里，别让它看起来像缺陷被藏
起来。
"""

from __future__ import annotations

from decimal import Decimal

from apps.priority.assessor import CATEGORY_PRIORS, CategoryPriorAssessor
from core.evolution.candidate import EvolutionCandidate, ProjectBenefit
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.judgement import FactorAssessor
from core.evolution.taxonomy import OpportunityCategory, RiskLevel


def _candidate(category: OpportunityCategory) -> EvolutionCandidate:
    """造一个某分类的候选。

    **技术类要带收益论证** —— 换技术不是理由，落在本项目哪一处才是（R-025）。
    """
    benefit = (
        ProjectBenefit(summary="少写一半样板代码", applies_to=("src/pkg/a.py",))
        if category is OpportunityCategory.TECHNOLOGY
        else None
    )
    return EvolutionCandidate(
        id="c01",
        category=category,
        title="一个候选",
        evidence=(Evidence(kind=EvidenceKind.FILE, ref="src/pkg/a.py"),),
        hypothesis="做了这件事会变好",
        expected_outcome=("变好了",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.80"),
        benefit=benefit,
    )


class TestThePriors:
    """先验表。"""

    def test_it_covers_every_category(self) -> None:
        """**五类候选都要有先验** —— 少一类，那一类就永远排不了序。"""
        assert set(CATEGORY_PRIORS) == set(OpportunityCategory)

    def test_every_prior_is_in_range(self) -> None:
        """**因子都在 0~1 之间** —— 出了值域就是非法数据。"""
        for category, judgement in CATEGORY_PRIORS.items():
            for name in ("value", "impact", "urgency", "cost"):
                number = getattr(judgement, name)
                assert Decimal(0) <= number <= Decimal(1), (category, name)

    def test_the_cost_is_never_zero(self) -> None:
        """**没有"零成本"** —— 成本下限的存在就是为了不让"没估过成本"霸榜，
        而一个先验把成本写成 0，等于默认它免费。
        """
        for category, judgement in CATEGORY_PRIORS.items():
            assert judgement.cost > 0, category


class TestTheAssessor:
    """判断来源本身。"""

    def test_it_satisfies_the_port(self) -> None:
        """**它实现的是那个端口** —— 换一个判断来源，下游一行都不用改。"""
        assert isinstance(CategoryPriorAssessor(), FactorAssessor)

    def test_it_gives_the_prior_of_the_category(self) -> None:
        """**按分类取先验。**"""
        assessor = CategoryPriorAssessor()
        judgement = assessor.assess(_candidate(OpportunityCategory.QUALITY))
        assert judgement is not None
        assert judgement == CATEGORY_PRIORS[OpportunityCategory.QUALITY]

    def test_it_never_says_it_cannot_judge(self) -> None:
        """**表里有的分类，它都给得出数** —— 先验表覆盖了全部五类。

        "判不出来"与"判成 0"在这个实现里分不开：先验为 0.30 的分类不是"判不
        出来"，是"这一类按先验只值 0.30"。
        """
        assessor = CategoryPriorAssessor()
        for category in OpportunityCategory:
            assert assessor.assess(_candidate(category)) is not None, category

    def test_an_unknown_category_is_not_judged(self) -> None:
        """**表里没有的分类就是判不出来** —— 返回 ``None``，不是 0。"""
        assessor = CategoryPriorAssessor(priors={})
        assert assessor.assess(_candidate(OpportunityCategory.QUALITY)) is None
