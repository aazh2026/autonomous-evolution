"""人工审批 —— 领域模型、持久化端口与等待信号。

设计依据：docs/design.md §7（「代码合并」初始自主权 **L2 = 需要人工审批**；
"允许系统自主改进被管理的软件项目，不等于允许它自主修改控制自己的系统"）、
§1（人工审批等待）、docs/tasks.md TASK-P1-046、待裁决 I-12（人工审批应是
**信号 / 事件**而非轮询，且有超时与撤回语义）。

四条硬约束：

1. **审批是"有没有"的事实，不是"允不允许"的许可。** 一条审批请求有确定的
   生命周期状态（pending / approved / rejected / expired / withdrawn）；"这个
   操作能不能做"是策略层的事（``core/policy/autonomy.py``）。两者分开，策略
   才不会被审批记录污染 —— 策略说"合并需人批"，人批了不代表任何操作都能做。
2. **过期是终态。** 过了有效期的请求**不能**用来放行 —— 哪怕它当时是
   approved：批准的是"当时那一份 diff"，diff 变了、代码变了，批准就不再成立。
   所以过期是读取时算出来的，不依赖谁去跑一个"把过期请求翻成 expired"的
   后台任务。
3. **拒绝是粘的。** 人拒绝过一次，五年后这条请求也不会因为过期而变成
   "没人反对"。过期盖不住拒绝 —— 只有它自己的状态作废。
4. **等待是信号，不是轮询**（I-12）。本任务**不绑定 Temporal**
   （TASK-P1-046 交付物说明），阶段一的实现是轮询，但**接口先按信号的形状
   定下来**：等审批等的是"一条会来的通知"。阶段三（``TASK-P3-010``）把它换成
   Temporal Signal 时，调用方一行都不用改。

**为什么把审批拆成"领域模型 + 端口"**：``core`` 这一层不碰数据库，但它必须能
回答"这条审批能不能放行"。所以这里定义 :class:`ApprovalStore` 协议，实现分两
处 —— 内存版在本模块，数据库版在 ``infrastructure/database/approval.py``。
领域逻辑（过期怎么算、结论怎么映射）留在 core，不随存储位置漂移。做法与
:mod:`core.policy.budget` 的预算账本完全一致。

**操作名用字符串存**：领域模型里的 ``operation`` 是 ``str`` 而不是策略层的
``Operation`` 枚举。这样依赖方向才是自然的那一边 —— **策略层依赖审批层**
（策略要问审批"有没有批过"），审批层不认识策略层。代价是转换要显式做
（``Operation.MERGE_TO_MAIN.value``），换来的是两层之间没有环。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable


class ApprovalStatus(StrEnum):
    """审批请求的生命周期状态。

    前四个是 TASK-P1-046 交付物点名的四个状态；``withdrawn`` 是 I-12 要求的
    **撤回**语义 —— 人撤回的请求与过期的请求不是一回事：撤回是"这条请求不作
    数了，重新提"，过期是"它自己过期了"。
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class ApprovalOutcome(StrEnum):
    """一次审批查询给出的**结论**。

    与 :class:`ApprovalStatus` 的区别：状态是记录本身的属性，结论是"拿这条
    记录去问现在能不能放行"得到的答案。多出来的 ``MISSING`` 表示压根没有这
    条请求 —— 没有记录也是一种结论，而且是最该被看见的那种。
    """

    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"
    MISSING = "missing"


class ApprovalError(Exception):
    """审批相关的错误基类。"""


class ApprovalNotFoundError(ApprovalError):
    """找不到这条审批请求。"""


class ApprovalAlreadyDecidedError(ApprovalError):
    """这条审批请求已经有结论了。"""


class ApprovalExpiredError(ApprovalError):
    """这条审批请求已经过期，不能再放行或表态。

    **单独一个错误类型，而不是复用"已表态"。** 两者要修的问题完全不同：已
    表态是"有人表过态了，换个人来"，过期是"这条请求死了，重新提一条"。混成
    一个错误，运维就分不清该重试还是该重新发起。
    """


def _as_utc(moment: datetime) -> datetime:
    """把时间统一成 UTC。

    naive 时间按 UTC 处理 —— 全库统一走 UTC（``core/entities/base.py`` 的
    ``utcnow``）。混用本地时区会让"过期了吗"这个问题在不同机器上得到不同答
    案，而这个问题恰恰是审批门最要紧的那个。
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


#: 状态 → 结论的直译表。状态本身能决定结论时用它，省得每处都写一遍
#: ``match``。
_OUTCOME_BY_STATUS: Mapping[ApprovalStatus, ApprovalOutcome] = MappingProxyType(
    {
        ApprovalStatus.PENDING: ApprovalOutcome.PENDING,
        ApprovalStatus.APPROVED: ApprovalOutcome.APPROVED,
        ApprovalStatus.REJECTED: ApprovalOutcome.REJECTED,
        ApprovalStatus.EXPIRED: ApprovalOutcome.EXPIRED,
        ApprovalStatus.WITHDRAWN: ApprovalOutcome.WITHDRAWN,
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """一次审批请求。

    字段对应 TASK-P1-046 交付物：``task_id`` / ``operation`` / diff 摘要 /
    验证证据引用 / 状态 / 请求人 / 审批人 / 时间戳。

    **不可变。** 审批记录是**证据**：它记着"人在这时候看到了什么、批了什么"。
    改一条已落库的审批记录等于改证据 —— 所以结论通过新记录表达，而不是原地
    改字段。
    """

    request_id: str
    task_id: str
    operation: str
    diff_summary: str
    evidence_ref: str
    status: ApprovalStatus
    requested_by: str
    requested_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    comment: str = ""

    def is_expired(self, now: datetime) -> bool:
        """这条请求是否已经过了有效期。"""
        if self.expires_at is None:
            return False
        return _as_utc(self.expires_at) <= _as_utc(now)

    def outcome(self, now: datetime) -> ApprovalOutcome:
        """拿这条记录去问，**现在**的结论是什么。

        顺序有讲究：

        1. **拒绝最优先。** 人的拒绝是粘的 —— 它不会因为过期而变成"没人反
           对"。把拒绝盖成过期，等于把一次明确的红灯说成"灯没亮"。
        2. **过期盖住批准。** 过了有效期的 approved 不能放行：批准的是"当时
           那一份 diff"，而 diff 会变。
        3. 其余按状态直译。
        """
        if self.status is ApprovalStatus.REJECTED:
            return ApprovalOutcome.REJECTED
        if self.is_expired(now):
            return ApprovalOutcome.EXPIRED
        return _OUTCOME_BY_STATUS[self.status]

    def releases(self, now: datetime) -> bool:
        """这条记录现在是否足以放行。"""
        return self.outcome(now) is ApprovalOutcome.APPROVED

    def with_decision(
        self,
        *,
        status: ApprovalStatus,
        decided_by: str,
        decided_at: datetime,
        comment: str = "",
    ) -> ApprovalRequest:
        """返回一个带上结论的副本。

        **已经作废的记录不再接受新结论。** 一条过期或被撤回的请求要是还能被
        批准，"过期不可放行"这条就守不住了 —— 因为批准会把它变回 approved。
        拒绝一条已过期的请求倒是无害的（结论只会更严），但也不该发生：状态
        机只允许对 pending 的请求表态。
        """
        if self.status is not ApprovalStatus.PENDING:
            raise ApprovalAlreadyDecidedError(
                f"审批请求 {self.request_id} 已经是 {self.status.value}，不能再表态"
            )
        return replace(
            self,
            status=status,
            decided_by=decided_by,
            decided_at=decided_at,
            comment=comment,
        )


@dataclass(frozen=True, slots=True)
class ApprovalVerdict:
    """一次审批结论 —— 查询方拿到的答案。"""

    outcome: ApprovalOutcome
    reason: str
    request: ApprovalRequest | None = None

    @property
    def is_approved(self) -> bool:
        return self.outcome is ApprovalOutcome.APPROVED

    @property
    def is_terminal(self) -> bool:
        """结论是否已经定型（不会再变了）。

        ``PENDING`` 与 ``MISSING`` 之外的都是定型结论 —— 等待方据此停止等待。
        """
        return self.outcome not in (ApprovalOutcome.PENDING, ApprovalOutcome.MISSING)


@runtime_checkable
class ApprovalStore(Protocol):
    """审批请求的持久化端口。

    实现必须保证 :meth:`decide` 的**互斥**：一条请求只能被表态一次。两个人
    同时点"批准"和"拒绝"时，第二个必须失败，而不是把第一个的结论覆盖掉。
    """

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        """存下这条请求。"""
        ...

    async def get(self, request_id: str) -> ApprovalRequest:
        """取一条请求；不存在时抛 :class:`ApprovalNotFoundError`。"""
        ...

    async def find(self, *, task_id: str, operation: str) -> ApprovalRequest | None:
        """这个任务在这个操作上**最近**的一条请求；没有则返回 ``None``。"""
        ...

    async def decide(
        self,
        *,
        request_id: str,
        status: ApprovalStatus,
        decided_by: str,
        decided_at: datetime,
        comment: str = "",
    ) -> ApprovalRequest:
        """给一条请求下结论。"""
        ...

    async def list_for_task(self, task_id: str) -> tuple[ApprovalRequest, ...]:
        """这个任务的全部请求，按请求时间排序。"""
        ...


class InMemoryApprovalStore:
    """内存审批存储。

    同进程内的互斥由一把锁保证。**它不能跨进程** —— 两个进程各有一份内存，
    互相看不见对方的审批：进程 A 认为"还没批"，进程 B 认为"已经批了"。审批
    门恰恰是**多进程**的地方（编排器、API、CLI 都要看同一条审批），所以生产
    路径必须用数据库实现（``infrastructure/database/approval.py``）。

    它仍然有用：单元测试不需要数据库，而 T1 的四条断言里有三条与存储位置无
    关。
    """

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        async with self._lock:
            if request.request_id in self._requests:
                raise ApprovalError(f"审批请求 {request.request_id} 已存在")
            self._requests[request.request_id] = request
            return request

    async def get(self, request_id: str) -> ApprovalRequest:
        async with self._lock:
            request = self._requests.get(request_id)
        if request is None:
            raise ApprovalNotFoundError(f"找不到审批请求 {request_id}")
        return request

    async def find(self, *, task_id: str, operation: str) -> ApprovalRequest | None:
        """这个任务在这个操作上**最近**的一条请求。

        "最近"按请求时间排序 —— 一次拒绝之后又发起新的一次请求时，找到的必
        须是新那一条，而不是永远卡在旧的那次拒绝上。
        """
        async with self._lock:
            candidates = [
                request
                for request in self._requests.values()
                if request.task_id == task_id and request.operation == operation
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda request: (request.requested_at, request.request_id))

    async def decide(
        self,
        *,
        request_id: str,
        status: ApprovalStatus,
        decided_by: str,
        decided_at: datetime,
        comment: str = "",
    ) -> ApprovalRequest:
        async with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise ApprovalNotFoundError(f"找不到审批请求 {request_id}")
            decided = request.with_decision(
                status=status, decided_by=decided_by, decided_at=decided_at, comment=comment
            )
            self._requests[request_id] = decided
            return decided

    async def list_for_task(self, task_id: str) -> tuple[ApprovalRequest, ...]:
        async with self._lock:
            requests = [
                request for request in self._requests.values() if request.task_id == task_id
            ]
        return tuple(
            sorted(requests, key=lambda request: (request.requested_at, request.request_id))
        )


@runtime_checkable
class ApprovalSignal(Protocol):
    """审批决定的**等待**端口。

    **阶段一不绑定 Temporal**（TASK-P1-046 交付物说明），但接口按信号的形状
    定下来：等审批等的是"一条会来的通知"，而不是"反复去问还在不在"。I-12 说
    得很直接 —— 人工审批应是信号/事件而非轮询。阶段三（``TASK-P3-010``）把实
    现换成 Temporal Signal 时，调用方一行都不用改。

    实现必须保证**超时是终态**：等不到就返回结论，而不是永远挂着。挂着的等
    待会让整个编排停在那儿 —— 而人可能永远不会来点那个按钮。
    """

    async def wait(self, request_id: str, *, timeout: float) -> ApprovalVerdict:
        """等这条请求有结论。"""
        ...


class PollingApprovalSignal:
    """阶段一的等待实现：轮询存储。

    **轮询是临时形态，接口不是。** 间隔取 50 毫秒 —— 人工审批的响应时间以秒
    计，轮询频率与之无关；但间隔也不能是 0，否则等待会把 CPU 吃光，而人还是
    不会因此快一秒。

    **超时后返回的是"没等到"，不是"没批"。** 等待方的处境是"我不知道人批了没
    —— 我不在这儿了"，这与"人拒绝了"是两回事，不能混成一句。
    """

    def __init__(
        self,
        store: ApprovalStore,
        *,
        clock: Callable[[], datetime] | None = None,
        interval: float = 0.05,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._interval = interval

    async def wait(self, request_id: str, *, timeout: float) -> ApprovalVerdict:
        deadline = time.monotonic() + timeout
        while True:
            request = await self._store.get(request_id)
            outcome = request.outcome(self._clock())
            if outcome is not ApprovalOutcome.PENDING:
                return ApprovalVerdict(
                    outcome=outcome,
                    reason=_describe(outcome, request),
                    request=request,
                )
            if time.monotonic() >= deadline:
                return ApprovalVerdict(
                    outcome=ApprovalOutcome.EXPIRED,
                    reason=(
                        f"等了 {timeout:.1f} 秒还没有人对审批请求 {request_id} 表态，"
                        f"等待结束（请求本身仍是 {request.status.value}）"
                    ),
                    request=request,
                )
            await asyncio.sleep(self._interval)


def _describe(outcome: ApprovalOutcome, request: ApprovalRequest) -> str:
    """把结论说成人话。"""
    match outcome:
        case ApprovalOutcome.APPROVED:
            return f"审批通过（审批人 {request.decided_by or '未知'}）"
        case ApprovalOutcome.PENDING:
            return "等待人工审批"
        case ApprovalOutcome.REJECTED:
            return f"审批被拒绝（审批人 {request.decided_by or '未知'}）"
        case ApprovalOutcome.EXPIRED:
            return f"审批已过期（有效期至 {request.expires_at}）"
        case ApprovalOutcome.WITHDRAWN:
            return f"审批已被撤回（{request.decided_by or '未知'}）"
        case ApprovalOutcome.MISSING:
            return "没有审批记录"


__all__ = [
    "ApprovalAlreadyDecidedError",
    "ApprovalError",
    "ApprovalExpiredError",
    "ApprovalNotFoundError",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalSignal",
    "ApprovalStatus",
    "ApprovalStore",
    "ApprovalVerdict",
    "InMemoryApprovalStore",
    "PollingApprovalSignal",
]
