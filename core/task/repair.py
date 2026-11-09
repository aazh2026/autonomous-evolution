"""有界自动修复 —— 领域契约。

设计依据：docs/design.md §6（"修复失败必须有次数、成本和时间上限"）；
docs/tasks.md TASK-P1-063（"三重上限为全局共享（次数 + 成本 + 时间），不是
每任务独立"）、R-098、I-04。

**三重上限是全局的，这是本模块存在的全部理由。** 状态机里已经有一条**每任务**
的修复次数闸门（:class:`core.task.state_machine.RepairBudgetExhaustedError`），
但每任务独立的上限挡不住并行的情况：两个任务各修 3 次，看着都没超，
合起来就是 6 次 —— 而上限说的是 5 次。**"上限"这个词只有在它是被共享的那一份
时才有意义**，所以这里定义的是一个**全局账本**，不是每任务一个计数器。

**三重上限分别是三样不同的东西，缺一不可。**

- **次数** —— 修了几次。次数用尽说明这条路走不通了。
- **成本** —— 花了多少钱。次数不多但每次很贵，同样会拖垮项目。
- **时间** —— 修了多久。**时间这条最容易被漏掉**：一次修复只要还在"修"，
  它就一直占着执行器；不设时限，一个跑不出来的任务能把整轮演进拖到天亮。

**为什么"预留"是一个动作而不是"查询"加"占用"两步。** 两步之间只要有让出
控制权的缝隙（``await``），两个并行的任务就会各自看到"还有 3 次"然后各自
占用 —— 总数突破了上限，而**没有任何一个任务报错**。所以
:meth:`RepairLedger.reserve` 把检查与占用合成一步，账本内部用一把锁把这件事
做实（同进程）；跨进程要换成数据库的行级锁，端口不变。

**全局是"按项目"的。** 范围键是 ``project_id`` —— 一个项目的修复额度不该被
另一个项目用掉，而同一个项目的所有任务共享同一份额度。

**失败归类不解析人话。** :class:`FailureSignal` 里全是结构化的字段：归类靠
:class:`FailureKind`，不靠读执行器写的措辞 —— 措辞是执行器定的，改一次措辞
就改一次分类，而分类决定"要不要自己修"。

**三类归类，三种后续动作。** 能自动修的排一次修复任务；得人来看的挂起等审批；
修不了的放弃（并留一笔记录 —— 悄悄放弃比悄悄失败更难查）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from core.task.execution_loop import (
    DEFAULT_MAX_REPAIRS,
    FailureCategory,
    FailureDiagnosis,
    RepairTask,
)
from core.task.outcome import TaskOutcome, VerificationVerdict

#: 修复次数的默认上限。与状态机每任务闸门的默认值一致 —— 不另立口径。
DEFAULT_MAX_ATTEMPTS: Final[int] = DEFAULT_MAX_REPAIRS


class RepairLimit(StrEnum):
    """三重上限里的哪一条被撞了。

    **要能说出撞的是哪一条。** "修复停了"是一句没法行动的话；"修复成本超了"
    才知道该批预算还是该改口径。
    """

    #: 次数用尽。
    COUNT = "count"
    #: 成本超了。
    COST = "cost"
    #: 时限到了。
    TIME = "time"


@dataclass(frozen=True, slots=True)
class RepairLimits:
    """全局共享的三重上限。

    ``max_cost`` 与 ``max_duration`` 为 ``None`` 表示**这一维不设限** —— 不是
    "上限是零"。三样都设死会让配置变得很啰嗦，而"先只限次数"是常见的起步
    姿态。

    :attr:`max_attempts` 是**全局**的次数上限，不是每任务的 —— 每任务那条在
    状态机里（:class:`core.task.state_machine.RepairBudgetExhaustedError`）。
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_cost: Decimal | None = None
    max_duration: timedelta | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 0:
            raise ValueError("修复次数上限不能是负数")
        if self.max_cost is not None and self.max_cost < 0:
            raise ValueError("修复成本上限不能是负数")
        if self.max_duration is not None and self.max_duration < timedelta(0):
            raise ValueError("修复时限不能是负数")


@dataclass(frozen=True, slots=True)
class RepairUsage:
    """全局已用掉的量。

    :attr:`elapsed` 是**修复窗口已经开了多久** —— 从第一次预留起算，而不是
    各项修复用时之和：时限要管的是"这件事已经拖了多久"，不是"加起来修了
    多久"（后者要预先知道每次要修多久，那种估计多半不准）。
    """

    attempts: int = 0
    cost: Decimal = Decimal(0)
    elapsed: timedelta = timedelta(0)


@dataclass(frozen=True, slots=True)
class RepairReservation:
    """一次预留的结果。

    :attr:`limit` 是**撞到的那一条上限**（拒绝时才有值）—— 调用方要据此
    决定是补预算、提配额还是认账停下。
    """

    #: 批不批。
    granted: bool
    #: 撞到的上限（拒绝时）。
    limit: RepairLimit | None = None
    #: 这次预留之后的用量。
    usage: RepairUsage = field(default_factory=RepairUsage)
    #: 人话理由。
    reason: str = ""


@runtime_checkable
class RepairLedger(Protocol):
    """修复额度的账本 —— **全局共享**的那一份。

    **端口而不是具体实现。** 领域层不该知道账本存在（分层：
    ``infrastructure → core``）；而"三重上限怎么算"是领域规则 —— 所以规则写
    在领域层，写账本的实现放在别处。

    **预留是一个原子动作。** :meth:`reserve` 把"检查"与"占用"合成一步 ——
    分成两步的话，两个并行的任务会各自看到"还有额度"然后各自占用，总数就
    突破了全局上限，而**没有一个任务会报错**。
    """

    async def reserve(
        self,
        *,
        scope: str,
        cost: Decimal,
        limits: RepairLimits,
    ) -> RepairReservation:
        """为一次修复预留额度。

        :param scope: 额度范围（项目 id）—— **同一范围共享同一份额度**。
        :param cost: 这次修复预计要花的钱。
        :param limits: 该范围适用的上限。

        批了就把额度记上去；不批就**一点都不记** —— 记一半比不记更糟
        （额度凭空少了一截，却没人真的修过）。
        """
        ...

    async def usage(self, scope: str) -> RepairUsage:
        """这个范围已经用掉多少。"""
        ...


def first_violation(
    usage: RepairUsage,
    *,
    cost: Decimal,
    limits: RepairLimits,
) -> tuple[RepairLimit, str] | None:
    """这次预留会撞上哪一条上限。**没撞就返回 ``None``。**

    **检查顺序是固定的**（次数 → 成本 → 时间），这样同一份用量总是报出同一条
    上限 —— 读日志的人看到的"卡在哪"是稳定的，不会这次报次数下次报成本。

    **到限即停。** 次数：再修一次就超了（所以上限为 3 时第 3 次可以修、第 4
    次不行）；成本：加上这次就超了；时间：窗口已经到顶了。
    """
    if usage.attempts + 1 > limits.max_attempts:
        return (
            RepairLimit.COUNT,
            f"修复次数已达全局上限 {limits.max_attempts} 次（已用 {usage.attempts} 次）",
        )
    if limits.max_cost is not None and usage.cost + cost > limits.max_cost:
        return (
            RepairLimit.COST,
            f"修复成本将超过全局上限 {limits.max_cost}（已用 {usage.cost}，本次 {cost}）",
        )
    if limits.max_duration is not None and usage.elapsed >= limits.max_duration:
        return (
            RepairLimit.TIME,
            f"修复总时长已达全局上限 {limits.max_duration}（已用 {usage.elapsed}）",
        )
    return None


@dataclass(slots=True)
class _ScopeState:
    """一个范围（项目）的额度账。"""

    attempts: int = 0
    cost: Decimal = Decimal(0)
    #: 修复窗口的起点 —— 第一次预留的时刻。
    started_at: datetime | None = None


class InMemoryRepairLedger:
    """进程内的修复额度账本。

    **一把锁把"检查 + 占用"做成一件事。** 同进程内两个并行的任务抢同一份额度
    时，锁保证只有一个能看到"还有额度" —— 这就是 T1 ④ 那种场景要挡住的事。

    **跨进程不成立。** 多个编排器进程各有一本账，就各算各的。要跨进程共享，
    得换成数据库实现（端口不变，实现换成带行级锁的那一份）—— 阶段一一个项目
    由一个编排器进程负责，所以这一版够用；**但这个边界写在代码里，不靠
    记忆**：见 :class:`RepairLedger` 的说明。
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._lock = asyncio.Lock()
        self._scopes: dict[str, _ScopeState] = {}
        self._clock = clock or (lambda: datetime.now(UTC))

    async def reserve(
        self,
        *,
        scope: str,
        cost: Decimal,
        limits: RepairLimits,
    ) -> RepairReservation:
        async with self._lock:
            state = self._scopes.setdefault(scope, _ScopeState())
            now = self._clock()
            elapsed = _elapsed_since(state.started_at, now)
            current = RepairUsage(attempts=state.attempts, cost=state.cost, elapsed=elapsed)

            violation = first_violation(current, cost=cost, limits=limits)
            if violation is not None:
                limit, reason = violation
                return RepairReservation(granted=False, limit=limit, usage=current, reason=reason)

            state.attempts += 1
            state.cost += cost
            if state.started_at is None:
                state.started_at = now
            return RepairReservation(
                granted=True,
                usage=RepairUsage(
                    attempts=state.attempts,
                    cost=state.cost,
                    elapsed=_elapsed_since(state.started_at, now),
                ),
            )

    async def usage(self, scope: str) -> RepairUsage:
        async with self._lock:
            state = self._scopes.get(scope)
            if state is None:
                return RepairUsage()
            return RepairUsage(
                attempts=state.attempts,
                cost=state.cost,
                elapsed=_elapsed_since(state.started_at, self._clock()),
            )


def _elapsed_since(started_at: datetime | None, now: datetime) -> timedelta:
    """修复窗口已经开了多久。**还没开过就是零。**"""
    if started_at is None:
        return timedelta(0)
    return now - started_at


class FailureKind(StrEnum):
    """失败的种类 —— **归类靠它，不靠解析人话**。

    每一类的后续动作不同，所以每一类都得能单独说出来。
    """

    #: 必检项没过（单测、集成测试、lint、架构规则）—— 就是改代码的事。
    CHECK_FAILED = "check_failed"
    #: 执行器自己崩了（起不来、装不上依赖）。
    EXECUTOR_ERROR = "executor_error"
    #: 验证引擎自己崩了 —— 没得出结论。
    VERIFIER_ERROR = "verifier_error"
    #: 有人取消。
    CANCELLED = "cancelled"
    #: 超时（且恢复探测没救回来）。
    TIMED_OUT = "timed_out"
    #: **任务的前提本身不成立** —— 验收标准与现状冲突、依赖的公共接口没了。
    SPEC_CONFLICT = "spec_conflict"
    #: 判不出是什么。
    UNKNOWN = "unknown"


#: 种类 → 归类。**一张表，改一处。**
#:
#: ``SPEC_CONFLICT`` 是"不可修复"那一类：改这个任务的代码解决不了它，硬修只会
#: 把问题推到别处 —— 而"推到别处"正是自动修复最该避免的。
#:
#: ``UNKNOWN`` 归"需人工"而不是"不可修复"：判不出是什么的时候，人往往知道
#: （"哦，接口改了"），而自动放弃一个任务比让人看一眼更糟。
_KIND_CATEGORY: Final[dict[FailureKind, FailureCategory]] = {
    FailureKind.CHECK_FAILED: FailureCategory.AUTO_REPAIRABLE,
    FailureKind.EXECUTOR_ERROR: FailureCategory.NEEDS_HUMAN,
    FailureKind.VERIFIER_ERROR: FailureCategory.NEEDS_HUMAN,
    FailureKind.CANCELLED: FailureCategory.NEEDS_HUMAN,
    FailureKind.TIMED_OUT: FailureCategory.NEEDS_HUMAN,
    FailureKind.SPEC_CONFLICT: FailureCategory.UNREPAIRABLE,
    FailureKind.UNKNOWN: FailureCategory.NEEDS_HUMAN,
}


@dataclass(frozen=True, slots=True)
class FailureSignal:
    """一次失败的原始信号 —— 归类的输入。

    **字段都是结构化的。** 没有自由文本参与判定 —— 只有 :attr:`reason` 与
    :attr:`detail` 是给人看的，它们不参与分类。措辞是执行器写的，改一次措辞
    就改一次分类，而分类决定"要不要自己修"。
    """

    kind: FailureKind
    reason: str = ""
    detail: str = ""
    #: 没过的必检项。
    failed_checks: tuple[str, ...] = ()


def kind_of(
    outcome: TaskOutcome,
    verdict: VerificationVerdict | None,
    *,
    failed_checks: Sequence[str] = (),
) -> FailureKind:
    """从判定结果与验证结论推出失败种类。

    **推不出的就归 ``UNKNOWN``，不猜。** 猜出来的类别会决定"要不要自己修"
    —— 猜错一次就是一次不该发生的返工。

    :attr:`FailureKind.SPEC_CONFLICT` **推不出来**，只能由知道根因的人给出来
    —— 判定"前提不成立"要看任务之外的东西（依赖的公共接口没了、验收标准与
    现状冲突），那些不是执行日志里读得出的。

    **"验证判失败"本身就是一种必检项没过。** 执行器报"我做完了"而验证引擎说
    "没过" —— 这正是最该自动修的一种失败（改代码就是了），所以它归
    :attr:`FailureKind.CHECK_FAILED`，哪怕没列出是哪几项没过。

    **这个函数只对失败调用。** 传进来一个"没失败"的判定，结果没有意义 ——
    它会落到 :attr:`FailureKind.EXECUTOR_ERROR`（归"需人工"）。那是**保守**
    的方向：自动去修一个没坏的东西，比停下来问一句糟得多。
    """
    if outcome is TaskOutcome.CANCELLED:
        kind = FailureKind.CANCELLED
    elif outcome is TaskOutcome.TIMED_OUT:
        kind = FailureKind.TIMED_OUT
    elif outcome is TaskOutcome.VERIFICATION_ERROR or verdict is VerificationVerdict.ERROR:
        kind = FailureKind.VERIFIER_ERROR
    elif failed_checks or verdict is VerificationVerdict.FAILED:
        kind = FailureKind.CHECK_FAILED
    elif outcome is TaskOutcome.FAILED:
        kind = FailureKind.UNKNOWN
    else:
        kind = FailureKind.EXECUTOR_ERROR
    return kind


def classify(signal: FailureSignal) -> FailureDiagnosis:
    """把失败信号归成诊断。

    **归类决定后续动作**：能自动修的排修复任务，得人来看的挂起，修不了的
    放弃（并留一笔记录 —— 悄悄放弃比悄悄失败更难查）。
    """
    category = _KIND_CATEGORY[signal.kind]
    reason = signal.reason.strip() or _default_reason(signal.kind)
    return FailureDiagnosis(
        reason=reason,
        category=category,
        detail=signal.detail,
        repair_hint=_default_hint(signal, category),
        evidence=signal.failed_checks,
    )


#: 每类失败的默认说法。**说得清是哪一类** —— 理由缺了没关系，类别不能缺。
_DEFAULT_REASON: Final[dict[FailureKind, str]] = {
    FailureKind.CHECK_FAILED: "必检项没过",
    FailureKind.EXECUTOR_ERROR: "执行器自己出错",
    FailureKind.VERIFIER_ERROR: "验证引擎自己出错 —— 没有得出结论",
    FailureKind.CANCELLED: "执行已取消",
    FailureKind.TIMED_OUT: "执行超时且恢复探测没救回来",
    FailureKind.SPEC_CONFLICT: "任务的前提本身不成立",
    FailureKind.UNKNOWN: "判不出失败原因",
}


def _default_reason(kind: FailureKind) -> str:
    """没说理由时的默认说法。"""
    return _DEFAULT_REASON[kind]


def _default_hint(signal: FailureSignal, category: FailureCategory) -> str:
    """给下一轮执行的施工建议。

    **只有能自动修的才给建议。** 给"需人来看"的失败写修复建议，等于在告诉
    Agent "照这个改" —— 而那个建议没人会批。
    """
    if category is not FailureCategory.AUTO_REPAIRABLE:
        return ""
    checks = "、".join(signal.failed_checks)
    if checks:
        return f"修掉没过的必检项：{checks}"
    if signal.kind is FailureKind.CHECK_FAILED:
        return "重跑没过的必检项并修到通过"
    return f"修掉失败原因：{signal.reason.strip() or _default_reason(signal.kind)}"


def build_repair_task(
    *,
    task_id: str,
    attempt: int,
    diagnosis: FailureDiagnosis,
) -> RepairTask:
    """生成修复任务。

    **第几次修复从 1 起算。** 状态机里那本账记的是"已经修了几次"，人看的是
    "这是第几次修" —— 后者从 1 起算才对得上嘴里的"又试了一次"。

    **指令不能是空的。** 空的指令会让 Agent 拿到一个"随便改改"的施工说明 ——
    而"随便改改"正是无限修复循环的起点。

    **次数那条守卫与模型上的 ``ge=1`` 是一道防线两道。** 模型那份拦得住 0，
    但它抛的是 pydantic 的 ``ValidationError`` —— 那句话说的是"字段不合法"，
    而不是"修复次数从 1 起算"。这里先把领域自己的规则写出来。
    """
    if attempt < 1:
        raise ValueError("修复次数从 1 起算")
    instruction = diagnosis.repair_hint.strip() or f"修复失败：{diagnosis.reason}"
    return RepairTask(
        task_id=task_id,
        attempt=attempt,
        instruction=instruction,
        diagnosis=diagnosis,
    )


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "FailureKind",
    "FailureSignal",
    "InMemoryRepairLedger",
    "RepairLedger",
    "RepairLimit",
    "RepairLimits",
    "RepairReservation",
    "RepairUsage",
    "build_repair_task",
    "classify",
    "first_violation",
    "kind_of",
]
