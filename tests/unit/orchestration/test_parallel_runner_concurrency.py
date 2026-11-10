"""TASK-P3-015 —— 并发限流：上限跨批次、跨引擎算在一处。

**这一份钉住的是"上限到底约束了谁"。** 单批跑的时候，并发数不超上限是显然
的 —— 一个信号量就够了。**要命的是两批同时跑**：早先这里每次 ``run()`` 现造
一个信号量，于是两批各有一个号牌池，同时跑着的任务数就成了两倍上限。

**上限只在单批里成立，等于没有上限。** 演进循环恰恰是**一轮接一轮**跑的 ——
上一轮还没收摊，下一轮又开进来了。

**这一层用的是 :class:`core.policy.resource.ConcurrencyLimiter`** —— 引擎持有
它，而不是每批新建；也可以把 :class:`core.policy.resource.ResourceGate` 手里
那一个注入进来，于是**跨引擎、跨批次、跨周期都算在同一个上限里**。

**证据是独立的仪表。** 断言用的是任务自己数的"同时跑着几个"，不是引擎报出来
的数 —— **用被测对象自己的读数证明被测对象，是循环论证。**
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from core.metrics.cost import CostAmount
from core.policy.budget import Budget
from core.policy.resource import ConcurrencyLimiter, ResourceGate
from core.task.execution_loop import WorkspaceInfo
from core.task.outcome import ExecutionStatus
from orchestration.parallel_runner import (
    EventCategory,
    NdjsonEventStore,
    ParallelRunner,
    ParallelTaskResult,
    TaskEventSink,
    TaskWorkspace,
)


@dataclass
class Gauge:
    """同时跑着几个 —— **这个数是并发上限的唯一证据**。"""

    live: int = 0
    peak: int = 0

    def enter(self) -> None:
        self.live += 1
        self.peak = max(self.peak, self.live)

    def leave(self) -> None:
        self.live -= 1


@dataclass
class FakeTask:
    """一个假任务 —— 进出各报一次数。"""

    task_id: str
    gauge: Gauge
    delay: float = 0.02
    branch: str = ""

    async def run(self, workspace: TaskWorkspace, events: TaskEventSink) -> ParallelTaskResult:
        """假装干活 —— **进出各报一次数，顺手发条事件**。"""
        self.branch = workspace.branch
        await events.emit(EventCategory.CODE, {"task": self.task_id})
        self.gauge.enter()
        try:
            await asyncio.sleep(self.delay)
            return ParallelTaskResult(
                status=ExecutionStatus.COMPLETED,
                cost=CostAmount(Decimal("0"), "USD"),
            )
        finally:
            self.gauge.leave()


class FakeWorkspacePort:
    """假的工作空间分配面 —— 不碰 git。"""

    def acquire(self, task_id: str, *, holder: str) -> WorkspaceInfo:
        """给一块假地盘。"""
        return WorkspaceInfo(
            workspace_id=f"ws-{holder}-{task_id}",
            path=f"/tmp/{task_id}",
            branch=f"branch/{task_id}",
        )

    def release(self, task_id: str) -> None:
        """收摊。"""


def _runner(
    tmp_path: Path,
    limit: int,
    *,
    limiter: ConcurrencyLimiter | None = None,
) -> ParallelRunner:
    """搭一套引擎。**上限只有一个出处。**"""
    return ParallelRunner(
        budget=Budget(max_parallel_agents=limit),
        workspaces=FakeWorkspacePort(),
        events=NdjsonEventStore(tmp_path / "events"),
        limiter=limiter,
    )


def _tasks(prefix: str, count: int, gauge: Gauge) -> list[FakeTask]:
    """一批任务。"""
    return [FakeTask(task_id=f"{prefix}-{index}", gauge=gauge) for index in range(count)]


class TestTheLimitIsSharedAcrossBatches:
    """两批同时跑 —— **上限还是那个上限**。"""

    async def test_two_concurrent_batches_never_exceed_the_limit(self, tmp_path: Path) -> None:
        """**两批各要四个号，号牌池只有三个 —— 同时跑着的不超过三个。**

        这是这一层存在的全部理由：单批不超上限是显然的，**两批同时跑也超不了，
        才说明上限是个真的上限。**
        """
        gauge = Gauge()
        runner = _runner(tmp_path, 3)

        await asyncio.gather(
            runner.run(_tasks("A", 4, gauge)),
            runner.run(_tasks("B", 4, gauge)),
        )

        assert gauge.peak <= 3

    async def test_the_peak_survives_the_second_batch(self, tmp_path: Path) -> None:
        """**峰值记在闸门上 —— 第二批冲不淡第一批的记录。**"""
        gauge = Gauge()
        runner = _runner(tmp_path, 3)

        await runner.run(_tasks("A", 4, gauge))
        first_peak = runner.limiter.peak
        await runner.run(_tasks("B", 2, gauge))

        assert runner.limiter.peak == first_peak

    async def test_the_report_agrees_with_the_gauge(self, tmp_path: Path) -> None:
        """**引擎报的数与任务自己数的数一致。**

        两处各数各的，"现在到底是几"就有两个答案 —— 而**限流器是真正在管这件
        事的那一个**，引擎不该再自己数一遍。
        """
        gauge = Gauge()
        runner = _runner(tmp_path, 3)

        report = await runner.run(_tasks("A", 6, gauge))

        assert report.peak_concurrency == gauge.peak
        assert report.peak_concurrency == runner.peak_concurrency


class TestTheInjectedLimiter:
    """注入闸门 —— **算在同一个上限里**。"""

    async def test_the_injected_limiter_is_the_one_that_is_used(self, tmp_path: Path) -> None:
        """**给了哪个闸门，就用哪个。**"""
        limiter = ConcurrencyLimiter(2)
        runner = _runner(tmp_path, 99, limiter=limiter)

        assert runner.limiter is limiter

    async def test_two_engines_share_one_gate(self, tmp_path: Path) -> None:
        """**两个引擎，一个闸门 —— 同时跑着的不超过闸门那个数。**

        这是"跨引擎"那一档：**两套编排各跑各的，但资源上限是同一个**。
        """
        gauge = Gauge()
        limiter = ConcurrencyLimiter(3)
        first = _runner(tmp_path, 99, limiter=limiter)
        second = _runner(tmp_path, 99, limiter=limiter)

        await asyncio.gather(
            first.run(_tasks("A", 4, gauge)),
            second.run(_tasks("B", 4, gauge)),
        )

        assert gauge.peak <= 3

    async def test_the_gate_limiter_can_be_injected(self, tmp_path: Path) -> None:
        """**闸门手里那一个也能拿来用 —— 资源控制是一件事，不是两件。**

        预算、成本、并发三者本来就该归一个闸门管；**分开放着就会各限各的**。
        """
        gate = ResourceGate(Budget(max_parallel_agents=2))
        gauge = Gauge()
        runner = _runner(tmp_path, 99, limiter=gate.limiter)

        await asyncio.gather(
            runner.run(_tasks("A", 4, gauge)),
            runner.run(_tasks("B", 4, gauge)),
        )

        assert gauge.peak <= 2
