"""TASK-P1-046 —— 人工审批门与它的四条验收断言。

这一组验的是 T1 的四句话：

    ① ``merge_to_main`` 在无审批记录时被 ``PolicyManager`` 拒绝
    ② 审批通过后允许合并
    ③ 审批被拒后任务进入 ``BLOCKED`` 状态
    ④ 审批请求超时过期后不可再用于放行

**验的是判定结果，不是门自己的报告。** ① ② ④ 走的是真正的
:meth:`PolicyManager.authorize` —— 门只是被问的那个。③ 验的是**状态机迁移**
的结果（:func:`core.task.state_machine.apply_action`），不是门说"我把它阻塞
了"。

**门与策略各管一件事。** 门管"这条审批现在算什么"，策略管"这个操作能不能
做"。两者的接缝就是 :class:`ApprovalChecker` 这个端口 —— 所以这里既测门，也
测接缝：接缝断了的话，① 会变成"门说不行"，而那恰恰是门没被问到时最容易出
现的假象。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.policy.approval import (
    ApprovalExpiredError,
    ApprovalOutcome,
    ApprovalStatus,
    InMemoryApprovalStore,
)
from core.policy.autonomy import DecisionKind, Operation, PolicyManager, TaskContext
from core.task.state_machine import TaskAction, TaskState, TransitionContext, apply_action
from harness.approval.gate import ApprovalGate, block_after_rejection

_NOW = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


class _Clock:
    """一只可以被往前拨的钟。

    **过期这件事没法靠等。** 真等 24 小时不现实，而把"过期"这件事交给
    ``sleep`` 就等于把断言交给运气。一只可拨的钟让"24 小时后"成为一句可以
    直接写出来的话。
    """

    def __init__(self, now: datetime = _NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _context(task_id: str = "TASK-1") -> TaskContext:
    return TaskContext(agent_id="agent-1", task_id=task_id, project_id="proj-1")


def _gate(clock: _Clock | None = None) -> ApprovalGate:
    the_clock = clock or _Clock()
    return ApprovalGate(InMemoryApprovalStore(), clock=the_clock)


async def _request_merge(
    gate: ApprovalGate, *, task_id: str = "TASK-1", requested_by: str = "agent-1"
) -> str:
    """发起一次合并审批，返回请求 id。"""
    request = await gate.request(
        task_id=task_id,
        operation=Operation.MERGE_TO_MAIN,
        diff_summary="README.md: 改一行",
        evidence_ref="artifact://sha256:abc",
        requested_by=requested_by,
    )
    return request.request_id


class TestTheMergeIsRefusedWithoutAnApprovalRecord:
    """T1 ① —— 无审批记录时，合并被策略拒绝。"""

    async def test_the_policy_refuses_the_merge(self) -> None:
        gate = _gate()
        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context(), approval=gate
        )
        assert decision.is_allowed is False
        assert decision.needs_approval is True
        assert decision.approval_outcome is ApprovalOutcome.MISSING

    async def test_the_refusal_says_why(self) -> None:
        """理由里必须说清是"没有审批记录"，而不是"还在等审批"。

        这两件事对人的含义完全不同：前者是"你还没去申请"，后者是"人正在
        看"。混成一句，人就不知道该自己去申请还是该催审批。
        """
        gate = _gate()
        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context(), approval=gate
        )
        assert "没有审批记录" in decision.reason

    async def test_a_request_that_is_not_decided_yet_does_not_release(self) -> None:
        gate = _gate()
        await _request_merge(gate)
        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context(), approval=gate
        )
        assert decision.is_allowed is False
        assert decision.approval_outcome is ApprovalOutcome.PENDING

    async def test_without_a_checker_nothing_is_allowed_either(self) -> None:
        """**少一个查询口不等于放行。**

        没有查询口时结论停在"需要审批" —— 绝不能因为"没人问"就变成"允许"。
        """
        decision = await PolicyManager.default().authorize(Operation.MERGE_TO_MAIN, _context())
        assert decision.is_allowed is False
        assert decision.needs_approval is True


class TestTheMergeIsAllowedAfterTheApproval:
    """T1 ② —— 审批通过后允许合并。"""

    async def test_the_policy_allows_the_merge(self) -> None:
        gate = _gate()
        request_id = await _request_merge(gate)
        await gate.approve(request_id=request_id, approver="alice")

        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context(), approval=gate
        )
        assert decision.is_allowed is True
        assert decision.kind is DecisionKind.ALLOW
        assert decision.approval_outcome is ApprovalOutcome.APPROVED

    async def test_the_approval_names_who_approved_it(self) -> None:
        gate = _gate()
        request_id = await _request_merge(gate)
        approved = await gate.approve(request_id=request_id, approver="alice")
        assert approved.status is ApprovalStatus.APPROVED
        assert approved.decided_by == "alice"

    async def test_an_approval_of_another_task_does_not_release_this_one(self) -> None:
        """审批是**按任务**记的。别的任务批过了，这一个任务照样没有审批。

        否则"甲任务的人批了"就能放行"乙任务的合并" —— 审批记录一旦不绑定
        任务，人在批的就不是自己看到的那个东西。
        """
        gate = _gate()
        request_id = await _request_merge(gate, task_id="TASK-OTHER")
        await gate.approve(request_id=request_id, approver="alice")

        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context("TASK-1"), approval=gate
        )
        assert decision.is_allowed is False


class TestTheRejectedApprovalBlocksTheTask:
    """T1 ③ —— 审批被拒后任务进入 ``BLOCKED``。"""

    async def test_the_policy_denies_the_merge(self) -> None:
        gate = _gate()
        request_id = await _request_merge(gate)
        await gate.reject(request_id=request_id, approver="alice", comment="改得太糙")

        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context(), approval=gate
        )
        assert decision.is_allowed is False
        assert decision.kind is DecisionKind.DENY
        assert decision.approval_outcome is ApprovalOutcome.REJECTED

    async def test_the_decision_says_the_human_refused(self) -> None:
        """判定结果里要能分出"人拒绝了"与"请求过期了"。

        前者要把任务推进 ``BLOCKED``（人明确说了不），后者只需重新发起一次
        审批（人还没说）。只靠 ``reason`` 的中文文案去分，那是拿字符串编程。
        """
        gate = _gate()
        request_id = await _request_merge(gate)
        await gate.reject(request_id=request_id, approver="alice")

        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context(), approval=gate
        )
        assert decision.is_rejected_by_human is True

    async def test_the_task_moves_to_blocked(self) -> None:
        gate = _gate()
        request_id = await _request_merge(gate)
        rejection = await gate.reject(
            request_id=request_id, approver="alice", task_state=TaskState.PASSED
        )
        assert rejection.blocked is not None
        assert rejection.blocked.previous is TaskState.PASSED
        assert rejection.blocked.current is TaskState.BLOCKED
        assert rejection.blocked.changed is True

    async def test_the_rejection_is_recorded_even_when_the_task_cannot_be_blocked(self) -> None:
        """**人的决定是事实，不因为状态机不配合而消失。**

        任务当前状态不允许阻塞时，拒绝照样记下来 —— 报出来的是"记下了，但
        没能阻塞"，而不是"这次拒绝没发生"。
        """
        gate = _gate()
        request_id = await _request_merge(gate)
        rejection = await gate.reject(
            request_id=request_id, approver="alice", task_state=TaskState.PLANNED
        )
        assert rejection.blocked is None
        assert rejection.request.status is ApprovalStatus.REJECTED
        assert "不允许阻塞" in rejection.reason

    def test_the_block_transition_follows_the_state_machine(self) -> None:
        """门算出来的迁移必须**就是状态机算出来的那一条**。

        门自己写一份迁移表，就会有两份真相 —— 状态机改了而门没改，门就会把
        任务推进到一个状态机认为非法的态上。
        """
        result = block_after_rejection(TaskState.VERIFYING, "TASK-1")
        expected = apply_action(
            TaskState.VERIFYING,
            TaskAction.BLOCK,
            TransitionContext(task_id="TASK-1"),
        )
        assert result.previous == expected.previous
        assert result.current == expected.current
        assert result.action == expected.action


class TestTheExpiredApprovalCannotRelease:
    """T1 ④ —— 审批请求超时过期后不可再用于放行。"""

    async def test_an_expired_approval_does_not_release(self) -> None:
        clock = _Clock()
        gate = _gate(clock)
        request_id = await _request_merge(gate)
        await gate.approve(request_id=request_id, approver="alice")

        clock.advance(timedelta(hours=25))

        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context(), approval=gate
        )
        assert decision.is_allowed is False
        assert decision.approval_outcome is ApprovalOutcome.EXPIRED

    async def test_approving_an_expired_request_is_refused(self) -> None:
        """人点批准时请求已经过期 —— 这次点击必须**失败**，并且留痕。

        悄悄记成"已批准"是最坏的结果：人以为批过了，系统却始终不放行，两边
        都以为自己是对的。
        """
        clock = _Clock()
        gate = _gate(clock)
        request_id = await _request_merge(gate)

        clock.advance(timedelta(hours=25))

        with pytest.raises(ApprovalExpiredError):
            await gate.approve(request_id=request_id, approver="alice")

        stored = await gate.store.get(request_id)
        assert stored.status is ApprovalStatus.EXPIRED

    async def test_the_refusal_names_the_deadline(self) -> None:
        clock = _Clock()
        gate = _gate(clock)
        request_id = await _request_merge(gate)
        await gate.approve(request_id=request_id, approver="alice")

        clock.advance(timedelta(hours=25))

        verdict = await gate.verdict(task_id="TASK-1", operation=Operation.MERGE_TO_MAIN)
        assert verdict.outcome is ApprovalOutcome.EXPIRED
        assert "过期" in verdict.reason


class TestTheRequestCarriesWhatTheHumanNeeds:
    """审批请求必须指向具体的改动与证据。"""

    async def test_the_request_expires_after_the_gate_ttl(self) -> None:
        """**有效期是门的属性。**

        每条请求都带着一份会到期的期限 —— 不带期限的批准，等于一张永久授权
        票：人批过一次，之后谁都能拿它放行任何一次合并。
        """
        clock = _Clock()
        gate = ApprovalGate(InMemoryApprovalStore(), clock=clock, ttl=timedelta(hours=6))
        request = await gate.request(
            task_id="TASK-1",
            operation=Operation.MERGE_TO_MAIN,
            diff_summary="README.md: 改一行",
            evidence_ref="artifact://sha256:abc",
            requested_by="agent-1",
        )
        assert request.expires_at == clock.now + timedelta(hours=6)

    async def test_a_request_without_a_diff_summary_is_refused(self) -> None:
        gate = _gate()
        with pytest.raises(ValueError, match="diff 摘要"):
            await gate.request(
                task_id="TASK-1",
                operation=Operation.MERGE_TO_MAIN,
                diff_summary="   ",
                evidence_ref="artifact://sha256:abc",
                requested_by="agent-1",
            )

    async def test_a_request_without_evidence_is_refused(self) -> None:
        gate = _gate()
        with pytest.raises(ValueError, match="验证证据"):
            await gate.request(
                task_id="TASK-1",
                operation=Operation.MERGE_TO_MAIN,
                diff_summary="README.md: 改一行",
                evidence_ref="",
                requested_by="agent-1",
            )

    async def test_a_request_without_a_requester_is_refused(self) -> None:
        gate = _gate()
        with pytest.raises(ValueError, match="请求人"):
            await gate.request(
                task_id="TASK-1",
                operation=Operation.MERGE_TO_MAIN,
                diff_summary="README.md: 改一行",
                evidence_ref="artifact://sha256:abc",
                requested_by="",
            )


class TestTheWithdrawnRequest:
    """撤回（I-12）—— 请求作废，但它不是一次拒绝。"""

    async def test_a_withdrawn_request_does_not_release(self) -> None:
        gate = _gate()
        request_id = await _request_merge(gate)
        await gate.withdraw(request_id=request_id, by="agent-1", comment="diff 改了")

        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context(), approval=gate
        )
        assert decision.is_allowed is False
        assert decision.approval_outcome is ApprovalOutcome.WITHDRAWN

    async def test_a_withdrawal_is_not_a_rejection(self) -> None:
        """**撤回不是拒绝。**

        撤回是"这条请求不作数了，重新提一条" —— diff 改了、或发起人自己撤了。
        它不把任务推进 ``BLOCKED``：人还没说不，只是这条请求过期作废了。
        """
        gate = _gate()
        request_id = await _request_merge(gate)
        await gate.withdraw(request_id=request_id, by="agent-1")

        decision = await PolicyManager.default().authorize(
            Operation.MERGE_TO_MAIN, _context(), approval=gate
        )
        assert decision.is_rejected_by_human is False


class TestWaitingForTheDecision:
    """等结果 —— 阶段一的实现是轮询，接口按信号定（I-12）。"""

    async def test_it_returns_once_the_request_is_decided(self) -> None:
        gate = _gate()
        request_id = await _request_merge(gate)
        await gate.approve(request_id=request_id, approver="alice")

        verdict = await gate.await_decision(request_id=request_id, timeout=1.0)
        assert verdict.outcome is ApprovalOutcome.APPROVED
        assert verdict.is_approved is True

    async def test_it_gives_up_rather_than_waiting_forever(self) -> None:
        """等不到就返回结论。挂着的等待会让整个编排停在那儿，而人可能永远
        不会来点那个按钮。"""
        gate = _gate()
        request_id = await _request_merge(gate)

        verdict = await gate.await_decision(request_id=request_id, timeout=0.05)
        assert verdict.outcome is ApprovalOutcome.EXPIRED
        assert verdict.is_terminal is True
