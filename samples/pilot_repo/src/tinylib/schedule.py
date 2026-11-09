"""调度 —— 下一次什么时候跑。

这一模块的逻辑是对的，但写法啰嗦：**嵌套条件堆在一起**。它是试点仓库里
"重构但不改变行为"这类任务的对象。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def next_run(interval_seconds, now=None):
    """下一次运行的时刻。

    间隔必须是正数 —— 0 或负数会让调度永远停在原地。
    """
    if interval_seconds <= 0:
        raise ValueError(f"间隔必须是正数：{interval_seconds}")
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment + timedelta(seconds=interval_seconds)


def is_due(last_run, interval_seconds, now=None):
    """到点了没有。"""
    if last_run is None:
        return True
    moment = now if now is not None else datetime.now(UTC)
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if interval_seconds <= 0:
        return False
    elapsed = (moment - last_run).total_seconds()
    if elapsed < 0:
        return False
    if elapsed >= interval_seconds:
        return True
    return False


def due_tasks(tasks, now=None):
    """挑出到点的任务。

    每个任务是 ``(名字, 上次运行时刻, 间隔秒数)`` 三元组。
    """
    result = []
    for name, last_run, interval_seconds in tasks:
        if is_due(last_run, interval_seconds, now):
            result.append(name)
    return result


__all__ = ["due_tasks", "is_due", "next_run"]
