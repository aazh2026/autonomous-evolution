"""TASK-P3-016 —— 强杀进程之后，从检查点恢复。

设计依据：docs/tasks.md TASK-P3-016（交付物"进程重启 / 机器重启后从检查点恢
复"；验收条件"在执行中强杀进程，重启后任务状态可恢复且不重复执行已完成的副作
用"；验证 T1）、R-091、R-095、R-099。

**这一组跑的是真东西。**

- **真的 PostgreSQL** —— 账本得活得比进程长，否则验的就不是断点恢复。
- **真的 git 仓库** —— 副作用是往仓库里提交一次，而**证人是 git 自己的提交
  历史**：``git log`` 里那条副作用提交**必须只有一条**。
- **真的 ``SIGKILL``** —— 子进程自己朝自己开枪。不可捕获、不可拦截：进程当
  场消失，``finally`` 不跑，数据库连接不关。

**为什么证人必须是外部的。** 系统自己的报告说"我跳过了这一步"，那验的是
"执行器与执行器自己的报告一致" —— 恰恰是最需要防的那种自证。git 不知道有
什么步骤账，它只知道仓库里有没有那个提交。**提交只有一条，才是真的没有重
复执行。**

**三个杀点覆盖三种窗口。** 进程可以死在副作用的任何位置，而每一种位置的
正确处理都不一样：

| 杀点 | 副作用做了吗 | 记录写了吗 | 重启后该做什么 |
| --- | --- | --- | --- |
| ``commit:before`` | 没有 | 没有（只有"开始了"） | **补做一次** |
| ``commit:after`` | **做了** | **没有** | **别做** —— 探测认出来就跳过 |
| ``publish:before`` | 上一步做了 | 上一步写了 | **跳过上一步**，补做这一步 |

第二行是这一层的全部难点所在：**"做完了但没记上"与"压根没做"在账本里长得
一模一样**。只有去问外部世界（git 里有没有那个提交）才分得开。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.entities import EvolutionCycle, Project
from core.metrics.cycle import CycleCounts, CycleStatus
from core.metrics.cycle import EvolutionCycle as CycleRecord
from core.task.resume import StepStatus
from infrastructure.database.repositories import (
    DECISION_RECORD,
    CandidateRepository,
    DecisionRepository,
    TaskRepository,
)
from infrastructure.database.step_effect import PostgresStepEffectLedger
from orchestration.resume import read_progress
from tests.integration.workers.resume_worker import DB_URL_ENV, EFFECT_COMMIT_MESSAGE

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 步骤的规范顺序。**恢复的断点由它决定** —— 账本知道自己记过什么，不知道
#: 步骤之间的先后。
STEP_ORDER = ("commit", "publish")


@dataclass(frozen=True, slots=True)
class Worker:
    """一次试验进程的参数。"""

    #: 在哪儿自杀。空串 = 不杀。
    kill_at: str = ""
    #: 租约多长。**0 = 占位一落库就过期** —— 上一个执行者一死，第二个立刻
    #: 就能接手，不用等。
    lease_seconds: int = 0
    #: 第几次尝试。
    attempt: int = 1
    #: 占位标记文件。写下它 = "我已经占住这一步了"。
    marker: str = ""
    #: 副作用要拖多久（秒）。
    hold_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class Report:
    """试验进程报回来的结果。"""

    #: 这一轮真正做了的步骤。
    completed: tuple[str, ...]
    #: 早就做完了、这一轮跳过的步骤。
    skipped: tuple[str, ...]
    #: 流程停住了吗。
    halted: bool
    #: 停在哪一步。
    halted_step: str | None
    #: 每一步落到了哪一档，按跑的顺序。
    states: tuple[str, ...]


def _git(repo: Path, *args: str) -> str:
    """跑一条 git 命令。

    **把身份与钩子都钉死。** 用户身份不配的话 ``git commit`` 会直接失败；
    全局配了签名或钩子的话，提交会被签名流程或钩子脚本搅黄 —— 而这两种失败
    与被测的东西毫无关系，却会让用例变成"红得没有道理"。
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=ases@example.invalid",
            "-c",
            "user.name=ases",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _effect_commits(repo: Path) -> list[str]:
    """仓库里有几个副作用提交。**这是证人的读数。**"""
    log = _git(repo, "log", "--format=%s")
    return [line for line in log.splitlines() if line == EFFECT_COMMIT_MESSAGE]


def _worker_command(task_id: str, repo: Path, worker: Worker) -> list[str]:
    """试验进程的命令行。"""
    return [
        sys.executable,
        "-m",
        "tests.integration.workers.resume_worker",
        "--task-id",
        task_id,
        "--repo",
        str(repo),
        "--kill-at",
        worker.kill_at,
        "--lease-seconds",
        str(worker.lease_seconds),
        "--attempt",
        str(worker.attempt),
        "--marker",
        worker.marker,
        "--hold-seconds",
        str(worker.hold_seconds),
    ]


def _worker_env(database_url: str) -> dict[str, str]:
    """试验进程的环境。

    **连接串走环境变量，不走命令行** —— 命令行参数在 ``ps`` 里是全局可见
    的，而连接串里有口令。
    """
    return {
        **os.environ,
        DB_URL_ENV: database_url,
        "PYTHONPATH": str(REPO_ROOT),
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个真的 git 仓库，带一个起点提交。"""
    path = (tmp_path / "repo").resolve()
    path.mkdir()
    _git(path, "init", "-q")
    (path / "README.md").write_text("起点\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


@pytest_asyncio.fixture
async def factory(
    engine: AsyncEngine, session: AsyncSession
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """一个会话工厂。"""
    rows = await session.execute(text("SELECT count(*) FROM task_step_effect"))
    assert rows.scalar_one() == 0, "步骤账不是干净的 —— 前置条件不满足"
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def task_id(session: AsyncSession) -> str:
    """一个真的任务行。

    **走生产路径建出来**（候选 → 决策 → 任务），不走 ``Task(...)`` 直接构造 ——
    仓储层是任务唯一的新增入口，而步骤账的外键指向的就是它。

    建这一条链是为了**外键是真的**：``task_step_effect.task_id`` 引用
    ``task.id``，没有真任务行就写不进账本。
    """
    project = Project(
        id="p-resume",
        name="断点恢复演示项目",
        goal={"objective": [{"id": "OBJ-1", "description": "把断点恢复跑通"}]},
        policy={"merge_to_main": "approval_required"},
        budget={"currency": "USD"},
        repository_url="https://example.invalid/repo.git",
        tech_stack=["python"],
    )
    session.add(project)
    await session.flush()

    cycle = EvolutionCycle.from_domain(
        CycleRecord.build(
            project_id=project.id,
            cycle_id="cycle-resume",
            trigger="schedule",
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            status=CycleStatus.COMPLETED,
            counts=CycleCounts(candidates=1, decisions=1, tasks=1, completed_tasks=0),
        )
    )
    session.add(cycle)
    await session.flush()

    candidate = await CandidateRepository(session).create(
        project_id=project.id,
        category="quality",
        title="补一个缺失的回归测试",
        evidence=[{"kind": "coverage_report", "ref": "artifact://report-1"}],
        value=Decimal("0.6"),
        risk=Decimal("0.2"),
        confidence=Decimal("0.8"),
        cycle_id=cycle.id,
    )
    decision = await DecisionRepository(session).record(
        candidate_id=candidate.id,
        decision=DECISION_RECORD,
        rationale="证据充分",
        cycle_id=cycle.id,
    )
    task = await TaskRepository(session).create_from_decision(
        decision.id,
        title="补一个缺失的回归测试",
        task_type="test",
        priority=Decimal("70"),
    )
    await session.commit()
    return str(task.id)


def _run_worker(
    database_url: str,
    task_id: str,
    repo: Path,
    worker: Worker | None = None,
) -> subprocess.CompletedProcess[str]:
    """拉起一个试验进程，等它跑完。"""
    return subprocess.run(
        _worker_command(task_id, repo, worker or Worker()),
        cwd=REPO_ROOT,
        env=_worker_env(database_url),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _report(result: subprocess.CompletedProcess[str]) -> Report:
    """读进程的报告。"""
    assert result.returncode == 0, f"试验进程失败：\n{result.stdout}\n{result.stderr}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    return Report(
        completed=tuple(payload["completed"]),
        skipped=tuple(payload["skipped"]),
        halted=bool(payload["halted"]),
        halted_step=payload["halted_step"],
        states=tuple(step["state"] for step in payload["steps"]),
    )


def _wait_for_the_marker(path: Path, *, timeout: float = 30.0) -> None:
    """等占位标记出现。

    **等的是"占位已经落库"这个事实。** 账本每次写都自己提交，所以标记文件
    一出现，就说明那一步的占位已经在库里了 —— 这时候拉起第二个执行者，它看
    到的就是"别人正在做"。
    """
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"等了 {timeout:.0f} 秒，占位标记 {path} 还没出现")
        time.sleep(0.02)


class TestTheKillThatLandsBetweenTheEffectAndTheRecord:
    """最难的那个窗口 —— **副作用做完了，记录还没写**。"""

    async def test_the_restart_does_not_repeat_the_effect(
        self,
        migrated_database: str,
        task_id: str,
        repo: Path,
    ) -> None:
        """**做过了就不要再做。**

        这是整个任务的核心断言：**已完成的副作用不重复执行**。
        """
        killed = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(kill_at="commit:after"),
        )

        assert killed.returncode == -signal.SIGKILL, f"没被杀掉：{killed.stdout}\n{killed.stderr}"
        # 证人第一读：副作用确实已经发生了。
        assert len(_effect_commits(repo)) == 1

        restarted = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(attempt=2),
        )
        report = _report(restarted)

        assert report.states[0] == "reconciled"
        # 证人第二读：**还是一条** —— 没有第二次提交。
        assert len(_effect_commits(repo)) == 1

    async def test_the_ledger_shows_the_step_was_started_but_not_finished(
        self,
        factory: async_sessionmaker[AsyncSession],
        migrated_database: str,
        task_id: str,
        repo: Path,
    ) -> None:
        """**强杀之后，账本诚实地记着"开始了，没写完成"。**

        **它不能记成"做完了"** —— 那一刻谁都不知道做完了没有。也不能记成
        "没做" —— 副作用明明已经发生了。**"开始了"是唯一诚实的写法**，
        而它正是触发探测的那个状态。
        """
        killed = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(kill_at="commit:after"),
        )
        assert killed.returncode == -signal.SIGKILL

        ledger = PostgresStepEffectLedger(factory)
        effects = await ledger.effects(task_id)

        assert len(effects) == 1
        assert effects[0].step == "commit"
        assert effects[0].status is StepStatus.STARTED


class TestTheKillThatLandsBeforeTheEffect:
    """**压根没做的那一步，重启之后要补做。**"""

    async def test_the_restart_performs_the_effect_exactly_once(
        self,
        migrated_database: str,
        task_id: str,
        repo: Path,
    ) -> None:
        """**没做的事要做 —— 而且只做一次。**"""
        killed = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(kill_at="commit:before"),
        )

        assert killed.returncode == -signal.SIGKILL
        assert _effect_commits(repo) == []

        restarted = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(attempt=2),
        )
        report = _report(restarted)

        assert report.completed == ("commit", "publish")
        assert len(_effect_commits(repo)) == 1


class TestTheKillThatLandsOnALaterStep:
    """**前面那一步已经记完了 —— 重启之后不能重做它。**"""

    async def test_the_finished_step_is_skipped_and_the_side_effect_is_not_repeated(
        self,
        migrated_database: str,
        task_id: str,
        repo: Path,
    ) -> None:
        """**账本说"做完了"，就跳过。**"""
        killed = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(kill_at="publish:before"),
        )

        assert killed.returncode == -signal.SIGKILL
        assert len(_effect_commits(repo)) == 1

        restarted = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(attempt=2),
        )
        report = _report(restarted)

        assert report.skipped == ("commit",)
        # 证人：还是一条提交 —— 上一步没有被重做。
        assert len(_effect_commits(repo)) == 1
        # 而没做完的那一步被补上了。
        assert (repo / "published.txt").exists()


class TestTheRecoverableState:
    """**"任务状态可恢复" —— 重启之后说得出来它停在哪儿。**"""

    async def test_the_progress_says_where_the_task_stopped(
        self,
        factory: async_sessionmaker[AsyncSession],
        migrated_database: str,
        task_id: str,
        repo: Path,
    ) -> None:
        """**账本说得出来断点。**"""
        killed = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(kill_at="publish:before"),
        )
        assert killed.returncode == -signal.SIGKILL

        ledger = PostgresStepEffectLedger(factory)
        progress = await read_progress(ledger, task_id, STEP_ORDER)

        assert progress.done == ("commit",)
        assert progress.started == ("publish",)
        assert progress.resume_from == "publish"

    async def test_the_task_row_survives_the_kill(
        self,
        session: AsyncSession,
        migrated_database: str,
        task_id: str,
        repo: Path,
    ) -> None:
        """**任务本身还在 —— 它没有被强杀带走。**

        强杀的是进程，不是数据。**这一条看着废话，其实是断点恢复的前提**：
        任务行没了的话，"这个任务跑到哪儿了"就无从问起。
        """
        killed = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(kill_at="publish:before"),
        )
        assert killed.returncode == -signal.SIGKILL

        rows = await session.execute(
            text("SELECT status, title FROM task WHERE id = :task_id"),
            {"task_id": task_id},
        )
        row = rows.one()

        assert row.title == "补一个缺失的回归测试"
        assert row.status


class TestTwoWorkersOnTheSameStep:
    """**两个执行者不会同时做同一件事。**"""

    async def test_the_second_worker_stops_instead_of_repeating_the_effect(
        self,
        migrated_database: str,
        task_id: str,
        repo: Path,
        tmp_path: Path,
    ) -> None:
        """**别人正在做 —— 别碰。**

        第一个执行者占着这一步不放，第二个执行者来了只能停。**两个执行者同
        时做同一件事，重复执行就不只是"多了一次提交"**，而是两个执行者拿到
        了两份互相打架的状态。
        """
        marker = tmp_path / "claimed.marker"
        worker = Worker(lease_seconds=60, marker=str(marker), hold_seconds=3)

        first = subprocess.Popen(
            _worker_command(task_id, repo, worker),
            cwd=REPO_ROOT,
            env=_worker_env(migrated_database),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 等第一个执行者真的占住了那一步（占位已经提交进库）。
            _wait_for_the_marker(marker)

            second = _run_worker(
                migrated_database,
                task_id,
                repo,
                Worker(lease_seconds=60),
            )
            report = _report(second)

            assert report.halted is True
            assert report.halted_step == "commit"
            assert report.completed == ()

            first_out, first_err = first.communicate(timeout=60)
            assert first.returncode == 0, f"第一个执行进程失败：{first_out}\n{first_err}"
        finally:
            if first.poll() is None:  # pragma: no cover - 只在断言提前失败时走到
                first.kill()
                first.communicate(timeout=30)

        # 证人：**只有一次提交** —— 第二个执行者确实没做副作用。
        assert len(_effect_commits(repo)) == 1


class TestTheDatabaseRefusesToRewriteHistory:
    """**数据库层的三道拒绝 —— 不是应用层的"请记得检查"。**"""

    async def test_a_completed_step_cannot_be_turned_back_into_failed(
        self,
        session: AsyncSession,
        migrated_database: str,
        task_id: str,
        repo: Path,
    ) -> None:
        """**把"做完了"改回"没做完"会被数据库拒绝。**

        这不是洁癖：改成"没做完"之后，恢复时就会把它**再做一遍** —— 而"再
        做一遍"正是这张表存在的理由被自己拆掉。
        """
        _report(_run_worker(migrated_database, task_id, repo))

        with pytest.raises(DatabaseError):
            await session.execute(
                text(
                    "UPDATE task_step_effect SET status = 'failed' "
                    "WHERE task_id = :task_id AND step = 'commit'"
                ),
                {"task_id": task_id},
            )

    async def test_a_recorded_step_cannot_be_deleted(
        self,
        session: AsyncSession,
        migrated_database: str,
        task_id: str,
        repo: Path,
    ) -> None:
        """**记下的账不能被抹掉。**

        抹掉之后，"这一步到底跑过没有"就没人能回答了 —— 而那正是这张表存
        的唯一东西。
        """
        _report(_run_worker(migrated_database, task_id, repo))

        with pytest.raises(DatabaseError):
            await session.execute(
                text("DELETE FROM task_step_effect WHERE task_id = :task_id"),
                {"task_id": task_id},
            )

    async def test_the_status_must_be_one_of_three(
        self,
        session: AsyncSession,
        migrated_database: str,
        task_id: str,
        repo: Path,
    ) -> None:
        """**状态只有三档。** 别的值写不进去 —— 免得半年后有人加第四档而
        读代码的人以为只有三档。

        **拿"开始了"的那一条试。** "做完了"的那一条会被不可逆的守卫先挡
        下来 —— 那样这条用例验的就不是约束本身了。
        """
        killed = _run_worker(
            migrated_database,
            task_id,
            repo,
            Worker(kill_at="commit:before"),
        )
        assert killed.returncode == -signal.SIGKILL

        with pytest.raises(DatabaseError):
            await session.execute(
                text(
                    "UPDATE task_step_effect SET status = 'weird' "
                    "WHERE task_id = :task_id AND step = 'commit'"
                ),
                {"task_id": task_id},
            )
