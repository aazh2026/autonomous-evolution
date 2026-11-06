"""TASK-P1-033 —— 取消传播链。

覆盖任务书的 T1 ②（传播途中某层失败时，链路仍在有限时间内收敛）。

**这一组测试守的是三条规则，它们互相咬合：**

1. **顺序固定** —— 任务 → Adapter → 进程 → 容器，从内到外。顺序反了的话，
   容器先停，执行器就再也给不出确认，链报"取消失败"而容器早就没了 ——
   结论与事实相反。
2. **每层给出结论后才走下一层** —— 不许在上一层还悬着的时候动下一层。
3. **一层失败不挡住后面的层** —— "这一层没停下来"不等于"后面的层不用管"：
   容器还在跑就占着工作空间，而工作空间是别的任务也要用的。

第 2 条与第 3 条看着矛盾，其实是一件事的两面：**结论**要一层一层给，
**动作**却不能因为某层失败就漏掉。链报"已取消"的条件是**每一层都确认**，
所以漏掉的动作不会变成虚假的成功。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from core.task.cancellation import (
    CancellationReport,
    CancellationTarget,
    LayerName,
    LayerOutcome,
    LayerState,
    StopReport,
)
from harness.sandbox.cancellation import (
    AdapterLayer,
    CancellationChain,
    LauncherContainerStopper,
    NoContainerStopper,
    NoProcessStopper,
    ProcessLayer,
    SandboxLayer,
    TaskLayer,
    build_cancellation_chain,
)


@dataclass(frozen=True, slots=True)
class _FakeAck:
    """执行器回执的替身。"""

    state: str
    detail: str = ""


@dataclass
class _FakeInterrupter:
    """执行器的替身 —— 记下自己被问过几次。"""

    state: str = "confirmed"
    detail: str = "执行进程已终止"
    calls: list[str] = field(default_factory=list)

    async def interrupt(self, session_id: str) -> _FakeAck:
        self.calls.append(session_id)
        return _FakeAck(state=self.state, detail=self.detail)


@dataclass
class _FakeProcessStopper:
    stopped: bool = True
    detail: str = "进程已被终止"
    calls: list[str] = field(default_factory=list)

    async def ensure_process_stopped(self, session_id: str) -> StopReport:
        self.calls.append(session_id)
        return StopReport(stopped=self.stopped, detail=self.detail)


@dataclass
class _FakeContainerStopper:
    stopped: bool = True
    calls: list[str] = field(default_factory=list)

    async def ensure_container_stopped(self, container_id: str) -> StopReport:
        self.calls.append(container_id)
        return StopReport(stopped=self.stopped, detail="容器已停止")


@dataclass
class _RecordingLayer:
    """把"进了这一层"与"这一层出了结论"都记进同一个日志。

    日志用来断言**严格串行**：上一层出了结论，下一层才进去。
    """

    layer: LayerName
    log: list[str]
    outcome: LayerOutcome | None = None

    @property
    def name(self) -> LayerName:
        return self.layer

    async def stop(self, target: CancellationTarget) -> LayerOutcome:
        self.log.append(f"进入:{self.layer.value}")
        await asyncio.sleep(0)
        self.log.append(f"结论:{self.layer.value}")
        if self.outcome is not None:
            return self.outcome
        return LayerOutcome(self.layer, LayerState.CONFIRMED, "好")


@dataclass
class _BrokenLayer:
    """这一层会抛异常。"""

    layer: LayerName
    log: list[str]

    @property
    def name(self) -> LayerName:
        return self.layer

    async def stop(self, target: CancellationTarget) -> LayerOutcome:
        self.log.append(f"进入:{self.layer.value}")
        raise RuntimeError("这一层炸了")


@dataclass
class _HangingLayer:
    """这一层永远不给出结论。"""

    layer: LayerName
    log: list[str]

    @property
    def name(self) -> LayerName:
        return self.layer

    async def stop(self, target: CancellationTarget) -> LayerOutcome:
        self.log.append(f"进入:{self.layer.value}")
        await asyncio.sleep(3600)
        raise AssertionError("不该走到这里")


def _target(container_id: str | None = "container-1") -> CancellationTarget:
    return CancellationTarget(task_id="TASK-1", session_id="session-1", container_id=container_id)


class TestTheChainVisitsTheLayersInOrder:
    """顺序固定，从内到外。"""

    async def test_the_four_layers_run_in_the_canonical_order(self) -> None:
        log: list[str] = []
        chain = CancellationChain(
            (
                _RecordingLayer(LayerName.TASK, log),
                _RecordingLayer(LayerName.ADAPTER, log),
                _RecordingLayer(LayerName.PROCESS, log),
                _RecordingLayer(LayerName.SANDBOX, log),
            )
        )

        report = await chain.cancel(_target())

        assert [outcome.layer for outcome in report.outcomes] == [
            LayerName.TASK,
            LayerName.ADAPTER,
            LayerName.PROCESS,
            LayerName.SANDBOX,
        ]
        assert log == [
            "进入:task",
            "结论:task",
            "进入:adapter",
            "结论:adapter",
            "进入:process",
            "结论:process",
            "进入:sandbox",
            "结论:sandbox",
        ]

    async def test_a_layer_is_finished_before_the_next_one_starts(self) -> None:
        """上一层出了结论，下一层才进去。

        反过来做的话，链会在上一层还悬着的时候动下一层 —— 而"上一层悬着"
        正是最需要顺序保证的状态。
        """
        log: list[str] = []
        chain = CancellationChain(
            (
                _RecordingLayer(LayerName.TASK, log),
                _RecordingLayer(LayerName.ADAPTER, log),
                _RecordingLayer(LayerName.PROCESS, log),
                _RecordingLayer(LayerName.SANDBOX, log),
            )
        )

        await chain.cancel(_target())

        for index in range(0, len(log), 2):
            assert log[index].startswith("进入")
            assert log[index + 1].startswith("结论")
            assert log[index].removeprefix("进入:") == log[index + 1].removeprefix("结论:")

    async def test_the_standard_chain_has_all_four_layers(self) -> None:
        """标准链永远是四层 —— 缺一层，事后对账时那条记录要靠猜。"""
        interrupter = _FakeInterrupter()

        chain = build_cancellation_chain(interrupter=interrupter)

        assert [layer.name for layer in chain.layers] == [
            LayerName.TASK,
            LayerName.ADAPTER,
            LayerName.PROCESS,
            LayerName.SANDBOX,
        ]


class TestTheChainAlwaysConverges:
    """T1 ② —— 某一层失败时，链路仍在有限时间内收敛。"""

    async def test_a_broken_layer_does_not_stop_the_chain(self) -> None:
        """一层炸了，后面的层**仍然要试**。

        容器还在跑就占着资源 —— 跳过它等于把一次取消变成一次泄漏。
        """
        log: list[str] = []
        chain = CancellationChain(
            (
                _RecordingLayer(LayerName.TASK, log),
                _BrokenLayer(LayerName.ADAPTER, log),
                _RecordingLayer(LayerName.PROCESS, log),
                _RecordingLayer(LayerName.SANDBOX, log),
            )
        )

        report = await chain.cancel(_target())

        assert "进入:process" in log
        assert "进入:sandbox" in log
        assert report.outcomes[1].state is LayerState.FAILED

    async def test_a_hanging_layer_is_given_up_on_and_the_chain_moves_on(self) -> None:
        """一层不给出结论时，它自己被判失败，链继续往下走。

        没有期限的话，一个卡住的层就把整条链卡住 —— 而"取消"最需要的就是
        "它一定会结束"。
        """
        log: list[str] = []
        chain = CancellationChain(
            (
                _RecordingLayer(LayerName.TASK, log),
                _HangingLayer(LayerName.ADAPTER, log),
                _RecordingLayer(LayerName.PROCESS, log),
            ),
            layer_timeout_seconds=0.05,
        )

        report = await chain.cancel(_target())

        assert report.outcomes[1].state is LayerState.FAILED
        assert "没有给出结论" in report.outcomes[1].detail
        assert "进入:process" in log

    async def test_the_chain_never_raises(self) -> None:
        """取消失败是一个**结论**，不是一个崩溃。

        抛异常会把"哪一层没停下来"这条信息丢掉，而那正是调用方处置时要
        用的东西。
        """
        chain = CancellationChain((_BrokenLayer(LayerName.ADAPTER, []),))

        report = await chain.cancel(_target())

        assert isinstance(report, CancellationReport)
        assert report.outcomes[0].state is LayerState.FAILED

    async def test_an_empty_chain_is_not_a_cancellation(self) -> None:
        """没跑过链就不能说取消过（fail-closed）。"""
        chain = CancellationChain(())

        report = await chain.cancel(_target())

        assert report.cancelled is False


class TestTheAdapterLayer:
    """Adapter 层 —— 执行器说它停下来了没有。"""

    async def test_a_confirmed_ack_is_confirmed(self) -> None:
        layer = AdapterLayer(_FakeInterrupter(state="confirmed"))

        outcome = await layer.stop(_target())

        assert outcome.state is LayerState.CONFIRMED

    async def test_a_refused_ack_is_refused(self) -> None:
        layer = AdapterLayer(_FakeInterrupter(state="refused", detail="进程不可控"))

        outcome = await layer.stop(_target())

        assert outcome.state is LayerState.REFUSED
        assert "进程不可控" in outcome.detail

    async def test_an_unfinished_ack_is_not_a_confirmation(self) -> None:
        """只回了"请求已发出"不算停下来了。

        拿"问过了"冒充"停住了"，那这条链就白走了 —— 而链存在的全部理由
        就是这个区别（I-02）。
        """
        layer = AdapterLayer(_FakeInterrupter(state="requested"))

        outcome = await layer.stop(_target())

        assert outcome.state is LayerState.UNCONFIRMED

    async def test_an_unknown_state_is_not_a_confirmation(self) -> None:
        """不认识的状态按"没确认"处理（fail-closed）。

        执行器换一套状态取值，我们宁可说"没停干净"也不能说"停了"。
        """
        layer = AdapterLayer(_FakeInterrupter(state="whatever"))

        outcome = await layer.stop(_target())

        assert outcome.state is LayerState.UNCONFIRMED

    async def test_the_session_id_is_passed_through(self) -> None:
        interrupter = _FakeInterrupter()

        await AdapterLayer(interrupter).stop(_target())

        assert interrupter.calls == ["session-1"]


class TestTheProcessLayer:
    async def test_a_stopped_process_is_confirmed(self) -> None:
        layer = ProcessLayer(_FakeProcessStopper(stopped=True))

        outcome = await layer.stop(_target())

        assert outcome.state is LayerState.CONFIRMED

    async def test_a_process_that_is_still_running_is_a_failure(self) -> None:
        layer = ProcessLayer(_FakeProcessStopper(stopped=False, detail="进程没退出"))

        outcome = await layer.stop(_target())

        assert outcome.state is LayerState.FAILED
        assert "进程没退出" in outcome.detail

    async def test_an_executor_without_a_process_is_confirmed(self) -> None:
        """没有独立进程时，后置条件**天然成立**。

        报失败才是撒谎 —— "没有进程"不是"进程没停掉"。
        """
        layer = ProcessLayer(NoProcessStopper())

        outcome = await layer.stop(_target())

        assert outcome.state is LayerState.CONFIRMED


class TestTheSandboxLayer:
    async def test_a_stopped_container_is_confirmed(self) -> None:
        layer = SandboxLayer(_FakeContainerStopper(stopped=True))

        outcome = await layer.stop(_target())

        assert outcome.state is LayerState.CONFIRMED

    async def test_a_container_that_will_not_stop_is_a_failure(self) -> None:
        layer = SandboxLayer(_FakeContainerStopper(stopped=False))

        outcome = await layer.stop(_target())

        assert outcome.state is LayerState.FAILED

    async def test_no_container_means_the_postcondition_holds(self) -> None:
        """没有容器时容器层报确认 —— 没有容器在跑。

        报失败会把"这次执行没跑在沙箱里"说成"容器没停掉"。
        """
        stopper = _FakeContainerStopper()
        layer = SandboxLayer(stopper)

        outcome = await layer.stop(_target(container_id=None))

        assert outcome.state is LayerState.CONFIRMED
        assert stopper.calls == []

    async def test_the_container_id_is_passed_through(self) -> None:
        stopper = _FakeContainerStopper()

        await SandboxLayer(stopper).stop(_target(container_id="container-9"))

        assert stopper.calls == ["container-9"]


class TestTheReport:
    async def test_it_is_cancelled_only_when_every_layer_confirmed(self) -> None:
        chain = build_cancellation_chain(
            interrupter=_FakeInterrupter(),
            process_stopper=_FakeProcessStopper(),
            container_stopper=_FakeContainerStopper(),
        )

        report = await chain.cancel(_target())

        assert report.cancelled is True
        assert report.unfinished == ()

    async def test_one_unconfirmed_layer_is_enough_to_block_the_cancellation(self) -> None:
        """少一层确认就不是"已取消" —— 那一类资源还在跑。"""
        chain = build_cancellation_chain(
            interrupter=_FakeInterrupter(),
            process_stopper=_FakeProcessStopper(stopped=False),
            container_stopper=_FakeContainerStopper(),
        )

        report = await chain.cancel(_target())

        assert report.cancelled is False
        assert [outcome.layer for outcome in report.unfinished] == [LayerName.PROCESS]

    async def test_an_unconfirmed_layer_blocks_the_cancellation_too(self) -> None:
        """只回了"请求已发出"的那一层，同样不算取消。

        那一层对应的资源可能还在跑 —— 而"还有东西在跑"与"已经取消"不能同时
        成立。把"未确认"当成"确认"，这条链就白走了（I-02）。
        """
        chain = build_cancellation_chain(interrupter=_FakeInterrupter(state="requested"))

        report = await chain.cancel(_target())

        assert report.cancelled is False
        assert [outcome.layer for outcome in report.unfinished] == [LayerName.ADAPTER]

    async def test_the_report_names_the_target(self) -> None:
        chain = build_cancellation_chain(interrupter=_FakeInterrupter())

        report = await chain.cancel(_target())

        assert report.target.task_id == "TASK-1"
        assert report.target.session_id == "session-1"


class TestTheTaskLayer:
    async def test_the_request_is_accepted(self) -> None:
        outcome = await TaskLayer().stop(_target())

        assert outcome.state is LayerState.CONFIRMED
        assert "TASK-1" in outcome.detail


class TestTheLauncherContainerStopper:
    """用启动器停容器 —— 停完还要**问回来**它是不是真的不在跑了。"""

    async def test_it_confirms_when_the_container_is_gone(self) -> None:
        launcher = _FakeLauncher(running=False)
        stopper = LauncherContainerStopper(launcher=launcher)

        report = await stopper.ensure_container_stopped("container-1")

        assert report.stopped is True
        assert launcher.stopped == ["container-1"]

    async def test_it_reports_a_failure_when_the_container_is_still_running(self) -> None:
        launcher = _FakeLauncher(running=True)
        stopper = LauncherContainerStopper(launcher=launcher)

        report = await stopper.ensure_container_stopped("container-1")

        assert report.stopped is False

    async def test_it_does_not_claim_success_when_docker_cannot_be_reached(self) -> None:
        """问不话 ≠ 没在跑。

        把"我们没停掉它"说成"它已经停了"，那这条链就撒了谎 —— 而它撒的
        恰恰是整个系统最需要真话的那句话。
        """
        launcher = _FakeLauncher(running=None)
        stopper = LauncherContainerStopper(launcher=launcher)

        report = await stopper.ensure_container_stopped("container-1")

        assert report.stopped is False
        assert "没能确认" in report.detail


@dataclass
class _FakeLauncher:
    """启动器的替身 —— 只回答取消链问的那两个问题。"""

    running: bool | None
    stopped: list[str] = field(default_factory=list)

    def stop(self, container_id: str, *, grace_seconds: int = 30) -> None:
        self.stopped.append(container_id)

    def container_running(self, container_id: str) -> bool | None:
        return self.running


class TestTheMissingContainerStopper:
    async def test_it_confirms_because_there_is_nothing_to_stop(self) -> None:
        report = await NoContainerStopper().ensure_container_stopped("container-1")

        assert report.stopped is True


@pytest.mark.parametrize(
    ("interrupter_state", "expected"),
    [
        ("confirmed", LayerState.CONFIRMED),
        ("refused", LayerState.REFUSED),
        ("requested", LayerState.UNCONFIRMED),
        ("未知状态", LayerState.UNCONFIRMED),
    ],
)
async def test_the_ack_state_is_mapped_without_guessing(
    interrupter_state: str, expected: LayerState
) -> None:
    """回执状态的映射表是穷举的 —— 没有"默认当成功"这一档。"""
    chain = build_cancellation_chain(interrupter=_FakeInterrupter(state=interrupter_state))

    report = await chain.cancel(_target())

    assert report.outcomes[1].state is expected
