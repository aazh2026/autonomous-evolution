"""TASK-P2-013 —— 真 git 的并行工作空间隔离。

**单元测试用假工作空间，是因为"隔离没做好"在假工作空间上表现为一条清楚的
断言失败；在真 git 上它表现为一段看不懂的 stderr。** 但"分支冲突不可能发生"
这句话的最终依据是真 git —— **git 自己就不许一条分支被两个 worktree 同时
检出**，那条硬约束才是隔离的根子。所以这一组真的起 ``git`` 跑一遍：

- 多个任务并发建 worktree，**目录互不干扰**（T1 ②）；
- 多个任务写**同一个文件名**，各自写各自的，谁也不踩谁；
- 临时目录也是各是各的；
- 事件流各写各的文件；
- 并发数照样受 ``max_parallel_agents`` 限制。

**观察都要在任务跑的时候做。** 跑完一轮，引擎已经把工作空间还回去了
（目录没了、分支留着 —— 分支才是产出），所以"跑完之后目录里有什么"是
问不到问题的。

需要真的装了 git。不需要 Docker 或数据库，所以不依赖那些夹具。
"""

from __future__ import annotations

import asyncio
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from core.metrics.cost import CostAmount
from core.policy.budget import Budget
from core.task.outcome import ExecutionStatus
from harness.workspace.git import SubprocessGitClient
from harness.workspace.manager import WorkspaceManager
from orchestration.parallel_runner import (
    EventCategory,
    ManagerWorkspacePort,
    NdjsonEventStore,
    ParallelRunner,
    ParallelTaskResult,
    TaskEventSink,
    TaskWorkspace,
)
from tests.unit.harness.workspace.doubles import FakeClock

#: 任务数。三个够看出"并发"与"串行"的区别。
TASKS = 3

#: 并发上限。**故意比任务数小** —— 上限要是没生效，峰值就会是 3。
LIMIT = 2

LEASE_TTL = 3600


def _git(cwd: Path, *args: str) -> str:
    """跑一条 git 命令，返回 stdout。"""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：\n{result.stdout}\n{result.stderr}")
    return result.stdout


def _branches(repo: Path) -> list[str]:
    """仓库里的分支名。"""
    raw = _git(repo, "branch", "--list", "--format=%(refname:short)")
    return [line.strip() for line in raw.splitlines() if line.strip()]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个真 git 仓库，有一个初始提交。

    初始提交是必需的：``git worktree add`` 要从 HEAD 检出，没有 HEAD 的
    仓库建不出 worktree。
    """
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "ases@example.invalid")
    _git(path, "config", "user.name", "ASES")
    (path / "README.md").write_text("基础\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "初始提交")
    return path


@pytest.fixture
def manager(repo: Path, tmp_path: Path) -> WorkspaceManager:
    """真的工作空间管理器。"""
    return WorkspaceManager(
        repo_path=repo,
        workspace_root=tmp_path / "workspaces",
        git=SubprocessGitClient(),
        clock=FakeClock(),
        lease_ttl_seconds=LEASE_TTL,
    )


@pytest.fixture
def engine(manager: WorkspaceManager, tmp_path: Path) -> ParallelRunner:
    """一台引擎 —— **上限是 2，任务是 3**。"""
    return ParallelRunner(
        budget=Budget(max_parallel_agents=LIMIT),
        workspaces=ManagerWorkspacePort(manager),
        events=NdjsonEventStore(tmp_path / "events"),
        scratch_root=tmp_path / "scratch",
    )


@pytest.fixture
def store(tmp_path: Path) -> NdjsonEventStore:
    """事件库。**要与引擎指向同一个根目录**。"""
    return NdjsonEventStore(tmp_path / "events")


class Overlap:
    """几个任务同时在跑 —— **顺便当个栅栏**。

    **固定睡一会儿是不够的。** git 建一个 worktree 要几十毫秒，睡短了任务
    就一个接一个地跑过去了，看着像并发，其实没重叠上。这里等的是"真的
    有第二个任务上来了"，而不是"大概差不多了"。

    **等不到也不至于死。** 超时了就自己走 —— 免得一个测试永远挂着。
    """

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._count = 0
        self._event = asyncio.Event()
        self.live = 0
        self.peak = 0

    def enter(self) -> None:
        self.live += 1
        self.peak = max(self.peak, self.live)

    def leave(self) -> None:
        self.live -= 1

    async def arrive(self) -> None:
        """等别的任务上来。"""
        self._count += 1
        if self._count >= self._parties:
            self._event.set()
            return
        try:
            await asyncio.wait_for(self._event.wait(), timeout=10)
        except TimeoutError:
            return


class RealGitTask:
    """一个真的跑在 worktree 里的任务。

    **它写的是同一个文件名** —— 各写各的目录。文件名一样才是考验：名字
    不一样的话，两个任务各写各的，谁也证明不了什么。

    **它把看到的东西都记下来** —— 因为跑完一轮工作空间就还回去了，事后
    是问不到问题的。
    """

    def __init__(self, task_id: str, repo: Path, overlap: Overlap) -> None:
        self._task_id = task_id
        self._repo = repo
        self.overlap = overlap
        self.path: Path | None = None
        self.branch = ""
        self.scratch: Path | None = None
        self.path_existed = False
        self.same_content = ""
        self.scratch_content = ""
        self.worktree_files: list[str] = []
        self.scratch_files: list[str] = []
        self.git_worktrees = ""

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
        self.overlap.enter()
        try:
            (workspace.path / "same.txt").write_text(self._task_id, encoding="utf-8")
            (workspace.path / f"only-{self._task_id}.txt").write_text(
                self._task_id, encoding="utf-8"
            )
            (workspace.scratch / "same.txt").write_text(self._task_id, encoding="utf-8")
            await events.emit(EventCategory.CODE, {"file": "same.txt"})
            # **等别的任务上来** —— 三个任务真的叠在一起，写文件的先后才
            # 不会决定谁踩到谁。
            await self.overlap.arrive()
            self.path_existed = workspace.path.exists()
            self.same_content = (workspace.path / "same.txt").read_text(encoding="utf-8")
            self.scratch_content = (workspace.scratch / "same.txt").read_text(encoding="utf-8")
            self.worktree_files = sorted(path.name for path in workspace.path.glob("*.txt"))
            self.scratch_files = sorted(path.name for path in workspace.scratch.glob("*.txt"))
            self.git_worktrees = _git(self._repo, "worktree", "list", "--porcelain")
            return ParallelTaskResult(
                status=ExecutionStatus.COMPLETED,
                cost=CostAmount(Decimal("1.00"), "USD"),
            )
        finally:
            self.overlap.leave()


def _tasks(repo: Path) -> list[RealGitTask]:
    """一批任务 —— **共用一个重叠计**。"""
    overlap = Overlap(LIMIT)
    return [RealGitTask(f"task-{index}", repo, overlap) for index in range(TASKS)]


class TestTheWorktreesAreIsolated:
    """T1 ② —— 并发任务的 Git Worktree 完全隔离。"""

    async def test_each_task_gets_its_own_worktree(
        self, engine: ParallelRunner, repo: Path
    ) -> None:
        tasks = _tasks(repo)

        report = await engine.run(tasks)

        paths = {task.path for task in tasks}
        assert len(paths) == TASKS
        assert all(task.path_existed for task in tasks)
        assert len(report.outcomes) == TASKS

    async def test_each_worktree_is_on_its_own_branch(
        self, engine: ParallelRunner, repo: Path
    ) -> None:
        await engine.run(_tasks(repo))

        branches = _branches(repo)
        for index in range(TASKS):
            assert f"agent/task-{index}" in branches

    async def test_git_knows_every_worktree(self, engine: ParallelRunner, repo: Path) -> None:
        """**真 git 认得这一份** —— 不是我们自己在数目录。"""
        tasks = _tasks(repo)

        await engine.run(tasks)

        for task in tasks:
            assert f"branch refs/heads/{task.branch}" in task.git_worktrees

    async def test_the_same_file_name_does_not_collide(
        self, engine: ParallelRunner, repo: Path
    ) -> None:
        """**同名文件各写各的** —— 谁也没踩谁。

        三个任务写的是同一个文件名，而它们是同时写的 —— 所以要是目录共享，
        后写的那个会把先写的盖掉，这一读就会读出别人的名字。
        """
        tasks = _tasks(repo)

        await engine.run(tasks)

        assert {task.task_id: task.same_content for task in tasks} == {
            f"task-{index}": f"task-{index}" for index in range(TASKS)
        }

    async def test_a_file_in_one_worktree_is_invisible_in_the_others(
        self, engine: ParallelRunner, repo: Path
    ) -> None:
        """**每个工作空间里只有它自己的文件。** 别人的文件跑不进来。"""
        tasks = _tasks(repo)

        await engine.run(tasks)

        for task in tasks:
            assert task.worktree_files == ["only-" + task.task_id + ".txt", "same.txt"]

    async def test_the_branches_are_never_conflicted(
        self, engine: ParallelRunner, repo: Path
    ) -> None:
        """**git 自己就不许一条分支被两个 worktree 同时检出** —— 那条硬约束
        就是"分支冲突不可能发生"的根子。"""
        await engine.run(_tasks(repo))

        agent_branches = [name for name in _branches(repo) if name.startswith("agent/")]
        assert len(agent_branches) == TASKS
        assert len(set(agent_branches)) == TASKS


class TestTheScratchDirectoriesAreIsolated:
    """T1 ② —— 临时目录完全隔离。"""

    async def test_each_task_gets_its_own_scratch_directory(
        self, engine: ParallelRunner, repo: Path
    ) -> None:
        tasks = _tasks(repo)

        await engine.run(tasks)

        scratches = {task.scratch for task in tasks}
        assert len(scratches) == TASKS

    async def test_the_same_file_name_in_scratch_does_not_collide(
        self, engine: ParallelRunner, repo: Path
    ) -> None:
        tasks = _tasks(repo)

        await engine.run(tasks)

        for task in tasks:
            assert task.scratch_files == ["same.txt"]
            assert task.scratch_content == task.task_id

    async def test_the_scratch_directories_are_cleaned_up(
        self, engine: ParallelRunner, repo: Path
    ) -> None:
        """**临时目录用完就没了** —— 留着只会攒垃圾。"""
        tasks = _tasks(repo)

        await engine.run(tasks)

        for task in tasks:
            assert task.scratch is not None
            assert not task.scratch.exists()


class TestTheEventsAreIsolatedUnderRealGit:
    """T1 ③ —— 事件流互不串扰。"""

    async def test_each_task_has_its_own_file(
        self, engine: ParallelRunner, tmp_path: Path, repo: Path
    ) -> None:
        await engine.run(_tasks(repo))

        files = sorted(path.name for path in (tmp_path / "events").glob("*.ndjson"))
        assert files == [f"task-{index}.ndjson" for index in range(TASKS)]

    async def test_the_audit_finds_no_cross_talk(
        self, engine: ParallelRunner, store: NdjsonEventStore, repo: Path
    ) -> None:
        await engine.run(_tasks(repo))

        assert store.audit() == ()


class TestTheConcurrencyIsBoundedUnderRealGit:
    """T1 ① —— 并发数照样受限制，**真 git 也一样**。

    **这一条最容易被忽略**：worktree 的创建要走 git 子进程，而 git 是阻塞的
    —— 一旦忘了把它挪出事件循环，整个并发就退化成串行，而"上限"那句话
    仍然成立（只是永远碰不到上限）。所以这里既要证明"没超"，也要证明
    "真的一起跑过"。
    """

    async def test_the_peak_never_exceeds_the_limit(
        self, engine: ParallelRunner, repo: Path
    ) -> None:
        report = await engine.run(_tasks(repo))

        assert report.peak_concurrency <= LIMIT

    async def test_the_tasks_really_overlap(self, engine: ParallelRunner, repo: Path) -> None:
        """**碰巧没重叠不算数** —— 要证明它们确实是同时跑的。"""
        tasks = _tasks(repo)

        await engine.run(tasks)

        assert tasks[0].overlap.peak == LIMIT


class TestTheWorktreesAreReleased:
    async def test_the_directories_are_removed(self, engine: ParallelRunner, repo: Path) -> None:
        tasks = _tasks(repo)

        await engine.run(tasks)

        for task in tasks:
            assert task.path is not None
            assert not task.path.exists()

    async def test_the_branches_survive(self, engine: ParallelRunner, repo: Path) -> None:
        """**分支是产出** —— 释放刻意留着它。"""
        await engine.run(_tasks(repo))

        branches = _branches(repo)
        for index in range(TASKS):
            assert f"agent/task-{index}" in branches
