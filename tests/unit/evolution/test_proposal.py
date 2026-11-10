"""TASK-P4-070 —— 系统自身改进提案：实体与状态机。

验证六件事：

1. **提案只能以草稿建出来** —— 别的状态只能由状态机推。
2. **自己测自己、自己批自己 —— 一律不许。**
3. **测的、批的必须是这一版内容** —— 内容一改，凭据全废。
4. **验收标准版本对不上 —— 不许发。**（R-104）
5. **风险与审批数不得低于天生下限** —— 规矩只能往严里加。
6. **发布只有一条路** —— 缺一样都不发。
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from core.evolution.proposal import (
    MINIMUM_APPROVALS_BY_KIND,
    MINIMUM_RISK_BY_KIND,
    IndependentTestReport,
    ProposalApproval,
    ProposalIndependenceError,
    ProposalIntegrityError,
    ProposalKind,
    ProposalStateError,
    ProposalStatus,
    SystemEvolutionProposal,
    bump_criteria_version,
    is_valid_criteria_version,
)
from core.evolution.taxonomy import RiskLevel


def _forged(proposal: SystemEvolutionProposal, **changes: object) -> SystemEvolutionProposal:
    """伪造一份 —— **绕过冻结直接改字段**。

    **这就是"对象上的状态可以伪造"的具体样子。** 冻结挡得住误改，挡不住故
    意 —— 而这一层要证明的就是：**伪造出来的那份，发布不出去。**
    """
    clone = object.__new__(type(proposal))
    for field_ in fields(proposal):
        object.__setattr__(clone, field_.name, getattr(proposal, field_.name))
    for name, value in changes.items():
        object.__setattr__(clone, name, value)
    return clone


def _proposal(
    *,
    kind: ProposalKind = ProposalKind.CODE_CHANGE,
    proposed_by: str = "director",
    criteria_version: str = "criteria-v1",
    risk: RiskLevel | None = None,
    required_approvals: int | None = None,
) -> SystemEvolutionProposal:
    """造一份草稿提案 —— **风险与审批数按规矩给**。"""
    return SystemEvolutionProposal(
        proposal_id="p-1",
        project_id="proj-1",
        kind=kind,
        title="改一下调度阈值",
        target="core/policy/scheduling.py",
        change_summary="把容差从 0.1 调到 0.05",
        details="实测更省",
        proposed_by=proposed_by,
        criteria_version=criteria_version,
        risk=risk if risk is not None else MINIMUM_RISK_BY_KIND[kind],
        required_approvals=(
            required_approvals
            if required_approvals is not None
            else MINIMUM_APPROVALS_BY_KIND[kind]
        ),
    )


def _report(proposal: SystemEvolutionProposal, *, tester: str = "tester") -> IndependentTestReport:
    """造一份测试报告 —— **测的就是这一版**。"""
    return IndependentTestReport(
        tester=tester,
        tested_digest=proposal.content_digest,
        passed=True,
        summary="全绿",
    )


def _approval(
    proposal: SystemEvolutionProposal,
    *,
    approver: str = "reviewer",
    approved: bool = True,
) -> ProposalApproval:
    """造一份审批 —— **批的就是这一版、这一版标准**。"""
    return ProposalApproval(
        approver=approver,
        approved_digest=proposal.content_digest,
        criteria_version=proposal.criteria_version,
        approved=approved,
    )


def _approved(proposal: SystemEvolutionProposal) -> SystemEvolutionProposal:
    """走完测试与审批 —— **只剩发布这一步**。"""
    submitted = proposal.submit()
    verified = submitted.record_test(_report(submitted))
    return verified.record_approval(_approval(verified))


class TestConstruction:
    """构造 —— **提案只能以草稿建出来**。"""

    def test_a_proposal_is_born_a_draft(self) -> None:
        assert _proposal().status is ProposalStatus.DRAFT

    def test_it_cannot_be_born_approved(self) -> None:
        """**凭空造一个"已审批"的提案 —— 不行。**"""
        with pytest.raises(ValueError, match="草稿"):
            SystemEvolutionProposal(
                proposal_id="p-1",
                project_id="proj-1",
                kind=ProposalKind.CODE_CHANGE,
                title="改一下",
                target="a.py",
                change_summary="改一下阈值",
                proposed_by="director",
                criteria_version="criteria-v1",
                risk=RiskLevel.MEDIUM,
                status=ProposalStatus.APPROVED,
            )

    def test_a_placeholder_summary_is_refused(self) -> None:
        """**"待补充"能过非空检查，但等于没写。**"""
        with pytest.raises(ValueError, match="占位"):
            SystemEvolutionProposal(
                proposal_id="p-1",
                project_id="proj-1",
                kind=ProposalKind.CODE_CHANGE,
                title="改一下",
                target="a.py",
                change_summary="待补充",
                proposed_by="director",
                criteria_version="criteria-v1",
                risk=RiskLevel.MEDIUM,
            )

    def test_the_criteria_version_shape_is_fixed(self) -> None:
        with pytest.raises(ValueError, match="形状"):
            _proposal(criteria_version="v3")

    def test_the_criteria_version_can_be_bumped(self) -> None:
        assert bump_criteria_version("criteria-v3") == "criteria-v4"
        assert is_valid_criteria_version("criteria-v12")
        assert not is_valid_criteria_version("v3")

    def test_bumping_a_malformed_version_raises(self) -> None:
        with pytest.raises(ValueError, match="形状"):
            bump_criteria_version("三")


class TestFloors:
    """下限 —— **规矩只能往严里加**。"""

    def test_the_risk_may_not_be_below_the_floor(self) -> None:
        with pytest.raises(ValueError, match="风险不得低于"):
            _proposal(kind=ProposalKind.ACCEPTANCE_CRITERIA_CHANGE, risk=RiskLevel.LOW)

    def test_the_approval_count_may_not_be_below_the_floor(self) -> None:
        with pytest.raises(ValueError, match="独立审批"):
            _proposal(kind=ProposalKind.GOVERNANCE_CHANGE, required_approvals=1)

    def test_a_higher_risk_is_allowed(self) -> None:
        """**可以更严 —— 那是项目自己的选择。**"""
        assert _proposal(kind=ProposalKind.CODE_CHANGE, risk=RiskLevel.HIGH).risk is RiskLevel.HIGH

    def test_the_governance_kinds_need_two_approvals(self) -> None:
        """**改标准、改策略、改治理 —— 一个人批不够。**"""
        for kind in (
            ProposalKind.ACCEPTANCE_CRITERIA_CHANGE,
            ProposalKind.POLICY_CHANGE,
            ProposalKind.GOVERNANCE_CHANGE,
        ):
            assert MINIMUM_APPROVALS_BY_KIND[kind] == 2


class TestTransitions:
    """状态机 —— **一步都不能跳**。"""

    def test_submit_moves_it_to_submitted(self) -> None:
        assert _proposal().submit().status is ProposalStatus.SUBMITTED

    def test_it_cannot_be_submitted_twice(self) -> None:
        with pytest.raises(ProposalStateError, match="草稿"):
            _proposal().submit().submit()

    def test_a_test_before_submitting_is_refused(self) -> None:
        proposal = _proposal()
        with pytest.raises(ProposalStateError, match="已提交"):
            proposal.record_test(_report(proposal))

    def test_a_failed_test_is_terminal(self) -> None:
        proposal = _proposal().submit()
        report = IndependentTestReport(
            tester="tester",
            tested_digest=proposal.content_digest,
            passed=False,
            summary="跑挂了",
        )
        failed = proposal.record_test(report)
        assert failed.status is ProposalStatus.FAILED_TEST
        with pytest.raises(ProposalStateError, match="已审批"):
            failed.publish(current_criteria_version="criteria-v1")

    def test_an_approval_before_the_test_is_refused(self) -> None:
        """**没测过的东西不许批。**"""
        proposal = _proposal().submit()
        with pytest.raises(ProposalStateError, match="独立测试"):
            proposal.record_approval(_approval(proposal))


class TestIndependence:
    """独立性 —— **自己测自己、自己批自己都不算**。"""

    def test_the_proposer_cannot_be_the_tester(self) -> None:
        proposal = _proposal(proposed_by="director").submit()
        with pytest.raises(ProposalIndependenceError, match="测试人"):
            proposal.record_test(_report(proposal, tester="director"))

    def test_the_proposer_cannot_be_the_approver(self) -> None:
        proposal = _proposal(proposed_by="director").submit()
        verified = proposal.record_test(_report(proposal))
        with pytest.raises(ProposalIndependenceError, match="审批人"):
            verified.record_approval(_approval(verified, approver="director"))

    def test_the_proposer_cannot_approve_themselves_at_publish(self) -> None:
        """**即使有人伪造了审批记录 —— 发布时还要再查一遍。**"""
        proposal = _proposal(proposed_by="director").submit()
        verified = proposal.record_test(_report(proposal))
        approved = verified.record_approval(_approval(verified, approver="reviewer"))
        forged = _forged(
            approved,
            approvals=(
                _approval(approved, approver="director"),
                _approval(approved, approver="reviewer"),
            ),
        )
        with pytest.raises(ProposalIndependenceError, match="提案人"):
            forged.publish(current_criteria_version="criteria-v1")


class TestBinding:
    """绑定 —— **测的、批的必须是这一版**。"""

    def test_a_test_of_another_version_is_refused(self) -> None:
        proposal = _proposal().submit()
        report = IndependentTestReport(
            tester="tester",
            tested_digest="0" * 64,
            passed=True,
        )
        with pytest.raises(ProposalIntegrityError, match="这一版"):
            proposal.record_test(report)

    def test_an_approval_of_another_version_is_refused(self) -> None:
        proposal = _proposal().submit()
        verified = proposal.record_test(_report(proposal))
        approval = ProposalApproval(
            approver="reviewer",
            approved_digest="0" * 64,
            criteria_version="criteria-v1",
            approved=True,
        )
        with pytest.raises(ProposalIntegrityError, match="这一版"):
            verified.record_approval(approval)

    def test_tampering_with_the_content_invalidates_the_approval(self) -> None:
        """**内容被偷改 —— 那份批准就不再成立。**

        这是"绑在内容摘要上"的意义：批准的是**人当时看到的那一份改动**。
        """
        approved = _approved(_proposal())
        tampered = _forged(approved, change_summary="偷偷改成别的")
        assert tampered.content_digest != approved.content_digest
        with pytest.raises(ProposalIntegrityError, match="这一版"):
            tampered.publish(current_criteria_version="criteria-v1")

    def test_an_approval_with_another_criteria_version_is_refused(self) -> None:
        """**批的时候用的不是判定时那一版标准 —— 不认。**"""
        proposal = _proposal().submit()
        verified = proposal.record_test(_report(proposal))
        approval = ProposalApproval(
            approver="reviewer",
            approved_digest=verified.content_digest,
            criteria_version="criteria-v2",
            approved=True,
        )
        with pytest.raises(ProposalIntegrityError, match="验收标准版本"):
            verified.record_approval(approval)


class TestCriteriaVersion:
    """验收标准版本 —— **R-104 的落点**。"""

    def test_the_standards_changing_after_the_approval_stops_the_publish(self) -> None:
        """**标准变了，那份批准就不再成立。**

        没有这一条，系统就能先改标准、再回头拿旧批准过关 —— 那正是 R-104 要防
        的那件事。
        """
        approved = _approved(_proposal())
        with pytest.raises(ProposalIntegrityError, match="验收标准"):
            approved.publish(current_criteria_version="criteria-v2")

    def test_the_same_version_publishes(self) -> None:
        approved = _approved(_proposal())
        published = approved.publish(current_criteria_version="criteria-v1")
        assert published.status is ProposalStatus.PUBLISHED


class TestApprovalCount:
    """审批数 —— **够数才算批完**。"""

    def test_one_approval_is_not_enough_for_a_governance_change(self) -> None:
        proposal = _proposal(kind=ProposalKind.GOVERNANCE_CHANGE).submit()
        verified = proposal.record_test(_report(proposal))
        once = verified.record_approval(_approval(verified, approver="reviewer-a"))
        assert once.status is ProposalStatus.VERIFIED
        twice = once.record_approval(_approval(once, approver="reviewer-b"))
        assert twice.status is ProposalStatus.APPROVED

    def test_the_same_person_twice_is_still_one_approval(self) -> None:
        """**一个人点两次批准不算两份意见。**

        审批数按**人**算 —— 一个人可以在系统里留三份审批记录，但那还是一个人的
        意见。所以治理类提案要的第二份审批**必须来自另一个人**。
        """
        proposal = _proposal(kind=ProposalKind.GOVERNANCE_CHANGE).submit()
        verified = proposal.record_test(_report(proposal))
        once = verified.record_approval(_approval(verified, approver="reviewer-a"))
        twice = once.record_approval(_approval(once, approver="reviewer-a"))
        assert twice.status is ProposalStatus.VERIFIED
        assert twice.approved_approvers == frozenset({"reviewer-a"})

    def test_a_rejection_is_sticky(self) -> None:
        """**拒过的东西不会因为没人再表态就变成"没人反对"。**"""
        proposal = _proposal().submit()
        verified = proposal.record_test(_report(proposal))
        rejected = verified.record_approval(
            _approval(verified, approver="reviewer", approved=False)
        )
        assert rejected.status is ProposalStatus.REJECTED
        with pytest.raises(ProposalStateError, match="独立测试"):
            rejected.record_approval(_approval(rejected, approver="other"))


class TestPublish:
    """发布 —— **唯一一条落地路径**。"""

    def test_a_draft_cannot_be_published(self) -> None:
        with pytest.raises(ProposalStateError, match="已审批"):
            _proposal().publish(current_criteria_version="criteria-v1")

    def test_a_tested_but_unapproved_proposal_cannot_be_published(self) -> None:
        proposal = _proposal().submit()
        verified = proposal.record_test(_report(proposal))
        with pytest.raises(ProposalStateError, match="已审批"):
            verified.publish(current_criteria_version="criteria-v1")

    def test_the_approved_one_publishes(self) -> None:
        published = _approved(_proposal()).publish(current_criteria_version="criteria-v1")
        assert published.status is ProposalStatus.PUBLISHED

    def test_the_digest_is_stable(self) -> None:
        """**同样的内容永远给同样的摘要** —— 否则"内容没变"会被误判成"被改过"。"""
        assert _proposal().content_digest == _proposal().content_digest
        other = _proposal(kind=ProposalKind.POLICY_CHANGE)
        assert _proposal().content_digest != other.content_digest
