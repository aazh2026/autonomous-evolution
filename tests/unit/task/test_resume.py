"""TASK-P3-016 —— 断点恢复：已完成的副作用不重复执行。

**这一组测的是协议本身**：占位、完成、失败、探测，四件事各自的语义。

**最要紧的一条是"结果未知"那一档。** 进程被强杀，可能停在副作用的任何一个
位置 —— 做完了但记录还没写下去（那就**不能重做**），或者压根没做（那就**应该
重做**）。这两种情况在账本里长得一模一样（都是"开始了但没写完成"），所以
**必须去问外部世界**，而问不出来的时候只能停下来。

**"停下来"不是失败，是正确。** 硬猜一个答案，猜错了就是重复执行或漏执行
副作用 —— 那正是这一层存在的理由被自己拆掉。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.task.resume import (
    InMemoryStepEffectLedger,
    ProbeOutcome,
    StepClaim,
    StepEffect,
    StepStatus,
    UnknownStepError,
    build_progress,
    build_step_key,
)
from orchestration.resume import ResumableStepRunner, Step, StepState, read_progress

#: 测试用的固定时刻 —— **不取"现在"**，否则租约那条用例会随跑的时刻变化。
START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

TASK = "11111111-1111-1111-1111-111111111111"


class _Clock:
    """一个能往前拨的时钟。"""

    def __init__(self, moment: datetime = START) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> None:
        self.moment += timedelta(**delta)


def _ledger(clock: _Clock | None = None, *, lease_seconds: int = 60) -> InMemoryStepEffectLedger:
    """一本账。"""
    return InMemoryStepEffectLedger(
        lease_seconds=lease_seconds,
        clock=clock if clock is not None else _Clock(),
    )


class TestTheLedger:
    """步骤账本身 —— **四种占位答复**。"""

    async def test_the_first_claim_acquires_the_step(self) -> None:
        """**第一次占位就拿到手。**"""
        ledger = _ledger()

        claim = await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        assert claim.claim is StepClaim.ACQUIRED
        assert claim.may_run is True

    async def test_a_second_claim_while_the_lease_is_alive_is_in_progress(self) -> None:
        """**别人正在做，就别碰。**

        这一条是"两个执行者不会同时做同一件事"的协议层依托 —— 数据库那一份
        靠唯一约束兜底，这里是同一套语义。
        """
        ledger = _ledger()
        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        second = await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        assert second.claim is StepClaim.IN_PROGRESS
        assert second.may_run is False

    async def test_a_claim_after_the_lease_expired_needs_a_probe(self) -> None:
        """**租约过期了，就是"上个执行者不见了"。**

        这时候谁也不知道副作用做完没有 —— **账本答不了**，得问外部世界。
        """
        clock = _Clock()
        ledger = _ledger(clock, lease_seconds=60)
        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        clock.advance(seconds=61)
        claim = await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        assert claim.claim is StepClaim.NEEDS_PROBE

    async def test_a_claim_after_done_is_already_done(self) -> None:
        """**做完了就是做完了。**"""
        ledger = _ledger()
        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")
        await ledger.complete(TASK, "commit", attempt=1)

        claim = await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        assert claim.claim is StepClaim.ALREADY_DONE
        assert claim.already_done is True

    async def test_a_claim_after_failure_acquires_it_again(self) -> None:
        """**失败意味着副作用没有生效 —— 可以重跑。**"""
        ledger = _ledger()
        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")
        await ledger.fail(TASK, "commit", attempt=1, reason="git 说不行")

        claim = await ledger.claim(TASK, "commit", attempt=2, idempotency_key="k2")

        assert claim.claim is StepClaim.ACQUIRED
        assert claim.effect is not None
        assert claim.effect.attempt == 2

    async def test_the_first_done_wins(self) -> None:
        """**完成先到先得。** 后来的记录改不动它。"""
        ledger = _ledger()
        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        first = await ledger.complete(TASK, "commit", attempt=1)
        second = await ledger.complete(TASK, "commit", attempt=1)

        assert first.status is StepStatus.DONE
        assert second.status is StepStatus.DONE
        assert len(await ledger.effects(TASK)) == 1

    async def test_a_late_failure_does_not_undo_a_done(self) -> None:
        """**迟到的失败不能把完成 undo 掉。**

        两个执行者跑同一步时，可能一个先成功、另一个后失败 —— **先成功的那
        个是真的**。把完成改成失败，等于把一件已经发生过的事说成没发生过。
        """
        ledger = _ledger()
        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")
        await ledger.complete(TASK, "commit", attempt=1)

        late = await ledger.fail(TASK, "commit", attempt=2, reason="迟到的失败")

        assert late.status is StepStatus.DONE

    async def test_completing_an_unclaimed_step_is_an_error(self) -> None:
        """**没占位就不能记完成。** 静默忽略的话，"完成没记上"就没人看见了。"""
        ledger = _ledger()

        with pytest.raises(UnknownStepError):
            await ledger.complete(TASK, "commit", attempt=1)


class TestTheRunner:
    """执行器 —— **拿到答复之后该干什么**。"""

    async def test_a_completed_step_is_not_executed_again(self) -> None:
        """**跑第二遍的时候，已经做完的那一步不再跑。**

        这是"不重复执行已完成的副作用"最直接的那一条。
        """
        ledger = _ledger()
        runs: list[str] = []

        async def _commit() -> str:
            runs.append("commit")
            return "done"

        step = Step(name="commit", run=_commit)
        runner = ResumableStepRunner(ledger)

        await runner.run(TASK, [step])
        second = await runner.run(TASK, [step])

        assert runs == ["commit"]
        assert second.skipped == ("commit",)
        assert second.completed == ()

    async def test_the_probe_says_completed_so_the_step_is_not_repeated(self) -> None:
        """**副作用做完了但记录没写下去 —— 探测认出来，就不重做。**

        这是强杀最可能落下的位置：**记录永远比副作用晚一步**。
        """
        ledger = _ledger()
        runs: list[str] = []

        async def _commit() -> str:
            runs.append("commit")
            return "done"

        async def _probe() -> ProbeOutcome:
            return ProbeOutcome.COMPLETED

        # 模拟"副作用做完了，记录还没写"：占了位，但不记完成。
        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")
        # 租约过期 —— 上个执行者不见了。
        expired = InMemoryStepEffectLedger(
            lease_seconds=0,
            clock=_Clock(),
        )
        await expired.claim(TASK, "commit", attempt=1, idempotency_key="k")

        runner = ResumableStepRunner(expired)
        report = await runner.run(TASK, [Step(name="commit", run=_commit, probe=_probe)])

        assert runs == []
        assert report.steps[0].state is StepState.RECONCILED
        assert report.skipped == ("commit",)

    async def test_the_probe_says_not_completed_so_the_step_runs_again(self) -> None:
        """**探测说没做 —— 那就做。**"""
        ledger = InMemoryStepEffectLedger(lease_seconds=0, clock=_Clock())
        runs: list[str] = []

        async def _commit() -> str:
            runs.append("commit")
            return "done"

        async def _probe() -> ProbeOutcome:
            return ProbeOutcome.NOT_COMPLETED

        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        runner = ResumableStepRunner(ledger)
        report = await runner.run(TASK, [Step(name="commit", run=_commit, probe=_probe)])

        assert runs == ["commit"]
        assert report.completed == ("commit",)

    async def test_an_unknown_probe_stops_the_run(self) -> None:
        """**探测不出来，就停下来。**

        猜"做完了"会漏掉一件事，猜"没做"会重复执行一次 —— **两种猜法都是错
        的**，所以唯一正确的动作是停。
        """
        ledger = InMemoryStepEffectLedger(lease_seconds=0, clock=_Clock())
        runs: list[str] = []

        async def _commit() -> str:
            runs.append("commit")
            return "done"

        async def _probe() -> ProbeOutcome:
            return ProbeOutcome.UNKNOWN

        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        runner = ResumableStepRunner(ledger)
        report = await runner.run(TASK, [Step(name="commit", run=_commit, probe=_probe)])

        assert runs == []
        assert report.halted is True
        assert report.steps[0].state is StepState.ABORTED

    async def test_a_step_without_a_probe_stops_the_run_when_the_result_is_unknown(self) -> None:
        """**没有探测的步骤在结果未知时只能停。**"""
        ledger = InMemoryStepEffectLedger(lease_seconds=0, clock=_Clock())
        runs: list[str] = []

        async def _commit() -> str:
            runs.append("commit")
            return "done"

        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        runner = ResumableStepRunner(ledger)
        report = await runner.run(TASK, [Step(name="commit", run=_commit)])

        assert runs == []
        assert report.steps[0].state is StepState.ABORTED

    async def test_a_contended_step_stops_the_run(self) -> None:
        """**别人正在做 —— 停，而且不碰它。**"""
        ledger = _ledger()
        runs: list[str] = []

        async def _commit() -> str:
            runs.append("commit")
            return "done"

        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")

        runner = ResumableStepRunner(ledger)
        report = await runner.run(TASK, [Step(name="commit", run=_commit)])

        assert runs == []
        assert report.steps[0].state is StepState.CONTENDED

    async def test_a_stopped_run_does_not_go_on_to_the_next_step(self) -> None:
        """**停下来的流程不往下走。** 在不确定的地基上继续盖房子没有意义。"""
        ledger = InMemoryStepEffectLedger(lease_seconds=0, clock=_Clock())
        runs: list[str] = []

        async def _first() -> str:
            runs.append("first")
            return "done"

        async def _second() -> str:
            runs.append("second")
            return "done"

        await ledger.claim(TASK, "first", attempt=1, idempotency_key="k")

        runner = ResumableStepRunner(ledger)
        report = await runner.run(
            TASK,
            [Step(name="first", run=_first), Step(name="second", run=_second)],
        )

        assert runs == []
        assert report.halted_step == "first"

    async def test_a_failure_is_recorded_and_raised(self) -> None:
        """**失败先记账，再把异常原样抛出去。**

        账上留着"这一步试过、失败了"，恢复的时候它是可以重跑的；异常交给调
        用方，因为失败的原因要由调用方处理。
        """
        ledger = _ledger()

        async def _commit() -> str:
            raise RuntimeError("git 说不行")

        runner = ResumableStepRunner(ledger)
        with pytest.raises(RuntimeError, match="git 说不行"):
            await runner.run(TASK, [Step(name="commit", run=_commit)])

        effects = await ledger.effects(TASK)
        assert len(effects) == 1
        assert effects[0].status is StepStatus.FAILED
        assert "git 说不行" in effects[0].note

    async def test_a_failed_step_can_be_run_again(self) -> None:
        """**失败的那一步，下一轮是可以重跑的。**"""
        ledger = _ledger()
        attempts: list[str] = []

        async def _commit() -> str:
            attempts.append("commit")
            if len(attempts) == 1:
                raise RuntimeError("第一次不行")
            return "done"

        runner = ResumableStepRunner(ledger)
        with pytest.raises(RuntimeError):
            await runner.run(TASK, [Step(name="commit", run=_commit)])

        report = await runner.run(TASK, [Step(name="commit", run=_commit)])

        assert attempts == ["commit", "commit"]
        assert report.completed == ("commit",)


class TestTheProgress:
    """任务状态 —— **从账本问出来的**。"""

    def test_the_progress_says_where_to_resume(self) -> None:
        """**"从哪一步接着跑"是第一个没做完的。**"""
        effects = (
            StepEffect(
                task_id=TASK,
                step="commit",
                attempt=1,
                status=StepStatus.DONE,
                idempotency_key="k",
                recorded_at=START,
            ),
            StepEffect(
                task_id=TASK,
                step="verify",
                attempt=1,
                status=StepStatus.STARTED,
                idempotency_key="k2",
                recorded_at=START,
            ),
        )

        progress = build_progress(TASK, effects, ["commit", "verify", "merge"])

        assert progress.done == ("commit",)
        assert progress.started == ("verify",)
        assert progress.resume_from == "verify"

    def test_a_finished_task_has_no_resume_point(self) -> None:
        """**全做完了就没有断点。**"""
        effects = (
            StepEffect(
                task_id=TASK,
                step="commit",
                attempt=1,
                status=StepStatus.DONE,
                idempotency_key="k",
                recorded_at=START,
            ),
        )

        progress = build_progress(TASK, effects, ["commit"])

        assert progress.resume_from is None
        assert progress.finished is True

    def test_the_latest_attempt_wins(self) -> None:
        """**旧的那条说的是上一次尝试的事。**"""
        effects = (
            StepEffect(
                task_id=TASK,
                step="commit",
                attempt=1,
                status=StepStatus.FAILED,
                idempotency_key="k",
                recorded_at=START,
            ),
            StepEffect(
                task_id=TASK,
                step="commit",
                attempt=2,
                status=StepStatus.DONE,
                idempotency_key="k2",
                recorded_at=START,
            ),
        )

        progress = build_progress(TASK, effects, ["commit"])

        assert progress.done == ("commit",)
        assert progress.failed == ()

    async def test_the_progress_is_read_from_the_ledger(self) -> None:
        """**"任务状态可恢复"问的就是这个。**"""
        ledger = _ledger()
        await ledger.claim(TASK, "commit", attempt=1, idempotency_key="k")
        await ledger.complete(TASK, "commit", attempt=1)

        progress = await read_progress(ledger, TASK, ["commit", "verify"])

        assert progress.done == ("commit",)
        assert progress.resume_from is None

    def test_the_step_key_is_derived_from_the_inputs(self) -> None:
        """**键必须由输入派生** —— 算两次要得到同一个键。"""
        assert build_step_key(TASK, "commit") == build_step_key(TASK, "commit")
        assert build_step_key(TASK, "commit") != build_step_key(TASK, "verify")

    def test_an_empty_step_key_is_refused(self) -> None:
        """**空键会让两个不同的步骤撞成同一个键。**"""
        with pytest.raises(ValueError):
            build_step_key(TASK, "")
