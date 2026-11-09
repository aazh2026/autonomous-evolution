"""TASK-P2-013 —— 多 Agent 并行执行与资源隔离。

设计依据：docs/tasks.md TASK-P2-013（交付物 ``orchestration/parallel_runner.py``；
验收"① 并发运行数严格受 ``max_parallel_agents`` 信号量限制；② 并发任务的 Git
Worktree 与临时目录完全隔离，互不产生文件冲突；③ 多任务并发时的 NDJSON 事件流
与成本归集互不串扰"）、R-013、R-017、R-045。

**这个文件钉住的是"不串扰"这件事。** 单跑一个任务看不出串扰 —— 串扰只在
两个任务同时跑的时候才发生，而那种错更糟：**它平时不出现，出现的时候已经
把两个任务的结果搅在一起了。**

所以拆成四组：

1. **并得起**（``TestTheConcurrencyIsBounded``）—— 同时跑着几个，严格不超过
   ``max_parallel_agents``。
2. **不踩脚**（``TestTheWorkspacesAreIsolated``）—— 工作区、临时目录各是各的。
3. **不串流**（``TestTheEventStreamsDoNotCrossTalk``）—— 事件各写各的文件。
4. **不串账**（``TestTheCostsDoNotCrossTalk``）—— 成本各记各的名下。

另外两组盯住两件容易漏的事：``TestAFailureDoesNotStopTheOthers``（一个任务
炸了不能把整批掀了 —— 那就等于白并行）与 ``TestTheReportKeepsTheInputOrder``
（结论按输入的顺序排，不按谁跑得快排）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from core.metrics.cost import CostAmount
from core.policy.budget import Budget, BudgetManager
from core.task.execution_loop import WorkspaceInfo
from core.task.outcome import ExecutionStatus
from orchestration.cost_accounting import ExecutionCostAccountant
from orchestration.parallel_runner import (
    EventCategory,
    NdjsonEventStore,
    ParallelEvent,
    ParallelRunner,
    ParallelTaskError,
    ParallelTaskResult,
    TaskEventSink,
    TaskWorkspace,
)

#: 一个任务的工作体：拿到地盘与出口，跑点自己的事。
Body = Callable[[TaskWorkspace, TaskEventSink], Awaitable[None]]


@dataclass
class Gauge:
    """同时跑着几个 —— **这个数是并发上限的唯一证据**。"""

    live: int = 0
    peak: int = 0
    log: list[str] = field(default_factory=list)

    def enter(self, task_id: str) -> None:
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.log.append(f"进:{task_id}")

    def leave(self, task_id: str) -> None:
        self.live -= 1
        self.log.append(f"出:{task_id}")


@dataclass
class FakeTask:
    """一个假任务 —— **它把看到的东西都记下来**。

    断言要用的东西就几样：拿到的工作区、发出的事件、进出工作区的顺序。
    """

    task_id: str
    gauge: Gauge | None = None
    cost: CostAmount | None = None
    error: Exception | None = None
    delay: float = 0.0
    body: Body | None = None
    emit: bool = True
    workspace: TaskWorkspace | None = None
    seen_categories: list[EventCategory] = field(default_factory=list)

    async def run(
        self,
        workspace: TaskWorkspace,
        events: TaskEventSink,
    ) -> ParallelTaskResult:
        self.workspace = workspace
        if self.gauge is not None:
            self.gauge.enter(self.task_id)
        try:
            if self.emit:
                await events.emit(EventCategory.CODE, {"file": f"src/{self.task_id}.py"})
                self.seen_categories.append(EventCategory.CODE)
            if self.body is not None:
                await self.body(workspace, events)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return ParallelTaskResult(
                status=ExecutionStatus.COMPLETED,
                cost=self.cost or CostAmount(Decimal("0"), "USD"),
            )
        finally:
            if self.gauge is not None:
                self.gauge.leave(self.task_id)


class FakeWorkspacePort:
    """假的工作空间分配面 —— **目录是真的，git 是假的**。

    **它会记账。** 分配了几次、释放了几次、同时有几处在分配中，都记下来。
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self.acquired: list[str] = []
        self.holders: list[str] = []
        self.released: list[str] = []
        self.live = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.fail_release = False

    def acquire(self, task_id: str, *, holder: str) -> WorkspaceInfo:
        self.acquired.append(task_id)
        self.holders.append(holder)
        self.live += 1
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            path = self._root / task_id
            path.mkdir(parents=True, exist_ok=True)
            return WorkspaceInfo(
                workspace_id=f"ws-{task_id}",
                path=str(path),
                branch=f"agent/{task_id}",
            )
        finally:
            self.in_flight -= 1

    def release(self, task_id: str) -> None:
        if self.fail_release:
            raise RuntimeError("收摊失败")
        self.released.append(task_id)
        self.live -= 1


class RecordingObserver:
    """记下每一次观测 —— **记的是谁花的**。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, CostAmount]] = []

    async def observe(self, cost: CostAmount, *, task_id: str, session_id: str) -> bool:
        self.records.append((task_id, session_id, cost))
        return True


def _budget(limit: int, **kwargs: object) -> Budget:
    """一份预算 —— **并发上限从这儿来**。"""
    return Budget(max_parallel_agents=limit, **kwargs)  # type: ignore[arg-type]


def _runner(
    tmp_path: Path,
    limit: int,
    *,
    workspaces: FakeWorkspacePort | None = None,
    observer: RecordingObserver | None = None,
    budget: Budget | None = None,
) -> tuple[ParallelRunner, NdjsonEventStore, FakeWorkspacePort, RecordingObserver]:
    """搭一套引擎。"""
    store = NdjsonEventStore(tmp_path / "events")
    spaces = workspaces or FakeWorkspacePort(tmp_path / "workspaces")
    seen = observer or RecordingObserver()
    # **一份预算，两处用。** 上限只有一个出处。
    the_budget = budget or _budget(limit)
    accountant = ExecutionCostAccountant(BudgetManager(the_budget), seen)
    runner = ParallelRunner(
        budget=the_budget,
        workspaces=spaces,
        events=store,
        cost_accountant=accountant,
        scratch_root=tmp_path / "scratch",
    )
    return runner, store, spaces, seen


class TestTheConcurrencyIsBounded:
    """并发运行数严格受 ``max_parallel_agents`` 限制。"""

    async def test_the_limit_comes_from_the_budget(self, tmp_path: Path) -> None:
        """**上限只有一个出处。** 别处再设一个，就会有两个答案。"""
        runner, _, _, _ = _runner(tmp_path, 3)

        assert runner.limit == 3

    async def test_the_limit_is_never_exceeded(self, tmp_path: Path) -> None:
        """八个任务、上限三个 —— 同时跑着的最多三个。"""
        gauge = Gauge()
        runner, _, _, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}", gauge=gauge, delay=0.02) for index in range(8)]

        report = await runner.run(tasks)

        assert gauge.peak <= 3
        assert report.peak_concurrency <= 3

    async def test_the_limit_is_actually_used(self, tmp_path: Path) -> None:
        """**上限低了跑不满，才是另一种问题。** 三个要真能同时跑。"""
        gauge = Gauge()
        runner, _, _, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}", gauge=gauge, delay=0.02) for index in range(8)]

        report = await runner.run(tasks)

        assert gauge.peak == 3
        assert report.peak_concurrency == 3

    async def test_a_limit_of_one_runs_them_one_by_one(self, tmp_path: Path) -> None:
        """上限是一 —— 那就是**排着队**跑，不是"碰巧没撞上"。"""
        gauge = Gauge()
        runner, _, _, _ = _runner(tmp_path, 1)
        tasks = [FakeTask(f"T-{index}", gauge=gauge) for index in range(4)]

        await runner.run(tasks)

        assert gauge.peak == 1
        assert gauge.log == [
            "进:T-0",
            "出:T-0",
            "进:T-1",
            "出:T-1",
            "进:T-2",
            "出:T-2",
            "进:T-3",
            "出:T-3",
        ]

    async def test_every_task_still_runs(self, tmp_path: Path) -> None:
        """**限流不是筛掉。** 八个任务一个都不能少。"""
        gauge = Gauge()
        runner, _, _, _ = _runner(tmp_path, 2)
        tasks = [FakeTask(f"T-{index}", gauge=gauge) for index in range(8)]

        report = await runner.run(tasks)

        assert len(report.outcomes) == 8
        assert [outcome.task_id for outcome in report.outcomes] == [
            f"T-{index}" for index in range(8)
        ]

    async def test_an_empty_run_is_fine(self, tmp_path: Path) -> None:
        """没活就不该有活。"""
        runner, _, _, _ = _runner(tmp_path, 4)

        report = await runner.run([])

        assert report.outcomes == ()
        assert report.peak_concurrency == 0


class TestTheWorkspacesAreIsolated:
    """并发任务的 Git Worktree 与临时目录完全隔离。"""

    async def test_each_task_gets_its_own_workspace(self, tmp_path: Path) -> None:
        runner, _, _, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}") for index in range(3)]

        await runner.run(tasks)

        paths = {task.workspace.path for task in tasks if task.workspace is not None}
        branches = {task.workspace.branch for task in tasks if task.workspace is not None}
        assert len(paths) == 3
        assert len(branches) == 3

    async def test_each_task_gets_its_own_scratch_directory(self, tmp_path: Path) -> None:
        """**临时目录也是各是各的** —— 共用一个就是等着被踩的那一块。"""
        runner, _, _, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}") for index in range(3)]

        await runner.run(tasks)

        scratches = {task.workspace.scratch for task in tasks if task.workspace is not None}
        assert len(scratches) == 3

    async def test_two_tasks_may_write_the_same_file_name(self, tmp_path: Path) -> None:
        """**同名文件不打架** —— 各自写在自己的地盘上。"""

        async def write(workspace: TaskWorkspace, _events: TaskEventSink) -> None:
            target = workspace.path / "same.txt"
            target.write_text(workspace.task_id, encoding="utf-8")
            scratch_target = workspace.scratch / "same.txt"
            scratch_target.write_text(workspace.task_id, encoding="utf-8")

        runner, _, _, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}", body=write) for index in range(3)]

        await runner.run(tasks)

        contents = {
            task.task_id: (task.workspace.path / "same.txt").read_text(encoding="utf-8")
            for task in tasks
            if task.workspace is not None
        }
        assert contents == {"T-0": "T-0", "T-1": "T-1", "T-2": "T-2"}

    async def test_the_scratch_directory_is_cleaned_up(self, tmp_path: Path) -> None:
        """**临时目录用完就没了** —— 留着只会攒垃圾。"""
        runner, _, _, _ = _runner(tmp_path, 2)
        tasks = [FakeTask(f"T-{index}") for index in range(2)]

        await runner.run(tasks)

        scratch_root = tmp_path / "scratch"
        assert not scratch_root.exists() or not list(scratch_root.iterdir())

    async def test_the_workspace_is_released_after_the_run(self, tmp_path: Path) -> None:
        """**用完要还。** 不还，下一轮就撞上租约了。"""
        runner, _, spaces, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}") for index in range(3)]

        await runner.run(tasks)

        assert sorted(spaces.released) == ["T-0", "T-1", "T-2"]
        assert spaces.live == 0

    async def test_the_workspace_is_released_even_when_the_task_fails(self, tmp_path: Path) -> None:
        """**炸了也要还** —— 否则一次失败就卡死后面的任务。"""
        runner, _, spaces, _ = _runner(tmp_path, 2)
        tasks = [FakeTask("T-0", error=RuntimeError("炸了"))]

        await runner.run(tasks)

        assert spaces.released == ["T-0"]

    async def test_the_workspaces_are_handed_out_one_at_a_time(self, tmp_path: Path) -> None:
        """**分配工作空间是串行的** —— git 自己就串行化 worktree 的增删，
        而"查租约 → 建租约 → 建 worktree"这串动作被打断就可能留下一个
        "目录在、租约不在"的工作空间。"""
        runner, _, spaces, _ = _runner(tmp_path, 4)
        tasks = [FakeTask(f"T-{index}") for index in range(4)]

        await runner.run(tasks)

        assert spaces.peak_in_flight == 1

    async def test_a_failed_release_does_not_fail_the_task(self, tmp_path: Path) -> None:
        """**收摊失败是运维的事**，跟"活干成没干成"是两回事。"""
        spaces = FakeWorkspacePort(tmp_path / "workspaces")
        spaces.fail_release = True
        runner, _, _, _ = _runner(tmp_path, 2, workspaces=spaces)
        tasks = [FakeTask("T-0")]

        report = await runner.run(tasks)

        assert report.outcomes[0].status is ExecutionStatus.COMPLETED


class TestTheEventStreamsDoNotCrossTalk:
    """多任务并发时的 NDJSON 事件流互不串扰。"""

    async def test_each_task_has_its_own_file(self, tmp_path: Path) -> None:
        runner, _store, _, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}") for index in range(3)]

        await runner.run(tasks)

        files = sorted(path.name for path in (tmp_path / "events").glob("*.ndjson"))
        assert files == ["T-0.ndjson", "T-1.ndjson", "T-2.ndjson"]

    async def test_each_file_holds_only_its_own_events(self, tmp_path: Path) -> None:
        runner, store, _, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}") for index in range(3)]

        await runner.run(tasks)

        for index in range(3):
            events = store.read(f"T-{index}")
            assert events
            assert {event.task_id for event in events} == {f"T-{index}"}

    async def test_the_audit_finds_no_cross_talk(self, tmp_path: Path) -> None:
        """**串流在结构上不会发生**，但"发生了能查出来"才让 bug 有地方冒头。"""
        runner, store, _, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}") for index in range(3)]

        await runner.run(tasks)

        assert store.audit() == ()

    async def test_the_audit_catches_a_planted_cross_talk(self, tmp_path: Path) -> None:
        """**查得出来才算数** —— 手工往别人的文件里塞一条试试。"""
        runner, store, _, _ = _runner(tmp_path, 2)
        tasks = [FakeTask("T-0")]
        await runner.run(tasks)

        intruder = ParallelEvent(
            task_id="T-0",
            session_id="T-0:planted",
            sequence=0,
            category=EventCategory.CODE,
            payload={"planted": True},
        )
        with store.path_of("T-1").open("a", encoding="utf-8") as handle:
            handle.write(intruder.to_json() + "\n")

        assert store.audit() != ()

    async def test_the_sequence_is_per_task(self, tmp_path: Path) -> None:
        """**序号是任务内的**，不是全局的 —— 两个任务的第一条都是序号 0。"""
        runner, store, _, _ = _runner(tmp_path, 3)
        tasks = [FakeTask(f"T-{index}") for index in range(3)]

        await runner.run(tasks)

        for index in range(3):
            sequences = [event.sequence for event in store.read(f"T-{index}")]
            assert sequences == list(range(len(sequences)))

    async def test_the_sink_cannot_write_into_another_task(self, tmp_path: Path) -> None:
        """**出口是按任务发的** —— 盖着的 task_id 不是任务能定的。"""
        runner, store, _, _ = _runner(tmp_path, 2)
        captured: list[TaskEventSink] = []

        async def grab(_workspace: TaskWorkspace, events: TaskEventSink) -> None:
            captured.append(events)

        tasks = [FakeTask("T-0", body=grab)]
        await runner.run(tasks)

        assert captured[0].task_id == "T-0"
        assert store.path_of(captured[0].task_id).name == "T-0.ndjson"

    async def test_the_categories_are_the_five_ones(self, tmp_path: Path) -> None:
        """**五类事件** —— 代码 / 测试 / 缺陷 / 依赖 / 运行时。"""
        runner, store, _, _ = _runner(tmp_path, 2)

        async def emit_all(_workspace: TaskWorkspace, events: TaskEventSink) -> None:
            for category in (
                EventCategory.CODE,
                EventCategory.TEST,
                EventCategory.DEFECT,
                EventCategory.DEPENDENCY,
                EventCategory.RUNTIME,
            ):
                await events.emit(category, {"what": category.value})

        tasks = [FakeTask("T-0", body=emit_all, emit=False)]
        await runner.run(tasks)

        categories = [event.category for event in store.read("T-0")]
        assert categories == [
            EventCategory.CODE,
            EventCategory.TEST,
            EventCategory.DEFECT,
            EventCategory.DEPENDENCY,
            EventCategory.RUNTIME,
        ]

    async def test_the_events_survive_a_round_trip(self, tmp_path: Path) -> None:
        """**读回来还是那些事。** 中文也要在。"""
        runner, store, _, _ = _runner(tmp_path, 2)

        async def emit(_workspace: TaskWorkspace, events: TaskEventSink) -> None:
            await events.emit(EventCategory.DEFECT, {"描述": "断言失败"})

        tasks = [FakeTask("T-0", body=emit, emit=False)]
        await runner.run(tasks)

        event = store.read("T-0")[0]
        assert event.payload["描述"] == "断言失败"


class TestTheCostsDoNotCrossTalk:
    """多任务并发时的成本归集互不串扰。"""

    async def test_each_cost_is_recorded_under_its_own_task(self, tmp_path: Path) -> None:
        """**记的是"谁花的"。** 落到同一个会话上，就说不清哪笔是谁的。"""
        runner, _, _, seen = _runner(tmp_path, 3)
        tasks = [
            FakeTask("T-0", cost=CostAmount(Decimal("1.00"), "USD")),
            FakeTask("T-1", cost=CostAmount(Decimal("2.00"), "USD")),
        ]

        await runner.run(tasks)

        recorded = {task_id: cost for task_id, _, cost in seen.records}
        assert recorded == {
            "T-0": CostAmount(Decimal("1.00"), "USD"),
            "T-1": CostAmount(Decimal("2.00"), "USD"),
        }

    async def test_each_cost_has_its_own_session(self, tmp_path: Path) -> None:
        """**会话是分开的** —— 串在一起就分不清哪笔是哪次执行的。"""
        runner, _, _, seen = _runner(tmp_path, 3)
        tasks = [FakeTask("T-0"), FakeTask("T-1")]

        await runner.run(tasks)

        sessions = {session_id for _, session_id, _ in seen.records}
        assert len(sessions) == 2

    async def test_the_totals_are_per_currency(self, tmp_path: Path) -> None:
        """**两种币种的数字加在一起是个没有意义的数** —— 而且加出来仍然像
        个像样的金额，错得查不出来。"""
        runner, _, _, _ = _runner(tmp_path, 3)
        tasks = [
            FakeTask("T-0", cost=CostAmount(Decimal("1.00"), "USD")),
            FakeTask("T-1", cost=CostAmount(Decimal("2.00"), "USD")),
            FakeTask("T-2", cost=CostAmount(Decimal("30.00"), "CNY")),
        ]

        report = await runner.run(tasks)

        assert report.total_of("USD") == Decimal("3.00")
        assert report.total_of("CNY") == Decimal("30.00")

    async def test_each_outcome_carries_its_own_cost(self, tmp_path: Path) -> None:
        """**成本挂在它自己名下** —— 不然就成了一个总数，谁也查不了。"""
        runner, _, _, _ = _runner(tmp_path, 3)
        tasks = [
            FakeTask("T-0", cost=CostAmount(Decimal("1.00"), "USD")),
            FakeTask("T-1", cost=CostAmount(Decimal("2.00"), "USD")),
        ]

        report = await runner.run(tasks)

        costs = {outcome.task_id: outcome.cost.amount for outcome in report.outcomes}
        assert costs == {"T-0": Decimal("1.00"), "T-1": Decimal("2.00")}

    async def test_a_currency_mismatch_is_not_silently_swallowed(self, tmp_path: Path) -> None:
        """**把 CNY 的钱记进 USD 的账户，比记错数字更坏** —— 数字错了能对账，
        币种错了对出来的是"另一个项目的钱"。"""
        runner, _, _, _ = _runner(tmp_path, 2)
        tasks = [FakeTask("T-0", cost=CostAmount(Decimal("10.00"), "CNY"))]

        report = await runner.run(tasks)

        assert report.outcomes[0].status is ExecutionStatus.FAILED
        assert "CurrencyMismatch" in report.outcomes[0].detail

    async def test_spending_over_the_budget_is_not_a_task_failure(self, tmp_path: Path) -> None:
        """**钱已经花了** —— 活干成了就是干成了。账本记不记得下是账本的事。"""
        budget = _budget(2, max_total_cost=Decimal("1.00"))
        runner, _, _, seen = _runner(tmp_path, 2, budget=budget)
        tasks = [FakeTask("T-0", cost=CostAmount(Decimal("2.00"), "USD"))]

        report = await runner.run(tasks)

        assert report.outcomes[0].status is ExecutionStatus.COMPLETED
        assert seen.records


class TestAFailureDoesNotStopTheOthers:
    """一个任务炸了不能把整批掀了。"""

    async def test_the_other_tasks_still_run(self, tmp_path: Path) -> None:
        runner, _, _, _ = _runner(tmp_path, 3)
        tasks = [
            FakeTask("T-0"),
            FakeTask("T-1", error=RuntimeError("炸了")),
            FakeTask("T-2"),
        ]

        report = await runner.run(tasks)

        statuses = {outcome.task_id: outcome.status for outcome in report.outcomes}
        assert statuses == {
            "T-0": ExecutionStatus.COMPLETED,
            "T-1": ExecutionStatus.FAILED,
            "T-2": ExecutionStatus.COMPLETED,
        }

    async def test_the_failure_says_what_went_wrong(self, tmp_path: Path) -> None:
        """**"失败"两个字没法行动。** 得说清是什么错。"""
        runner, _, _, _ = _runner(tmp_path, 2)
        tasks = [FakeTask("T-0", error=RuntimeError("炸了"))]

        report = await runner.run(tasks)

        assert "炸了" in report.outcomes[0].detail
        assert "RuntimeError" in report.outcomes[0].detail

    async def test_the_money_a_failed_task_spent_is_still_recorded(self, tmp_path: Path) -> None:
        """**失败不等于没花钱** —— 跑了半个钟头再炸，那半个钟头的钱已经出去了。"""
        runner, _, _, _seen = _runner(tmp_path, 2)
        tasks = [
            FakeTask(
                "T-0",
                error=ParallelTaskError("炸了", cost=CostAmount(Decimal("5.00"), "USD")),
            )
        ]

        report = await runner.run(tasks)

        assert report.outcomes[0].cost.amount == Decimal("5.00")

    async def test_the_events_of_a_failed_task_are_kept(self, tmp_path: Path) -> None:
        """**炸之前发生的事也是事** —— 查问题的人要看得见。"""
        runner, store, _, _ = _runner(tmp_path, 2)
        tasks = [FakeTask("T-0", error=RuntimeError("炸了"))]

        report = await runner.run(tasks)

        assert report.outcomes[0].events == 1
        assert len(store.read("T-0")) == 1


class TestTheReportKeepsTheInputOrder:
    """结论按输入的顺序排，不按谁跑得快排。"""

    async def test_the_slow_task_comes_back_first(self, tmp_path: Path) -> None:
        """**完成顺序是随机的**，而"这次跑的第几个失败了"不该取决于谁跑得快。"""
        runner, _, _, _ = _runner(tmp_path, 3)
        tasks = [
            FakeTask("T-0", delay=0.05),
            FakeTask("T-1", delay=0.0),
            FakeTask("T-2", delay=0.0),
        ]

        report = await runner.run(tasks)

        assert [outcome.task_id for outcome in report.outcomes] == ["T-0", "T-1", "T-2"]

    async def test_the_report_names_the_limit_it_ran_with(self, tmp_path: Path) -> None:
        runner, _, _, _ = _runner(tmp_path, 3)

        report = await runner.run([FakeTask("T-0")])

        assert report.limit == 3
        assert report.outcome_of("T-0") is not None
        assert report.outcome_of("T-9") is None
