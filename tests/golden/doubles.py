"""金标用的打桩件。

**为什么不在这里复用 ``tests/unit/`` 下已有的那些？** 因为那些是**别的
测试的私有零件** —— 它们带自己的语境、自己的断言口径。从一个测试模块里
import 另一个测试模块的私有名字，等于把两个测试焊死在一起：改了一边，
另一边莫名其妙地坏了。

**为什么打桩件要"真的能跑"？** 因为金标要验的是**判定逻辑**，而判定逻辑
的输入必须是真的对象 —— 真的契约、真的决策、真的提案。打桩件替掉的只有
**外面那些碰不到的东西**：子进程、存储、外部服务。

**它们替不掉的**：领域对象的自校验。``AcceptanceContract`` 的 ``required``
不能为空、``TaskAuthorization`` 的身份四元组不能有空字段 —— 这些是模型
自己在 ``__post_init__`` 里守着的，金标要验的正是这条，所以金标**用真的
模型**，让模型自己拒绝。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.evolution.proposal import (
    IndependentTestReport,
    ProposalApproval,
    ProposalKind,
    ProposalStatus,
    SystemEvolutionProposal,
)
from core.evolution.risk import ReviewerScope
from core.evolution.taxonomy import RiskLevel
from core.policy.self_evolution import SelfEvolutionPolicy
from harness.verification.engine import CheckExecution
from orchestration.self_evolution import (
    ProposalAuditEvent,
    ProposalDraft,
    SelfEvolutionFlow,
    SelfEvolutionPorts,
)


@dataclass
class ScriptedRunner:
    """按命令给结果的检查运行器。

    **结果按命令给，不按工作区给。** 六层各自跑各自的命令，一条命令的结果
    只在那一层有意义。

    :param results: 命令 → 执行结果。
    :param default: 没登记的命令一律给这个结果。
    """

    results: Mapping[tuple[str, ...], CheckExecution] = field(default_factory=dict)
    default: CheckExecution = field(default_factory=lambda: CheckExecution(0, "", ""))
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def run(
        self,
        command: Sequence[str],
        *,
        workspace: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> CheckExecution:
        """跑一条命令。工作区、环境、超时对这个替身没有意义 —— 剧本说了算。"""
        del workspace, env, timeout_seconds
        key = tuple(command)
        self.calls.append(key)
        return self.results.get(key, self.default)


def ok(stdout: str = "") -> CheckExecution:
    """跑通了。"""
    return CheckExecution(0, stdout, "")


def failed(stdout: str = "", stderr: str = "") -> CheckExecution:
    """跑完了，但没过。"""
    return CheckExecution(1, stdout, stderr)


def not_started() -> CheckExecution:
    """**压根没跑起来** —— 命令不存在、权限不够。

    它与 ``failed`` 是两件事：``failed`` 是"跑了，结论是没有"，
    ``not_started`` 是"没有结论"。把它当成前者，就等于把"没验成"算成
    "验过了"。
    """
    return CheckExecution(127, "", "command not found", started=False)


# --------------------------------------------------------------------------
# 自我改进流程的打桩件
# --------------------------------------------------------------------------


class MemoryStore:
    """内存提案存储。"""

    def __init__(self) -> None:
        self.items: dict[str, SystemEvolutionProposal] = {}

    async def save(self, proposal: SystemEvolutionProposal) -> None:
        self.items[proposal.proposal_id] = proposal

    async def get(self, proposal_id: str) -> SystemEvolutionProposal | None:
        return self.items.get(proposal_id)


class MemoryTester:
    """独立测试 —— **测过了**。"""

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


class MemoryApprovals:
    """审批存储 —— **结论放这儿，不放在对象上**。

    这是流程层存在的理由：对象上的状态可以被伪造，存储里的那份不能。
    """

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


class MemoryCriteria:
    """验收标准版本 —— **治理外置的那一份**。"""

    def __init__(self, *, version: str = "criteria-v1") -> None:
        self.version = version

    async def current_version(self) -> str:
        return self.version

    async def advance(self, version: str) -> None:
        self.version = version


class MemoryAudit:
    """审计写入口 —— **可以故意写不进去**。

    :attr:`broken` 打开之后每一次写都抛异常。它模拟的是审计存储真的坏了
    那种情况 —— 那种时候系统必须**停手**，而不是"审计失败就放行"。
    """

    def __init__(self) -> None:
        self.events: list[ProposalAuditEvent] = []
        self.broken = False

    async def record(self, event: ProposalAuditEvent) -> None:
        if self.broken:
            raise RuntimeError("审计存储写不进去")
        self.events.append(event)


class MemoryApplier:
    """落地处 —— **只有流程能碰到它**。

    ``applied`` 是空的，就说明没有任何改动真的发生。这是"审计写不进去就
    不动手"这条规则最直接的证据。
    """

    def __init__(self) -> None:
        self.applied: list[SystemEvolutionProposal] = []

    async def apply(self, proposal: SystemEvolutionProposal) -> None:
        self.applied.append(proposal)


@dataclass(frozen=True, slots=True)
class SelfEvolutionRig:
    """一套自我改进流程 —— 端口全是内存版的。"""

    flow: SelfEvolutionFlow
    store: MemoryStore
    approvals: MemoryApprovals
    criteria: MemoryCriteria
    audit: MemoryAudit
    applier: MemoryApplier


def self_evolution_rig(*, policy: SelfEvolutionPolicy | None = None) -> SelfEvolutionRig:
    """造一套流程。"""
    store = MemoryStore()
    approvals = MemoryApprovals()
    criteria = MemoryCriteria()
    audit = MemoryAudit()
    applier = MemoryApplier()
    ports = SelfEvolutionPorts(
        store=store,
        tester=MemoryTester(),
        approvals=approvals,
        criteria=criteria,
        audit=audit,
        applier=applier,
    )
    flow = SelfEvolutionFlow(policy=policy or SelfEvolutionPolicy(), ports=ports)
    return SelfEvolutionRig(
        flow=flow,
        store=store,
        approvals=approvals,
        criteria=criteria,
        audit=audit,
        applier=applier,
    )


def proposal_draft() -> ProposalDraft:
    """一份提案草稿。"""
    return ProposalDraft(
        proposal_id="p-1",
        project_id="proj-1",
        kind=ProposalKind.CODE_CHANGE,
        title="改一下调度阈值",
        target="core/policy/scheduling.py",
        change_summary="把容差从 0.1 调到 0.05",
        proposed_by="director",
    )


def proposal_approval(
    proposal: SystemEvolutionProposal,
    *,
    approver: str = "reviewer",
    approved: bool = True,
    scopes: tuple[ReviewerScope, ...] = (),
) -> ProposalApproval:
    """一份审批 —— **绑在这一版内容与这一版标准上**。"""
    return ProposalApproval(
        approver=approver,
        approved_digest=proposal.content_digest,
        criteria_version=proposal.criteria_version,
        approved=approved,
        scopes=scopes,
    )


async def submit_and_approve(rig: SelfEvolutionRig) -> SystemEvolutionProposal:
    """走完前三步，再把审批结论放进存储 —— **走到发布门口**。

    流程到这里该做的都做了：提交、独立测试、发起审批、记录审批、审批存储
    里也有结论。接下来的 ``publish`` 要是因为审计写不进去而失败，那失败
    的原因就只能是审计 —— 不会是"前置步骤没做"。
    """
    await rig.flow.submit(proposal_draft())
    await rig.flow.evaluate("p-1")
    await rig.flow.request_approval("p-1")
    proposal = rig.store.items["p-1"]
    await rig.flow.record_approval("p-1", proposal_approval(proposal))
    rig.approvals.conclusions["req-1"].append(proposal_approval(proposal))
    return proposal


__all__ = [
    "MemoryApplier",
    "MemoryApprovals",
    "MemoryAudit",
    "MemoryCriteria",
    "MemoryStore",
    "MemoryTester",
    "ProposalStatus",
    "RiskLevel",
    "ScriptedRunner",
    "SelfEvolutionRig",
    "failed",
    "not_started",
    "ok",
    "proposal_approval",
    "proposal_draft",
    "self_evolution_rig",
    "submit_and_approve",
]
