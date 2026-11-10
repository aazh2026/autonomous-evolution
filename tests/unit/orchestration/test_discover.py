"""TASK-P3-013 —— 发现编排：观察变候选，证据核不上就丢掉。

**测的是"翻译与核验"，不是"读"。** 读取在 ``tests/unit/observe/`` 里。

这一份测试守住三件事：

1. **每一类观察变成哪一类候选** —— 规则表是唯一的一份，抄本迟早对不上；
2. **证据核不上，候选就作废** —— 带一条假证据往前走是最难查的一类错；
3. **丢弃要留痕** —— "证据指错了"与"这一类本来就不产出候选"，补救动作完全
   不同。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.observe import Observation, ObservationKind, ObservationSnapshot
from core.evolution.opportunity import (
    OPPORTUNITY_RULES,
    OpportunityRule,
    Resolution,
    ResolutionStatus,
)
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from orchestration.discover import OpportunityDiscoverer


class _Resolver:
    """一个可控的核验器 —— 按引用给出核验结果。"""

    def __init__(self, statuses: dict[str, ResolutionStatus] | None = None) -> None:
        self._statuses = statuses or {}

    def resolve(self, evidence: Evidence) -> Resolution:
        """按引用给结果。默认核得上。"""
        status = self._statuses.get(evidence.ref, ResolutionStatus.RESOLVED)
        return Resolution(status=status, detail=f"{evidence.ref} 的核验结果")


def _observation(
    kind: ObservationKind,
    subject: str,
    ref: str = "src/tinylib/money.py",
) -> Observation:
    """造一条观察。"""
    return Observation(
        kind=kind,
        subject=subject,
        fact=f"{subject} 有问题",
        evidence=(Evidence(kind=EvidenceKind.FILE, ref=ref),),
    )


def _snapshot(*observations: Observation) -> ObservationSnapshot:
    """造一份快照。"""
    return ObservationSnapshot(
        project_id="P1",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        observations=observations,
    )


class TestTheRules:
    """规则表 —— 观察到候选的那本账。"""

    def test_five_kinds_have_a_rule(self) -> None:
        """**六类观察，五条规则。** 文档那一类故意没有 —— 决策记录写的是已经
        做过的决定，不是待办。
        """
        assert len(OPPORTUNITY_RULES) == 5
        assert ObservationKind.DOCS not in OPPORTUNITY_RULES

    def test_every_rule_names_the_kind_it_belongs_to(self) -> None:
        """**规则的种类与它的键要一致** —— 抄本迟早对不上。"""
        for kind, rule in OPPORTUNITY_RULES.items():
            assert rule.kind is kind

    def test_every_rule_belongs_to_one_of_the_five_categories(self) -> None:
        """**五类演进机会分类法定死了**（R-024）。"""
        for rule in OPPORTUNITY_RULES.values():
            assert rule.category in set(OpportunityCategory)

    def test_no_rule_produces_a_technology_candidate(self) -> None:
        """**技术类不在观察这一侧。** 技术候选要的是"这个趋势对本项目有收益"
        的论证（R-025），观察给不出趋势。
        """
        for rule in OPPORTUNITY_RULES.values():
            assert rule.category is not OpportunityCategory.TECHNOLOGY


class TestTheTranslation:
    """观察 → 候选。"""

    def test_changes_becomes_an_engineering_candidate(self) -> None:
        """**改动频繁 → 工程实现**（可维护性）。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.CHANGES, "src/tinylib/money.py"))
        )
        assert len(discovery.candidates) == 1
        assert discovery.candidates[0].category is OpportunityCategory.ENGINEERING

    def test_tests_become_a_quality_candidate(self) -> None:
        """**用例没过 → 质量**（缺陷）。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.TESTS, "known_issues/test_x.py::test_y"))
        )
        assert discovery.candidates[0].category is OpportunityCategory.QUALITY

    def test_static_becomes_an_architecture_candidate(self) -> None:
        """**依赖方向不对 → 架构**（怎么搭）。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.STATIC, "pkg.a -> pkg.b"))
        )
        assert discovery.candidates[0].category is OpportunityCategory.ARCHITECTURE

    def test_runtime_becomes_an_engineering_candidate(self) -> None:
        """**跑起来出事了 → 工程实现**（运行时表现）。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.RUNTIME, "cli"))
        )
        assert discovery.candidates[0].category is OpportunityCategory.ENGINEERING

    def test_feedback_becomes_a_product_candidate(self) -> None:
        """**人报上来了 → 产品**（往哪儿走）。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.FEEDBACK, "PILOT-001"))
        )
        assert discovery.candidates[0].category is OpportunityCategory.PRODUCT

    def test_docs_produces_no_candidate(self) -> None:
        """**决策记录写的是当初已经做过的决定，不是待办。**"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.DOCS, "0001-first"))
        )
        assert discovery.candidates == ()

    def test_the_dropped_one_says_why(self) -> None:
        """**丢弃也要留痕** —— 不然读的人分不清"被判断为不适用"与"压根没
        被看见"。
        """
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.DOCS, "0001-first"))
        )
        assert len(discovery.dropped) == 1
        assert discovery.dropped[0].subject == "0001-first"
        assert "不产出候选" in discovery.dropped[0].reason

    def test_the_candidate_carries_the_evidence(self) -> None:
        """**候选的证据就是观察的证据** —— 抄一遍会把两处抄歪。"""
        observation = _observation(ObservationKind.CHANGES, "src/tinylib/money.py")
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(_snapshot(observation))
        assert discovery.candidates[0].evidence == observation.evidence

    def test_the_title_names_the_subject(self) -> None:
        """**标题 = 前缀 + 主体** —— 主体是具体的东西。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.CHANGES, "src/tinylib/money.py"))
        )
        assert "src/tinylib/money.py" in discovery.candidates[0].title

    def test_the_hypothesis_names_the_subject(self) -> None:
        """**假设必须说得清是"这一件"还是"这一类"。**"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.CHANGES, "src/tinylib/money.py"))
        )
        assert "src/tinylib/money.py" in discovery.candidates[0].hypothesis

    def test_the_candidate_has_an_expected_outcome(self) -> None:
        """**没有预期结果就无法事后核验**（R-033）。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.CHANGES, "a.py"))
        )
        assert discovery.candidates[0].expected_outcome


class TestTheEvidenceGate:
    """证据核不上，候选就作废。"""

    def test_an_evidence_that_is_missing_kills_the_candidate(self) -> None:
        """**证据指错了地方还说"有证据"，那是在骗人。**"""
        resolver = _Resolver({"src/tinylib/money.py": ResolutionStatus.MISSING})
        discovery = OpportunityDiscoverer(resolver=resolver).discover(
            _snapshot(_observation(ObservationKind.CHANGES, "src/tinylib/money.py"))
        )
        assert discovery.candidates == ()
        assert len(discovery.dropped) == 1

    def test_the_drop_says_the_evidence_is_wrong(self) -> None:
        resolver = _Resolver({"src/tinylib/money.py": ResolutionStatus.MISSING})
        discovery = OpportunityDiscoverer(resolver=resolver).discover(
            _snapshot(_observation(ObservationKind.CHANGES, "src/tinylib/money.py"))
        )
        assert "证据指错了地方" in discovery.dropped[0].reason

    def test_an_evidence_that_cannot_be_checked_also_kills_it(self) -> None:
        """**证明不了它真实，就不能说它真实。**"""
        resolver = _Resolver({"src/tinylib/money.py": ResolutionStatus.UNAVAILABLE})
        discovery = OpportunityDiscoverer(resolver=resolver).discover(
            _snapshot(_observation(ObservationKind.CHANGES, "src/tinylib/money.py"))
        )
        assert discovery.candidates == ()

    def test_the_two_drops_say_different_things(self) -> None:
        """**"证据指错了"与"查不了"要分得开** —— 补救动作不同。"""
        missing = OpportunityDiscoverer(
            resolver=_Resolver({"a.py": ResolutionStatus.MISSING})
        ).discover(_snapshot(_observation(ObservationKind.CHANGES, "a.py", "a.py")))
        unavailable = OpportunityDiscoverer(
            resolver=_Resolver({"a.py": ResolutionStatus.UNAVAILABLE})
        ).discover(_snapshot(_observation(ObservationKind.CHANGES, "a.py", "a.py")))
        assert missing.dropped[0].reason != unavailable.dropped[0].reason
        assert "查不了" in unavailable.dropped[0].reason

    def test_one_bad_evidence_kills_the_whole_candidate(self) -> None:
        """**一条证据核不上，整条候选就作废** —— 不搞"大部分证据核得上就
        算"。
        """
        observation = Observation(
            kind=ObservationKind.FEEDBACK,
            subject="PILOT-001",
            fact="有问题",
            evidence=(
                Evidence(kind=EvidenceKind.ISSUE, ref="PILOT-001"),
                Evidence(kind=EvidenceKind.FILE, ref="docs/issues.md"),
            ),
        )
        resolver = _Resolver({"docs/issues.md": ResolutionStatus.MISSING})
        discovery = OpportunityDiscoverer(resolver=resolver).discover(_snapshot(observation))
        assert discovery.candidates == ()


class TestTheIds:
    """候选的 id。"""

    def test_they_are_unique(self) -> None:
        """**候选 id 是主键** —— 撞了落库就炸。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(
                _observation(ObservationKind.CHANGES, "a.py"),
                _observation(ObservationKind.TESTS, "b.py::test_x"),
            )
        )
        ids = [item.id for item in discovery.candidates]
        assert len(ids) == len(set(ids))

    def test_they_are_stable(self) -> None:
        """**同一批观察跑两次，候选 id 一样** —— 否则跨轮的比对就没法做了。"""
        observations = (_observation(ObservationKind.CHANGES, "src/tinylib/money.py"),)
        first = OpportunityDiscoverer(resolver=_Resolver()).discover(_snapshot(*observations))
        second = OpportunityDiscoverer(resolver=_Resolver()).discover(_snapshot(*observations))
        assert first.candidates[0].id == second.candidates[0].id

    def test_a_duplicate_subject_produces_one_candidate(self) -> None:
        """**同一主体只出一个候选** —— 后面的丢掉，并留痕。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(
                _observation(ObservationKind.CHANGES, "a.py"),
                _observation(ObservationKind.CHANGES, "a.py"),
            )
        )
        assert len(discovery.candidates) == 1
        assert len(discovery.dropped) == 1
        assert "重复" in discovery.dropped[0].reason

    def test_the_id_survives_a_path(self) -> None:
        """**主体要能进 id** —— 路径里有斜杠与冒号，要洗一遍。"""
        discovery = OpportunityDiscoverer(resolver=_Resolver()).discover(
            _snapshot(_observation(ObservationKind.TESTS, "known_issues/test_x.py::test_y"))
        )
        assert "/" not in discovery.candidates[0].id
        assert "::" not in discovery.candidates[0].id


class TestACustomRuleTable:
    """规则表可以换。"""

    def test_a_custom_rule_wins(self) -> None:
        """**规则表只有一个地方说了算** —— 换掉它，行为跟着变。"""
        rule = OpportunityRule(
            kind=ObservationKind.CHANGES,
            category=OpportunityCategory.PRODUCT,
            title_prefix="自定义",
            hypothesis_template="关于 {subject}",
            expected_outcome=("成了",),
            risk=RiskLevel.LOW,
            confidence=Decimal("0.5"),
            estimated_effort="3d",
        )
        discoverer = OpportunityDiscoverer(
            resolver=_Resolver(),
            rules={ObservationKind.CHANGES: rule},
        )
        discovery = discoverer.discover(_snapshot(_observation(ObservationKind.CHANGES, "a.py")))
        assert discovery.candidates[0].category is OpportunityCategory.PRODUCT
        assert discovery.candidates[0].title.startswith("自定义")
