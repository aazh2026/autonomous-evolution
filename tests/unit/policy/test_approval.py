"""TASK-P1-046 —— 人工审批的领域模型与存储端口。

这一层不碰数据库，只回答三件事：**一条审批记录现在算什么**（结论）、**过期
怎么算**、**一条记录能不能被表态两次**。

**过期是这里最要紧的一条。** 人批的是"当时那一份 diff" —— diff 变了、代码变
了，那份批准就不再成立。所以过期不是"还没批"，而是一个**终态**：过了有效期的
记录不能用来放行，哪怕它当时是 approved。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.policy.approval import (
    ApprovalAlreadyDecidedError,
    ApprovalNotFoundError,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalVerdict,
    InMemoryApprovalStore,
    PollingApprovalSignal,
)

_NOW = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


def _request(
    *,
    request_id: str = "req-1",
    task_id: str = "TASK-1",
    operation: str = "merge_to_main",
    diff_summary: str = "README.md: 改一行",
    evidence_ref: str = "artifact://sha256:abc",
    status: ApprovalStatus = ApprovalStatus.PENDING,
    requested_by: str = "agent",
    requested_at: datetime = _NOW,
    decided_by: str | None = None,
    decided_at: datetime | None = None,
    expires_at: datetime | None = _NOW + timedelta(hours=24),
    comment: str = "",
) -> ApprovalRequest:
    """造一条审批请求。默认是一条刚发起的 pending，24 小时后过期。"""
    return ApprovalRequest(
        request_id=request_id,
        task_id=task_id,
        operation=operation,
        diff_summary=diff_summary,
        evidence_ref=evidence_ref,
        status=status,
        requested_by=requested_by,
        requested_at=requested_at,
        decided_by=decided_by,
        decided_at=decided_at,
        expires_at=expires_at,
        comment=comment,
    )


class TestTheOutcomeOfARecord:
    """一条记录现在给出什么结论。"""

    def test_a_pending_record_is_pending(self) -> None:
        assert _request().outcome(_NOW) is ApprovalOutcome.PENDING

    def test_an_approved_record_is_approved(self) -> None:
        approved = _request(status=ApprovalStatus.APPROVED, decided_by="alice")
        assert approved.outcome(_NOW) is ApprovalOutcome.APPROVED
        assert approved.releases(_NOW) is True

    def test_a_rejected_record_is_rejected(self) -> None:
        rejected = _request(status=ApprovalStatus.REJECTED, decided_by="alice")
        assert rejected.outcome(_NOW) is ApprovalOutcome.REJECTED
        assert rejected.releases(_NOW) is False


class TestExpiry:
    """过期 —— I-12 的验收点："审批过期后不可放行"。"""

    def test_an_expired_record_is_expired(self) -> None:
        request = _request(expires_at=_NOW + timedelta(minutes=1))
        assert request.outcome(_NOW + timedelta(minutes=2)) is ApprovalOutcome.EXPIRED

    def test_an_expired_approval_cannot_release(self) -> None:
        """**批过了也不能放行。**

        批的是"当时那一份 diff"。代码变了之后，那份批准说的就不再是眼前这
        份代码 —— 拿它放行，等于让人对一个自己没看过的版本点了头。
        """
        approved = _request(
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            expires_at=_NOW + timedelta(minutes=1),
        )
        later = _NOW + timedelta(minutes=2)
        assert approved.outcome(later) is ApprovalOutcome.EXPIRED
        assert approved.releases(later) is False

    def test_a_rejection_survives_the_expiry(self) -> None:
        """**拒绝是粘的。**

        人拒绝过一次，五年后这条请求也不会因为过期而变成"没人反对"。把拒绝
        盖成过期，等于把一次明确的红灯说成"灯没亮"。
        """
        rejected = _request(
            status=ApprovalStatus.REJECTED,
            decided_by="alice",
            expires_at=_NOW + timedelta(minutes=1),
        )
        assert rejected.outcome(_NOW + timedelta(days=365)) is ApprovalOutcome.REJECTED

    def test_a_record_without_an_expiry_never_expires(self) -> None:
        request = _request(expires_at=None)
        assert request.outcome(_NOW + timedelta(days=3650)) is ApprovalOutcome.PENDING

    def test_the_expiry_boundary_is_inclusive(self) -> None:
        """**到点即过期。**

        有效期是"到什么时候为止"，而不是"从什么时候开始"。把边界判成"还没
        过期"，就等于给了每一份批准多出一瞬间的余生 —— 而那一瞬间可能正好
        是 diff 改掉的那一刻。
        """
        request = _request(expires_at=_NOW)
        assert request.is_expired(_NOW) is True
        assert request.outcome(_NOW) is ApprovalOutcome.EXPIRED

    def test_a_naive_expiry_is_read_as_utc(self) -> None:
        """naive 时间按 UTC 处理 —— 混用本地时区会让"过期了吗"在不同机器上
        得到不同答案，而这个问题恰恰是最要紧的那个。

        真正会出事的是**混用**：到期时间是 naive（某台机器写的），而"现在"是
        带时区的（另一台机器问的）。统一按 UTC 读，两者能比；不统一，直接比
        会抛 "can't compare offset-naive and offset-aware datetimes" —— 审批门
        在最要紧的那一刻炸掉，而不是给出结论。
        """
        request = _request(expires_at=datetime(2026, 11, 9, 11, 0))

        later = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
        assert request.outcome(later) is ApprovalOutcome.EXPIRED

        earlier = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
        assert request.outcome(earlier) is ApprovalOutcome.PENDING


class TestTheRecordIsImmutable:
    """审批记录是证据，不能原地改。"""

    def test_a_decision_produces_a_new_record(self) -> None:
        original = _request()
        decided = original.with_decision(
            status=ApprovalStatus.APPROVED, decided_by="alice", decided_at=_NOW
        )
        assert original.status is ApprovalStatus.PENDING
        assert decided.status is ApprovalStatus.APPROVED
        assert decided.request_id == original.request_id

    def test_a_decided_record_cannot_be_decided_again(self) -> None:
        decided = _request(status=ApprovalStatus.APPROVED, decided_by="alice")
        with pytest.raises(ApprovalAlreadyDecidedError):
            decided.with_decision(status=ApprovalStatus.REJECTED, decided_by="bob", decided_at=_NOW)


class TestTheInMemoryStore:
    """内存存储 —— 单元测试用；生产路径用数据库实现。"""

    async def test_a_created_record_can_be_read_back(self) -> None:
        store = InMemoryApprovalStore()
        created = await store.create(_request())
        assert await store.get(created.request_id) == created

    async def test_an_unknown_id_is_refused(self) -> None:
        store = InMemoryApprovalStore()
        with pytest.raises(ApprovalNotFoundError):
            await store.get("没有这条")

    async def test_a_record_can_only_be_decided_once(self) -> None:
        store = InMemoryApprovalStore()
        await store.create(_request())
        await store.decide(
            request_id="req-1",
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=_NOW,
        )
        with pytest.raises(ApprovalAlreadyDecidedError):
            await store.decide(
                request_id="req-1",
                status=ApprovalStatus.REJECTED,
                decided_by="bob",
                decided_at=_NOW,
            )

    async def test_find_returns_the_latest_request(self) -> None:
        """一次拒绝之后又发起新的一次请求时，找到的必须是新那一条 ——
        否则这个任务会永远卡在旧的那次拒绝上。"""
        store = InMemoryApprovalStore()
        await store.create(_request(request_id="req-old", requested_at=_NOW))
        await store.create(_request(request_id="req-new", requested_at=_NOW + timedelta(minutes=1)))
        found = await store.find(task_id="TASK-1", operation="merge_to_main")
        assert found is not None
        assert found.request_id == "req-new"

    async def test_find_returns_none_when_there_is_no_record(self) -> None:
        store = InMemoryApprovalStore()
        assert await store.find(task_id="TASK-1", operation="merge_to_main") is None


class TestThePollingSignal:
    """阶段一的等待实现（轮询）。"""

    async def test_it_returns_when_the_record_is_decided(self) -> None:
        store = InMemoryApprovalStore()
        await store.create(_request())
        signal = PollingApprovalSignal(store, clock=lambda: _NOW)
        await store.decide(
            request_id="req-1",
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=_NOW,
        )
        verdict = await signal.wait("req-1", timeout=1.0)
        assert verdict.outcome is ApprovalOutcome.APPROVED
        assert verdict.is_approved is True

    async def test_it_gives_up_when_the_wait_times_out(self) -> None:
        """等不到就返回结论，不能一直挂着 —— 挂着的等待会让整个编排停在那
        儿，而人可能永远不会来点那个按钮。"""
        store = InMemoryApprovalStore()
        await store.create(_request())
        signal = PollingApprovalSignal(store, clock=lambda: _NOW, interval=0.01)
        verdict = await signal.wait("req-1", timeout=0.05)
        assert verdict.outcome is ApprovalOutcome.EXPIRED
        assert verdict.is_terminal is True
        assert verdict.request is not None
        assert verdict.request.status is ApprovalStatus.PENDING


class TestTheVerdict:
    """结论本身 —— 等待方据此决定还等不等。"""

    def test_a_pending_verdict_is_not_terminal(self) -> None:
        """**还在等人就不算定型。**

        把"还没定"当成"定了"，等待方会立刻走掉 —— 而人还在看。一个被判成
        定型的 pending 结论会让编排以为"人已经表态了"，而实际上没有人说
        过话。
        """
        verdict = ApprovalVerdict(outcome=ApprovalOutcome.PENDING, reason="等待人工审批")
        assert verdict.is_terminal is False

    def test_a_missing_verdict_is_not_terminal(self) -> None:
        verdict = ApprovalVerdict(outcome=ApprovalOutcome.MISSING, reason="没有审批记录")
        assert verdict.is_terminal is False

    def test_a_rejected_verdict_is_terminal(self) -> None:
        verdict = ApprovalVerdict(outcome=ApprovalOutcome.REJECTED, reason="审批被拒绝")
        assert verdict.is_terminal is True
        assert verdict.is_approved is False
