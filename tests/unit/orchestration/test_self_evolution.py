"""TASK-P4-070 —— 自我改进的流程：唯一一条落地路径。

验证六件事：

1. **走完全程才发布** —— 提交 → 独立测试 → 审批 → 发布。
2. **对象上的状态不算数** —— 发布前要从审批存储里把结论取回来重新核对。
3. **内容被改 —— 批准就作废。**
4. **验收标准变了 —— 批准就不再成立。**（R-104）
5. **审计写不进去 —— 就不发布**（失败关闭）。
6. **改验收标准的那一类 —— 发布后版本推进一版。**
"""

from __future__ import annotations

from dataclasses import dataclass, fields

import pytest

from core.evolution.proposal import (
    IndependentTestReport,
    ProposalApproval,
    ProposalIndependenceError,
    ProposalKind,
    ProposalStatus,
    SystemEvolutionProposal,
)
from core.evolution.risk import ReviewerScope
from core.evolution.taxonomy import RiskLevel
from core.policy.self_evolution import SelfEvolutionPolicy
from orchestration.self_evolution import (
    ProposalAuditError,
    ProposalAuditEvent,
    ProposalDraft,
    PublishOutcome,
    SelfEvolutionError,
    SelfEvolutionFlow,
    SelfEvolutionPorts,
)


def _forged(proposal: SystemEvolutionProposal, **changes: object) -> SystemEvolutionProposal:
    """伪造一份 —— **绕过冻结直接改字段**。

    **这就是"对象上的状态可以伪造"的具体样子。** 而这正是流程层存在的理由：
    **流程不信对象，它只信审批存储里的那份结论。**
    """
    clone = object.__new__(type(proposal))
    for field_ in fields(proposal):
        object.__setattr__(clone, field_.name, getattr(proposal, field_.name))
    for name, value in changes.items():
        object.__setattr__(clone, name, value)
    return clone


class _Store:
    """内存提案存储。"""

    def __init__(self) -> None:
        self.items: dict[str, SystemEvolutionProposal] = {}

    async def save(self, proposal: SystemEvolutionProposal) -> None:
        self.items[proposal.proposal_id] = proposal

    async def get(self, proposal_id: str) -> SystemEvolutionProposal | None:
        return self.items.get(proposal_id)


class _Tester:
    """独立测试 —— **测试人是谁可以换**。"""

    def __init__(self, *, tester: str = "tester", passed: bool = True) -> None:
        self.tester = tester
        self.passed = passed

    async def test(self, proposal: SystemEvolutionProposal) -> IndependentTestReport:
        return IndependentTestReport(
            tester=self.tester,
            tested_digest=proposal.content_digest,
            passed=self.passed,
            summary="跑过了",
        )


class _Approvals:
    """审批存储 —— **结论放在这儿，不放在对象上**。"""

    def __init__(self) -> None:
        self.requests: list[SystemEvolutionProposal] = []
        self.conclusions: dict[str, list[ProposalApproval]] = {}

    async def request(self, proposal: SystemEvolutionProposal) -> str:
        self.requests.append(proposal)
        request_id = f"req-{len(self.requests)}"
        self.conclusions.setdefault(request_id, [])
        return request_id

    async def fetch(self, request_id: str) -> tuple[ProposalApproval, ...]:
        return tuple(self.conclusions.get(request_id, ()))


class _Criteria:
    """验收标准版本 —— **治理外置的那一份**。"""

    def __init__(self, version: str = "criteria-v1") -> None:
        self.version = version

    async def current_version(self) -> str:
        return self.version

    async def advance(self, version: str) -> None:
        self.version = version


class _Audit:
    """审计写入口 —— **可以故意写不进去**。"""

    def __init__(self) -> None:
        self.events: list[ProposalAuditEvent] = []
        self.broken = False

    async def record(self, event: ProposalAuditEvent) -> None:
        if self.broken:
            raise RuntimeError("审计存储写不进去")
        self.events.append(event)


class _Applier:
    """落地处 —— **只有流程能碰到它**。"""

    def __init__(self) -> None:
        self.applied: list[SystemEvolutionProposal] = []

    async def apply(self, proposal: SystemEvolutionProposal) -> None:
        self.applied.append(proposal)


@dataclass(frozen=True, slots=True)
class _Rig:
    """一套流程 —— **端口全是内存版的**。"""

    flow: SelfEvolutionFlow
    store: _Store
    approvals: _Approvals
    criteria: _Criteria
    audit: _Audit
    applier: _Applier


def _rig(
    *,
    policy: SelfEvolutionPolicy | None = None,
    tester: _Tester | None = None,
) -> _Rig:
    """造一套流程。"""
    store = _Store()
    approvals = _Approvals()
    criteria = _Criteria()
    audit = _Audit()
    applier = _Applier()
    ports = SelfEvolutionPorts(
        store=store,
        tester=tester if tester is not None else _Tester(),
        approvals=approvals,
        criteria=criteria,
        audit=audit,
        applier=applier,
    )
    flow = SelfEvolutionFlow(policy=policy or SelfEvolutionPolicy(), ports=ports)
    return _Rig(flow, store, approvals, criteria, audit, applier)


def _draft(*, kind: ProposalKind = ProposalKind.CODE_CHANGE) -> ProposalDraft:
    """造一份提案草稿 —— **风险与审批数不在这里**。"""
    return ProposalDraft(
        proposal_id="p-1",
        project_id="proj-1",
        kind=kind,
        title="改一下调度阈值",
        target="core/policy/scheduling.py",
        change_summary="把容差从 0.1 调到 0.05",
        proposed_by="director",
    )


def _approval(
    proposal: SystemEvolutionProposal,
    *,
    approver: str = "reviewer",
    approved: bool = True,
    scopes: tuple[ReviewerScope, ...] = (),
) -> ProposalApproval:
    """造一份审批 —— **绑在这一版内容与这一版标准上**。"""
    return ProposalApproval(
        approver=approver,
        approved_digest=proposal.content_digest,
        criteria_version=proposal.criteria_version,
        approved=approved,
        scopes=scopes,
    )


async def _submitted(rig: _Rig, *, kind: ProposalKind = ProposalKind.CODE_CHANGE) -> None:
    """走完前三步 —— **提交、测试、发起审批**。"""
    await rig.flow.submit(_draft(kind=kind))
    await rig.flow.evaluate("p-1")
    await rig.flow.request_approval("p-1")


class TestTheWholePath:
    """走完全程 —— **四步缺一不可**。"""

    async def test_it_publishes_after_the_test_and_the_approval(self) -> None:
        """**测过、批过，才发布得了。**"""
        rig = _rig()
        await _submitted(rig)
        proposal = rig.store.items["p-1"]
        await rig.flow.record_approval("p-1", _approval(proposal))
        rig.approvals.conclusions["req-1"].append(_approval(proposal))

        outcome = await rig.flow.publish("p-1")

        assert isinstance(outcome, PublishOutcome)
        assert outcome.proposal.status is ProposalStatus.PUBLISHED
        assert len(rig.applier.applied) == 1
        assert rig.store.items["p-1"].status is ProposalStatus.PUBLISHED

    async def test_the_audit_records_the_publish(self) -> None:
        """**留痕是为了事后答得出"这次凭什么发的"。**"""
        rig = _rig()
        await _submitted(rig)
        proposal = rig.store.items["p-1"]
        await rig.flow.record_approval("p-1", _approval(proposal))
        rig.approvals.conclusions["req-1"].append(_approval(proposal))

        await rig.flow.publish("p-1")

        publishes = [event for event in rig.audit.events if event.action == "publish"]
        assert len(publishes) == 1
        assert publishes[0].digest == proposal.content_digest
        assert publishes[0].criteria_version == "criteria-v1"

    async def test_a_proposal_cannot_be_published_before_it_is_approved(self) -> None:
        """**没批过的不许发。**"""
        rig = _rig()
        await _submitted(rig)

        with pytest.raises(SelfEvolutionError, match="没有审批结论"):
            await rig.flow.publish("p-1")
        assert rig.applier.applied == []


class TestTheStoreIsTheTruth:
    """对象上的状态不算数 —— **存储里那份说了算**。"""

    async def test_a_forged_status_does_not_publish(self) -> None:
        """**伪造一个"已审批"的对象 —— 发布不出去。**

        这是"不存在绕过审批的发布路径"的真正落点：对象上的 ``status`` 是可以
        改的，审批存储里的结论不是。
        """
        rig = _rig()
        await _submitted(rig)
        rig.store.items["p-1"] = _forged(rig.store.items["p-1"], status=ProposalStatus.APPROVED)

        with pytest.raises(SelfEvolutionError, match="没有审批结论"):
            await rig.flow.publish("p-1")
        assert rig.applier.applied == []

    async def test_the_tester_cannot_be_the_proposer(self) -> None:
        """**自己测自己 —— 这一步就过不去。**"""
        rig = _rig(tester=_Tester(tester="director"))
        with pytest.raises(ProposalIndependenceError, match="测试人"):
            await _submitted(rig)
        assert rig.applier.applied == []


class TestIntegrity:
    """内容与标准 —— **对不上就不发**。"""

    async def test_tampering_with_the_content_stops_the_publish(self) -> None:
        """**内容被偷改 —— 那份批准就作废了。**"""
        rig = _rig()
        await _submitted(rig)
        proposal = rig.store.items["p-1"]
        await rig.flow.record_approval("p-1", _approval(proposal))
        rig.approvals.conclusions["req-1"].append(_approval(proposal))
        rig.store.items["p-1"] = _forged(proposal, change_summary="偷偷改成别的")

        with pytest.raises(SelfEvolutionError, match="这一版内容"):
            await rig.flow.publish("p-1")
        assert rig.applier.applied == []

    async def test_the_criteria_changing_after_the_approval_stops_the_publish(self) -> None:
        """**标准变了 —— 那份批准就不再成立。**（R-104）"""
        rig = _rig()
        await _submitted(rig)
        proposal = rig.store.items["p-1"]
        await rig.flow.record_approval("p-1", _approval(proposal))
        rig.approvals.conclusions["req-1"].append(_approval(proposal))
        rig.criteria.version = "criteria-v2"

        with pytest.raises(SelfEvolutionError, match="验收标准"):
            await rig.flow.publish("p-1")
        assert rig.applier.applied == []

    async def test_the_proposer_cannot_be_among_the_approvers(self) -> None:
        """**提案人自己批自己 —— 不算独立审批。**"""
        rig = _rig()
        await _submitted(rig)
        proposal = rig.store.items["p-1"]
        rig.approvals.conclusions["req-1"].append(_approval(proposal, approver="director"))

        with pytest.raises(SelfEvolutionError, match="提案人"):
            await rig.flow.publish("p-1")
        assert rig.applier.applied == []

    async def test_a_rejection_stops_the_publish(self) -> None:
        """**有人拒过 —— 拒绝是终态。**"""
        rig = _rig()
        await _submitted(rig)
        proposal = rig.store.items["p-1"]
        rig.approvals.conclusions["req-1"].append(_approval(proposal, approved=False))

        with pytest.raises(SelfEvolutionError, match="拒过"):
            await rig.flow.publish("p-1")
        assert rig.applier.applied == []


class TestFailClosed:
    """失败关闭 —— **审计写不进去就不发布**。"""

    async def test_a_broken_audit_blocks_the_publish(self) -> None:
        """**没留痕的发布比没发布的改动更糟** —— 前者连"发生过"都答不上来。"""
        rig = _rig()
        await _submitted(rig)
        proposal = rig.store.items["p-1"]
        await rig.flow.record_approval("p-1", _approval(proposal))
        rig.approvals.conclusions["req-1"].append(_approval(proposal))
        rig.audit.broken = True

        with pytest.raises(ProposalAuditError, match="审计"):
            await rig.flow.publish("p-1")
        assert rig.applier.applied == []


class TestCriteriaAdvance:
    """改验收标准 —— **发布之后版本才推进**。"""

    async def test_an_acceptance_criteria_change_advances_the_version(self) -> None:
        rig = _rig()
        await _submitted(rig, kind=ProposalKind.ACCEPTANCE_CRITERIA_CHANGE)
        proposal = rig.store.items["p-1"]
        await rig.flow.record_approval(
            "p-1", _approval(proposal, approver="reviewer-a", scopes=(ReviewerScope.GOVERNANCE,))
        )
        rig.approvals.conclusions["req-1"].append(
            _approval(proposal, approver="reviewer-a", scopes=(ReviewerScope.GOVERNANCE,))
        )
        await rig.flow.record_approval("p-1", _approval(proposal, approver="reviewer-b"))
        rig.approvals.conclusions["req-1"].append(_approval(proposal, approver="reviewer-b"))

        outcome = await rig.flow.publish("p-1")

        assert outcome.criteria_version == "criteria-v2"
        assert rig.criteria.version == "criteria-v2"

    async def test_an_ordinary_change_leaves_the_version_alone(self) -> None:
        """**改别的东西不该动标准版本。**"""
        rig = _rig()
        await _submitted(rig)
        proposal = rig.store.items["p-1"]
        await rig.flow.record_approval("p-1", _approval(proposal))
        rig.approvals.conclusions["req-1"].append(_approval(proposal))

        outcome = await rig.flow.publish("p-1")

        assert outcome.criteria_version == "criteria-v1"
        assert rig.criteria.version == "criteria-v1"


class TestTheFlowsOwnRules:
    """流程自己定的那些规矩 —— **风险与审批数由规矩给**。"""

    async def test_a_governance_change_needs_two_approvers(self) -> None:
        """**一个人批不够 —— 而少一份就发不出去。**"""
        rig = _rig()
        await _submitted(rig, kind=ProposalKind.GOVERNANCE_CHANGE)
        proposal = rig.store.items["p-1"]
        assert proposal.required_approvals == 2
        rig.approvals.conclusions["req-1"].append(_approval(proposal, approver="reviewer-a"))

        with pytest.raises(SelfEvolutionError, match="独立审批"):
            await rig.flow.publish("p-1")
        assert rig.applier.applied == []

    async def test_a_governance_change_needs_a_governance_approver(self) -> None:
        """**有几个人批了不算数 —— 得有一个人看得懂。**

        **这一条是 TASK-P4-060 落到 TASK-P4-070 上的那一笔。** 改治理的提案
        要两位审批人 —— 但如果两位都没有治理资格，那还是没人看得懂，**发出去
        等于没人管**。所以流程在核对时还要看**谁批的、批的人当时持有什么资格**。
        """
        rig = _rig()
        await _submitted(rig, kind=ProposalKind.GOVERNANCE_CHANGE)
        proposal = rig.store.items["p-1"]
        rig.approvals.conclusions["req-1"].append(_approval(proposal, approver="reviewer-a"))
        rig.approvals.conclusions["req-1"].append(_approval(proposal, approver="reviewer-b"))

        with pytest.raises(SelfEvolutionError, match="评审资格"):
            await rig.flow.publish("p-1")
        assert rig.applier.applied == []

    async def test_a_governance_change_with_a_qualified_approver_publishes(self) -> None:
        rig = _rig()
        await _submitted(rig, kind=ProposalKind.GOVERNANCE_CHANGE)
        proposal = rig.store.items["p-1"]
        qualified = _approval(proposal, approver="reviewer-a", scopes=(ReviewerScope.GOVERNANCE,))
        plain = _approval(proposal, approver="reviewer-b")
        await rig.flow.record_approval("p-1", qualified)
        rig.approvals.conclusions["req-1"].append(qualified)
        await rig.flow.record_approval("p-1", plain)
        rig.approvals.conclusions["req-1"].append(plain)

        await rig.flow.publish("p-1")

        assert rig.applier.applied != []

    async def test_the_risk_comes_from_the_policy(self) -> None:
        """**提案人给不了风险档 —— 那是规矩的事。**"""
        policy = SelfEvolutionPolicy(risk_overrides={ProposalKind.CODE_CHANGE: RiskLevel.HIGH})
        rig = _rig(policy=policy)
        proposal = await rig.flow.submit(_draft())
        assert proposal.risk is RiskLevel.HIGH
