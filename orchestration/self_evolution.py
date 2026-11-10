"""系统自身改进的流程 —— **提交、独立测试、审批、发布**。

设计依据：docs/tasks.md ``TASK-P4-070``（交付物"``SystemEvolutionProposal`` 实
体 + 独立测试 + 审批后发布"；验收条件"系统提出的自我修改全部走独立测试与审批；
**不存在绕过审批的发布路径**"）、R-102（不得自主修改控制自身的系统）、R-104
（防止系统自改验收标准）、``TASK-P1-011``（治理外置 + 变更需审批）。

**这一层是真正的闸。** 领域层（:mod:`core.evolution.proposal`）的状态机是
**纵深防御** —— 它让"漏写一步"在代码里就发生不了。但 Python 里的对象是**可以
伪造的**：绕过冻结直接改字段，谁都能把状态改成"已发布"。

**所以发布前要重新核对，而不是相信对象。** :meth:`SelfEvolutionFlow.publish`
每次发布都**从审批存储里把结论重新取回来**，再拿它跟提案当前的内容摘要、验
收标准版本、提案人逐项对。**对象说什么不算，存储里那份说了算** —— 于是伪造
一个"已审批"的对象发布不出去，偷偷改了内容也发布不出去（摘要对不上），偷偷
把验收标准改了再拿旧批准过关也发布不出去（版本对不上）。

**审计写在效果之前。** 审计写不进去，就**不动手** —— 失败关闭。没留痕的发布
比没发布的改动更糟：前者连"它发生过"都答不上来。

**验收标准是治理外置的。** 当前生效的版本由 :class:`CriteriaPort` 管着，流程
既不自己存也不自己改 —— 只有当**一份已审批的"改验收标准"提案真的发布了**，
版本才推进一版。推进也是**写审计之前**的那一步链上的事。

**这一层不碰数据库、不跑 LLM。** 它只认端口 —— 实现分两处：内存版在测试里，
数据库版在 ``infrastructure/database/``。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.evolution.proposal import (
    IndependentTestReport,
    ProposalApproval,
    ProposalKind,
    SystemEvolutionProposal,
    bump_criteria_version,
)
from core.policy.self_evolution import SelfEvolutionPolicy


class SelfEvolutionError(Exception):
    """自我改进流程里的错误 —— **卡住了就说卡在哪**。"""


class ProposalAuditError(SelfEvolutionError):
    """审计写不进去 —— **因此不发布**。"""


@dataclass(frozen=True, slots=True)
class ProposalAuditEvent:
    """一条提案审计 —— **发布前写下的那一笔**。

    :param action: 做了什么 —— ``submit`` / ``test`` / ``approve`` / ``publish``。
    :param proposal_id: 哪一份提案。
    :param project_id: 哪个项目。
    :param digest: **那一版内容的摘要** —— 事后核对的凭据。
    :param criteria_version: **判定时那一版验收标准** —— R-104 的凭据。
    :param detail: 补充说明。
    """

    action: str
    proposal_id: str
    project_id: str
    digest: str
    criteria_version: str
    detail: str = ""


@runtime_checkable
class ProposalStorePort(Protocol):
    """提案的存放处 —— **流程读回来的一切都从这儿来**。"""

    async def save(self, proposal: SystemEvolutionProposal) -> None:
        """存下这份提案。"""
        ...

    async def get(self, proposal_id: str) -> SystemEvolutionProposal | None:
        """取这份提案 —— **没有就是没有**。"""
        ...


@runtime_checkable
class IndependentTestPort(Protocol):
    """独立测试的执行处。"""

    async def test(self, proposal: SystemEvolutionProposal) -> IndependentTestReport:
        """跑一次独立测试 —— **返回测试报告**。

        **报告里必须带上测的是哪一版内容。** 没有这一项，"测的是哪一版"就没人
        核对了 —— 那是这份测试唯一有意义的凭据。
        """
        ...


@runtime_checkable
class ApprovalPort(Protocol):
    """审批的进出口。"""

    async def request(self, proposal: SystemEvolutionProposal) -> str:
        """发起审批 —— **返回审批请求号**。

        **请求里必须带上提案当前的内容摘要与验收标准版本。** 批的人批的是
        "他看到的那一版"，请求不带这两样，批的就是个空壳。
        """
        ...

    async def fetch(self, request_id: str) -> tuple[ProposalApproval, ...]:
        """取回这个请求下的全部审批结论 —— **一条都不能少**。"""
        ...


@runtime_checkable
class CriteriaPort(Protocol):
    """验收标准版本的存放处 —— **治理外置的那一份**。"""

    async def current_version(self) -> str:
        """现在生效的是哪一版。"""
        ...

    async def advance(self, version: str) -> None:
        """把生效版本推进到这一版。"""
        ...


@runtime_checkable
class ProposalAuditPort(Protocol):
    """提案审计的写入口 —— **写失败就抛**。"""

    async def record(self, event: ProposalAuditEvent) -> None:
        """记一条审计 —— **写不进去就抛异常**。"""
        ...


@runtime_checkable
class ProposalApplierPort(Protocol):
    """改动的落地处 —— **只有流程能碰到它**。"""

    async def apply(self, proposal: SystemEvolutionProposal) -> None:
        """把这份提案的改动真的做出来。"""
        ...


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    """一份提案的**内容** —— **还没有状态的那部分**。

    **为什么要单独一个类型。** 提案人给得出的是"改什么、为什么改"，给不出的是
    "算多高风险""要几个人批""现在是第几版标准"—— 后三样是**规矩与治理**的事。
    把它们分开，提案人就没法自己给自己降低标准。

    :param proposal_id: 提案编号。
    :param project_id: 归属项目。
    :param kind: 提案的种类。
    :param title: 一句话标题。
    :param target: 要改什么。
    :param change_summary: 改了什么。
    :param proposed_by: 谁提的。
    :param details: 补充说明。
    """

    proposal_id: str
    project_id: str
    kind: ProposalKind
    title: str
    target: str
    change_summary: str
    proposed_by: str
    details: str = ""


@dataclass(frozen=True, slots=True)
class SelfEvolutionPorts:
    """流程要用的那几个端口 —— **一个一个列出来，比一个大接口清楚**。"""

    store: ProposalStorePort
    tester: IndependentTestPort
    approvals: ApprovalPort
    criteria: CriteriaPort
    audit: ProposalAuditPort
    applier: ProposalApplierPort


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """发布的结果。

    :param proposal: **已发布的那份提案**。
    :param criteria_version: 发布之后的验收标准版本 —— **改标准的那一类会推进
        一版**。
    """

    proposal: SystemEvolutionProposal
    criteria_version: str


@dataclass(frozen=True, slots=True)
class SelfEvolutionFlow:
    """系统自身改进的流程 —— **四步：提交、测试、审批、发布**。

    :param policy: 规矩 —— **风险与审批数**。
    :param ports: 端口 —— **存储、测试、审批、标准、审计、落地**。
    """

    policy: SelfEvolutionPolicy
    ports: SelfEvolutionPorts

    # -- 提交 ---------------------------------------------------------------

    async def submit(self, draft: ProposalDraft) -> SystemEvolutionProposal:
        """提交一份提案 —— **从草稿进流程**。

        **风险与审批数由规矩给，提案人给不了。** 提案人能写的只有"改什么、为什么
        改"；要几份审批、算多高风险，是 :class:`SelfEvolutionPolicy` 说了算，
        而"现在是第几版标准"由治理外置的那一份说了算。
        """
        version = await self.ports.criteria.current_version()
        proposal = SystemEvolutionProposal(
            proposal_id=draft.proposal_id,
            project_id=draft.project_id,
            kind=draft.kind,
            title=draft.title,
            target=draft.target,
            change_summary=draft.change_summary,
            proposed_by=draft.proposed_by,
            criteria_version=version,
            risk=self.policy.risk_for(draft.kind),
            required_approvals=self.policy.approvals_for(draft.kind),
            details=draft.details,
        )
        submitted = proposal.submit()
        await self._audit("submit", submitted, "提案已提交，等独立测试")
        await self.ports.store.save(submitted)
        return submitted

    # -- 独立测试 -----------------------------------------------------------

    async def evaluate(self, proposal_id: str) -> SystemEvolutionProposal:
        """跑独立测试 —— **测试人不能是提案人**。

        **核对放在实体里。** 端口返回什么由端口说了算，但"测试人是不是提案人"
        "测的是不是这一版"这两条是**领域规则** —— :meth:`SystemEvolutionProposal
        .record_test` 会拒掉对不上的报告。
        """
        proposal = await self._load(proposal_id)
        report = await self.ports.tester.test(proposal)
        updated = proposal.record_test(report)
        await self._audit("test", updated, report.summary)
        await self.ports.store.save(updated)
        return updated

    # -- 审批 ---------------------------------------------------------------

    async def request_approval(self, proposal_id: str) -> SystemEvolutionProposal:
        """发起审批 —— **绑在这一版内容与这一版标准上**。"""
        proposal = await self._load(proposal_id)
        request_id = await self.ports.approvals.request(proposal)
        updated = proposal.request_approval(request_id)
        await self._audit("approve", updated, f"已发起审批：{request_id}")
        await self.ports.store.save(updated)
        return updated

    async def record_approval(
        self,
        proposal_id: str,
        approval: ProposalApproval,
    ) -> SystemEvolutionProposal:
        """记一份审批 —— **审批人不能是提案人**。"""
        proposal = await self._load(proposal_id)
        updated = proposal.record_approval(approval)
        await self._audit("approve", updated, f"{approval.approver} 表了态")
        await self.ports.store.save(updated)
        return updated

    # -- 发布 ---------------------------------------------------------------

    async def publish(self, proposal_id: str) -> PublishOutcome:
        """发布 —— **唯一一条落地路径**。

        **五步，顺序不能换。**

        1. **从存储里取回审批结论** —— 不相信对象上的状态。
        2. **逐项核对** —— 审批够不够数、批的是不是这一版内容、用的是不是这一
           版标准、有没有人拒过、有没有人是提案人自己。
        3. **实体层的闸** —— 状态、独立测试、内容摘要。
        4. **写审计** —— 写不进去就**到此为止**（失败关闭）。
        5. **落地改动**；若是"改验收标准"那一类，**推进标准版本**。

        **为什么核对放在实体之前。** 实体层已经查过一遍了，但实体层查的是**对
        象自己的字段** —— 而对象是可以伪造的。这里查的是**存储里的结论**，那
        是别人写的、别人改不了的那一份。
        """
        proposal = await self._load(proposal_id)
        current = await self.ports.criteria.current_version()
        stored = await self._fetch_approvals(proposal)
        self._verify(proposal, stored, current)

        published = proposal.publish(current_criteria_version=current)
        await self._audit(
            "publish",
            published,
            f"审批人：{', '.join(sorted(published.approved_approvers))}",
        )
        await self.ports.applier.apply(published)
        if published.kind is ProposalKind.ACCEPTANCE_CRITERIA_CHANGE:
            await self.ports.criteria.advance(bump_criteria_version(current))
        await self.ports.store.save(published)
        return PublishOutcome(
            proposal=published,
            criteria_version=await self.ports.criteria.current_version(),
        )

    # -- 内部 ---------------------------------------------------------------

    async def _load(self, proposal_id: str) -> SystemEvolutionProposal:
        """取一份提案 —— **没有就是没有**。"""
        proposal = await self.ports.store.get(proposal_id)
        if proposal is None:
            raise SelfEvolutionError(f"没有这份提案：{proposal_id}")
        return proposal

    async def _fetch_approvals(
        self, proposal: SystemEvolutionProposal
    ) -> tuple[ProposalApproval, ...]:
        """从审批存储里取回结论 —— **没有请求号就没有结论**。"""
        if proposal.approval_request_id is None:
            raise SelfEvolutionError(
                f"提案 {proposal.proposal_id} 没有发起过审批 —— 没有可核对的审批结论"
            )
        return await self.ports.approvals.fetch(proposal.approval_request_id)

    def _verify(
        self,
        proposal: SystemEvolutionProposal,
        stored: Sequence[ProposalApproval],
        current_criteria_version: str,
    ) -> None:
        """把存储里的结论与提案当前的样子逐项对上 —— **对不上就停**。"""
        if not stored:
            raise SelfEvolutionError(f"提案 {proposal.proposal_id} 没有审批结论")
        if any(not approval.approved for approval in stored):
            raise SelfEvolutionError(
                f"提案 {proposal.proposal_id} 有人拒过 —— 拒绝是终态，发布不了"
            )
        approvers = {approval.approver for approval in stored}
        if len(approvers) < proposal.required_approvals:
            raise SelfEvolutionError(
                f"提案 {proposal.proposal_id} 只有 {len(approvers)} 份独立审批，"
                f"要 {proposal.required_approvals} 份"
            )
        if proposal.proposed_by in approvers:
            raise SelfEvolutionError("审批人里有提案人自己 —— 这不算独立审批")
        digest = proposal.content_digest
        for approval in stored:
            if approval.approved_digest != digest:
                raise SelfEvolutionError(
                    f"审批 {approval.approver} 批的不是这一版内容 —— 内容变了，批准作废"
                )
            if approval.criteria_version != proposal.criteria_version:
                raise SelfEvolutionError(
                    f"审批 {approval.approver} 用的验收标准版本与提案判定时的不一致"
                )
        if proposal.criteria_version != current_criteria_version:
            raise SelfEvolutionError("验收标准在审批之后变了 —— 这份批准不再成立（R-104）")

    async def _audit(
        self,
        action: str,
        proposal: SystemEvolutionProposal,
        detail: str = "",
    ) -> None:
        """写一条审计 —— **写不进去就抛，因而后面的事不会发生**。"""
        event = ProposalAuditEvent(
            action=action,
            proposal_id=proposal.proposal_id,
            project_id=proposal.project_id,
            digest=proposal.content_digest,
            criteria_version=proposal.criteria_version,
            detail=detail,
        )
        try:
            await self.ports.audit.record(event)
        except Exception as error:
            raise ProposalAuditError(
                f"提案 {proposal.proposal_id} 的审计写不进去 —— 不往下走：{error}"
            ) from error


__all__ = [
    "ApprovalPort",
    "CriteriaPort",
    "IndependentTestPort",
    "ProposalApplierPort",
    "ProposalAuditError",
    "ProposalAuditEvent",
    "ProposalAuditPort",
    "ProposalDraft",
    "ProposalStorePort",
    "PublishOutcome",
    "SelfEvolutionError",
    "SelfEvolutionFlow",
    "SelfEvolutionPorts",
]
