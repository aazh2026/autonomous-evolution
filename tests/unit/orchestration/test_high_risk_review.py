"""TASK-P4-060 —— 高风险变更的放行流程：唯一一条执行路径。

验证六件事：

1. **走完全程才放行** —— 提交 → 发起评审 → 记录评审 → 放行。
2. **对象上的状态不算数** —— 放行前要从评审存储里把结论取回来重新核对。
3. **资格只认名册** —— 自己声明的、没登记的，都不算。
4. **通用资格不顶替专门资格** —— "什么都会一点的人"顶不了"真的懂架构的人"。
5. **审计写不进去 —— 就不放行**（失败关闭）。
6. **规矩给什么，流程就照着什么办** —— 风险档、评审数、资格要求。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields

import pytest

from core.evolution.proposal import ProposalKind
from core.evolution.risk import ReviewerScope
from core.evolution.taxonomy import RiskLevel
from core.policy.autonomy import Operation
from core.policy.high_risk import (
    HighRiskChange,
    HighRiskPolicy,
    HighRiskReview,
    HighRiskScopeError,
    HighRiskStatus,
)
from orchestration.high_risk_review import (
    ChangeDraft,
    HighRiskReviewError,
    HighRiskReviewFlow,
    HighRiskReviewPorts,
    ReleaseAuditError,
    ReleaseAuditEvent,
    ReleaseEntry,
)


def _forged(change: HighRiskChange, **changes: object) -> HighRiskChange:
    """伪造一份 —— **绕过冻结直接改字段**。

    **这就是"对象上的状态可以伪造"的具体样子。** 而这正是流程层存在的理由：
    **流程不信对象，它只信评审存储里的那份结论与名册。**
    """
    clone = object.__new__(type(change))
    for field_ in fields(change):
        object.__setattr__(clone, field_.name, getattr(change, field_.name))
    for name, value in changes.items():
        object.__setattr__(clone, name, value)
    return clone


class _Changes:
    """内存变更存储。"""

    def __init__(self) -> None:
        self.items: dict[str, HighRiskChange] = {}

    async def save(self, change: HighRiskChange) -> None:
        self.items[change.change_id] = change

    async def get(self, change_id: str) -> HighRiskChange | None:
        return self.items.get(change_id)


class _Reviews:
    """评审存储 —— **权威的那一份**。"""

    def __init__(self) -> None:
        self.requests: list[HighRiskChange] = []
        self.conclusions: dict[str, list[HighRiskReview]] = {}

    async def request(self, change: HighRiskChange) -> str:
        self.requests.append(change)
        request_id = f"req-{len(self.requests)}"
        self.conclusions.setdefault(request_id, [])
        return request_id

    async def record(self, request_id: str, review: HighRiskReview) -> None:
        self.conclusions.setdefault(request_id, []).append(review)

    async def fetch(self, request_id: str) -> tuple[HighRiskReview, ...]:
        return tuple(self.conclusions.get(request_id, ()))


class _Registry:
    """评审人名册 —— **谁有资格评审什么**。"""

    def __init__(self) -> None:
        self.profiles: dict[str, frozenset[ReviewerScope]] = {}

    def register(self, reviewer: str, scopes: Sequence[ReviewerScope]) -> None:
        self.profiles[reviewer] = frozenset(scopes)

    async def scopes_of(self, reviewer: str) -> frozenset[ReviewerScope]:
        return self.profiles.get(reviewer, frozenset())

    async def covers(self, reviewer: str, required: Sequence[ReviewerScope]) -> bool:
        wanted = set(required)
        if not wanted:
            return reviewer in self.profiles
        return wanted <= self.profiles.get(reviewer, frozenset())


class _Audit:
    """审计写入口 —— **可以故意写不进去**。"""

    def __init__(self) -> None:
        self.events: list[ReleaseAuditEvent] = []
        self.broken = False

    async def record(self, event: ReleaseAuditEvent) -> None:
        if self.broken:
            raise RuntimeError("审计存储写不进去")
        self.events.append(event)


class _Executor:
    """执行处 —— **只有流程能碰到它**。"""

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


@dataclass(frozen=True, slots=True)
class _Rig:
    """一套流程 —— **端口全是内存版的**。"""

    flow: HighRiskReviewFlow
    changes: _Changes
    reviews: _Reviews
    registry: _Registry
    audit: _Audit
    executor: _Executor
    ledger: _Ledger


def _rig(*, policy: HighRiskPolicy | None = None) -> _Rig:
    """造一套流程。"""
    changes = _Changes()
    reviews = _Reviews()
    registry = _Registry()
    audit = _Audit()
    executor = _Executor()
    ledger = _Ledger()
    ports = HighRiskReviewPorts(
        changes=changes,
        reviews=reviews,
        registry=registry,
        audit=audit,
        executor=executor,
        ledger=ledger,
    )
    flow = HighRiskReviewFlow(policy=policy or HighRiskPolicy(), ports=ports)
    return _Rig(flow, changes, reviews, registry, audit, executor, ledger)


def _draft(
    *,
    operation: Operation = Operation.MERGE_TO_MAIN,
    kind: ProposalKind = ProposalKind.CODE_CHANGE,
    risk: RiskLevel | None = None,
) -> ChangeDraft:
    """造一份变更草稿 —— **风险标记不在这里**。"""
    return ChangeDraft(
        change_id="c-1",
        project_id="proj-1",
        operation=operation,
        kind=kind,
        title="把调度器并进主干",
        target="core/policy/scheduling.py",
        summary="把适配层的分支合并回主干",
        proposed_by="director",
        risk=risk,
    )


async def _reviewed(rig: _Rig, *reviewers: str, draft: ChangeDraft | None = None) -> None:
    """走完前三步 —— **提交、发起评审、记录评审**。"""
    source = draft if draft is not None else _draft()
    await rig.flow.submit(source)
    await rig.flow.request_review("c-1")
    for reviewer in reviewers:
        change = rig.changes.items["c-1"]
        await rig.flow.record_review(
            "c-1",
            HighRiskReview(
                reviewer=reviewer,
                reviewed_digest=change.content_digest,
                approved=True,
            ),
        )


class TestTheWholePath:
    """走完全程 —— **四步缺一不可**。"""

    async def test_it_releases_after_the_reviews(self) -> None:
        rig = _rig()
        await _reviewed(rig, "reviewer-a", "reviewer-b")

        outcome = await rig.flow.release("c-1")

        assert outcome.reviewers == ("reviewer-a", "reviewer-b")
        assert len(rig.executor.executed) == 1
        assert rig.changes.items["c-1"].status.value == "released"

    async def test_the_audit_records_the_release(self) -> None:
        """**留痕是为了事后答得出"这次凭什么放的"。**"""
        rig = _rig()
        await _reviewed(rig, "reviewer-a", "reviewer-b")
        change = rig.changes.items["c-1"]

        await rig.flow.release("c-1")

        releases = [event for event in rig.audit.events if event.action == "release"]
        assert len(releases) == 1
        assert releases[0].digest == change.content_digest

    async def test_the_ledger_records_who_reviewed_it(self) -> None:
        """**台账是事后查得到的那一份。**"""
        rig = _rig()
        await _reviewed(rig, "reviewer-a", "reviewer-b")

        await rig.flow.release("c-1")

        assert len(rig.ledger.entries) == 1
        entry = rig.ledger.entries[0]
        assert entry.reviewers == ("reviewer-a", "reviewer-b")
        assert entry.required_reviews == 2

    async def test_it_cannot_be_released_before_it_is_reviewed(self) -> None:
        """**没批过的不许放。**"""
        rig = _rig()
        await rig.flow.submit(_draft())

        with pytest.raises(HighRiskReviewError, match="没有发起过评审"):
            await rig.flow.release("c-1")
        assert rig.executor.executed == []


class TestTheStoreIsTheTruth:
    """对象上的状态不算数 —— **存储里那份说了算**。"""

    async def test_a_forged_status_does_not_release(self) -> None:
        """**伪造一个"已批准"的对象 —— 放行不出去。**

        这就是"对象上的状态可以伪造"的具体样子：有人绕过状态机，直接把字段改成
        ``approved``。**流程不认对象上的字** —— 它回评审存储里把结论取出来重
        新核对。
        """
        rig = _rig()
        await _reviewed(rig, "reviewer-a", "reviewer-b")
        rig.changes.items["c-1"] = _forged(
            rig.changes.items["c-1"], reviews=(), status=HighRiskStatus.APPROVED
        )
        rig.reviews.conclusions["req-1"] = []

        with pytest.raises(HighRiskReviewError, match="没有评审结论"):
            await rig.flow.release("c-1")
        assert rig.executor.executed == []

    async def test_the_reviews_are_read_back_from_the_store(self) -> None:
        """**存储里少一份 —— 就不够数。**"""
        rig = _rig()
        await _reviewed(rig, "reviewer-a", "reviewer-b")
        rig.reviews.conclusions["req-1"] = rig.reviews.conclusions["req-1"][:1]

        with pytest.raises(HighRiskReviewError, match="独立评审"):
            await rig.flow.release("c-1")
        assert rig.executor.executed == []

    async def test_a_rejection_stops_the_release(self) -> None:
        """**有人拒过 —— 拒绝是终态。**"""
        rig = _rig()
        await rig.flow.submit(_draft())
        await rig.flow.request_review("c-1")
        change = rig.changes.items["c-1"]
        await rig.flow.record_review(
            "c-1",
            HighRiskReview(
                reviewer="reviewer-a",
                reviewed_digest=change.content_digest,
                approved=False,
            ),
        )

        with pytest.raises(HighRiskReviewError, match="拒过"):
            await rig.flow.release("c-1")
        assert rig.executor.executed == []


class TestTheRegistry:
    """名册 —— **资格只认名册**。"""

    async def test_an_unregistered_reviewer_does_not_cover_the_scope(self) -> None:
        """**没登记的人批了也不算"有资格的人批了"。**"""
        rig = _rig()
        await _reviewed(
            rig, "reviewer-a", "reviewer-b", draft=_draft(kind=ProposalKind.ARCHITECTURE_CHANGE)
        )

        with pytest.raises(HighRiskScopeError, match="评审资格"):
            await rig.flow.release("c-1")
        assert rig.executor.executed == []

    async def test_a_registered_reviewer_covers_the_scope(self) -> None:
        rig = _rig()
        rig.registry.register("arch-reviewer", [ReviewerScope.ARCHITECTURE])
        await _reviewed(
            rig,
            "arch-reviewer",
            "reviewer-b",
            draft=_draft(kind=ProposalKind.ARCHITECTURE_CHANGE),
        )

        outcome = await rig.flow.release("c-1")

        assert outcome.covered_scopes == ("architecture",)

    async def test_the_general_scope_does_not_substitute(self) -> None:
        """**"什么都会一点的人"顶不了"真的懂架构的人"。**"""
        rig = _rig()
        rig.registry.register("generalist", [ReviewerScope.GENERAL])
        await _reviewed(
            rig,
            "generalist",
            "reviewer-b",
            draft=_draft(kind=ProposalKind.ARCHITECTURE_CHANGE),
        )

        with pytest.raises(HighRiskScopeError, match="评审资格"):
            await rig.flow.release("c-1")
        assert rig.executor.executed == []


class TestIntegrity:
    """内容 —— **对不上就不放**。"""

    async def test_tampering_with_the_content_stops_the_release(self) -> None:
        """**内容被偷改 —— 那份评审就作废了。**"""
        rig = _rig()
        await _reviewed(rig, "reviewer-a", "reviewer-b")
        rig.changes.items["c-1"] = _forged(rig.changes.items["c-1"], summary="偷偷改成别的")

        with pytest.raises(HighRiskReviewError, match="这一版内容"):
            await rig.flow.release("c-1")
        assert rig.executor.executed == []

    async def test_the_proposer_cannot_be_among_the_reviewers(self) -> None:
        """**提出人自己批自己 —— 不算独立评审。**

        实体那一层在 ``record_review`` 时就拦下了；**这一条测的是"有人绕过它，
        直接往存储里塞一份"** —— 流程放行前要再查一遍。
        """
        rig = _rig()
        await rig.flow.submit(_draft())
        await rig.flow.request_review("c-1")
        change = rig.changes.items["c-1"]
        rig.reviews.conclusions["req-1"] = [
            HighRiskReview(
                reviewer="director",
                reviewed_digest=change.content_digest,
                approved=True,
            ),
            HighRiskReview(
                reviewer="reviewer-b",
                reviewed_digest=change.content_digest,
                approved=True,
            ),
        ]

        with pytest.raises(HighRiskReviewError, match="提出人"):
            await rig.flow.release("c-1")
        assert rig.executor.executed == []


class TestFailClosed:
    """失败关闭 —— **审计写不进去就不放行**。"""

    async def test_a_broken_audit_blocks_the_release(self) -> None:
        """**没留痕的放行比没放行的变更更糟** —— 前者连"发生过"都答不上来。"""
        rig = _rig()
        await _reviewed(rig, "reviewer-a", "reviewer-b")
        rig.audit.broken = True

        with pytest.raises(ReleaseAuditError, match="审计"):
            await rig.flow.release("c-1")
        assert rig.executor.executed == []
        assert rig.ledger.entries == []


class TestTheFlowsOwnRules:
    """规矩给的那些数 —— **流程照着办**。"""

    async def test_a_merge_is_high_risk_and_needs_two_reviews(self) -> None:
        """**合并到主干天生是高风险** —— 而高风险要两份独立评审。"""
        rig = _rig()
        change = await rig.flow.submit(_draft())

        assert change.risk is RiskLevel.HIGH
        assert change.required_reviews == 2

    async def test_an_ordinary_change_is_medium_risk(self) -> None:
        rig = _rig()
        change = await rig.flow.submit(
            _draft(operation=Operation.CODE_CHANGES, kind=ProposalKind.CODE_CHANGE)
        )

        assert change.risk is RiskLevel.MEDIUM
        assert change.required_reviews == 1

    async def test_the_caller_may_only_mark_higher(self) -> None:
        """**只能往高里标** —— 往低里标的那一份，压根标不出来。

        提出人把合并标成"低风险"，流程**不听** —— 风险档取天生的下限与所报值
        里更高的那个。
        """
        rig = _rig()
        change = await rig.flow.submit(_draft(risk=RiskLevel.LOW))

        assert change.risk is RiskLevel.HIGH
        assert change.required_reviews == 2

    async def test_the_policy_may_make_it_stricter(self) -> None:
        """**项目可以更严** —— 那是项目自己的选择。"""
        policy = HighRiskPolicy(reviews_by_risk={RiskLevel.HIGH: 3})
        rig = _rig(policy=policy)
        change = await rig.flow.submit(_draft())

        assert change.required_reviews == 3

    async def test_the_scope_floor_comes_from_the_kind(self) -> None:
        """**改架构的改动 —— 得有架构评审人批。**"""
        rig = _rig()
        change = await rig.flow.submit(_draft(kind=ProposalKind.ARCHITECTURE_CHANGE))

        assert change.required_scopes == (ReviewerScope.ARCHITECTURE,)
