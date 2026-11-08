"""人工审批门。

设计依据：docs/design.md §7（代码合并 = L2 需人工审批）、§1（人工审批等待）、
§6（Task 状态机）、docs/tasks.md TASK-P1-046、待裁决 I-12（审批应是信号/事件
而非轮询，且有超时与撤回语义）。

**门站在"策略说要人批"与"人批了"之间。** 策略层（``core/policy/autonomy.py``）
只管规则表：合并到 main 需要人批。它不知道人批没批 —— 那是审批记录里的事
实，存在数据库里。门把这两件事接起来，并且守住三条线：

1. **没有审批记录 = 不能放行。** 门是 :class:`ApprovalChecker` 的实现，策略
   判定时问的就是它。查不到记录时结论是"没有审批记录" —— 不是"批过了"。
2. **过期不可放行。** 人批的是"当时那一份 diff"。diff 变了、代码变了，那份
   批准就不再成立（I-12 的验收点）。
3. **拒绝即阻塞。** 人明确说了不，任务就该停在 ``BLOCKED`` 等人来看 —— 而
   不是继续往合并的方向走，或者被自动修复拉起来再撞一次同样的墙。

**门只算迁移，不落任务状态。** 任务状态归编排层管（``core/task/state_machine.py``
给出迁移结果，仓储层写库）。门把"该进 BLOCKED"这件事算清楚，剩下的是调用方
的事 —— 两边都写状态就会有一边写丢。

**阶段一不绑定 Temporal**（TASK-P1-046 交付物说明），等待由
:class:`PollingApprovalSignal` 轮询实现；但接口
(:class:`~core.policy.approval.ApprovalSignal`) 按信号形状定，阶段三换成
Temporal Signal 时调用方不用改。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core.policy.approval import (
    ApprovalExpiredError,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalSignal,
    ApprovalStatus,
    ApprovalStore,
    ApprovalVerdict,
    PollingApprovalSignal,
)
from core.policy.autonomy import Operation
from core.task.state_machine import (
    IllegalTransitionError,
    TaskAction,
    TaskState,
    TransitionContext,
    TransitionResult,
    apply_action,
)

#: 审批请求默认多久过期。
#:
#: **默认是"当天有效"，不是"永久有效"。** 永久有效的审批意味着人批过一次，
#: 之后任何人都能拿它放行任何一次合并 —— 批的是"当时那一次"，不是"以后所有
#: 一次"。
DEFAULT_TTL = timedelta(hours=24)

#: 请求没有过期时间时，等待的兜底上限（秒）。一小时：比人吃饭的时间长，比
#: 编排器空转一整夜短。
DEFAULT_WAIT_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class ApprovalRejection:
    """一次拒绝的结果。

    **拒绝一定被记下来了。** 人的决定是事实 —— 状态机不配合、调用方中途出错，
    都不该让这条拒绝消失。所以这里返回"记下来的那条请求"和"能不能顺带把任
    务阻塞掉"两件事，而不是一个二选一。
    """

    request: ApprovalRequest
    #: 任务进入 ``BLOCKED`` 的迁移结果。``None`` 表示当前状态不允许阻塞。
    blocked: TransitionResult | None
    reason: str


def block_after_rejection(current: TaskState, task_id: str) -> TransitionResult:
    """审批被拒 → 任务进入 ``BLOCKED``（design.md §6）。

    **被拒不是"再等等"。** 人已经说了不，任务就该停在那里等人来看。

    迁移非法时抛 :class:`IllegalTransitionError` —— 状态机不替人做主（§6
    第一条硬约束：非法迁移抛错，不做静默兜底）。调用方
    (:meth:`ApprovalGate.reject`) 会把它接住并如实报告，但**不会**因此丢掉
    那次拒绝。
    """
    return apply_action(current, TaskAction.BLOCK, TransitionContext(task_id=task_id))


class ApprovalGate:
    """人工审批门。

    用法：

    - 编排层在合并前 :meth:`request` 一次（带上 diff 摘要与验证证据引用）；
    - 人通过 API / CLI :meth:`approve` 或 :meth:`reject`；
    - 编排层 :meth:`await_decision` 等结果，或直接用门当
      :class:`~core.policy.autonomy.ApprovalChecker` 问策略层要判定。
    """

    def __init__(
        self,
        store: ApprovalStore,
        *,
        signal: ApprovalSignal | None = None,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._signal = (
            signal if signal is not None else PollingApprovalSignal(store, clock=self._clock)
        )
        self._ttl = ttl

    @property
    def store(self) -> ApprovalStore:
        return self._store

    async def request(
        self,
        *,
        task_id: str,
        operation: Operation,
        diff_summary: str,
        evidence_ref: str,
        requested_by: str,
    ) -> ApprovalRequest:
        """发起一次审批请求。

        **必须带上 diff 摘要与验证证据引用。** 人批的是"他看到的那个东西" ——
        没有 diff 摘要，批准就变成了对一件不知道是什么的事点头。这两个字段
        留空时直接报错，而不是记一条空记录：空记录在审批列表里看不出问题，
        直到某天有人拿它放行了一次不该放的合并。

        **有效期是门的属性，不是单次请求的属性。** 这个门发起的审批都活
        :attr:`ttl` 那么久；要另一种有效期就另开一个门 —— 让"这次请求特别
        急"这种说法没有实现的余地，期限才不会变成一件可以临时放宽的事。
        """
        if not diff_summary.strip():
            raise ValueError("审批请求必须带 diff 摘要 —— 人批的是他看到的那份改动")
        if not evidence_ref.strip():
            raise ValueError("审批请求必须带验证证据引用 —— 没有证据的改动不该被批准")
        if not requested_by.strip():
            raise ValueError("审批请求必须记下请求人")

        now = self._now()
        request = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            task_id=task_id,
            operation=operation.value,
            diff_summary=diff_summary,
            evidence_ref=evidence_ref,
            status=ApprovalStatus.PENDING,
            requested_by=requested_by,
            requested_at=now,
            expires_at=now + self._ttl,
        )
        return await self._store.create(request)

    async def approve(
        self, *, request_id: str, approver: str, comment: str = ""
    ) -> ApprovalRequest:
        """批准一条请求。

        **过期的请求批不了。** 这时抛 :class:`ApprovalExpiredError`，同时把
        请求标成 ``expired`` 留痕 —— 人确实点了按钮，但这条请求已经死了；
        把这次点击记下来，比让它无声无息地失败要好。
        """
        request = await self._store.get(request_id)
        now = self._now()
        if request.is_expired(now):
            await self._store.decide(
                request_id=request_id,
                status=ApprovalStatus.EXPIRED,
                decided_by=approver,
                decided_at=now,
                comment=comment or "过期后尝试批准",
            )
            raise ApprovalExpiredError(
                f"审批请求 {request_id} 已于 {request.expires_at} 过期，不能再批准；"
                f"请重新发起一次审批"
            )
        return await self._store.decide(
            request_id=request_id,
            status=ApprovalStatus.APPROVED,
            decided_by=approver,
            decided_at=now,
            comment=comment,
        )

    async def reject(
        self,
        *,
        request_id: str,
        approver: str,
        comment: str = "",
        task_state: TaskState | None = None,
    ) -> ApprovalRejection:
        """拒绝一条请求，并把任务推进 ``BLOCKED``。

        ``task_state`` 是任务当前的状态 —— 门不自己去读（任务状态归编排层
        管），调用方知道就传进来。**不传就只记拒绝，不动任务状态**：这比猜
        一个状态再迁移要诚实。

        **拒绝先落库，再算迁移。** 顺序反了的话，状态机抛错会把人的拒绝一
        并吞掉 —— 而那正是最不该丢的一条记录。
        """
        request = await self._store.decide(
            request_id=request_id,
            status=ApprovalStatus.REJECTED,
            decided_by=approver,
            decided_at=self._now(),
            comment=comment,
        )
        reason = f"审批请求 {request_id} 已被 {approver} 拒绝"
        blocked: TransitionResult | None = None
        if task_state is not None:
            try:
                blocked = block_after_rejection(task_state, request.task_id)
            except IllegalTransitionError as error:
                reason = f"{reason}；但任务当前状态 {task_state.value} 不允许阻塞（{error}）"
        return ApprovalRejection(request=request, blocked=blocked, reason=reason)

    async def withdraw(self, *, request_id: str, by: str, comment: str = "") -> ApprovalRequest:
        """撤回一条还没表态的请求（I-12 的撤回语义）。

        **撤回不是拒绝。** 撤回是"这条请求不作数了，重新提一条" —— 比如
        diff 改了、或发起人自己撤了。它**不**把任务推进 ``BLOCKED``：人还没说
        不，只是这条请求过期作废了。把两者混同，会让一次正常的重提看起来像
        一次人工否决。
        """
        return await self._store.decide(
            request_id=request_id,
            status=ApprovalStatus.WITHDRAWN,
            decided_by=by,
            decided_at=self._now(),
            comment=comment,
        )

    async def verdict(self, *, task_id: str, operation: Operation) -> ApprovalVerdict:
        """这个任务在这个操作上的审批结论。"""
        request = await self._store.find(task_id=task_id, operation=operation.value)
        if request is None:
            return ApprovalVerdict(
                outcome=ApprovalOutcome.MISSING,
                reason=f"任务 {task_id} 的 {operation.value} 没有任何审批记录",
            )
        outcome = request.outcome(self._now())
        return ApprovalVerdict(outcome=outcome, reason=_describe(outcome, request), request=request)

    async def verdict_for(self, request_id: str) -> ApprovalVerdict:
        """**指定那一条**请求的结论。

        与 :meth:`verdict` 的区别：那个按 (任务, 操作) 找**最近**的一条，
        这个只看给定的这一条。

        **为什么需要后者。** 合并路径批的是**具体的某个提交** —— 分支头变了
        就要重新批。那么"这次合并能不能做"就必须落在**同一条**请求上：按
        "最近一条"来判，人批的东西与被判定的东西就可能不是同一个。

        结论照旧由 :meth:`ApprovalRequest.outcome` 现算 —— 过期是读取时的
        判断，不依赖谁去跑一个"把过期请求翻成 expired"的后台任务。
        """
        request = await self._store.get(request_id)
        outcome = request.outcome(self._now())
        return ApprovalVerdict(outcome=outcome, reason=_describe(outcome, request), request=request)

    async def approval_outcome(self, *, task_id: str, operation: Operation) -> ApprovalOutcome:
        """:class:`~core.policy.autonomy.ApprovalChecker` 的实现。

        **策略层问的就是这个。** 没有记录时给 ``MISSING`` —— 不是"批过了"，
        也不是"还没批"，是"压根没申请"。
        """
        return (await self.verdict(task_id=task_id, operation=operation)).outcome

    async def await_decision(
        self, *, request_id: str, timeout: float | None = None
    ) -> ApprovalVerdict:
        """等这条请求有结论。

        **默认等的是请求自己的有效期**，不是拍一个固定时长：请求的 deadline
        就是这件事的 deadline，等更久没有意义（等到的是一条死掉的请求）。
        """
        request = await self._store.get(request_id)
        if timeout is None:
            timeout = self._seconds_until_expiry(request)
        return await self._signal.wait(request_id, timeout=timeout)

    def _seconds_until_expiry(self, request: ApprovalRequest) -> float:
        """离过期还有多少秒。"""
        if request.expires_at is None:
            return DEFAULT_WAIT_SECONDS
        expiry = request.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        remaining = (expiry - self._now()).total_seconds()
        return max(remaining, 0.0)

    def _now(self) -> datetime:
        return self._clock()


def _describe(outcome: ApprovalOutcome, request: ApprovalRequest) -> str:
    """把结论说成人话。"""
    match outcome:
        case ApprovalOutcome.APPROVED:
            return f"审批通过（审批人 {request.decided_by or '未知'}）"
        case ApprovalOutcome.PENDING:
            return f"等待人工审批（请求人 {request.requested_by}）"
        case ApprovalOutcome.REJECTED:
            return f"审批被拒绝（审批人 {request.decided_by or '未知'}）"
        case ApprovalOutcome.EXPIRED:
            return f"审批已过期（有效期至 {request.expires_at}），不能用于放行"
        case ApprovalOutcome.WITHDRAWN:
            return f"审批已被撤回（{request.decided_by or '未知'}），需重新发起"
        case ApprovalOutcome.MISSING:
            return "没有审批记录"


__all__ = [
    "DEFAULT_TTL",
    "DEFAULT_WAIT_SECONDS",
    "ApprovalGate",
    "ApprovalRejection",
    "block_after_rejection",
]
