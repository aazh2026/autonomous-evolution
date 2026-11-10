"""断点恢复的试验进程 —— **被强杀的那个进程**。

设计依据：docs/tasks.md TASK-P3-016（"在执行中强杀进程，重启后任务状态可
恢复且不重复执行已完成的副作用"）。

**这个进程做的是真事。** 它连的是真的 PostgreSQL，跑的是真的
:class:`~infrastructure.database.step_effect.PostgresStepEffectLedger` 与
:class:`~orchestration.resume.ResumableStepRunner`，副作用是往一个真的 git
仓库里提交一次。**没有替身**：替身出来的"没有重复执行"证明不了任何事 ——
重复执行之所以危险，正是因为它在替身的世界里看不出来。

**它自己把自己杀掉。** 杀点是确定的（``commit:before`` /
``commit:after`` / ``publish:before``），所以"副作用做完、记录还没写"这个
最难复现的窗口**必然被复现**，而不是"碰运气撞上"。用的信号是 ``SIGKILL``
—— 不可捕获、不可拦截，进程当场消失，**连 ``finally`` 都不会跑**。

**账本连着的是真库**，所以强杀之后账本还活着 —— 这正是要验的东西。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.task.resume import ProbeOutcome
from infrastructure.database.step_effect import PostgresStepEffectLedger
from orchestration.resume import ResumableStepRunner, Step

#: 副作用提交用的消息。**证人就盯它** —— 出现两次就是重复执行了。
EFFECT_COMMIT_MESSAGE = "ases-effect"

#: 环境变量：数据库连接串。**走环境变量不走命令行** —— 命令行参数在
#: ``ps`` 里是全局可见的，连接串里有口令。
DB_URL_ENV = "ASES_RESUME_DB_URL"


@dataclass(frozen=True, slots=True)
class Options:
    """这个进程要做什么。"""

    #: 在哪儿自杀。空串 = 不杀。
    kill_at: str = ""
    #: 占位标记文件。写下它 = "我已经占住这一步了"（供并发的那个用例同步）。
    marker: str = ""
    #: 副作用要拖多久（秒）。给并发用例一个"另一个执行者还在做"的窗口。
    hold_seconds: float = 0.0


def _die_here(options: Options, point: str) -> None:
    """在指定的点上把自己彻底杀掉。

    **用 ``SIGKILL`` 而不是 ``SIGTERM``。** ``SIGTERM`` 可以被捕获 —— 生产
    代码里通常有清理逻辑，于是"被杀"变成了"有序退出"，而**有序退出会把
    状态收拾干净**：那正是断点恢复要处理的场景里**不会发生**的事。

    ``SIGKILL`` 不可捕获、不可拦截：进程当场消失，缓冲区不刷、``finally``
    不跑、数据库连接不关。**这才是"机器断电"在软件里的样子。**
    """
    if point != options.kill_at:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os.kill(os.getpid(), signal.SIGKILL)
    # 走不到这里 —— 进程已经没了。这几行只是让类型检查器知道这个函数
    # 不会从这里正常返回。
    time.sleep(60)


def _git(repo: Path, *args: str) -> str:
    """跑一条 git 命令。

    **把身份钉死。** `git commit` 需要提交者身份，没配就直接退出 128
    （"Author identity unknown"）—— CI 的 runner 上没有全局身份，
    本地有，于是**本地绿的、CI 红的**。这里显式钉上，跟
    `tests/integration/test_resume_after_kill.py` 里那个 `_git` 一致。
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
    """仓库里有多少个副作用提交。**这是证人的读数。**"""
    log = _git(repo, "log", "--format=%s")
    return [line for line in log.splitlines() if line == EFFECT_COMMIT_MESSAGE]


def _has_effect_commit(repo: Path) -> bool:
    """副作用做完了吗 —— **问仓库，不问账本**。

    **问外部世界，这是探测的定义。** 账本自己记的东西只能证明"我以为我做
    过了"；git 的提交历史才是那件事有没有真的发生过的独立证据。
    """
    return bool(_effect_commits(repo))


def _build_steps(repo: Path, options: Options) -> list[Step]:
    """两步：**提交**与**发布**。

    两步都有探测 —— 步骤的结果未知时，问的是外部世界：仓库里有没有那个提
    交、有没有那个文件。
    """

    async def _commit() -> str:
        # 杀点一：占位已经落库，副作用还没开始。
        _die_here(options, "commit:before")
        _write_marker(options)
        if options.hold_seconds:
            await asyncio.sleep(options.hold_seconds)
        (repo / "artifact.txt").write_text("副作用产物\n", encoding="utf-8")
        _git(repo, "add", "artifact.txt")
        _git(repo, "commit", "-m", EFFECT_COMMIT_MESSAGE)
        # 杀点二：**副作用已经生效，记录还没写**。这是最难的那个窗口。
        _die_here(options, "commit:after")
        return "committed"

    async def _probe_commit() -> ProbeOutcome:
        if _has_effect_commit(repo):
            return ProbeOutcome.COMPLETED
        return ProbeOutcome.NOT_COMPLETED

    async def _publish() -> str:
        # 杀点三：上一步已经记完完成，这一步刚占位。
        _die_here(options, "publish:before")
        (repo / "published.txt").write_text("已发布\n", encoding="utf-8")
        return "published"

    async def _probe_publish() -> ProbeOutcome:
        if (repo / "published.txt").exists():
            return ProbeOutcome.COMPLETED
        return ProbeOutcome.NOT_COMPLETED

    return [
        Step(name="commit", run=_commit, probe=_probe_commit),
        Step(name="publish", run=_publish, probe=_probe_publish),
    ]


def _write_marker(options: Options) -> None:
    """写下占位标记。"""
    if not options.marker:
        return
    Path(options.marker).write_text("claimed\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="断点恢复试验进程")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--marker", default="")
    parser.add_argument("--kill-at", default="")
    parser.add_argument("--hold-seconds", type=float, default=0.0)
    parser.add_argument("--lease-seconds", type=int, default=0)
    parser.add_argument("--attempt", type=int, default=1)
    return parser.parse_args()


async def _run() -> int:
    """跑一遍，把结果打成一行 JSON。"""
    args = _parse_args()
    options = Options(
        kill_at=args.kill_at,
        marker=args.marker,
        hold_seconds=args.hold_seconds,
    )

    database_url = os.environ.get(DB_URL_ENV)
    if not database_url:
        raise SystemExit(f"环境变量 {DB_URL_ENV} 没给")

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        ledger = PostgresStepEffectLedger(factory, lease_seconds=args.lease_seconds)
        runner = ResumableStepRunner(ledger, attempt=args.attempt)
        try:
            report = await runner.run(args.task_id, _build_steps(Path(args.repo), options))
        except Exception as exc:
            # **报出来比静默退出有用。** 父进程拿到的是"它失败了"加原因，
            # 而不是一句没有线索的非零退出码。
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
            return 1
        print(
            json.dumps(
                {
                    "task_id": report.task_id,
                    "completed": list(report.completed),
                    "skipped": list(report.skipped),
                    "halted": report.halted,
                    "halted_step": report.halted_step,
                    "steps": [
                        {"step": item.step, "state": item.state.value} for item in report.steps
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    """入口。"""
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
