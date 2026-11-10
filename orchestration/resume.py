"""断点恢复的执行器 —— 把"这一步做完了没有"这件事管起来。

设计依据：docs/tasks.md TASK-P3-016（"在执行中强杀进程，重启后任务状态可恢复
且不重复执行已完成的副作用"）、R-091（重试 / 超时 / 恢复机制）、R-095（中断后
可恢复或重新执行）。

**这一层是"怎么把账用起来"。** :mod:`core.task.resume` 定义了账与协议，
这里定义**拿到协议给出的答复之后该干什么**。

三种答复，三种动作：

| 答复 | 意思 | 动作 |
| --- | --- | --- |
| ``ALREADY_DONE`` | 这一步做完了 | **跳过** —— 副作用不重复执行 |
| ``IN_PROGRESS`` | 别人正在做 | **停** —— 别碰 |
| ``NEEDS_PROBE`` | 上个执行者不见了，结果未知 | **去问外部世界** |

**"去问外部世界"是关键的一步。** 账本只能回答"有人开始过、没写完成"，
它答不了"到底做完没有" —— 那要问 git 仓库里有没有那个提交、对象存储里有
没有那个对象。**问出来"做完了"就不重做，问出来"没做"才重做，问不出来就停
下来。** 停下来不是偷懒：硬猜一个答案，猜错了就是重复执行或漏执行。

**失败会原样抛出去。** 账本上记一条 ``failed``，然后把异常交给调用方 ——
因为"哪一步失败了、为什么失败"是调用方要处理的，而吞掉异常会让它变成
"这个任务莫名其妙地停了"。**账本记状态，异常带原因。**
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from core.task.resume import (
    ProbeOutcome,
    StepClaim,
    StepEffectLedger,
    TaskProgress,
    build_progress,
    build_step_key,
)


class StepState(StrEnum):
    """一步最终落到了什么状态。

    **"跳过"与"做完"是两回事，报告里要分得开。** 跳过意味着"这一步是上一
    个进程做的"，而做完意味着"这一步是这一轮做的" —— 事后查账时这两个数字
    不一样。
    """

    #: 这一轮做的，做完了。
    DONE = "done"
    #: 早就做完了，这一轮跳过。
    SKIPPED = "skipped"
    #: 探测确认早就做完了，这一轮跳过（**没有重做**）。
    RECONCILED = "reconciled"
    #: 别人正在做 —— 停。
    CONTENDED = "contended"
    #: 结果未知且问不出来 —— 停。
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class Step:
    """一步 —— **副作用怎么跑，以及怎么问它做完没有**。

    :param name: 步骤名。**它是账本主键的一部分** —— 同一个任务里同名的步骤
        只能有一个副作用。
    :param run: 干活。**这一步就是副作用本身**。
    :param probe: 探测 —— 问外部世界"这一步做完没有"。**没有探测的步骤在结果
        未知时只能停下来**：账本答不了"做完没有"，硬猜就是重复执行或漏执行。
    """

    name: str
    run: Callable[[], Awaitable[object]]
    probe: Callable[[], Awaitable[ProbeOutcome]] | None = None


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """一步的结果。

    :param step: 哪一步。
    :param state: 落到了哪一档。
    :param detail: **为什么** —— 说给人听的一句话。
    :param value: 干活的结果（跳过时是 ``None``）。
    """

    step: str
    state: StepState
    detail: str = ""
    value: object | None = None

    @property
    def halted(self) -> bool:
        """这一步是不是把整个流程停住了。"""
        return self.state in (StepState.CONTENDED, StepState.ABORTED)


@dataclass(frozen=True, slots=True)
class ResumeReport:
    """一轮跑下来的结果 —— **每一档都留着**。

    :param task_id: 哪个任务。
    :param steps: 每一步的结果，按跑的顺序。
    """

    task_id: str
    steps: tuple[StepOutcome, ...] = ()

    @property
    def completed(self) -> tuple[str, ...]:
        """这一轮真正做了的步骤。"""
        return tuple(item.step for item in self.steps if item.state is StepState.DONE)

    @property
    def skipped(self) -> tuple[str, ...]:
        """早就做完了、这一轮跳过的步骤。

        **跳过也是成果** —— 它是"强杀之后没有重复执行"的直接证据。
        """
        return tuple(
            item.step
            for item in self.steps
            if item.state in (StepState.SKIPPED, StepState.RECONCILED)
        )

    @property
    def halted(self) -> bool:
        """流程停住了吗。"""
        return any(item.halted for item in self.steps)

    @property
    def halted_step(self) -> str | None:
        """停在哪一步。"""
        for item in self.steps:
            if item.halted:
                return item.step
        return None

    @property
    def ok(self) -> bool:
        """全部步骤都落到"做完或跳过"了吗。"""
        return not self.halted


class ResumableStepRunner:
    """可恢复的步骤执行器。

    **它只做一件事：每一步都先问账本，再决定要不要动手。**

    :param ledger: 步骤账。
    :param attempt: 第几次尝试。**它进的是幂等键**（``task_id:attempt`` 那条
        线），而步骤账的主键是 ``(task_id, step)`` —— 重试的是同一件事，不是
        另一件事。
    """

    __slots__ = ("_attempt", "_ledger")

    def __init__(self, ledger: StepEffectLedger, *, attempt: int = 1) -> None:
        if attempt < 1:
            raise ValueError(f"attempt 必须从 1 起算 —— 收到 {attempt}")
        self._ledger = ledger
        self._attempt = attempt

    @property
    def ledger(self) -> StepEffectLedger:
        """步骤账。"""
        return self._ledger

    async def run(self, task_id: str, steps: Sequence[Step]) -> ResumeReport:
        """按顺序跑一遍。

        **中途停了就不往下走。** 停下来的理由是"这一步的结果问不出来"或者
        "别人正在做" —— 这两种情况下继续跑后面的步骤，就是在不确定的地基上
        继续盖房子。
        """
        outcomes: list[StepOutcome] = []
        for step in steps:
            outcome = await self._run_one(task_id, step)
            outcomes.append(outcome)
            if outcome.halted:
                break
        return ResumeReport(task_id=task_id, steps=tuple(outcomes))

    async def _run_one(self, task_id: str, step: Step) -> StepOutcome:
        """跑一步 —— **先问账本**。"""
        claim = await self._ledger.claim(
            task_id,
            step.name,
            attempt=self._attempt,
            idempotency_key=build_step_key(task_id, step.name),
        )
        if claim.claim is StepClaim.ALREADY_DONE:
            return StepOutcome(step=step.name, state=StepState.SKIPPED, detail=claim.reason)
        if claim.claim is StepClaim.IN_PROGRESS:
            return StepOutcome(
                step=step.name,
                state=StepState.CONTENDED,
                detail=claim.reason,
            )
        if claim.claim is StepClaim.NEEDS_PROBE:
            return await self._reconcile(task_id, step)
        return await self._execute(task_id, step)

    async def _reconcile(self, task_id: str, step: Step) -> StepOutcome:
        """结果未知 —— **去问外部世界**。

        **三种回答，三种动作。** 问出来"做完了"就不重做；问出来"没做"就接
        过来重做；**问不出来就停下来**。
        """
        if step.probe is None:
            return StepOutcome(
                step=step.name,
                state=StepState.ABORTED,
                detail=("这一步的结果未知，而它没有探测 —— 账本答不了'做完没有'，停下来，不猜"),
            )

        outcome = await step.probe()
        if outcome is ProbeOutcome.COMPLETED:
            await self._ledger.complete(task_id, step.name, attempt=self._attempt)
            return StepOutcome(
                step=step.name,
                state=StepState.RECONCILED,
                detail="探测确认副作用已经生效 —— 不重做",
            )
        if outcome is ProbeOutcome.NOT_COMPLETED:
            taken = await self._ledger.take_over(
                task_id,
                step.name,
                attempt=self._attempt,
                idempotency_key=build_step_key(task_id, step.name),
            )
            if not taken.may_run:
                return StepOutcome(
                    step=step.name,
                    state=StepState.ABORTED,
                    detail=taken.reason,
                )
            return await self._execute(task_id, step)
        return StepOutcome(
            step=step.name,
            state=StepState.ABORTED,
            detail="探测不出来这一步到底做完没有 —— 停下来，不猜",
        )

    async def _execute(self, task_id: str, step: Step) -> StepOutcome:
        """真干活。

        **失败先记账，再把异常原样抛出去。** 账上留着"这一步试过、失败了"，
        恢复的时候它是可以重跑的（失败意味着副作用没有生效）；异常交给调用
        方，因为失败的原因要由调用方处理。
        """
        try:
            value = await step.run()
        except Exception as exc:
            await self._ledger.fail(
                task_id,
                step.name,
                attempt=self._attempt,
                reason=f"{type(exc).__name__}: {exc}",
            )
            raise
        await self._ledger.complete(task_id, step.name, attempt=self._attempt)
        return StepOutcome(step=step.name, state=StepState.DONE, value=value)


async def read_progress(
    ledger: StepEffectLedger,
    task_id: str,
    order: Sequence[str],
) -> TaskProgress:
    """这个任务跑到哪儿了。

    **"任务状态可恢复"问的就是这个。** 任务行上的 ``status`` 只说得出"跑到
    RUNNING 了"，说不出"跑哪一步了" —— 断点恢复要的是后者。
    """
    effects = await ledger.effects(task_id)
    return build_progress(task_id, effects, order)


__all__ = [
    "ResumableStepRunner",
    "ResumeReport",
    "Step",
    "StepOutcome",
    "StepState",
    "read_progress",
]
