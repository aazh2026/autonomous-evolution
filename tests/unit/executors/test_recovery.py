"""TASK-P1-034 —— 超时的处置流程。

覆盖任务书 T1 ①（超时且会话可恢复 → ``interrupted_resumable``，可续跑）、
T1 ②（超时且会话已损坏 → ``failed``，且未留下僵尸会话）与 T1 ③（重试产生
新的幂等键，不复用旧键）。

**这一组测的是"决策"，不是"资源"。** 真的进程与真的容器在
``tests/integration/test_timeout_recovery.py`` 里 —— 那一组断言的是**操作系
统**与 **docker** 那一侧的事实。这一组断言的是流程的形状：什么情况续跑、
什么情况重试、什么情况判失败，以及每一种情况下幂等键对不对。

**替身会记下调用顺序。** "先 interrupt 再探测"（I-03）是一条**顺序**约束，
只断言最终结论的话，把这两步换过来结论可能一模一样 —— 顺序错了就查不出
来。所以替身把每一次调用记进一个列表里，顺序是可以断言的。
"""

from __future__ import annotations

import pytest

from core.task.cancellation import (
    CancellationReport,
    CancellationTarget,
    LayerName,
    LayerOutcome,
    LayerState,
)
from executors.base import CancellationAck, CancellationState, ErrorCategory, ExecutionTask
from executors.recovery import (
    OUTCOME_ACTIONS,
    RecoveryAction,
    RecoveryOutcome,
    RecoveryPlan,
    RecoveryPorts,
    SessionProbeResult,
    recover_from_timeout,
)


def _task(*, attempt: int = 1) -> ExecutionTask:
    return ExecutionTask(
        session_id="session-1",
        task_id="TASK-1",
        title="任务",
        instruction="做一件事",
        timeout_seconds=60,
        attempt=attempt,
    )


def _report(*, container_stopped: bool = True) -> CancellationReport:
    """一份四层齐全的回收报告。

    默认每一层都确认 —— 收干净了。只有容器层可以失败，用来造"还有东西在
    跑"的情形。
    """
    return CancellationReport(
        target=CancellationTarget(task_id="TASK-1", session_id="session-1"),
        outcomes=(
            LayerOutcome(LayerName.TASK, LayerState.CONFIRMED, "请求已受理"),
            LayerOutcome(LayerName.ADAPTER, LayerState.CONFIRMED, "执行器确认已停"),
            LayerOutcome(LayerName.PROCESS, LayerState.CONFIRMED, "进程没了"),
            LayerOutcome(
                LayerName.SANDBOX,
                LayerState.CONFIRMED if container_stopped else LayerState.FAILED,
                "容器没了" if container_stopped else "容器还在跑",
            ),
        ),
    )


class _FakeInterrupter:
    """会记账的执行器替身。"""

    def __init__(self, *, state: CancellationState = CancellationState.CONFIRMED) -> None:
        self._state = state
        self.calls: list[str] = []

    async def interrupt(self, session_id: str) -> CancellationAck:
        self.calls.append(session_id)
        return CancellationAck(
            session_id=session_id,
            state=self._state,
            detail="进程已终止" if self._state is CancellationState.CONFIRMED else "停不下来",
        )


class _FakeProbe:
    """会记账的探测替身。"""

    def __init__(self, result: SessionProbeResult | None) -> None:
        self._result = result
        self.calls: list[str] = []

    async def probe_session(self, session_id: str) -> SessionProbeResult:
        self.calls.append(session_id)
        if self._result is None:
            raise RuntimeError("探测问不上话")
        return self._result


class _FakeReclaimer:
    """会记账的回收替身。"""

    def __init__(self, *, container_stopped: bool = True, raises: bool = False) -> None:
        self._container_stopped = container_stopped
        self._raises = raises
        self.calls: list[CancellationTarget] = []

    async def reclaim(self, target: CancellationTarget) -> CancellationReport:
        self.calls.append(target)
        if self._raises:
            raise RuntimeError("docker 问不上话")
        return _report(container_stopped=self._container_stopped)


def _ports(
    *,
    interrupter: _FakeInterrupter | None = None,
    probe: _FakeProbe | None = None,
    reclaimer: _FakeReclaimer | None = None,
) -> RecoveryPorts:
    return RecoveryPorts(
        interrupter=interrupter or _FakeInterrupter(),
        probe=probe or _FakeProbe(SessionProbeResult(resumable=True, reason="会话还在")),
        reclaimer=reclaimer or _FakeReclaimer(),
    )


class TestTheOutcomeDecidesTheAction:
    """结论与动作一一对应 —— 这张表是流程的骨架。"""

    def test_every_outcome_has_exactly_one_action(self) -> None:
        assert set(OUTCOME_ACTIONS) == set(RecoveryOutcome)
        assert len(OUTCOME_ACTIONS) == len(RecoveryOutcome)

    def test_the_mapping_is_the_one_the_task_describes(self) -> None:
        """可续跑 → 续跑；可重试 → 重试；失败 → 失败。

        写死这三行，而不是只断言"键值都在" —— 后者对**映射错了**完全没
        感觉：把 resume 与 retry 对调，键值一样多，测试照样绿。
        """
        assert OUTCOME_ACTIONS[RecoveryOutcome.INTERRUPTED_RESUMABLE] is RecoveryAction.RESUME
        assert OUTCOME_ACTIONS[RecoveryOutcome.INTERRUPTED_RETRYABLE] is RecoveryAction.RETRY
        assert OUTCOME_ACTIONS[RecoveryOutcome.FAILED] is RecoveryAction.FAIL

    async def test_the_action_always_agrees_with_the_outcome(self) -> None:
        """不管走哪条路，方案里的动作都必须与结论对得上。

        结论与动作是两处写下的东西，**它们可能对不上** —— 而"结论说可续
        跑、动作却是判失败"是最坏的一种不一致：调用方读动作，编排层读
        结论，两边会做出相反的决定。
        """
        cases = [
            (SessionProbeResult(resumable=True), ErrorCategory.RETRYABLE),
            (SessionProbeResult(resumable=False), ErrorCategory.RETRYABLE),
            (SessionProbeResult(resumable=False), ErrorCategory.NON_RETRYABLE),
        ]
        for probe_result, category in cases:
            plan = await recover_from_timeout(
                _task(),
                ports=_ports(probe=_FakeProbe(probe_result)),
                error_category=category,
            )
            assert plan.action is OUTCOME_ACTIONS[plan.outcome]


class TestARecoverableSession:
    """T1 ① —— 超时且会话可恢复 → ``interrupted_resumable``，可续跑。"""

    async def test_the_outcome_is_interrupted_resumable(self) -> None:
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(probe=_FakeProbe(SessionProbeResult(resumable=True, reason="会话还在"))),
        )

        assert plan.outcome is RecoveryOutcome.INTERRUPTED_RESUMABLE
        assert plan.action is RecoveryAction.RESUME

    async def test_the_resume_keeps_the_same_attempt_and_the_same_key(self) -> None:
        """续跑是**同一次尝试**的延续 —— 键不变。

        换键就把续跑变成了"又一次新提交"，而这次执行明明还没结束。
        """
        task = _task(attempt=3)
        plan = await recover_from_timeout(
            task,
            ports=_ports(probe=_FakeProbe(SessionProbeResult(resumable=True))),
        )

        assert plan.attempt == 3
        assert plan.idempotency_key == "TASK-1:3"

    async def test_the_reason_carries_the_evidence(self) -> None:
        """方案要带上**依据**，不只带结论。

        一个"可以续跑"而不说为什么的方案，事后没人能判断该不该信它。
        """
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(
                probe=_FakeProbe(
                    SessionProbeResult(resumable=True, reason="会话还在，工作空间也在")
                )
            ),
        )

        assert "会话还在，工作空间也在" in plan.reason


class TestASessionThatCannotBeResumed:
    """T1 ② ③ —— 续不上了：该重试还是该判失败，看分类。"""

    async def test_a_retryable_timeout_gets_a_new_idempotency_key(self) -> None:
        """T1 ③ —— 重试产生新的幂等键，不复用旧键。"""
        task = _task(attempt=2)
        plan = await recover_from_timeout(
            task,
            ports=_ports(probe=_FakeProbe(SessionProbeResult(resumable=False, reason="会话没了"))),
            error_category=ErrorCategory.RETRYABLE,
        )

        assert plan.outcome is RecoveryOutcome.INTERRUPTED_RETRYABLE
        assert plan.action is RecoveryAction.RETRY
        assert plan.attempt == 3
        assert plan.idempotency_key == "TASK-1:3"

    async def test_the_new_key_is_not_the_old_one(self) -> None:
        """新键与旧键**必须不同** —— 否则重放去重会把这次当成上次。

        这一条单独立一个用例：新键算错（例如写成 ``TASK-1:2:3``）时，
        "键等于 ``TASK-1:3``"这种断言仍可能蒙对，而"不等于旧键"不会。
        """
        plan = await recover_from_timeout(
            _task(attempt=2),
            ports=_ports(probe=_FakeProbe(SessionProbeResult(resumable=False))),
            error_category=ErrorCategory.RETRYABLE,
        )

        assert plan.idempotency_key is not None
        assert plan.idempotency_key != "TASK-1:2"

    async def test_a_non_retryable_timeout_fails(self) -> None:
        """T1 ② —— 续不上且不值得再试 → ``failed``。"""
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(probe=_FakeProbe(SessionProbeResult(resumable=False, reason="会话没了"))),
            error_category=ErrorCategory.NON_RETRYABLE,
        )

        assert plan.outcome is RecoveryOutcome.FAILED
        assert plan.action is RecoveryAction.FAIL

    async def test_a_failed_plan_has_no_next_attempt_and_no_key(self) -> None:
        """判失败之后没有下一次执行 —— 也就没有下一次尝试的键。

        给一个"不会再跑"的方案发一把键，等于让调用方以为还有一次执行。
        """
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(probe=_FakeProbe(SessionProbeResult(resumable=False))),
            error_category=ErrorCategory.NON_RETRYABLE,
        )

        assert plan.attempt is None
        assert plan.idempotency_key is None


class TestTheOrderOfTheSteps:
    """I-03 —— 先 interrupt，再探测。"""

    async def test_the_interrupt_happens_before_the_probe(self) -> None:
        """探一个还在跑的会话，探出来的"可恢复"没有意义。

        所以顺序必须是 **interrupt → 回收 → 探测**。把这两步换过来，最终
        结论可能一模一样 —— 顺序错了只能靠**记账**查出来。
        """
        calls: list[str] = []

        class _RecordingInterrupter(_FakeInterrupter):
            async def interrupt(self, session_id: str) -> CancellationAck:
                calls.append("interrupt")
                return await super().interrupt(session_id)

        class _RecordingProbe(_FakeProbe):
            async def probe_session(self, session_id: str) -> SessionProbeResult:
                calls.append("probe")
                return await super().probe_session(session_id)

        await recover_from_timeout(
            _task(),
            ports=_ports(
                interrupter=_RecordingInterrupter(),
                probe=_RecordingProbe(SessionProbeResult(resumable=True)),
            ),
        )

        assert calls == ["interrupt", "probe"]

    async def test_the_reclaim_happens_before_the_probe(self) -> None:
        """探测之前要先把资源收干净。

        收不干净就探，探出来的是"会话还在" —— 而那个会话背后可能还有一
        个没停掉的进程在改工作空间。
        """
        calls: list[str] = []

        class _RecordingProbe(_FakeProbe):
            async def probe_session(self, session_id: str) -> SessionProbeResult:
                calls.append("probe")
                return await super().probe_session(session_id)

        class _RecordingReclaimer(_FakeReclaimer):
            async def reclaim(self, target: CancellationTarget) -> CancellationReport:
                calls.append("reclaim")
                return await super().reclaim(target)

        await recover_from_timeout(
            _task(),
            ports=_ports(
                probe=_RecordingProbe(SessionProbeResult(resumable=True)),
                reclaimer=_RecordingReclaimer(),
            ),
        )

        assert calls == ["reclaim", "probe"]

    async def test_the_reclaim_receives_the_container(self) -> None:
        """容器 id 要**传下去** —— 回收要能停掉那个容器。

        漏传的话，容器会留在那里跑，而方案还报"收干净了"。
        """
        reclaimer = _FakeReclaimer()
        await recover_from_timeout(
            _task(),
            ports=_ports(reclaimer=reclaimer),
            container_id="container-9",
        )

        assert reclaimer.calls[0].container_id == "container-9"
        assert reclaimer.calls[0].session_id == "session-1"


class TestWhenSomethingGoesWrong:
    """每一条出错的路都判失败 —— 而且**如实说出**是哪一步出的问题。"""

    async def test_an_unconfirmed_interrupt_fails_closed(self) -> None:
        """中断没确认 —— 我们不知道跑飞的东西停了没有，不往下走。"""
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(interrupter=_FakeInterrupter(state=CancellationState.REFUSED)),
        )

        assert plan.outcome is RecoveryOutcome.FAILED
        assert plan.interrupted is False

    async def test_an_unclean_reclaim_fails_closed(self) -> None:
        """还有东西在跑 —— 不能带着它继续。

        这时**不能**续跑：两个执行同时改一个工作空间，比晚点续跑坏得多。
        """
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(
                probe=_FakeProbe(SessionProbeResult(resumable=True)),
                reclaimer=_FakeReclaimer(container_stopped=False),
            ),
        )

        assert plan.outcome is RecoveryOutcome.FAILED
        assert plan.reclaimed is False
        assert "容器" in plan.reclaim_detail

    async def test_the_reclaim_runs_even_when_the_interrupt_was_not_confirmed(
        self,
    ) -> None:
        """不知道它停了，正是最该去收一收的时候。"""
        reclaimer = _FakeReclaimer()
        await recover_from_timeout(
            _task(),
            ports=_ports(
                interrupter=_FakeInterrupter(state=CancellationState.REFUSED), reclaimer=reclaimer
            ),
        )

        assert len(reclaimer.calls) == 1

    async def test_a_probe_that_raises_is_not_taken_as_a_verdict(self) -> None:
        """探测出错 ≠ 确认不能续跑。

        理由里必须写着"探不出来"，而不是"确认不能" —— 事后对账时要能分
        清这两种情况。
        """
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(probe=_FakeProbe(None)),
            error_category=ErrorCategory.NON_RETRYABLE,
        )

        assert plan.outcome is RecoveryOutcome.FAILED
        assert "探不出来" in plan.reason

    async def test_a_reclaim_that_raises_is_reported_as_not_reclaimed(self) -> None:
        """回收报错是一个**结论**（还有东西在跑），不是一个崩溃。"""
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(reclaimer=_FakeReclaimer(raises=True)),
        )

        assert plan.outcome is RecoveryOutcome.FAILED
        assert plan.reclaimed is False
        assert "回收报错" in plan.reclaim_detail

    async def test_needs_human_is_not_decided_by_the_system(self) -> None:
        """需人工的分类不自己拍板 —— 续跑与重试都是拍板。"""
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(probe=_FakeProbe(SessionProbeResult(resumable=True))),
            error_category=ErrorCategory.NEEDS_HUMAN,
        )

        assert plan.outcome is RecoveryOutcome.FAILED
        assert plan.action is RecoveryAction.FAIL
        assert plan.error_category is ErrorCategory.NEEDS_HUMAN


class TestThePlanIsAConclusion:
    """方案是一份**结论** —— 它带的东西都要经得起追问。"""

    async def test_it_records_what_happened_to_the_resources(self) -> None:
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(),
        )

        assert isinstance(plan, RecoveryPlan)
        assert plan.interrupted is True
        assert plan.reclaimed is True
        assert plan.reclaim_detail

    @pytest.mark.parametrize(
        "category",
        [ErrorCategory.RETRYABLE, ErrorCategory.NON_RETRYABLE, ErrorCategory.NEEDS_HUMAN],
    )
    async def test_it_records_the_error_category(self, category: ErrorCategory) -> None:
        """分类要留在方案里 —— 编排层靠它决定要不要转成阻塞态。"""
        plan = await recover_from_timeout(
            _task(),
            ports=_ports(probe=_FakeProbe(SessionProbeResult(resumable=False))),
            error_category=category,
        )

        assert plan.error_category is category
