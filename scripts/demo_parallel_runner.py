"""TASK-P2-013 的演示 —— 多 Agent 并行执行与资源隔离。

跑法::

    uv run python -m scripts.demo_parallel_runner
    # 或
    .venv/bin/python scripts/demo_parallel_runner.py

**这个演示要证明的是 TASK-P2-013 的验收条件**："① 并发运行数严格受
``max_parallel_agents`` 信号量限制；② 并发任务的 Git Worktree 与临时目录
完全隔离，互不产生文件冲突；③ 多任务并发时的 NDJSON 事件流与成本归集互不
串扰"。

所以演示的重点有三条：

**第一条：并得起。** 上限是 2，就同时跑 2 个 —— 演示里真的起三个任务，
每个任务都在自己那一轮里记录"此刻有几个任务同时在跑"，跑完看峰值。**峰值
必须正好是 2，而且不能是 3。**

**第二条：并不串。** 三个任务写**同一个文件名** —— 名字一样才是考验，
名字不一样的话各写各的，谁也证明不了什么。而"互不串扰"要盯三样：工作区、
事件流、成本。

**第三条：炸一个不要紧。** 一个任务失败不能把整批掀了 —— 那就等于白并行。

**为什么工作区的观察要在任务跑的时候做。** 跑完一轮，引擎已经把工作空间
还回去了（目录没了、分支留着 —— 分支才是产出），所以演示里每个任务都把
自己看到的东西记下来，跑完再对账。

**不需要数据库，也不需要网络。** 全部都是真的 git 与真的文件。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from core.metrics.cost import CostAmount
from core.policy.budget import Budget, BudgetManager
from core.task.outcome import ExecutionStatus
from harness.workspace.git import SubprocessGitClient
from harness.workspace.manager import WorkspaceManager
from harness.workspace.models import SystemClock
from orchestration.cost_accounting import ExecutionCostAccountant
from orchestration.parallel_runner import (
    EventCategory,
    ManagerWorkspacePort,
    NdjsonEventStore,
    ParallelRunner,
    ParallelTaskResult,
    TaskEventSink,
    TaskWorkspace,
)

#: 任务数。**比上限多一个** —— 那样"上限"才有得说。
TASKS = 3

#: 并发上限。**这是唯一的出处** —— 别的层不再另设一个。
MAX_PARALLEL = 2

#: 租约时长。
LEASE_TTL = 3600


class DemoError(RuntimeError):
    """演示里某个前提不成立。"""


def _banner(step: str, title: str) -> None:
    """一节的分隔线。"""
    print(f"\n{'─' * 78}\n{step}  {title}\n{'─' * 78}")


def _git(cwd: Path, *args: str) -> str:
    """跑一条 git 命令。"""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DemoError(f"git {' '.join(args)} 失败：\n{result.stdout}\n{result.stderr}")
    return result.stdout


def _build_repo(root: Path) -> Path:
    """造一个真的 git 仓库。"""
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "ases@example.invalid")
    _git(repo, "config", "user.name", "ASES")
    (repo / "README.md").write_text("基础\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "初始提交")
    return repo


class DemoTask:
    """一个真的跑在 worktree 里的任务。

    **它写的是同一个文件名。** 三个任务同时写同一个名字，各写各的目录 ——
    要是目录共享了，后写的就把先写的盖掉了，而那一读就能读出别人的名字。
    """

    def __init__(self, task_id: str, *, fail: bool = False) -> None:
        self._task_id = task_id
        self._fail = fail
        self.path: Path | None = None
        self.branch = ""
        self.scratch: Path | None = None
        self.same_content = ""
        self.worktree_files: list[str] = []
        self.scratch_files: list[str] = []
        self.scratch_content = ""

    @property
    def task_id(self) -> str:
        return self._task_id

    async def run(
        self,
        workspace: TaskWorkspace,
        events: TaskEventSink,
    ) -> ParallelTaskResult:
        self.path = workspace.path
        self.branch = workspace.branch
        self.scratch = workspace.scratch
        (workspace.path / "same.txt").write_text(self._task_id, encoding="utf-8")
        (workspace.scratch / "same.txt").write_text(self._task_id, encoding="utf-8")
        await events.emit(EventCategory.CODE, {"file": "same.txt", "任务": self._task_id})
        await events.emit(EventCategory.TEST, {"用例": f"test_{self._task_id}"})
        # **等一等**，让几个任务真的叠在一起 —— 固定睡一小会儿是不够的，
        # git 建 worktree 要几十毫秒。
        await asyncio.sleep(0.2)
        self.same_content = (workspace.path / "same.txt").read_text(encoding="utf-8")
        self.worktree_files = sorted(path.name for path in workspace.path.glob("*.txt"))
        self.scratch_files = sorted(path.name for path in workspace.scratch.glob("*.txt"))
        self.scratch_content = (workspace.scratch / "same.txt").read_text(encoding="utf-8")
        if self._fail:
            raise RuntimeError(f"「{self._task_id}」炸了")
        return ParallelTaskResult(
            status=ExecutionStatus.COMPLETED,
            cost=CostAmount(Decimal("1.50"), "USD"),
        )


class PrintingObserver:
    """把每次观测都打出来 —— **记的是谁花的**。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, CostAmount]] = []

    async def observe(self, cost: CostAmount, *, task_id: str, session_id: str) -> bool:
        self.records.append((task_id, session_id, cost))
        print(f"    记账：{task_id}｜{session_id}｜{cost}")
        return True


async def _step_parallel(
    root: Path,
) -> tuple[ParallelRunner, NdjsonEventStore, list[DemoTask], int]:
    """真 git、真并发 —— **上限是 2**。"""
    _banner("第 1 步", "三个任务，上限两个 —— 并行跑起来")
    repo = _build_repo(root)
    manager = WorkspaceManager(
        repo_path=repo,
        workspace_root=root / "workspaces",
        git=SubprocessGitClient(),
        clock=SystemClock(),
        lease_ttl_seconds=LEASE_TTL,
    )
    store = NdjsonEventStore(root / "events")
    observer = PrintingObserver()
    # **一份预算，两处用。** 上限只有一个出处 —— 另建一份就又有了两个答案。
    budget = Budget(max_parallel_agents=MAX_PARALLEL)
    runner = ParallelRunner(
        budget=budget,
        workspaces=ManagerWorkspacePort(manager),
        events=store,
        cost_accountant=ExecutionCostAccountant(BudgetManager(budget), observer),
        scratch_root=root / "scratch",
    )
    tasks = [DemoTask(f"task-{index}") for index in range(TASKS)]

    report = await runner.run(tasks)

    print(f"并发上限：{report.limit}")
    print(f"实际峰值：{report.peak_concurrency}")
    print("\n各任务拿到的地盘：")
    for task in tasks:
        print(f"  {task.task_id}｜{task.branch}｜{task.path}")

    problems: list[str] = []
    if report.peak_concurrency > MAX_PARALLEL:
        problems.append(f"峰值 {report.peak_concurrency} 超了上限 {MAX_PARALLEL}")
    if report.peak_concurrency != MAX_PARALLEL:
        problems.append(f"峰值只有 {report.peak_concurrency} —— 上限没吃满，并行没真起来")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        raise DemoError("并发上限没守住")
    print(f"\n✅ 同时跑着的任务从来没有超过 {MAX_PARALLEL} 个")
    return runner, store, tasks, report.peak_concurrency


def _step_workspaces(tasks: list[DemoTask], repo: Path) -> None:
    """工作区与临时目录 —— **同名文件各写各的**。"""
    _banner("第 2 步", "工作区隔离：三个任务写同一个文件名")
    print("各任务工作区里的内容：")
    for task in tasks:
        print(f"  {task.task_id}：{task.worktree_files}｜same.txt = {task.same_content!r}")
    print("\n各任务临时目录里的内容：")
    for task in tasks:
        print(f"  {task.task_id}：{task.scratch_files}")

    problems: list[str] = []
    for task in tasks:
        if task.same_content != task.task_id:
            problems.append(f"{task.task_id} 的工作区里读出的是 {task.same_content!r}")
        if task.worktree_files != ["same.txt"]:
            problems.append(f"{task.task_id} 的工作区里混进了别人的文件：{task.worktree_files}")
        if task.scratch_content != task.task_id:
            problems.append(f"{task.task_id} 的临时目录里读出的是 {task.scratch_content!r}")
        if task.scratch_files != ["same.txt"]:
            problems.append(f"{task.task_id} 的临时目录里混进了别人的文件：{task.scratch_files}")
    if len({task.path for task in tasks}) != TASKS:
        problems.append("几个任务的工作区不是各是各的")
    if len({task.scratch for task in tasks}) != TASKS:
        problems.append("几个任务的临时目录不是各是各的")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        raise DemoError("工作区没有隔开")
    print("\n✅ 同名文件各写各的，谁也没踩谁")
    print(f"   （git 里的分支：{', '.join(sorted(_branches(repo)))})")


def _branches(repo: Path) -> list[str]:
    """仓库里的分支名。"""
    raw = _git(repo, "branch", "--list", "--format=%(refname:short)")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _step_events(store: NdjsonEventStore, tasks: list[DemoTask]) -> None:
    """NDJSON 事件流 —— **一个任务一个文件**。"""
    _banner("第 3 步", "事件流隔离：一个任务一个 NDJSON 文件")
    for task in tasks:
        events = store.read(task.task_id)
        print(f"\n{task.task_id}.ndjson（{len(events)} 条）")
        for event in events:
            print(f"  #{event.sequence} [{event.category.value}] {dict(event.payload)}")

    problems: list[str] = []
    problems.extend(store.audit())
    for task in tasks:
        events = store.read(task.task_id)
        if not events:
            problems.append(f"{task.task_id} 没有事件")
        if {event.task_id for event in events} != {task.task_id}:
            problems.append(f"{task.task_id} 的文件里混进了别人的事件")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        raise DemoError("事件流串了")
    print("\n✅ 每个文件里只有它自己那个任务的事件")


async def _step_failure(root: Path) -> None:
    """一个任务炸了 —— **不能把整批掀了**。"""
    _banner("第 4 步", "一个任务炸了，其余照跑")
    repo = _build_repo(root / "second")
    manager = WorkspaceManager(
        repo_path=repo,
        workspace_root=root / "second" / "workspaces",
        git=SubprocessGitClient(),
        clock=SystemClock(),
        lease_ttl_seconds=LEASE_TTL,
    )
    store = NdjsonEventStore(root / "second" / "events")
    runner = ParallelRunner(
        budget=Budget(max_parallel_agents=MAX_PARALLEL),
        workspaces=ManagerWorkspacePort(manager),
        events=store,
        scratch_root=root / "second" / "scratch",
    )
    tasks = [
        DemoTask("task-0"),
        DemoTask("task-1", fail=True),
        DemoTask("task-2"),
    ]

    report = await runner.run(tasks)

    for outcome in report.outcomes:
        print(f"  {outcome.task_id}：{outcome.status.value}｜{outcome.detail or '（正常完成）'}")

    problems: list[str] = []
    statuses = {outcome.task_id: outcome.status for outcome in report.outcomes}
    if statuses.get("task-1") is not ExecutionStatus.FAILED:
        problems.append("炸了的那个任务没有记成失败")
    for task_id in ("task-0", "task-2"):
        if statuses.get(task_id) is not ExecutionStatus.COMPLETED:
            problems.append(f"{task_id} 被别人的失败连累了")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        raise DemoError("一个失败带翻了整批")
    print("\n✅ 一个任务炸了，其余两个照跑 —— 否则并行就白并行了")


async def _run(args: argparse.Namespace) -> int:
    """跑一遍演示。"""
    root = (
        Path(args.workspace)
        if args.workspace
        else Path(tempfile.mkdtemp(prefix="parallel-runner-"))
    )
    try:
        _, store, tasks, _ = await _step_parallel(root)
        _step_workspaces(tasks, root / "repo")
        _step_events(store, tasks)
        await _step_failure(root)
    except DemoError as error:
        print(f"\n❌ {error}")
        return 1
    finally:
        if not args.workspace:
            shutil.rmtree(root, ignore_errors=True)
    _banner("完成", "并得起、并不串、炸一个不要紧")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TASK-P2-013 多 Agent 并行执行与资源隔离演示")
    parser.add_argument(
        "--workspace",
        help="用这个目录当根目录（默认建一个临时的，演示结束删掉）",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
