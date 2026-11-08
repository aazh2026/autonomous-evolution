"""超时的处置 —— 超时不等于失败。

设计依据：docs/design.md §6（"Agent 超时不等于任务失败，需要判断是否可以
恢复"）、docs/tasks.md TASK-P1-034（R-095：中断后可恢复或重新执行；R-097：
超时不等于失败；I-03：须先 interrupt 再探测可恢复性，避免僵尸会话）。

**一次超时要处理两件事，而它们不是同一件。**

1. **收干净** —— 跑飞的那个执行还在改工作空间。不把它停掉，后面无论续跑
   还是重做，都会有**两个东西同时改同一个目录**。这就是 I-03 说的"僵尸
   会话"。
2. **下结论** —— 这次执行到底算不算完？如果超时就直接判失败，那么一个只
   是慢了的执行会被当成做砸的：任务被扔掉，而它其实只差一分钟（R-097）。

**顺序不能换：先 interrupt，再探测。** 探一个还在跑的会话，探出来的"可
恢复"没有意义 —— 它随时可能在你探测的那一刻又改了一处文件。所以本模块
的顺序是 **interrupt → 回收 → 探测 → 决策**，每一步的结论都是下一步的
前提。

**两个问题，两个答案。** 本模块刻意不把"能不能续跑"与"该不该重试"揉成
一个判断，因为它们问的是两件事，证据也来自两处：

- **能不能续跑** —— 问 :class:`SessionProbe`（会话还在吗、工作还在吗）；
- **该不该再试一次** —— 问 :class:`~executors.base.ErrorCategory`（这次
  超时是不是瞬时故障）。系统里已经有一份分类（``executors/cli/exit_codes.py``
  把超时归为可重试），重造一份只会让两份表漂移。

**为什么端口住在 ``executors/`` 而不是 ``core/``。** 取消链的端口住在
``core/task/cancellation.py``，因为那条链的两头是 ``executors/`` 与
``harness/``，两边互不 import（待裁决 13）。而这次只有 ``executors/`` 一头
—— 沙箱那一侧通过**实现**端口来适配，不必让两边共享同一份声明。方向仍然
是"适配层 → 领域"。

**失败时也要收干净。** 判失败的那几条路径同样会走回收：僵尸容器不管这
次任务最后是续跑、重试还是失败，都得被收掉 —— 它占着工作空间，而工作空
间是别的任务也要用的。

**决策是穷尽的。** 三个结论与三个动作一一对应，这条映射写在
:data:`OUTCOME_ACTIONS` 里，并由测试钉死：多一个结论少一个动作，或者两
者的对应关系变了，都必须让测试变红。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol

from core.task.cancellation import CancellationReport, CancellationTarget, Interrupter
from executors.base import ErrorCategory, ExecutionTask, build_idempotency_key


class RecoveryOutcome(StrEnum):
    """超时处置的结论。"""

    #: 会话还在，可以接着跑。
    INTERRUPTED_RESUMABLE = "interrupted_resumable"
    #: 会话续不上了，但这次超时值得再试一次。
    INTERRUPTED_RETRYABLE = "interrupted_retryable"
    #: 这次执行到此为止 —— 判失败。
    FAILED = "failed"


class RecoveryAction(StrEnum):
    """超时之后该做什么。"""

    #: 接着上一次跑。
    RESUME = "resume"
    #: 从头再跑一次。
    RETRY = "retry"
    #: 不再跑，判失败。
    FAIL = "fail"


#: 结论 → 动作。**一一对应**：每个结论恰好一个动作。
OUTCOME_ACTIONS: Final[MappingProxyType[RecoveryOutcome, RecoveryAction]] = MappingProxyType(
    {
        RecoveryOutcome.INTERRUPTED_RESUMABLE: RecoveryAction.RESUME,
        RecoveryOutcome.INTERRUPTED_RETRYABLE: RecoveryAction.RETRY,
        RecoveryOutcome.FAILED: RecoveryAction.FAIL,
    }
)


@dataclass(frozen=True, slots=True)
class SessionProbeResult:
    """会话能不能接着跑的探测结论。

    :attr:`resumable` 是**结论**，:attr:`reason` 是**依据** —— 只给结论
    不给依据的"不可恢复"，事后没人能判断该不该信它。

    **探不出来与"确认不能"不是一回事。** 前者是"不知道"，后者是"知道了，
    不行"。混成一句话，就会在"其实能续跑"的那一次上把任务判死 —— 所以
    探测失败时结论按"不可恢复"处理，但**理由写的是"探不出来"**。
    """

    resumable: bool
    reason: str = ""


class SessionProbe(Protocol):
    """探测会话还能不能接着跑。"""

    async def probe_session(self, session_id: str) -> SessionProbeResult:
        """这个会话还能不能接着跑。"""


class ResourceReclaimer(Protocol):
    """把这次执行留下的资源收干净。

    **回收不是"取消"。** 取消是用户提的要求；回收是**处置**：不管这次任务
    最后是续跑、重试还是失败，跑飞的那个执行与它占着的容器都得被收掉。

    **收干净的标准是"每一类资源都不在了"，不是"收过了"。** 端口返回的是
    取消链那种**逐层结论**，正是为了能回答"哪一类还在跑"。
    """

    async def reclaim(self, target: CancellationTarget) -> CancellationReport:
        """收干净，并如实回报每一层的结论。"""


@dataclass(frozen=True, slots=True)
class RecoveryPorts:
    """处置一次超时要用到的三方。

    **为什么打包成一个对象。** 三方是同一个决定的三个输入，散成三个参数
    只会让调用点变长，也让"哪几个是必填的"这件事看不出来。打包之后，
    端口是**一个**必填项。
    """

    #: 执行器 —— 用来中断那次跑飞的执行。
    interrupter: Interrupter
    #: 探测会话还能不能接着跑。
    probe: SessionProbe
    #: 回收这次执行留下的资源。
    reclaimer: ResourceReclaimer


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """超时的处置方案 —— 一份**结论**，不是一个动作。

    :attr:`attempt` 与 :attr:`idempotency_key` 说的是"**如果还要跑**，用第
    几次尝试、哪个键"。判失败时两者都是 ``None`` —— 没有下一次执行，也
    就没有下一次尝试。

    **续跑不换幂等键，重试换。** 续跑是**同一次尝试**的延续，键还是那个
    键（:func:`~executors.base.build_idempotency_key` 由 ``task_id + attempt``
    派生）；重试是**新的一次尝试**，必须换键 —— 否则重放去重会把新的一
    次当成旧的同一次提交而丢掉（I-01）。
    """

    outcome: RecoveryOutcome
    action: RecoveryAction
    reason: str
    #: 这次行动用的是第几次尝试。判失败时是 ``None``。
    attempt: int | None
    #: 这次行动的幂等键。判失败时是 ``None``。
    idempotency_key: str | None
    #: 中断有没有被确认。
    interrupted: bool
    #: 回收有没有收干净。
    reclaimed: bool
    #: 回收的细节 —— 哪几层没收住。
    reclaim_detail: str = ""
    #: 这次超时的错误分类。**需人工的分类落在 ``FAILED`` 这一格上** ——
    #: 本模块的词汇表里没有"挂起"；编排层看这一项自己决定要不要转成阻塞
    #: 态。
    error_category: ErrorCategory = ErrorCategory.RETRYABLE


async def recover_from_timeout(
    task: ExecutionTask,
    *,
    ports: RecoveryPorts,
    container_id: str | None = None,
    error_category: ErrorCategory = ErrorCategory.RETRYABLE,
) -> RecoveryPlan:
    """处置一次超时，给出方案。

    :param task: 超时的那次执行。
    :param ports: 处置要用的三方。
    :param container_id: 这次执行用的沙箱容器。没有容器时回收只管进程。
    :param error_category: 这次超时的分类 —— 决定"不可续跑"之后是重试还
        是判失败。默认**可重试**，与 ``executors/cli/exit_codes.py`` 对超时
        的分类一致。
    """
    target = CancellationTarget(
        task_id=task.task_id,
        session_id=task.session_id,
        container_id=container_id,
    )

    # ① 先中断 —— I-03：不 interrupt 就去探测，探的是还在跑的会话。
    interrupted, interrupt_detail = await _interrupt(ports.interrupter, target)

    # ② 回收 —— 不留僵尸会话。**不管中断有没有确认都要收**：不知道它停了
    # 正是最该去收一收的时候。而"收不干净"比"没确认"更严重 —— 还有东西
    # 在跑的时候去续跑，等于让两个执行同时改一个工作空间。
    reclaimed, reclaim_detail = await _reclaim(ports.reclaimer, target)

    # ③ 探测会话还能不能接着跑。**总是问一次** —— 探测是没有副作用的读操
    # 作，而"该不该用它的答案"是决策的事，不该在流程里分叉成两条路。
    facts = _Facts(
        interrupted=interrupted,
        interrupt_detail=interrupt_detail,
        reclaimed=reclaimed,
        reclaim_detail=reclaim_detail,
        probe=await _probe(ports.probe, target),
        error_category=error_category,
    )
    return _decide(task, facts)


@dataclass(frozen=True, slots=True)
class _Facts:
    """处置过程中**问出来**的事实。

    与决策分开，是为了把"问"和"判"隔开：流程只负责把事实问齐，判定规则
    集中在 :func:`_decide` 一处。
    """

    interrupted: bool
    interrupt_detail: str
    reclaimed: bool
    reclaim_detail: str
    #: 探测结论。``None`` 表示**探不出来**（探测本身出错）。
    probe: SessionProbeResult | None
    error_category: ErrorCategory


def _decide(task: ExecutionTask, facts: _Facts) -> RecoveryPlan:
    """把事实变成方案。

    **判定顺序就是约束的强弱**：先看"我们到底清没清干净"（那是**事实**），
    再看"该不该由人拍板"（那是**判断**），最后才看"能不能续跑"。事实不
    成立时，后面的判断一概不做 —— 没清干净就谈不上续跑。
    """
    if not facts.interrupted:
        return _failed(
            facts,
            f"中断没有确认 —— 无从知道跑飞的东西停下来了没有：{facts.interrupt_detail}",
        )
    if not facts.reclaimed:
        return _failed(
            facts,
            f"回收没收干净 —— 还有东西在跑，不能带着它继续：{facts.reclaim_detail}",
        )
    if facts.error_category == ErrorCategory.NEEDS_HUMAN:
        # 该由人来看的，系统不自己拍板 —— 续跑与重试都是"拍板"。
        return _failed(facts, "这次超时该由人来看 —— 系统不自己拍板")
    probe = facts.probe
    if probe is None:
        probe = SessionProbeResult(resumable=False, reason="探测会话能不能续跑时出错了 —— 探不出来")
    if probe.resumable:
        # 续跑：**同一次尝试**，幂等键不变。
        return RecoveryPlan(
            outcome=RecoveryOutcome.INTERRUPTED_RESUMABLE,
            action=RecoveryAction.RESUME,
            reason=probe.reason or "会话还在，可以接着跑",
            attempt=task.attempt,
            idempotency_key=build_idempotency_key(task.task_id, task.attempt),
            interrupted=facts.interrupted,
            reclaimed=facts.reclaimed,
            reclaim_detail=facts.reclaim_detail,
            error_category=facts.error_category,
        )
    if facts.error_category == ErrorCategory.RETRYABLE:
        next_attempt = task.attempt + 1
        return RecoveryPlan(
            outcome=RecoveryOutcome.INTERRUPTED_RETRYABLE,
            action=RecoveryAction.RETRY,
            reason=f"{probe.reason}；这次超时可以再试一次",
            attempt=next_attempt,
            # **新的一次尝试 = 新的键。** 复用旧键的话，重放去重会把这一次
            # 当成上一次的重投而丢掉（I-01）。
            idempotency_key=build_idempotency_key(task.task_id, next_attempt),
            interrupted=facts.interrupted,
            reclaimed=facts.reclaimed,
            reclaim_detail=facts.reclaim_detail,
            error_category=facts.error_category,
        )
    return _failed(facts, f"{probe.reason}；这次超时也不值得再试一次")


def _failed(facts: _Facts, reason: str) -> RecoveryPlan:
    """判失败。

    **没有下一次尝试，也就没有下一次尝试的键** —— :attr:`RecoveryPlan.attempt`
    与 :attr:`RecoveryPlan.idempotency_key` 都是 ``None``。给一个"不会再
    跑"的方案发一把键，等于让调用方以为还有一次执行。
    """
    return RecoveryPlan(
        outcome=RecoveryOutcome.FAILED,
        action=RecoveryAction.FAIL,
        reason=reason,
        attempt=None,
        idempotency_key=None,
        interrupted=facts.interrupted,
        reclaimed=facts.reclaimed,
        reclaim_detail=facts.reclaim_detail,
        error_category=facts.error_category,
    )


async def _interrupt(interrupter: Interrupter, target: CancellationTarget) -> tuple[bool, str]:
    """中断那次执行，并等它给出确认。

    **回执按字符串比对**，与取消链的 Adapter 层同一套做法 —— 执行器换一套
    状态取值时，我们宁可说"没确认"也不能说"确认了"（fail-closed）。

    **抛异常也按"没确认"处理。** 中断这一步本身失败，说明我们**不知道**那
    个执行停了没有 —— 不知道就不能往下走。
    """
    try:
        ack = await interrupter.interrupt(target.session_id)
    except Exception as exc:
        return False, f"中断请求本身失败了：{exc}"
    if str(ack.state) != "confirmed":
        return False, f"执行器没有确认停下（状态 {ack.state!s}）：{ack.detail}"
    return True, ack.detail


async def _reclaim(reclaimer: ResourceReclaimer, target: CancellationTarget) -> tuple[bool, str]:
    """回收资源，并如实读出"收干净了没有"。

    **回收失败不抛异常。** 回收失败是一个**结论**（还有东西在跑），调用方
    需要知道"哪一类还在跑"才能处置 —— 异常会把这条信息丢掉。
    """
    try:
        report = await reclaimer.reclaim(target)
    except Exception as exc:
        return False, f"回收报错：{exc}"
    unfinished = report.unfinished
    if not unfinished:
        return True, "所有资源都已回收"
    detail = "、".join(f"{outcome.layer.value}：{outcome.detail}" for outcome in unfinished)
    return False, f"还有资源没收住 —— {detail}"


async def _probe(probe: SessionProbe, target: CancellationTarget) -> SessionProbeResult | None:
    """探测会话。探不出来（出错）时返回 ``None``。

    **出错与"不可恢复"必须分开。** 探不出来说明我们**不知道**；而"不可
    恢复"是一个已经问出来的结论。混成一句话，就会在"其实能续跑"的那一
    次上把任务判死。
    """
    try:
        return await probe.probe_session(target.session_id)
    except Exception:
        return None


__all__ = [
    "OUTCOME_ACTIONS",
    "RecoveryAction",
    "RecoveryOutcome",
    "RecoveryPlan",
    "RecoveryPorts",
    "ResourceReclaimer",
    "SessionProbe",
    "SessionProbeResult",
    "recover_from_timeout",
]
