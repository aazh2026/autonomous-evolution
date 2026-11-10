"""TASK-P4-060 —— 高风险变更的规矩与实体。

验证五件事：

1. **风险档决定要几份独立评审** —— 而不是"改动的种类"决定。
2. **评审资格住在名册里** —— 自己声明的不算。
3. **规矩只能往严里加** —— 写一个比下限更松的值，**策略压根建不出来**。
4. **实体的校验一道都不能少** —— 风险下限、份数下限、资格下限、独立性、绑定。
5. **两张高风险表要对得上** —— 操作那张与能力那张（跨层的一致性）。
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from core.evolution.proposal import REQUIRED_SCOPES_BY_KIND, ProposalKind
from core.evolution.risk import MINIMUM_REVIEWS_BY_RISK, ReviewerScope
from core.evolution.taxonomy import RiskLevel
from core.policy.autonomy import Operation
from core.policy.high_risk import (
    HIGH_RISK_OPERATIONS,
    HighRiskChange,
    HighRiskIndependenceError,
    HighRiskIntegrityError,
    HighRiskPolicy,
    HighRiskReview,
    HighRiskScopeError,
    HighRiskStateError,
    HighRiskStatus,
    ReviewerRegistry,
    risk_for_operation,
)
from harness.security.authorization import HIGH_RISK_CAPABILITIES, capability_for


def _forged(change: HighRiskChange, **changes: object) -> HighRiskChange:
    """伪造一份 —— **绕过冻结直接改字段**。

    **这就是"对象上的状态可以伪造"的具体样子。** 冻结挡得住误改，挡不住故
    意 —— 而这一层要证明的就是：**伪造出来的那份，放行不出去。**
    """
    clone = object.__new__(type(change))
    for field_ in fields(change):
        object.__setattr__(clone, field_.name, getattr(change, field_.name))
    for name, value in changes.items():
        object.__setattr__(clone, name, value)
    return clone


def _change(
    *,
    operation: Operation = Operation.MERGE_TO_MAIN,
    kind: ProposalKind = ProposalKind.CODE_CHANGE,
    risk: RiskLevel = RiskLevel.HIGH,
    required_reviews: int = 2,
    proposed_by: str = "director",
    required_scopes: tuple[ReviewerScope, ...] = (),
) -> HighRiskChange:
    """造一份草稿变更 —— **风险与评审数按规矩给**。"""
    return HighRiskChange(
        change_id="c-1",
        project_id="proj-1",
        operation=operation,
        kind=kind,
        title="把调度器并进主干",
        target="core/policy/scheduling.py",
        summary="把适配层的分支合并回主干",
        proposed_by=proposed_by,
        risk=risk,
        required_reviews=required_reviews,
        required_scopes=required_scopes,
    )


def _review(
    change: HighRiskChange,
    *,
    reviewer: str = "reviewer",
    approved: bool = True,
    digest: str | None = None,
) -> HighRiskReview:
    """造一份评审 —— **评的就是这一版**。"""
    return HighRiskReview(
        reviewer=reviewer,
        reviewed_digest=digest if digest is not None else change.content_digest,
        approved=approved,
    )


def _approved(change: HighRiskChange, *reviewers: str) -> HighRiskChange:
    """走完评审 —— **只剩放行这一步**。"""
    submitted = change.submit()
    under_review = submitted.request_review("req-1")
    result = under_review
    for reviewer in reviewers:
        result = result.record_review(_review(result, reviewer=reviewer))
    return result


class TestTheFloors:
    """下限 —— **风险越高，要几份就越多**。"""

    def test_high_risk_needs_two_independent_reviews(self) -> None:
        """**一份是自证 —— 两份才意味着另一个人也看了。**"""
        assert MINIMUM_REVIEWS_BY_RISK[RiskLevel.HIGH] == 2

    def test_the_count_follows_the_risk_not_the_kind(self) -> None:
        """**尺子对着每一次的标记，不能对着类目。**

        同一类改动，这一次是高风险、那次是低风险，都是常事 —— 所以"要几份"必
        须由**风险档**推出来，而不是由"是哪一类改动"写死。
        """
        assert MINIMUM_REVIEWS_BY_RISK[RiskLevel.MEDIUM] == 1
        assert MINIMUM_REVIEWS_BY_RISK[RiskLevel.LOW] == 0

    def test_the_merge_operation_is_high_risk(self) -> None:
        assert risk_for_operation(Operation.MERGE_TO_MAIN) is RiskLevel.HIGH

    def test_an_ordinary_operation_is_medium(self) -> None:
        assert risk_for_operation(Operation.CODE_CHANGES) is RiskLevel.MEDIUM

    def test_the_scope_floor_follows_the_kind(self) -> None:
        """**"看得懂吗"跟着改动的种类走。**"""
        assert REQUIRED_SCOPES_BY_KIND[ProposalKind.ARCHITECTURE_CHANGE] == (
            ReviewerScope.ARCHITECTURE,
        )
        assert REQUIRED_SCOPES_BY_KIND[ProposalKind.DEPENDENCY_CHANGE] == (
            ReviewerScope.SECURITY,
        )


class TestOnlyStricter:
    """规矩只能往严里加 —— **往松了写就是建不出来**。"""

    def test_a_looser_review_count_is_refused(self) -> None:
        with pytest.raises(ValueError, match="往严里加"):
            HighRiskPolicy(reviews_by_risk={RiskLevel.HIGH: 1})

    def test_a_stricter_review_count_is_accepted(self) -> None:
        policy = HighRiskPolicy(reviews_by_risk={RiskLevel.HIGH: 3})
        assert policy.reviews_for(RiskLevel.HIGH) == 3

    def test_a_dropped_scope_is_refused(self) -> None:
        with pytest.raises(ValueError, match="往严里加"):
            HighRiskPolicy(
                extra_scopes_by_kind={
                    ProposalKind.GOVERNANCE_CHANGE: (ReviewerScope.GENERAL,),
                }
            )

    def test_an_extra_scope_is_accepted(self) -> None:
        policy = HighRiskPolicy(
            extra_scopes_by_kind={
                ProposalKind.GOVERNANCE_CHANGE: (
                    ReviewerScope.GOVERNANCE,
                    ReviewerScope.SECURITY,
                ),
            }
        )
        assert policy.scopes_for(ProposalKind.GOVERNANCE_CHANGE) == (
            ReviewerScope.SECURITY,
            ReviewerScope.GOVERNANCE,
        )


class TestTheRegistry:
    """名册 —— **谁有资格评审什么**。"""

    def test_an_unregistered_reviewer_has_no_scope(self) -> None:
        registry = ReviewerRegistry()
        assert registry.scopes_of("stranger") == frozenset()
        assert not registry.is_registered("stranger")

    def test_a_registered_reviewer_keeps_his_scopes(self) -> None:
        registry = ReviewerRegistry()
        registry.register("arch-reviewer", [ReviewerScope.ARCHITECTURE])
        assert registry.scopes_of("arch-reviewer") == frozenset({ReviewerScope.ARCHITECTURE})

    def test_the_general_scope_does_not_substitute(self) -> None:
        """**"什么都会一点的人"顶不了"真的懂架构的人"。**"""
        registry = ReviewerRegistry()
        registry.register("generalist", [ReviewerScope.GENERAL])
        assert not registry.covers("generalist", (ReviewerScope.ARCHITECTURE,))
        assert registry.covers("generalist", (ReviewerScope.GENERAL,))

    def test_an_empty_requirement_still_needs_a_registered_person(self) -> None:
        """**没要求资格 ≠ 谁都能评** —— 至少得是在册的。"""
        registry = ReviewerRegistry()
        assert not registry.covers("stranger", ())
        registry.register("someone", [ReviewerScope.GENERAL])
        assert registry.covers("someone", ())


class TestTheEntity:
    """实体 —— **构造时就得全过**。"""

    def test_a_merge_may_not_be_marked_low_risk(self) -> None:
        """**合并到主干天生是高风险** —— 标低了就建不出来。"""
        with pytest.raises(ValueError, match="风险不得低于"):
            _change(risk=RiskLevel.LOW)

    def test_high_risk_needs_two_reviews(self) -> None:
        with pytest.raises(ValueError, match="独立评审"):
            _change(required_reviews=1)

    def test_the_scope_floor_is_filled_in(self) -> None:
        """**没提要求 = 用天生的下限** —— 而不是"什么都不要"。"""
        change = _change(kind=ProposalKind.ARCHITECTURE_CHANGE)
        assert change.required_scopes == (ReviewerScope.ARCHITECTURE,)

    def test_the_scope_floor_may_not_be_reduced(self) -> None:
        with pytest.raises(ValueError, match="评审资格"):
            _change(
                kind=ProposalKind.ARCHITECTURE_CHANGE,
                required_scopes=(ReviewerScope.GENERAL,),
            )

    def test_it_cannot_be_born_approved(self) -> None:
        """**凭空造一个"已批准"的变更 —— 不行。**"""
        with pytest.raises(ValueError, match="草稿"):
            HighRiskChange(
                change_id="c-1",
                project_id="proj-1",
                operation=Operation.MERGE_TO_MAIN,
                kind=ProposalKind.CODE_CHANGE,
                title="合并",
                target="a.py",
                summary="把分支并回主干",
                proposed_by="director",
                risk=RiskLevel.HIGH,
                required_reviews=2,
                status=HighRiskStatus.APPROVED,
            )


class TestTheStateMachine:
    """状态机 —— **一步都不能跳**。"""

    def test_it_cannot_be_released_before_it_is_reviewed(self) -> None:
        change = _change()
        with pytest.raises(HighRiskStateError, match="已批准"):
            change.release(covered_scopes=frozenset())

    def test_one_review_is_not_enough(self) -> None:
        approved = _approved(_change(), "reviewer-a")
        assert approved.status is HighRiskStatus.UNDER_REVIEW
        with pytest.raises(HighRiskStateError, match="已批准"):
            approved.release(covered_scopes=frozenset())

    def test_a_forged_approved_state_still_checks_the_count(self) -> None:
        """**伪造一个"已批准"的对象 —— 份数还是要够。**

        这就是"纵深防御"那一层存在的理由：状态是可以改的，而**该有几份**是改不
        掉的。
        """
        approved = _approved(_change(), "reviewer-a")
        forged = _forged(approved, status=HighRiskStatus.APPROVED)
        with pytest.raises(HighRiskStateError, match="独立评审"):
            forged.release(covered_scopes=frozenset())

    def test_two_reviews_release_it(self) -> None:
        approved = _approved(_change(), "reviewer-a", "reviewer-b")
        assert approved.status is HighRiskStatus.APPROVED
        released = approved.release(covered_scopes=frozenset())
        assert released.status is HighRiskStatus.RELEASED

    def test_the_proposer_cannot_review_their_own_change(self) -> None:
        submitted = _change().submit()
        under_review = submitted.request_review("req-1")
        with pytest.raises(HighRiskIndependenceError, match="评审人"):
            under_review.record_review(_review(under_review, reviewer="director"))

    def test_a_review_of_another_version_is_refused(self) -> None:
        submitted = _change().submit()
        under_review = submitted.request_review("req-1")
        with pytest.raises(HighRiskIntegrityError, match="这一版"):
            under_review.record_review(_review(under_review, digest="0" * 64))

    def test_a_rejection_is_sticky(self) -> None:
        """**拒过的东西不会因为没人再表态就变成"没人反对"。**"""
        submitted = _change().submit()
        under_review = submitted.request_review("req-1")
        rejected = under_review.record_review(_review(under_review, approved=False))
        assert rejected.status is HighRiskStatus.REJECTED
        with pytest.raises(HighRiskStateError, match="评审中"):
            rejected.record_review(_review(rejected, reviewer="reviewer-b"))

    def test_releasing_without_the_required_scope_is_refused(self) -> None:
        """**有几个人批了不算数 —— 得有一个人看得懂。**"""
        change = _change(kind=ProposalKind.ARCHITECTURE_CHANGE)
        approved = _approved(change, "reviewer-a", "reviewer-b")
        with pytest.raises(HighRiskScopeError, match="评审资格"):
            approved.release(covered_scopes=frozenset())

    def test_releasing_with_the_required_scope_works(self) -> None:
        change = _change(kind=ProposalKind.ARCHITECTURE_CHANGE)
        approved = _approved(change, "reviewer-a", "reviewer-b")
        released = approved.release(covered_scopes=frozenset({ReviewerScope.ARCHITECTURE}))
        assert released.status is HighRiskStatus.RELEASED


class TestConsistency:
    """两张高风险表要对得上 —— **一张按操作分，一张按能力分**。

    **为什么要有这一条。** ``harness/security/authorization.py`` 按**资源能力**
    分（能不能碰主干、能不能部署），``core/policy/high_risk.py`` 按**操作**分。
    两张表各自都会被改 —— 而**改了一边忘了另一边**，就会出现"能力上写着高风险、
    操作上没列"或者反过来。**这一条测试把两张表钉在一起。**
    """

    def test_every_high_risk_operation_maps_to_a_high_risk_capability(self) -> None:
        for operation in HIGH_RISK_OPERATIONS:
            capability = capability_for(operation)
            assert capability is not None, f"{operation.value} 没有对应的能力"
            assert capability in HIGH_RISK_CAPABILITIES, (
                f"{operation.value} 映射到的能力 {capability.value} 不在高风险能力表里"
            )

    def test_every_operation_with_a_high_risk_capability_is_high_risk(self) -> None:
        for operation in Operation:
            capability = capability_for(operation)
            if capability is None or capability not in HIGH_RISK_CAPABILITIES:
                continue
            assert operation in HIGH_RISK_OPERATIONS, (
                f"{operation.value} 用的是高风险能力 {capability.value}，"
                "却没被列进高风险操作表"
            )
