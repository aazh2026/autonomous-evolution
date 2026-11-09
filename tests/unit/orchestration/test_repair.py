"""TASK-P1-063 —— 有界自动修复的编排（全局三重上限）。

设计依据：docs/tasks.md TASK-P1-063、T1 ①（修复次数达上限后停止并置
``BLOCKED``）、T1 ②（修复成本超预算后停止）、T1 ③（修复总时长超限后停止）、
T1 ④（并行两个任务时，全局上限不会被各自突破）；docs/design.md §6。

**这里验的是"协调器怎么答"。** 领域层的算术在
``tests/unit/core/test_repair.py``；这里看的是编排层的**结论**：准不准修、
撞的是哪一条上限、该走哪条迁移。

**为什么"全局共享"要有并发的那一条。** 每任务的上限在状态机里，两条都看得出
来 —— 但它们各管各的：每任务那条管"这个任务修了几次"，全局那条管"这个项目的
所有任务一共修了多少次"。**只看前者的挡不住并行**：两个任务各修 3 次，看着都
没超，合起来就是 6 次，而上限说的是 5 次。所以 T1 ④ 要真的把两个任务并发跑起来。

**协调器只算迁移，不落状态。** 所以"被拒之后任务停在 ``BLOCKED``"这句话，
在**循环跑完的状态**里才看得见（单元测试里状态是传进去的参数）。这一条由
``TestTheLoopUsesTheGlobalLimit`` 盯着。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.task.execution_loop import FailureCategory
from core.task.outcome import VerificationVerdict
from core.task.repair import (
    FailureKind,
    FailureSignal,
    InMemoryRepairLedger,
    RepairLimit,
    RepairLimits,
)
from core.task.state_machine import TaskState
from orchestration.loop_a import build_loop_a_graph, run_loop_a
from orchestration.repair import RepairCoordinator, RepairOutcome, RepairRequest
from tests.support.execution_loop import StubHandlers, ready_state

_PROJECT = "P-1"


class _FakeClock:
    """能往前拨的钟 —— 时限那条线要靠它才测得动。"""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _signal(kind: FailureKind = FailureKind.CHECK_FAILED) -> FailureSignal:
    """一个失败信号。"""
    return FailureSignal(kind=kind, failed_checks=("unit_tests_pass",))


def _request(
    task_id: str = "TASK-1",
    *,
    project_id: str = _PROJECT,
    current_state: TaskState = TaskState.VERIFYING,
    cost: Decimal = Decimal(0),
    repair_count: int = 0,
) -> RepairRequest:
    """一次修复请求。"""
    return RepairRequest(
        task_id=task_id,
        project_id=project_id,
        current_state=current_state,
        cost=cost,
        repair_count=repair_count,
    )


def _coordinator(
    *,
    limits: RepairLimits | None = None,
    ledger: InMemoryRepairLedger | None = None,
) -> tuple[RepairCoordinator, InMemoryRepairLedger]:
    """搭一个协调器与它那本账。"""
    book = ledger or InMemoryRepairLedger()
    return RepairCoordinator(book, limits=limits or RepairLimits(max_attempts=3)), book


class TestTheCountLimit:
    """T1 ① —— 修复次数达上限后停止并置 ``BLOCKED``。"""

    async def test_the_repairs_are_allowed_up_to_the_limit(self) -> None:
        """**到限即停。** 上限 2 次时前两次都该放行 —— "还能修 2 次"就该包含
        第 2 次本身。"""
        coordinator, _ = _coordinator(limits=RepairLimits(max_attempts=2))

        first = await coordinator.plan(_request(), signal=_signal())
        second = await coordinator.plan(_request(), signal=_signal())

        assert first.allowed is True
        assert second.allowed is True

    async def test_the_repair_after_the_limit_is_refused(self) -> None:
        """第 3 次撞上限。"""
        coordinator, _ = _coordinator(limits=RepairLimits(max_attempts=2))

        await coordinator.plan(_request(), signal=_signal())
        await coordinator.plan(_request(), signal=_signal())
        third = await coordinator.plan(_request(), signal=_signal())

        assert third.allowed is False
        assert third.limit is RepairLimit.COUNT

    async def test_the_task_is_blocked_when_the_count_is_used_up(self) -> None:
        """**停下来挂起。** 不是"再试一次看看"，也不是停在原地。"""
        coordinator, _ = _coordinator(limits=RepairLimits(max_attempts=1))

        await coordinator.plan(_request(), signal=_signal())
        refused = await coordinator.plan(_request(), signal=_signal())

        assert refused.transition.current is TaskState.BLOCKED
        assert refused.transition.changed is True

    async def test_the_refusal_says_which_limit_was_hit(self) -> None:
        """**"修复停了"是一句没法行动的话** —— "次数超了"才知道该提配额还是
        该认账。"""
        coordinator, _ = _coordinator(limits=RepairLimits(max_attempts=1))

        await coordinator.plan(_request(), signal=_signal())
        refused = await coordinator.plan(_request(), signal=_signal())

        assert "1" in refused.reason

    async def test_the_allowed_repair_moves_the_task_to_repairing(self) -> None:
        """准修就往 ``REPAIRING`` 走 —— 那条迁移是状态机判的，编排层不自己
        判断。"""
        coordinator, _ = _coordinator()

        outcome = await coordinator.plan(_request(), signal=_signal())

        assert outcome.transition.current is TaskState.REPAIRING

    async def test_the_refused_reservation_consumes_nothing(self) -> None:
        """**被拒就一点都不记。** 额度凭空少了一截却没人真的修过，比不记更
        糟。"""
        coordinator, book = _coordinator(limits=RepairLimits(max_attempts=1))

        await coordinator.plan(_request(), signal=_signal())
        await coordinator.plan(_request(), signal=_signal())

        assert (await book.usage(_PROJECT)).attempts == 1


class TestTheCostLimit:
    """T1 ② —— 修复成本超预算后停止。"""

    async def test_the_cost_within_the_budget_is_allowed(self) -> None:
        """预算之内不该被拦。"""
        coordinator, _ = _coordinator(limits=RepairLimits(max_attempts=10, max_cost=Decimal("10")))

        outcome = await coordinator.plan(_request(cost=Decimal("6")), signal=_signal())

        assert outcome.allowed is True

    async def test_the_cost_that_exceeds_the_budget_is_refused(self) -> None:
        """**加上这次就超了** —— 超出去就不是"以后再说"，是现在就停。"""
        coordinator, _ = _coordinator(limits=RepairLimits(max_attempts=10, max_cost=Decimal("10")))

        outcome = await coordinator.plan(_request(cost=Decimal("11")), signal=_signal())

        assert outcome.allowed is False
        assert outcome.limit is RepairLimit.COST

    async def test_the_task_is_blocked_when_the_cost_is_over(self) -> None:
        """撞成本上限也要停下来挂起。"""
        coordinator, _ = _coordinator(limits=RepairLimits(max_attempts=10, max_cost=Decimal("10")))

        outcome = await coordinator.plan(_request(cost=Decimal("11")), signal=_signal())

        assert outcome.transition.current is TaskState.BLOCKED

    async def test_the_costs_accumulate_across_repairs(self) -> None:
        """**成本是累加的。** 每次都只花一点，加起来超了预算，一样要停。"""
        coordinator, book = _coordinator(
            limits=RepairLimits(max_attempts=10, max_cost=Decimal("10"))
        )

        await coordinator.plan(_request(cost=Decimal("6")), signal=_signal())
        refused = await coordinator.plan(_request(cost=Decimal("6")), signal=_signal())

        assert refused.allowed is False
        assert refused.limit is RepairLimit.COST
        assert (await book.usage(_PROJECT)).cost == Decimal("6")


class TestTheTimeLimit:
    """T1 ③ —— 修复总时长超限后停止。"""

    async def test_the_first_repair_opens_the_window(self) -> None:
        """**时限从第一次预留起算** —— 不是从各项修复用时之和。"""
        clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        coordinator = RepairCoordinator(
            InMemoryRepairLedger(clock=clock),
            limits=RepairLimits(max_attempts=10, max_duration=timedelta(minutes=30)),
        )

        outcome = await coordinator.plan(_request(), signal=_signal())

        assert outcome.allowed is True

    async def test_the_repair_after_the_deadline_is_refused(self) -> None:
        """**一次修复只要还在"修"就一直占着执行器** —— 不设时限，一个跑不出来
        的任务能把整轮演进拖到天亮。"""
        clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        coordinator = RepairCoordinator(
            InMemoryRepairLedger(clock=clock),
            limits=RepairLimits(max_attempts=10, max_duration=timedelta(minutes=30)),
        )
        await coordinator.plan(_request(), signal=_signal())

        clock.advance(timedelta(minutes=31))
        refused = await coordinator.plan(_request(), signal=_signal())

        assert refused.allowed is False
        assert refused.limit is RepairLimit.TIME

    async def test_the_task_is_blocked_when_the_time_is_over(self) -> None:
        """撞时限也要停下来挂起。"""
        clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        coordinator = RepairCoordinator(
            InMemoryRepairLedger(clock=clock),
            limits=RepairLimits(max_attempts=10, max_duration=timedelta(minutes=30)),
        )
        await coordinator.plan(_request(), signal=_signal())

        clock.advance(timedelta(minutes=31))
        refused = await coordinator.plan(_request(), signal=_signal())

        assert refused.transition.current is TaskState.BLOCKED


class TestTheGlobalLimitIsShared:
    """T1 ④ —— 并行两个任务时，全局上限不会被各自突破。"""

    async def test_two_tasks_cannot_each_take_the_whole_budget(self) -> None:
        """**两个任务各修 3 次，但全局上限是 5 —— 总次数不超过 5。**

        **这正是每任务上限挡不住的那一种。** 两个任务各修 3 次，看着都没超，
        合起来就是 6 次 —— 而上限说的是 5 次。
        """
        coordinator, book = _coordinator(limits=RepairLimits(max_attempts=5))

        async def repair_three_times(task_id: str) -> list[RepairOutcome]:
            outcomes: list[RepairOutcome] = []
            for round_number in range(3):
                outcomes.append(
                    await coordinator.plan(
                        _request(task_id, repair_count=round_number),
                        signal=_signal(),
                    )
                )
            return outcomes

        first, second = await asyncio.gather(
            repair_three_times("TASK-A"),
            repair_three_times("TASK-B"),
        )

        granted = [item for item in first + second if item.allowed]
        assert len(granted) == 5
        assert (await book.usage(_PROJECT)).attempts == 5

    async def test_the_refused_one_is_blocked(self) -> None:
        """**被拒的那个也要停下来挂起** —— 不能是"这次不算"。"""
        coordinator, _ = _coordinator(limits=RepairLimits(max_attempts=5))

        async def repair_three_times(task_id: str) -> list[RepairOutcome]:
            outcomes: list[RepairOutcome] = []
            for round_number in range(3):
                outcomes.append(
                    await coordinator.plan(
                        _request(task_id, repair_count=round_number),
                        signal=_signal(),
                    )
                )
            return outcomes

        first, second = await asyncio.gather(
            repair_three_times("TASK-A"),
            repair_three_times("TASK-B"),
        )

        refused = [item for item in first + second if not item.allowed]
        assert len(refused) == 1
        assert refused[0].transition.current is TaskState.BLOCKED

    async def test_a_budget_that_is_big_enough_lets_both_tasks_finish(self) -> None:
        """**上限不是用来挡事的，是用来兜底的。** 额度够时两个任务都该修满
        各自的 3 次 —— 把上限当配额用就成了另一道每任务闸门。"""
        coordinator, book = _coordinator(limits=RepairLimits(max_attempts=6))

        async def repair_three_times(task_id: str) -> list[RepairOutcome]:
            outcomes: list[RepairOutcome] = []
            for round_number in range(3):
                outcomes.append(
                    await coordinator.plan(
                        _request(task_id, repair_count=round_number),
                        signal=_signal(),
                    )
                )
            return outcomes

        first, second = await asyncio.gather(
            repair_three_times("TASK-A"),
            repair_three_times("TASK-B"),
        )

        assert all(item.allowed for item in first + second)
        assert (await book.usage(_PROJECT)).attempts == 6

    async def test_another_project_has_its_own_budget(self) -> None:
        """**一个项目的额度不该被另一个项目用掉。**"""
        coordinator, book = _coordinator(limits=RepairLimits(max_attempts=1))

        await coordinator.plan(_request("TASK-A", project_id="P-1"), signal=_signal())
        other = await coordinator.plan(_request("TASK-B", project_id="P-2"), signal=_signal())

        assert other.allowed is True
        assert (await book.usage("P-2")).attempts == 1


class TestThePerTaskLimitStillApplies:
    """每任务那条闸门还在 —— 两条闸门是叠加的，不是二选一。"""

    async def test_the_per_task_limit_refuses_before_the_global_one(self) -> None:
        """**每任务修满了就停** —— 与全局额度无关。"""
        coordinator, _book = _coordinator(limits=RepairLimits(max_attempts=100))

        outcome = await coordinator.plan(
            _request(repair_count=3),
            signal=_signal(),
        )

        assert outcome.allowed is False
        assert outcome.limit is RepairLimit.COUNT

    async def test_the_per_task_refusal_consumes_no_global_budget(self) -> None:
        """**顺序反了就会白占一格** —— 这个任务修不了，不该把项目的额度也搭
        进去。"""
        coordinator, book = _coordinator(limits=RepairLimits(max_attempts=100))

        await coordinator.plan(_request(repair_count=3), signal=_signal())

        assert (await book.usage(_PROJECT)).attempts == 0


class TestTheClassification:
    """失败归类决定"要不要自己修"。"""

    async def test_a_failure_that_needs_a_human_is_not_repaired(self) -> None:
        """**该由人来看的失败不自动修** —— 自动修多半是在修一个不存在的问题。"""
        coordinator, _book = _coordinator()

        outcome = await coordinator.plan(
            _request(),
            signal=FailureSignal(kind=FailureKind.VERIFIER_ERROR),
        )

        assert outcome.allowed is False
        assert outcome.diagnosis.category is FailureCategory.NEEDS_HUMAN

    async def test_an_unrepairable_failure_is_not_repaired(self) -> None:
        """**任务的前提本身不成立** —— 改这个任务的代码解决不了它。"""
        coordinator, _ = _coordinator()

        outcome = await coordinator.plan(
            _request(),
            signal=FailureSignal(kind=FailureKind.SPEC_CONFLICT),
        )

        assert outcome.allowed is False
        assert outcome.diagnosis.category is FailureCategory.UNREPAIRABLE

    async def test_a_failure_that_is_not_repaired_consumes_no_budget(self) -> None:
        """**额度是给"真的要修"这件事留的。** 没修就占一格，等于凭空少了
        一截却没人真的修过。"""
        coordinator, book = _coordinator()

        await coordinator.plan(
            _request(),
            signal=FailureSignal(kind=FailureKind.SPEC_CONFLICT),
        )

        assert (await book.usage(_PROJECT)).attempts == 0

    async def test_the_repair_task_carries_the_diagnosis(self) -> None:
        """**修复任务带着诊断走** —— 而不是一句"再试一次"。"""
        coordinator, _ = _coordinator()

        outcome = await coordinator.plan(_request(), signal=_signal())

        assert outcome.repair_task is not None
        assert outcome.repair_task.diagnosis is outcome.diagnosis
        assert outcome.repair_task.attempt == 1

    async def test_the_redelivery_does_not_repair_twice(self) -> None:
        """**重投不重复修复。** 任务已经在 ``REPAIRING``，这次是幂等空操作 ——
        不重复造任务，也不占额度。"""
        coordinator, book = _coordinator()

        outcome = await coordinator.plan(
            _request(current_state=TaskState.REPAIRING),
            signal=_signal(),
        )

        assert outcome.transition.changed is False
        assert outcome.repair_task is None
        assert (await book.usage(_PROJECT)).attempts == 0


class TestTheLoopUsesTheGlobalLimit:
    """循环 A 真的用上全局上限 —— **接上了才算数**。"""

    async def test_the_global_limit_stops_the_loop_earlier_than_the_per_task_one(
        self,
    ) -> None:
        """**全局上限比每任务那条先到就先停。** 每任务能修 3 次而全局只给 1
        次时，循环不该修满 3 次。"""
        handlers = StubHandlers(
            verdicts=[VerificationVerdict.FAILED],
            repair_limits=RepairLimits(max_attempts=1),
        )

        final = await run_loop_a(build_loop_a_graph(handlers.handlers), ready_state())

        assert final.task_state is TaskState.BLOCKED
        assert final.repair_count == 1
        assert handlers.journal.nodes.count("agent_executes") == 2

    async def test_the_cost_limit_stops_the_loop(self) -> None:
        """**成本上限也接上了。** 打桩执行器每轮报 0.5，而全局预算只有 0.4 ——
        第一次修复就该被拦下。"""
        handlers = StubHandlers(
            verdicts=[VerificationVerdict.FAILED],
            repair_limits=RepairLimits(max_attempts=10, max_cost=Decimal("0.4")),
        )

        final = await run_loop_a(build_loop_a_graph(handlers.handlers), ready_state())

        assert final.task_state is TaskState.BLOCKED
        assert final.repair_count == 0

    async def test_the_per_task_limit_still_stops_the_loop(self) -> None:
        """**两条闸门是叠加的。** 全局额度宽裕时，每任务那条照样把循环停下
        来。"""
        handlers = StubHandlers(
            verdicts=[VerificationVerdict.FAILED],
            repair_limits=RepairLimits(max_attempts=100),
        )

        final = await run_loop_a(build_loop_a_graph(handlers.handlers), ready_state())

        assert final.task_state is TaskState.BLOCKED
        assert final.repair_count == 3

    async def test_the_reason_names_the_limit_that_was_hit(self) -> None:
        """**停下来的理由要说得清是哪一条。**"""
        handlers = StubHandlers(
            verdicts=[VerificationVerdict.FAILED],
            repair_limits=RepairLimits(max_attempts=1),
        )

        final = await run_loop_a(build_loop_a_graph(handlers.handlers), ready_state())

        assert final.blocked_reason
        assert "1" in final.blocked_reason


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        (RepairLimits(max_attempts=1), RepairLimit.COUNT),
        (RepairLimits(max_attempts=10, max_cost=Decimal("0")), RepairLimit.COST),
    ],
)
async def test_the_limit_that_was_hit_is_reported(
    limits: RepairLimits,
    expected: RepairLimit,
) -> None:
    """**撞的是哪一条要说得出来** —— 三条线各有各的补救动作。"""
    coordinator, _ = _coordinator(limits=limits)

    await coordinator.plan(_request(cost=Decimal("1")), signal=_signal())
    outcome = await coordinator.plan(_request(cost=Decimal("1")), signal=_signal())

    assert outcome.limit is expected
