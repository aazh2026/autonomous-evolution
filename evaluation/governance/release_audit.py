"""TASK-P4-060 的 T2 —— 把真实的审批语料送进新的放行闸里跑一遍。

设计依据：docs/tasks.md ``TASK-P4-060``（高风险变更治理；交付物"架构级变更的独
立评审与放行流程"；验收条件"**标记为高风险的变更 100% 经过独立评审后才可执
行**"；验证 T1 + T2）、R-061（高风险操作独立授权）、R-102（不得自主修改控制自身
的系统）。

**这个模块不读数据库。** 它只做判定 —— 输入是若干条"真实审批请求"的投影，输
出是每一份变更在新规矩下的结论。数据从哪来是别的事（``scripts/run_release_
audit.py`` 从 Postgres 里读），这样这段判定能被单独测。

**为什么非得拿真语料跑。** 新闸的规矩写在纸上，谁都能写一串"应该会拦住"的自
测用例 —— 而那些用例里的数据是自己造的，**造出来的数据证明不了真数据上的效
果**。真语料里有什么就有什么：那批合并到主干的审批，每条只有一个人签了字。
**那正是这一次要量出来的东西。**

**三个场景，为什么是三个。**

1. ``as-is`` —— **实测**：照真语料原样送进新闸。量的是"**按现在这样跑下去会
   怎样**"。
2. ``paired`` —— **推演**：给每一份补上语料里另一位真实评审人。**推演不是实
   测**，它的作用是**证明闸门不是"一律拦住"**：份数齐了之后它放行。一道只会拦
   不会放的闸，和没有闸是一回事。
3. ``architecture`` —— **推演**：把每一份都当作架构级改动。**这一条量的是资格
   那一层** —— 真语料里没有"改动种类"这个信息，所以那一层在真数据上答不了这
   个问题，只能推演。

**现行规矩是基线。** 旧的审批门只要求"** somebody ≠ 提出人** 表态" —— 那批全
都过了。新规矩下的通过数与它的差，**就是这次治理的代价**。

**旧审批没有绑摘要 —— 它绑的是任务。** 回放时把那份结论绑到这份变更的内容摘要
上：这份评审**说的就是这一份改动**，绑摘要不是这次量出来的东西（旧数据里根本
没有摘要可比），而是新规矩加的那一道绑定。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from core.evolution.proposal import ProposalKind
from core.evolution.risk import ReviewerScope
from core.policy.autonomy import Operation
from core.policy.high_risk import (
    HighRiskChange,
    HighRiskError,
    HighRiskIndependenceError,
    HighRiskPolicy,
    HighRiskReview,
    ReviewerRegistry,
)
from orchestration.high_risk_review import (
    ChangeDraft,
    HighRiskReviewFlow,
    HighRiskReviewPorts,
    ReleaseAuditEvent,
    ReleaseEntry,
)

#: 语料里"有过结论"的两种状态 —— **只有表态过的才算一份评审**。
CONCLUDED_STATUSES: Final[frozenset[str]] = frozenset({"approved", "rejected"})

#: 拦住的原因 —— **按规矩分，不按句子分**。
#:
#: **为什么按消息分。** 流程抛的是同一个异常类型，区别只在消息里。消息是流程
#: 自己写的 —— **改了这个分类就会落空**，而落空不是悄悄的：没匹配上的记成
#: ``other``，看板上一眼看得出来。
REASON_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("没有发起过评审", "no_request"),
    ("没有评审结论", "no_reviews"),
    ("有人拒过", "rejected"),
    ("份独立评审", "not_enough_reviews"),
    ("提出人自己", "not_independent"),
    ("不是这一版内容", "digest_mismatch"),
    ("只有已批准的变更可以放行", "state"),
    ("评审资格不足", "missing_scope"),
)

#: 规则的名字 —— **看板上的那几行**。
RULE_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "passed": "放行了",
        "no_request": "没发起过评审",
        "no_reviews": "没有评审结论",
        "rejected": "有人拒过",
        "not_enough_reviews": "独立评审份数不够",
        "not_independent": "评审人里有提出人自己",
        "digest_mismatch": "评的不是这一版内容",
        "state": "状态不对",
        "missing_scope": "评审资格不足",
        "other": "其它（分类没匹配上）",
    }
)


@dataclass(frozen=True, slots=True)
class LiveApproval:
    """真实审批请求的一行 —— **从库里读出来的那一份**。

    :param request_id: 审批请求编号。
    :param task_id: 这次审批是为哪个任务发起的。
    :param project_id: 归属项目。
    :param operation: 什么操作。
    :param status: 审批状态。
    :param requested_by: 谁提的。
    :param decided_by: 谁表了态 —— **没表态就是没有**。
    :param title: 任务标题。
    :param diff_summary: 改动的摘要。
    :param evidence_ref: 证据引用。
    :param requested_at: 什么时候提的。
    :param decided_at: 什么时候表的态。
    """

    request_id: str
    task_id: str
    project_id: str
    operation: str
    status: str
    requested_by: str
    decided_by: str | None
    title: str
    diff_summary: str
    evidence_ref: str
    requested_at: datetime | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuditScenario:
    """一次审计的场景 —— **把真语料按某种假设送进新闸**。

    :param name: 场景编号。
    :param label: 人话。
    :param kind: 这一次**当作哪一类改动**。
    :param measured: **是不是实测** —— 推演的一律说推演。
    :param pair_reviewers: **要不要给每一份补上另一位真实评审人**。
    """

    name: str
    label: str
    kind: ProposalKind
    measured: bool
    pair_reviewers: bool


@dataclass(frozen=True, slots=True)
class ChangeVerdict:
    """一份变更在新闸下的结论。

    :param change_id: 变更编号 —— 这里就是任务号。
    :param task_id: 任务号。
    :param project_id: 归属项目。
    :param operation: 什么操作。
    :param kind: 这一次当作哪一类改动。
    :param risk: 风险档 —— **由规矩给的**。
    :param required_reviews: 这一份要几份独立评审。
    :param approvers: 批过的人。
    :param rule: 结论归到哪条规矩上。
    :param reason: **卡在哪** —— 拦住时的原话。
    """

    change_id: str
    task_id: str
    project_id: str
    operation: str
    kind: str
    risk: str
    required_reviews: int
    approvers: tuple[str, ...]
    rule: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReleaseAuditReport:
    """一次审计的结论。

    :param scenario: 场景编号。
    :param label: 场景的人话。
    :param measured: **是不是实测**。
    :param total: 一共几份变更。
    :param released: 放行了几份。
    :param blocked: 拦住了几份。
    :param rules: 按规矩分的条数。
    :param verdicts: 每一份变更的结论。
    :param self_reviews: **语料里"自己批自己"的条数** —— 那几条压根不算评审。
    """

    scenario: str
    label: str
    measured: bool
    total: int
    released: int
    blocked: int
    rules: Mapping[str, int]
    verdicts: tuple[ChangeVerdict, ...]
    self_reviews: int = 0

    def to_dict(self) -> dict[str, object]:
        """转成能写进 JSONL 的一行。"""
        return {
            "scenario": self.scenario,
            "label": self.label,
            "measured": self.measured,
            "total": self.total,
            "released": self.released,
            "blocked": self.blocked,
            "rules": dict(self.rules),
            "self_reviews": self.self_reviews,
            "verdicts": [
                {
                    "change_id": verdict.change_id,
                    "task_id": verdict.task_id,
                    "project_id": verdict.project_id,
                    "operation": verdict.operation,
                    "kind": verdict.kind,
                    "risk": verdict.risk,
                    "required_reviews": verdict.required_reviews,
                    "approvers": list(verdict.approvers),
                    "rule": verdict.rule,
                    "reason": verdict.reason,
                }
                for verdict in self.verdicts
            ],
        }


@dataclass(frozen=True, slots=True)
class LegacyGate:
    """现行规矩下的结果 —— **基线**。

    :param total: 一共几份审批请求。
    :param released: 按旧规矩放行了几个。
    :param self_approved: **提出人自己批了自己的有几个** —— 旧规矩拦不住这个。
    """

    total: int
    released: int
    self_approved: int


def classify(message: str) -> str:
    """把一条拦住的理由归到**规矩**上。"""
    for needle, rule in REASON_RULES:
        if needle in message:
            return rule
    return "other"


def legacy_gate(approvals: Sequence[LiveApproval]) -> LegacyGate:
    """现行规矩放行了几个 —— **基线**。

    **旧规矩是什么。** 一次审批 somebody ≠ 提出人表态就放行 —— 不看有没有第二
    个人，不看这个人懂不懂。**这就是新规矩要改的那件事。**
    """
    released = 0
    self_approved = 0
    for approval in approvals:
        if approval.status != "approved" or approval.decided_by is None:
            continue
        if approval.decided_by == approval.requested_by:
            self_approved += 1
            continue
        released += 1
    return LegacyGate(total=len(approvals), released=released, self_approved=self_approved)


def roster_from_corpus(
    approvals: Sequence[LiveApproval],
) -> Mapping[str, tuple[ReviewerScope, ...]]:
    """按语料里出现过的人造一份名册 —— **一律按最松的一档登记**。

    **为什么这么登记。** 语料里没有记"谁有什么资格" —— 只有名字。**登记得越松，
    过得越多** —— 所以按最松的一档（``general``）登记，是对新闸**最不利**的假
    设：**在这个假设下它都拦不住，那才是真的拦不住**；在这个假设下它拦住了，
    换成更严的登记只会拦得更死。
    """
    names = {approval.decided_by for approval in approvals if approval.decided_by}
    return MappingProxyType(dict.fromkeys(sorted(names), (ReviewerScope.GENERAL,)))


def scenario_as_is() -> AuditScenario:
    """**实测** —— 照真语料原样送进新闸。"""
    return AuditScenario(
        name="as-is",
        label="照真语料原样跑（实测）",
        kind=ProposalKind.CODE_CHANGE,
        measured=True,
        pair_reviewers=False,
    )


def scenario_paired() -> AuditScenario:
    """**推演** —— 给每一份补上语料里另一位真实评审人。

    **这一条量的是"闸门会不会放行"。** 一道只会拦不会放的闸，和没有闸是一回
    事 —— 所以得有一个场景证明它**在该放的时候放**。
    """
    return AuditScenario(
        name="paired",
        label="补上另一位真实评审人（推演）",
        kind=ProposalKind.CODE_CHANGE,
        measured=False,
        pair_reviewers=True,
    )


def scenario_architecture() -> AuditScenario:
    """**推演** —— 把每一份都当作架构级改动。

    **这一条量的是"资格那一层有没有牙齿"。** 真语料里没有"改动种类"这个信
    息，所以那一层在真数据上答不了这个问题 —— 只能推演。
    """
    return AuditScenario(
        name="architecture",
        label="当作架构级改动（推演）",
        kind=ProposalKind.ARCHITECTURE_CHANGE,
        measured=False,
        pair_reviewers=True,
    )


class _Changes:
    """内存变更存储。"""

    def __init__(self) -> None:
        self.items: dict[str, HighRiskChange] = {}

    async def save(self, change: HighRiskChange) -> None:
        self.items[change.change_id] = change

    async def get(self, change_id: str) -> HighRiskChange | None:
        return self.items.get(change_id)


class _Reviews:
    """内存评审存储 —— **权威的那一份**。"""

    def __init__(self) -> None:
        self.requests: list[HighRiskChange] = []
        self.conclusions: dict[str, list[HighRiskReview]] = {}

    async def request(self, change: HighRiskChange) -> str:
        self.requests.append(change)
        request_id = f"req-{len(self.conclusions) + 1}"
        self.conclusions.setdefault(request_id, [])
        return request_id

    async def record(self, request_id: str, review: HighRiskReview) -> None:
        self.conclusions.setdefault(request_id, []).append(review)

    async def fetch(self, request_id: str) -> tuple[HighRiskReview, ...]:
        return tuple(self.conclusions.get(request_id, ()))


@dataclass(frozen=True, slots=True)
class _RegistryPort:
    """把领域层的名册包成端口 —— **资格只能从这儿查**。"""

    registry: ReviewerRegistry

    async def scopes_of(self, reviewer: str) -> frozenset[ReviewerScope]:
        return self.registry.scopes_of(reviewer)

    async def covers(self, reviewer: str, required: Sequence[ReviewerScope]) -> bool:
        return self.registry.covers(reviewer, required)


class _Audit:
    """放行审计 —— **记下来的每一笔**。"""

    def __init__(self) -> None:
        self.events: list[ReleaseAuditEvent] = []

    async def record(self, event: ReleaseAuditEvent) -> None:
        self.events.append(event)


class _Executor:
    """执行处 —— **放行了什么，看这儿**。"""

    def __init__(self) -> None:
        self.executed: list[HighRiskChange] = []

    async def execute(self, change: HighRiskChange) -> None:
        self.executed.append(change)


class _Ledger:
    """放行台账。"""

    def __init__(self) -> None:
        self.entries: list[ReleaseEntry] = []

    async def append(self, entry: ReleaseEntry) -> None:
        self.entries.append(entry)


async def audit(
    approvals: Sequence[LiveApproval],
    *,
    scenario: AuditScenario,
    roster: Mapping[str, tuple[ReviewerScope, ...]],
) -> ReleaseAuditReport:
    """把语料按场景送进**真的**放行闸里跑一遍。

    **走的是生产的那条路。** 提交、发起评审、记录评审、放行 —— 四步都是
    :class:`~orchestration.high_risk_review.HighRiskReviewFlow` 里的真方法，
    连实体那一层的校验都是真的。**唯一被换掉的是存储** —— 真语料在库里，闸在
    内存里跑。
    """
    registry = ReviewerRegistry()
    for reviewer, scopes in roster.items():
        registry.register(reviewer, scopes)
    changes = _Changes()
    reviews = _Reviews()
    audit_log = _Audit()
    executor = _Executor()
    ledger = _Ledger()
    ports = HighRiskReviewPorts(
        changes=changes,
        reviews=reviews,
        registry=_RegistryPort(registry),
        audit=audit_log,
        executor=executor,
        ledger=ledger,
    )
    flow = HighRiskReviewFlow(policy=HighRiskPolicy(), ports=ports)

    everyone = sorted({a.decided_by for a in approvals if a.decided_by})
    verdicts: list[ChangeVerdict] = []
    self_reviews = 0
    for group in _group_by_task(approvals):
        verdict, skipped = await _audit_one(flow, group, scenario, everyone)
        verdicts.append(verdict)
        self_reviews += skipped

    rules: dict[str, int] = {}
    for verdict in verdicts:
        rules[verdict.rule] = rules.get(verdict.rule, 0) + 1
    released = sum(1 for verdict in verdicts if verdict.rule == "passed")
    return ReleaseAuditReport(
        scenario=scenario.name,
        label=scenario.label,
        measured=scenario.measured,
        total=len(verdicts),
        released=released,
        blocked=len(verdicts) - released,
        rules=MappingProxyType(rules),
        verdicts=tuple(verdicts),
        self_reviews=self_reviews,
    )


def _group_by_task(
    approvals: Sequence[LiveApproval],
) -> list[tuple[LiveApproval, ...]]:
    """按任务分组 —— **一个任务就是一次变更**。

    **为什么按任务而不是按审批请求。** 一次合并对应一个任务；那个任务上可能先
    批过又被拒、后来又批过一次 —— **那是同一次变更的几次表态**，不是几次变更。
    """
    grouped: dict[str, list[LiveApproval]] = {}
    for approval in approvals:
        grouped.setdefault(approval.task_id, []).append(approval)
    return [tuple(group) for group in grouped.values()]


def _usable(text: str, fallback: str) -> str:
    """这段话能不能用 —— **不能用就用一句实话顶上**。"""
    stripped = text.strip()
    if not stripped:
        return fallback
    return stripped


async def _audit_one(
    flow: HighRiskReviewFlow,
    group: tuple[LiveApproval, ...],
    scenario: AuditScenario,
    everyone: Sequence[str],
) -> tuple[ChangeVerdict, int]:
    """把一次变更送进闸里跑一遍 —— **返回结论与"自己批自己"的条数**。"""
    first = group[0]
    draft = ChangeDraft(
        change_id=first.task_id,
        project_id=first.project_id,
        operation=Operation(first.operation),
        kind=scenario.kind,
        title=_usable(first.title, f"任务 {first.task_id}"),
        target=_usable(first.evidence_ref, first.task_id),
        summary=_usable(
            first.diff_summary,
            f"操作 {first.operation}，证据 {first.evidence_ref}",
        ),
        proposed_by=first.requested_by,
    )
    change = await flow.submit(draft)
    await flow.request_review(change.change_id)

    approvers: set[str] = set()
    self_reviews = 0
    for approval in group:
        if approval.status not in CONCLUDED_STATUSES or approval.decided_by is None:
            continue
        review = HighRiskReview(
            reviewer=approval.decided_by,
            reviewed_digest=change.content_digest,
            approved=approval.status == "approved",
            decided_at=approval.decided_at,
        )
        try:
            await flow.record_review(change.change_id, review)
        except HighRiskIndependenceError:
            # **自己批自己的那一条压根不算评审** —— 记下来，但不作数。
            self_reviews += 1
            continue
        if review.approved:
            approvers.add(review.reviewer)

    if scenario.pair_reviewers:
        for reviewer in everyone:
            if reviewer in approvers or reviewer == change.proposed_by:
                continue
            review = HighRiskReview(
                reviewer=reviewer,
                reviewed_digest=change.content_digest,
                approved=True,
            )
            await flow.record_review(change.change_id, review)
            approvers.add(reviewer)

    try:
        outcome = await flow.release(change.change_id)
    except HighRiskError as error:
        message = str(error)
        return (
            ChangeVerdict(
                change_id=change.change_id,
                task_id=change.change_id,
                project_id=change.project_id,
                operation=change.operation.value,
                kind=change.kind.value,
                risk=change.risk.value,
                required_reviews=change.required_reviews,
                approvers=tuple(sorted(approvers)),
                rule=classify(message),
                reason=message,
            ),
            self_reviews,
        )
    return (
        ChangeVerdict(
            change_id=outcome.change.change_id,
            task_id=outcome.change.change_id,
            project_id=outcome.change.project_id,
            operation=outcome.change.operation.value,
            kind=outcome.change.kind.value,
            risk=outcome.change.risk.value,
            required_reviews=outcome.change.required_reviews,
            approvers=outcome.reviewers,
            rule="passed",
            reason="",
        ),
        self_reviews,
    )


__all__ = [
    "CONCLUDED_STATUSES",
    "REASON_RULES",
    "RULE_LABELS",
    "AuditScenario",
    "ChangeVerdict",
    "LegacyGate",
    "LiveApproval",
    "ReleaseAuditReport",
    "audit",
    "classify",
    "legacy_gate",
    "roster_from_corpus",
    "scenario_architecture",
    "scenario_as_is",
    "scenario_paired",
]
