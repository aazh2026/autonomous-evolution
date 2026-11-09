"""TASK-P1-063 —— 有界自动修复的领域契约（三重上限 + 失败归类）。

**这里验的是规则本身，不依赖数据库。** 两组东西：额度账本的算术（次数 / 成本
/ 时间三条线各算各的），以及失败归类的口径（哪一类能自动修、哪一类得人来看、
哪一类修不了）。

**为什么"全局共享"要在领域层单独立一组用例。** 每任务的上限在状态机里，全局
的上限在这里 —— 两处各数一次早晚会对不上，而这正是要挡住的事：两个任务各修 3
次，看着都没超，合起来 6 次，而上限说的是 5 次。

**为什么"预留是一个原子动作"要能被证伪。** 分成"查询"加"占用"两步的话，两
个并行的任务会各自看到"还有 3 次"然后各自占用 —— 总数突破了上限，而**没有
任何一个任务报错**。所以这里要有一条用例断言"并发预留时批出去的总次数不超过
上限"。

**失败归类不解析人话。** 归类只看结构化的字段 —— 措辞是执行器定的，改一次措
辞就改一次分类，而分类决定"要不要自己修"。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.task.execution_loop import FailureCategory
from core.task.outcome import TaskOutcome, VerificationVerdict
from core.task.repair import (
    DEFAULT_MAX_ATTEMPTS,
    FailureKind,
    FailureSignal,
    InMemoryRepairLedger,
    RepairLimit,
    RepairLimits,
    RepairUsage,
    build_repair_task,
    classify,
    first_violation,
    kind_of,
)


class _FakeClock:
    """能往前拨的钟 —— 时限那条线要靠它才测得动。

    **不打桩就没法测时限。** 真等三十秒不现实，而"把钟拨过去"正是时限到期
    这件事本身。
    """

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _signal(kind: FailureKind = FailureKind.CHECK_FAILED, **kwargs: object) -> FailureSignal:
    """一个失败信号。"""
    return FailureSignal(kind=kind, **kwargs)  # type: ignore[arg-type]


class TestTheLimits:
    """三重上限本身。"""

    def test_a_negative_count_is_rejected(self) -> None:
        """**负的上限是没意义的配置** —— 与其让它在半夜炸掉，不如当场拒绝。"""
        with pytest.raises(ValueError):
            RepairLimits(max_attempts=-1)

    def test_a_negative_cost_is_rejected(self) -> None:
        """同上。"""
        with pytest.raises(ValueError):
            RepairLimits(max_cost=Decimal("-1"))

    def test_a_negative_duration_is_rejected(self) -> None:
        """同上。"""
        with pytest.raises(ValueError):
            RepairLimits(max_duration=timedelta(minutes=-1))

    def test_the_cost_and_the_duration_may_be_left_unbounded(self) -> None:
        """**"不设限"不等于"上限是零"。** 三样都设死会让配置很啰嗦，而"先只
        限次数"是常见的起步姿态。"""
        limits = RepairLimits(max_attempts=3)

        assert limits.max_cost is None
        assert limits.max_duration is None

    def test_the_default_count_is_the_same_as_the_per_task_one(self) -> None:
        """**不另立口径。** 全局与每任务那条闸门用同一个默认值，省得两处对不上。"""
        assert DEFAULT_MAX_ATTEMPTS == 3


class TestWhichLimitIsHit:
    """撞的是哪一条上限。"""

    def test_the_count_is_checked_first(self) -> None:
        """**检查顺序是固定的**（次数 → 成本 → 时间）—— 同一份用量总是报出同一
        条上限，读日志的人看到的"卡在哪"是稳定的。"""
        limits = RepairLimits(max_attempts=1, max_cost=Decimal("1"), max_duration=timedelta(0))
        usage = RepairUsage(attempts=1, cost=Decimal("5"), elapsed=timedelta(hours=1))

        violation = first_violation(usage, cost=Decimal("1"), limits=limits)

        assert violation is not None
        assert violation[0] is RepairLimit.COUNT

    def test_the_cost_is_checked_before_the_time(self) -> None:
        """**成本比时间先报。** 成本是能立刻批的数字，而时限到了只能等 —— 先说
        能马上动的那个。"""
        limits = RepairLimits(max_attempts=10, max_cost=Decimal("1"), max_duration=timedelta(0))
        usage = RepairUsage(attempts=0, cost=Decimal("5"), elapsed=timedelta(hours=1))

        violation = first_violation(usage, cost=Decimal("1"), limits=limits)

        assert violation is not None
        assert violation[0] is RepairLimit.COST

    def test_the_last_allowed_attempt_is_allowed(self) -> None:
        """**到限即停。** 上限 3 次时第 3 次可以修 —— "还能修 3 次"就该包含第 3
        次本身。"""
        limits = RepairLimits(max_attempts=3)
        usage = RepairUsage(attempts=2)

        assert first_violation(usage, cost=Decimal(0), limits=limits) is None

    def test_the_attempt_after_the_limit_is_hit(self) -> None:
        """第 4 次撞次数上限。"""
        limits = RepairLimits(max_attempts=3)
        usage = RepairUsage(attempts=3)

        violation = first_violation(usage, cost=Decimal(0), limits=limits)

        assert violation is not None
        assert violation[0] is RepairLimit.COUNT

    def test_the_cost_that_exactly_reaches_the_limit_is_allowed(self) -> None:
        """**刚好用完不叫超。** 预算用满是可以接受的，超出去才是问题。"""
        limits = RepairLimits(max_cost=Decimal("10"))
        usage = RepairUsage(cost=Decimal("6"))

        assert first_violation(usage, cost=Decimal("4"), limits=limits) is None

    def test_the_cost_that_exceeds_the_limit_is_hit(self) -> None:
        """超一点就是超了。"""
        limits = RepairLimits(max_cost=Decimal("10"))
        usage = RepairUsage(cost=Decimal("6"))

        violation = first_violation(usage, cost=Decimal("4.01"), limits=limits)

        assert violation is not None
        assert violation[0] is RepairLimit.COST

    def test_the_elapsed_window_blocks_further_repairs(self) -> None:
        """**时间这条最容易漏。** 一次修复只要还在"修"就一直占着执行器 ——
        不设时限，一个跑不出来的任务能把整轮演进拖到天亮。"""
        limits = RepairLimits(max_duration=timedelta(minutes=30))
        usage = RepairUsage(elapsed=timedelta(minutes=30))

        violation = first_violation(usage, cost=Decimal(0), limits=limits)

        assert violation is not None
        assert violation[0] is RepairLimit.TIME

    def test_nothing_is_hit_when_every_dimension_is_fine(self) -> None:
        """没撞就返回 ``None`` —— 别硬凑一条上限出来。"""
        limits = RepairLimits(
            max_attempts=3,
            max_cost=Decimal("10"),
            max_duration=timedelta(hours=1),
        )
        usage = RepairUsage(attempts=1, cost=Decimal("1"), elapsed=timedelta(minutes=5))

        assert first_violation(usage, cost=Decimal("1"), limits=limits) is None


class TestTheLedger:
    """额度账本。"""

    async def test_a_granted_reservation_is_counted(self) -> None:
        """**批了就把额度记上去。**"""
        ledger = InMemoryRepairLedger()

        reservation = await ledger.reserve(
            scope="P-1",
            cost=Decimal("2"),
            limits=RepairLimits(max_attempts=3, max_cost=Decimal("10")),
        )

        assert reservation.granted is True
        usage = await ledger.usage("P-1")
        assert usage.attempts == 1
        assert usage.cost == Decimal("2")

    async def test_a_refused_reservation_records_nothing(self) -> None:
        """**不批就一点都不记。** 记一半比不记更糟 —— 额度凭空少了一截，却没
        人真的修过。"""
        ledger = InMemoryRepairLedger()
        limits = RepairLimits(max_attempts=1)

        await ledger.reserve(scope="P-1", cost=Decimal(0), limits=limits)
        refused = await ledger.reserve(scope="P-1", cost=Decimal(0), limits=limits)

        assert refused.granted is False
        assert refused.limit is RepairLimit.COUNT
        usage = await ledger.usage("P-1")
        assert usage.attempts == 1

    async def test_two_scopes_do_not_share_a_budget(self) -> None:
        """**一个项目的额度不该被另一个项目用掉。**"""
        ledger = InMemoryRepairLedger()
        limits = RepairLimits(max_attempts=1)

        await ledger.reserve(scope="P-1", cost=Decimal(0), limits=limits)
        other = await ledger.reserve(scope="P-2", cost=Decimal(0), limits=limits)

        assert other.granted is True

    async def test_the_usage_of_an_unknown_scope_is_zero(self) -> None:
        """没修过的项目就是零 —— 不是"报错"，也不是"上次剩下的"。"""
        ledger = InMemoryRepairLedger()

        usage = await ledger.usage("P-none")

        assert usage == RepairUsage()

    async def test_the_concurrent_reservations_never_exceed_the_limit(self) -> None:
        """**并发预留时批出去的总次数不超过上限。**

        **这是"预留是一个原子动作"这条要求的正面证明。** 分成"查询"加"占用"
        两步的话，这些预留会各自看到"还有额度"然后各自占用 —— 总数突破了上限，
        而**没有一个任务报错**。
        """
        ledger = InMemoryRepairLedger()
        limits = RepairLimits(max_attempts=5)

        reservations = await asyncio.gather(
            *(ledger.reserve(scope="P-1", cost=Decimal(0), limits=limits) for _ in range(20))
        )

        granted = [item for item in reservations if item.granted]
        assert len(granted) == 5
        assert (await ledger.usage("P-1")).attempts == 5

    async def test_the_window_opens_at_the_first_reservation(self) -> None:
        """**时限从第一次预留起算** —— 不是从各项修复用时之和。时限要管的是
        "这件事已经拖了多久"，不是"加起来修了多久"。"""
        clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        ledger = InMemoryRepairLedger(clock=clock)
        limits = RepairLimits(max_duration=timedelta(minutes=30))

        assert (await ledger.usage("P-1")).elapsed == timedelta(0)

        await ledger.reserve(scope="P-1", cost=Decimal(0), limits=limits)
        clock.advance(timedelta(minutes=10))

        assert (await ledger.usage("P-1")).elapsed == timedelta(minutes=10)


class TestTheFailureTaxonomy:
    """失败归类 —— 三类，三种后续动作。"""

    def test_a_check_failure_is_auto_repairable(self) -> None:
        """**必检项没过就是改代码的事** —— 这是唯一该自动修的一类。"""
        diagnosis = classify(_signal(FailureKind.CHECK_FAILED, failed_checks=("unit",)))

        assert diagnosis.category is FailureCategory.AUTO_REPAIRABLE
        assert diagnosis.auto_repairable is True

    def test_an_executor_error_needs_a_human(self) -> None:
        """**执行器自己崩了不该自动修** —— 自动修多半是在修一个不存在的问题。"""
        diagnosis = classify(_signal(FailureKind.EXECUTOR_ERROR))

        assert diagnosis.category is FailureCategory.NEEDS_HUMAN

    def test_a_verifier_error_needs_a_human(self) -> None:
        """**验证引擎自己崩了 —— 没有得出结论。** 没结论就没法判断该修什么。"""
        diagnosis = classify(_signal(FailureKind.VERIFIER_ERROR))

        assert diagnosis.category is FailureCategory.NEEDS_HUMAN

    def test_a_cancelled_failure_needs_a_human(self) -> None:
        """有人取消 —— 不是技术失败。"""
        diagnosis = classify(_signal(FailureKind.CANCELLED))

        assert diagnosis.category is FailureCategory.NEEDS_HUMAN

    def test_a_timeout_needs_a_human(self) -> None:
        """超时且恢复探测没救回来 —— 重试多半还是超时。"""
        diagnosis = classify(_signal(FailureKind.TIMED_OUT))

        assert diagnosis.category is FailureCategory.NEEDS_HUMAN

    def test_a_spec_conflict_is_unrepairable(self) -> None:
        """**任务的前提本身不成立。** 改这个任务的代码解决不了它，硬修只会把
        问题推到别处 —— 而"推到别处"正是自动修复最该避免的。"""
        diagnosis = classify(_signal(FailureKind.SPEC_CONFLICT))

        assert diagnosis.category is FailureCategory.UNREPAIRABLE

    def test_an_unknown_failure_needs_a_human(self) -> None:
        """**判不出是什么的时候，人往往知道。** 自动放弃一个任务比让人看一眼
        更糟。"""
        diagnosis = classify(_signal(FailureKind.UNKNOWN))

        assert diagnosis.category is FailureCategory.NEEDS_HUMAN

    def test_the_reason_is_never_empty(self) -> None:
        """**理由缺了没关系，类别不能缺。** 而理由也不能空 —— 空的理由让日志里
        出现"失败了，原因："。"""
        diagnosis = classify(_signal(FailureKind.UNKNOWN))

        assert diagnosis.reason.strip() != ""

    def test_the_given_reason_is_kept(self) -> None:
        """给了理由就用给的 —— 归类不覆盖执行方知道的事。"""
        diagnosis = classify(_signal(FailureKind.CHECK_FAILED, reason="单测挂了"))

        assert diagnosis.reason == "单测挂了"

    def test_the_failed_checks_become_the_evidence(self) -> None:
        """**没过的必检项要留下来。** 它是"凭什么说没过"的凭据。"""
        diagnosis = classify(_signal(FailureKind.CHECK_FAILED, failed_checks=("unit", "lint")))

        assert diagnosis.evidence == ("unit", "lint")

    def test_only_auto_repairable_failures_get_a_hint(self) -> None:
        """**给"需人来看"的失败写修复建议，等于在告诉 Agent"照这个改"** ——
        而那个建议没人会批。"""
        diagnosis = classify(_signal(FailureKind.SPEC_CONFLICT))

        assert diagnosis.repair_hint == ""


class TestTheKind:
    """从判定结果推出失败种类。"""

    def test_a_cancelled_outcome_is_cancelled(self) -> None:
        """取消就是取消。"""
        assert kind_of(TaskOutcome.CANCELLED, None) is FailureKind.CANCELLED

    def test_a_timed_out_outcome_is_timed_out(self) -> None:
        """超时不是失败 —— 处置不一样。"""
        assert kind_of(TaskOutcome.TIMED_OUT, None) is FailureKind.TIMED_OUT

    def test_a_verification_error_is_a_verifier_error(self) -> None:
        """**结论不可得就是验证引擎自己出了问题** —— 还没到"该修什么"那一步。"""
        assert kind_of(TaskOutcome.VERIFICATION_ERROR, None) is FailureKind.VERIFIER_ERROR

    def test_a_verifier_error_verdict_is_a_verifier_error(self) -> None:
        """验证引擎自己报错也一样。"""
        assert kind_of(TaskOutcome.FAILED, VerificationVerdict.ERROR) is FailureKind.VERIFIER_ERROR

    def test_the_failed_checks_are_check_failures(self) -> None:
        """**必检项没过就是改代码的事。**"""
        assert (
            kind_of(TaskOutcome.FAILED, VerificationVerdict.FAILED, failed_checks=("unit",))
            is FailureKind.CHECK_FAILED
        )

    def test_a_failed_verdict_is_a_check_failure(self) -> None:
        """**"Agent 说做完了，验证说没过" —— 这正是最该自动修的一种失败。**
        哪怕没列出是哪几项没过。"""
        kind = kind_of(TaskOutcome.COMPLETED, VerificationVerdict.FAILED)

        assert kind is FailureKind.CHECK_FAILED

    def test_a_failed_outcome_without_details_is_unknown(self) -> None:
        """**推不出的就归 UNKNOWN，不猜。** 猜出来的类别会决定"要不要自己
        修" —— 猜错一次就是一次不该发生的返工。"""
        assert kind_of(TaskOutcome.FAILED, None) is FailureKind.UNKNOWN


class TestTheRepairTask:
    """修复任务。"""

    def test_the_attempt_starts_at_one(self) -> None:
        """**"这是第几次修"从 1 起算** —— 状态机里那本账记的是"已经修了几
        次"，人说的是另一个口径。"""
        diagnosis = classify(_signal(FailureKind.CHECK_FAILED, failed_checks=("unit",)))

        repair = build_repair_task(task_id="TASK-1", attempt=1, diagnosis=diagnosis)

        assert repair.attempt == 1

    def test_the_attempt_must_not_be_zero(self) -> None:
        """0 不是"第几次" —— 那是"还没修过"。

        **断言的是领域自己的那句话。** 只断言"抛了个 ValueError"的话，模型上
        的 ``ge=1`` 也能顶上（pydantic 的 ``ValidationError`` 就是
        ``ValueError`` 的子类），那条领域规则就没人守了。
        """
        diagnosis = classify(_signal(FailureKind.CHECK_FAILED))

        with pytest.raises(ValueError, match="从 1 起算"):
            build_repair_task(task_id="TASK-1", attempt=0, diagnosis=diagnosis)

    def test_the_instruction_is_never_empty(self) -> None:
        """**空的指令会让 Agent 拿到一个"随便改改"的施工说明** —— 而"随便改
        改"正是无限修复循环的起点。"""
        diagnosis = classify(_signal(FailureKind.CHECK_FAILED, reason="单测挂了"))

        repair = build_repair_task(task_id="TASK-1", attempt=1, diagnosis=diagnosis)

        assert repair.instruction.strip() != ""

    def test_the_hint_becomes_the_instruction(self) -> None:
        """诊断给的建议就是施工说明。"""
        diagnosis = classify(_signal(FailureKind.CHECK_FAILED, failed_checks=("unit",)))

        repair = build_repair_task(task_id="TASK-1", attempt=2, diagnosis=diagnosis)

        assert repair.instruction == diagnosis.repair_hint
        assert repair.diagnosis is diagnosis
