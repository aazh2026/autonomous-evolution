"""断点恢复 —— 任务状态可恢复，已完成的副作用不重复执行。

设计依据：docs/tasks.md TASK-P3-016（交付物：进程重启 / 机器重启后从检查点恢复；
验收条件"在执行中强杀进程，重启后任务状态可恢复且不重复执行已完成的副作用"；
验证 T1）、R-091（重试 / 超时 / 恢复机制）、R-095（中断后可恢复或重新执行）、
R-099（验收证据不因下一轮丢失）。

**这一层回答的是"进程死了之后，怎么知道哪些事已经做完了"。**

代码库里本来就有两样东西看起来像答案：

1. :class:`~core.task.state_machine.TaskState` 的状态机里有一条"幂等重投是
   空操作"的规则（``core/task/state_machine.py``）—— 它保护的是**状态迁移**，
   不是**迁移之前那个副作用**。重投一次 ``START``，状态机不会报错，但工作区
   会被重新开一次、会话会被重新开一次。
2. :class:`executors.base.CodingAgentAdapter` 的幂等键 ``task_id:attempt``
   （``executors/base.py``）—— 键算出来了，但**从来没有落库，也从来没有被
   当作查询条件用过**。它只是一串被写进事件载荷里的字符串。

于是"重复执行已完成的副作用"这件事，今天没有任何机制挡着。

**这里补的是一本账：谁做完了哪一步。**

三条不可让步的规矩：

1. **先记意图，再做副作用，最后记完成。** 只有"做完了"这一条记录，重启之后
   才知道得跳过；只有"开始了"这一条，重启之后才知道**结果未知**、得去问
   外部世界。三条规矩少一条，恢复就是靠猜。
2. **完成是不可逆的。** 一旦记了 ``DONE``，后来的任何记录都改不了它 —— 迟到的
   失败不能把已完成的事实 undo 掉。这条规矩让"谁先到谁赢"成立，也让两个
   工作者并发跑同一步时不至于互相踩。
3. **结果未知时不猜。** 进程被强杀，可能停在副作用的任何一个位置。账本只能
   告诉你"开始了但没写完成"，它**答不了**"到底做完没有"—— 那要问外部世界
   （git 仓库里有没有那个提交？对象存储里有没有那个对象？）。问不出答案时
   **停下来**，而不是重跑一遍。

**"账本"和"执行器幂等键"是两回事。** 执行器的键是 ``task_id:attempt``，
区分的是**同一次任务的不同尝试**；这里的键是 ``task_id:step``，区分的是**同
一个任务的不同步骤**。一个任务里的不同步骤是不同的事，反过来同一件事重试
时 attempt 变了、step 不变 —— 两个键各有各的用处，混用会丢掉其中一个的语义。

**账本这一层保证不了"绝不重复"，它保证的是"重复之前先问一句"。** 剩下的窗口
（副作用做完、记录还没写下去的那一刻被强杀）由**探测**（probe）兜底：探测
问的是外部世界的真实状态，而不是账本自己的记忆。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable


class StepStatus(StrEnum):
    """一个步骤副作用的状态。

    **三档，不是两档。** "做完了"与"没做完"之间还夹着一档"**开始了，但不知
    道做完没有**" —— 进程在副作用中途被强杀就落在这里。把它并进"没做完"，
    重启之后就会把已经做完的事再做一遍；把它并进"做完了"，就会漏掉没做完的
    事。**中间那档才是断点恢复真正要处理的情况。**
    """

    #: 副作用开始了，但还没有写完成记录。
    STARTED = "started"
    #: 副作用做完了。这条记录不可逆。
    DONE = "done"
    #: 尝试过，明确失败了 —— 副作用没有生效，可以重跑。
    FAILED = "failed"


class StepClaim(StrEnum):
    """要跑一个步骤时，账本给出的答复。

    **四种，不是"能跑 / 不能跑"两种。** "不能跑"至少有两种完全不同的原因：
    一种是"**已经做完了**"（跳过，正是我们要的），另一种是"**别人正在做**"
    （别碰，等着）。把后一种说成前一种，副作用就会被重复执行 —— 那正是这一
    层要防的事。
    """

    #: 你拿到了这一步，可以执行副作用。
    ACQUIRED = "acquired"
    #: 这一步已经做完了 —— 跳过。
    ALREADY_DONE = "already_done"
    #: 别人正在跑这一步（租约还活着）—— 别碰。
    IN_PROGRESS = "in_progress"
    #: 上一个执行者不见了 —— 这一步到底做完没有，**得问外部世界**。
    NEEDS_PROBE = "needs_probe"


class ProbeOutcome(StrEnum):
    """外部世界对"这一步的副作用发生了吗"的回答。

    **"不知道"是一档，不是布尔值。** 探测探不到的时候，硬把它当成"没发生"
    就会重复执行副作用，硬把它当成"发生了"就会漏掉一件事。两种都错，所以
    单列一档：**探测不出来就停下来。**
    """

    COMPLETED = "completed"
    NOT_COMPLETED = "not_completed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StepEffect:
    """一步副作用的账。

    :param task_id: 属于哪个任务。
    :param step: 哪一步。**步骤名就是这一层的主键的一部分** —— 同一个任务的
        同一个步骤，副作用最多发生一次。
    :param attempt: 第几次尝试做这一步。**它不参与主键** —— 重试的是同一件
        事，不是另一件事。
    :param status: 状态。
    :param idempotency_key: 这一步的幂等键（``task_id:step``）。落库是为了
        事后能对账：**键散落在各处，就没法回答"这两个记录说的是同一件事吗"。
    :param recorded_at: 这条记录写下的时刻。
    :param lease_until: 租约到什么时候。**这是"别人正在做"与"上个执行者死
        了"的分界** —— 租约还在，就是有人在管；租约过期了，就只剩一个"开始
        了"的残骸。
    :param note: 备注 —— 失败原因、探测结论等等。
    """

    task_id: str
    step: str
    attempt: int
    status: StepStatus
    idempotency_key: str
    recorded_at: datetime
    lease_until: datetime | None = None
    note: str = ""

    @property
    def is_done(self) -> bool:
        """这一步的副作用做完了吗。"""
        return self.status is StepStatus.DONE


@dataclass(frozen=True, slots=True)
class StepClaimResult:
    """占一步的结果 —— **带理由**。

    :param claim: 答复。
    :param effect: 账本里那一条（新建的或已有的）。
    :param reason: 为什么。**理由是必需品** —— "不能跑"和"别人在跑"要采取的
        后续动作完全不同，而调用方只有拿到理由才知道该等还是该停。
    """

    claim: StepClaim
    effect: StepEffect | None = None
    reason: str = ""

    @property
    def may_run(self) -> bool:
        """可以执行副作用吗。"""
        return self.claim is StepClaim.ACQUIRED

    @property
    def already_done(self) -> bool:
        """已经做完了吗（可以跳过）。"""
        return self.claim is StepClaim.ALREADY_DONE


class UnknownStepError(Exception):
    """账本里没有这一步。

    **这个错不该被吞掉。** 记完成、记失败、接管这三件事都以"占过位"为前提；
    账本里没有记录，说明调用方的流程乱了 —— 静默忽略的话，"完成没记上"
    这种最该被看见的事就没人看见了。
    """

    def __init__(self, task_id: str, step: str) -> None:
        super().__init__(f"任务 {task_id} 的步骤 {step} 没有占位记录 —— 不能记完成或失败")
        self.task_id = task_id
        self.step = step


@runtime_checkable
class StepEffectLedger(Protocol):
    """步骤账的持久化端口。

    实现必须保证两件事：

    1. **"占位"是原子的。** 两个执行者同时跑同一个任务时，只能有一个拿到
       ``ACQUIRED`` —— 另一个必须拿到 ``IN_PROGRESS``，不能也去执行副作用。
    2. **"完成"是不可逆的。** 第一个写下的 ``DONE`` 赢，后来者改不动它。
    """

    async def claim(
        self,
        task_id: str,
        step: str,
        *,
        attempt: int,
        idempotency_key: str,
    ) -> StepClaimResult:
        """占这一步。"""
        ...

    async def take_over(
        self,
        task_id: str,
        step: str,
        *,
        attempt: int,
        idempotency_key: str,
    ) -> StepClaimResult:
        """接管一个租约已过期、结果未知的步骤。"""
        ...

    async def complete(self, task_id: str, step: str, *, attempt: int) -> StepEffect:
        """记完成。**先到先得。**"""
        ...

    async def fail(self, task_id: str, step: str, *, attempt: int, reason: str) -> StepEffect:
        """记失败。**不能 undo 已经完成的事实。**"""
        ...

    async def effects(self, task_id: str) -> tuple[StepEffect, ...]:
        """这个任务的全部步骤记录。"""
        ...


def build_step_key(task_id: str, step: str) -> str:
    """步骤的幂等键 —— ``task_id:step``。

    **与执行器的幂等键不是一回事。** 执行器的键是 ``task_id:attempt``，区分
    的是同一次任务的不同尝试；这里的键区分的是**同一个任务的不同步骤**。

    **键必须由输入派生。** 由时间、随机数或会话状态派生的话，同一件事算两次
    会得到两个键，账本就认不出来了。
    """
    if not task_id:
        raise ValueError("task_id 不能为空")
    if not step:
        raise ValueError("step 不能为空")
    return f"{task_id}:{step}"


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """任务跑到哪儿了 —— **从账本问出来的**。

    **这是"任务状态可恢复"的那个状态。** 任务行上的 ``status`` 只说得出
    "跑到 RUNNING 了"，说不出"跑哪一步了"。断点恢复要的是后者。

    :param task_id: 哪个任务。
    :param done: 做完了的步骤。
    :param started: 开始了但没写完成的步骤 —— **结果未知的那几个**。
    :param failed: 明确失败的步骤。
    :param attempt: 当前尝试次数。
    """

    task_id: str
    done: tuple[str, ...] = ()
    started: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    attempt: int = 1

    @property
    def resume_from(self) -> str | None:
        """从哪一步接着跑。

        **"第一个没做完的"就是接着跑的那一步。** 全部做完的话就是 ``None`` ——
        任务已经收尾，没有断点可续。
        """
        pending = tuple(step for step in self.started) + tuple(step for step in self.failed)
        if not pending:
            return None
        return pending[0]

    @property
    def finished(self) -> bool:
        """全部步骤都做完了吗。"""
        return not self.started and not self.failed and bool(self.done)


def build_progress(
    task_id: str,
    effects: Sequence[StepEffect],
    order: Sequence[str],
) -> TaskProgress:
    """从账本拼出任务进度。

    :param effects: 这个任务的全部步骤记录。
    :param order: 步骤的规范顺序 —— **决定"从哪一步接着跑"**。账本只知道自己
        记过什么，不知道步骤之间的先后；顺序得由调用方给。

    **同一个步骤可能有多条记录**（换过 attempt）。**取最新的那条** —— 旧的那
    条说的是"上一次尝试的事"。
    """
    latest: dict[str, StepEffect] = {}
    for effect in effects:
        current = latest.get(effect.step)
        if current is None or effect.attempt >= current.attempt:
            latest[effect.step] = effect

    done = tuple(step for step in order if latest.get(step) is not None and latest[step].is_done)
    started = tuple(
        step
        for step in order
        if step not in done
        and latest.get(step) is not None
        and latest[step].status is StepStatus.STARTED
    )
    failed = tuple(
        step
        for step in order
        if step not in done
        and latest.get(step) is not None
        and latest[step].status is StepStatus.FAILED
    )
    attempt = max((effect.attempt for effect in effects), default=1)
    return TaskProgress(
        task_id=task_id,
        done=done,
        started=started,
        failed=failed,
        attempt=attempt,
    )


class InMemoryStepEffectLedger:
    """内存版的步骤账。

    **同一把锁管全部状态转移。** "查现状"和"改现状"之间不能让出控制权 ——
    两个执行者同时来占同一个步骤时，只有一个能拿到 ``ACQUIRED``。

    **跨进程不成立。** 内存账本只活在进程里，进程一死就没了 —— **这正是这个
    模块需要一份数据库实现的原因**（``infrastructure/database/step_effect.py``）。
    """

    __slots__ = ("_clock", "_effects", "_lease_seconds", "_lock")

    def __init__(
        self,
        *,
        lease_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds < 0:
            raise ValueError(f"租约时长不能为负 —— 收到 {lease_seconds}")
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._effects: dict[tuple[str, str], StepEffect] = {}
        self._lock = asyncio.Lock()

    async def claim(
        self,
        task_id: str,
        step: str,
        *,
        attempt: int,
        idempotency_key: str,
    ) -> StepClaimResult:
        """占这一步。"""
        async with self._lock:
            key = (task_id, step)
            existing = self._effects.get(key)
            if existing is None:
                effect = self._start(task_id, step, attempt, idempotency_key)
                self._effects[key] = effect
                return StepClaimResult(
                    claim=StepClaim.ACQUIRED,
                    effect=effect,
                    reason="第一次做这一步",
                )
            if existing.status is StepStatus.DONE:
                return StepClaimResult(
                    claim=StepClaim.ALREADY_DONE,
                    effect=existing,
                    reason="这一步已经做完了 —— 跳过",
                )
            if existing.status is StepStatus.FAILED:
                effect = self._start(task_id, step, attempt, idempotency_key)
                self._effects[key] = effect
                return StepClaimResult(
                    claim=StepClaim.ACQUIRED,
                    effect=effect,
                    reason="上一次尝试明确失败了 —— 副作用没有生效，可以重跑",
                )
            if existing.lease_until is not None and existing.lease_until > self._clock():
                return StepClaimResult(
                    claim=StepClaim.IN_PROGRESS,
                    effect=existing,
                    reason="别人正在跑这一步（租约还没到期）—— 别碰",
                )
            return StepClaimResult(
                claim=StepClaim.NEEDS_PROBE,
                effect=existing,
                reason="上一个执行者不见了 —— 这一步到底做完没有，得问外部世界",
            )

    async def take_over(
        self,
        task_id: str,
        step: str,
        *,
        attempt: int,
        idempotency_key: str,
    ) -> StepClaimResult:
        """接管一个租约已过期、结果未知的步骤。"""
        async with self._lock:
            key = (task_id, step)
            existing = self._effects.get(key)
            if existing is None:
                raise UnknownStepError(task_id, step)
            if existing.status is StepStatus.DONE:
                return StepClaimResult(
                    claim=StepClaim.ALREADY_DONE,
                    effect=existing,
                    reason="这一步已经做完了 —— 跳过",
                )
            effect = self._start(task_id, step, attempt, idempotency_key)
            self._effects[key] = effect
            return StepClaimResult(
                claim=StepClaim.ACQUIRED,
                effect=effect,
                reason="接管了上一次的现场（探测确认副作用没生效）",
            )

    async def complete(self, task_id: str, step: str, *, attempt: int) -> StepEffect:
        """记完成。**先到先得。**"""
        async with self._lock:
            key = (task_id, step)
            existing = self._effects.get(key)
            if existing is None:
                raise UnknownStepError(task_id, step)
            if existing.status is StepStatus.DONE:
                return existing
            effect = replace(
                existing,
                attempt=attempt,
                status=StepStatus.DONE,
                recorded_at=self._clock(),
                lease_until=None,
            )
            self._effects[key] = effect
            return effect

    async def fail(self, task_id: str, step: str, *, attempt: int, reason: str) -> StepEffect:
        """记失败。**不能 undo 已经完成的事实。**"""
        async with self._lock:
            key = (task_id, step)
            existing = self._effects.get(key)
            if existing is None:
                raise UnknownStepError(task_id, step)
            if existing.status is StepStatus.DONE:
                return existing
            effect = replace(
                existing,
                attempt=attempt,
                status=StepStatus.FAILED,
                recorded_at=self._clock(),
                lease_until=None,
                note=reason,
            )
            self._effects[key] = effect
            return effect

    async def effects(self, task_id: str) -> tuple[StepEffect, ...]:
        """这个任务的全部步骤记录。"""
        async with self._lock:
            rows = [effect for effect in self._effects.values() if effect.task_id == task_id]
        return tuple(sorted(rows, key=lambda effect: (effect.recorded_at, effect.step)))

    def _start(
        self,
        task_id: str,
        step: str,
        attempt: int,
        idempotency_key: str,
    ) -> StepEffect:
        """记一条"开始了"。"""
        now = self._clock()
        return StepEffect(
            task_id=task_id,
            step=step,
            attempt=attempt,
            status=StepStatus.STARTED,
            idempotency_key=idempotency_key,
            recorded_at=now,
            lease_until=now + timedelta(seconds=self._lease_seconds),
        )


__all__ = [
    "InMemoryStepEffectLedger",
    "ProbeOutcome",
    "StepClaim",
    "StepClaimResult",
    "StepEffect",
    "StepEffectLedger",
    "StepStatus",
    "TaskProgress",
    "UnknownStepError",
    "build_progress",
    "build_step_key",
]
