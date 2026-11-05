"""ASES worker 主循环。

设计依据：docs/tasks.md TASK-P1-007（compose 里要有 worker）。

**这一版 worker 做什么**：

1. 确认数据库连得上、对象存储可写（``ensure_ready`` —— 建桶/建目录）。
2. 然后待命：周期性心跳，等 SIGTERM/SIGINT 优雅退出。

**为什么不是空壳**：容器编排里最难的部分不是业务逻辑，是"依赖还没好就启动"
"收到 SIGTERM 不退出"这些。留一个真的在跑、有心跳、能优雅退出的进程，
这些问题现在就被验证过了。等到 TASK-P1-030 把真正的任务循环放进来时，
这些基础问题已经不再是风险。

**心跳有两个去处**：日志，以及一个**心跳文件**。

心跳文件是给容器编排看的。进程"活着"和进程"还在正常干活"是两件事 ——
一个卡在死锁里的 worker 同样显示为 running。健康检查去看心跳文件的新鲜度，
才能区分这两种情况。没有它，``docker compose up --wait`` 会在 worker
崩溃重启循环里报告"就绪"，然后所有人以为栈起来了。

**心跳间隔**取 30 秒：足够在日志里看出"活着"，又不至于把日志刷爆。
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from infrastructure.config.settings import get_settings
from infrastructure.database.engine import dispose_engine, get_engine
from infrastructure.observability.logging import configure_logging, get_logger
from infrastructure.storage.object_store import build_content_addressed_store

SERVICE_NAME = "ases-worker"

#: 心跳间隔（秒）。
HEARTBEAT_SECONDS = 30.0

#: 心跳文件最多允许多旧（秒）还算活着。
#:
#: 取心跳间隔的三倍：容忍一次心跳没写成（比如磁盘抖了一下），
#: 又不至于让"卡死了两个心跳周期"看起来还健康。
HEARTBEAT_STALE_SECONDS = HEARTBEAT_SECONDS * 3

logger = get_logger(__name__)


def write_heartbeat(path: Path) -> None:
    """刷新心跳文件。

    写时间戳而不是只 touch：文件内容能直接看出"最后一次心跳是什么时候"，
    排障时不用去猜 mtime 的时区。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")


async def wait_for_dependencies() -> None:
    """确认依赖就绪，不就绪就直接失败。

    **启动时失败比运行中失败好**：容器会立刻重启并暴露问题，
    而不是让一个半残的进程假装正常、直到某次任务写入产物时才炸。
    """
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("dependency_database_ready")

    store = build_content_addressed_store()
    created = await store.ensure_ready()
    logger.info("dependency_object_store_ready", created=created)


async def run() -> None:
    """worker 主循环。"""
    settings = get_settings()
    configure_logging(settings)
    heartbeat_file = settings.worker_heartbeat_file

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(signum: signal.Signals) -> None:
        logger.info("worker_signal_received", signal=signum.name)
        stop.set()

    # 把信号号作为参数传进去，而不是让回调自己去读 —— asyncio 的
    # add_signal_handler 约定回调只收信号号这一个参数。
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_stop, sig)

    logger.info(
        "worker_started",
        service=SERVICE_NAME,
        environment=settings.environment.value,
    )
    try:
        await wait_for_dependencies()
        # 依赖就绪之后才落第一笔心跳：在此之前 worker 还没真正开始工作，
        # 让编排过早地认为它"已在正常循环"是不诚实的。
        write_heartbeat(heartbeat_file)
        logger.info("worker_ready", heartbeat_file=str(heartbeat_file))
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                write_heartbeat(heartbeat_file)
                logger.info("worker_heartbeat")
    finally:
        await dispose_engine()
        logger.info("worker_stopped", service=SERVICE_NAME)


def main() -> None:
    """同步入口。"""
    asyncio.run(run())


if __name__ == "__main__":
    main()


__all__ = [
    "HEARTBEAT_SECONDS",
    "HEARTBEAT_STALE_SECONDS",
    "SERVICE_NAME",
    "main",
    "run",
    "write_heartbeat",
]
