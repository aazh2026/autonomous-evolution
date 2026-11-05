"""工程交付层 —— 能否正确交付。

设计依据：docs/design.md §10（工程交付层：任务成功率、返工率、回归缺陷率）；
docs/tasks.md TASK-P1-071。

这一层量的不是"干了多少活"，而是"干出来的东西对不对"。

**回归缺陷率为什么按"引入了缺陷的任务"算，而不是按缺陷个数算。** 一次
提交引入 1 个缺陷和引入 10 个缺陷，在"这批交付有多少比例是有问题的"这个
问题上没有区别 —— 区别在于严重性，而严重性属于别的指标。这里报比例，
把缺陷**个数**放进 ``detail``，两个信息都不丢。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evaluation.common import MetricResult, MetricState, rate

#: 任务成功率的指标 id。
TASK_SUCCESS_RATE = "task_success_rate"
#: 返工率的指标 id。
REWORK_RATE = "rework_rate"
#: 回归缺陷率的指标 id。
REGRESSION_DEFECT_RATE = "regression_defect_rate"


@dataclass(frozen=True)
class TaskOutcome:
    """一个任务的交付结果 —— 工程交付层指标的唯一输入。"""

    task_id: str
    #: 任务是否已完成。
    completed: bool
    #: 返工次数。大于 0 表示这个任务返过工。
    repair_count: int = 0
    #: 本次交付引入的新缺陷个数。
    #:
    #: ``None`` 表示**缺陷数据尚不可得**，不是"没有缺陷"。这两件事在报表上
    #: 长得一模一样，而含义相反 —— 后者是一次"查过了，确实没有"的结论。
    #: 阶段一的库里还没有缺陷表（TASK-P1-083 之后才有），若把"没有数据"
    #: 折成 0，任何一次交付都会报出"回归缺陷率 0%"，一个**假的安全信号**。
    introduced_defects: int | None = None


def task_success_rate(outcomes: Sequence[TaskOutcome]) -> MetricResult:
    """任务成功率 = 已完成任务数 / 任务总数。

    **分母是全部任务，包括未完成的。** 只拿"跑完的任务"当分母，会让一个
    跑不完就放弃的系统显示出很高的成功率 —— 那些没跑完的任务从分母里
    消失了。
    """
    if not outcomes:
        return rate(TASK_SUCCESS_RATE, 0, 0, empty_state=MetricState.NO_SAMPLES)
    completed = sum(1 for item in outcomes if item.completed)
    return rate(
        TASK_SUCCESS_RATE,
        completed,
        len(outcomes),
        detail={"total_tasks": len(outcomes)},
    )


def rework_rate(outcomes: Sequence[TaskOutcome]) -> MetricResult:
    """返工率 = 返过工的任务数 / 任务总数。

    返工 = ``repair_count > 0``。返工次数的**总和**放进 ``detail`` ——
    "有多少任务返过工"与"返了多少次"是两个问题，前者是率，后者是量。
    """
    if not outcomes:
        return rate(REWORK_RATE, 0, 0, empty_state=MetricState.NO_SAMPLES)
    reworked = sum(1 for item in outcomes if item.repair_count > 0)
    return rate(
        REWORK_RATE,
        reworked,
        len(outcomes),
        detail={
            "total_tasks": len(outcomes),
            "total_repairs": sum(item.repair_count for item in outcomes),
        },
    )


def regression_defect_rate(outcomes: Sequence[TaskOutcome]) -> MetricResult:
    """回归缺陷率 = 引入过新缺陷的**已完成**任务数 / 缺陷数据可得的已完成任务数。

    两个分母上的取舍：

    1. 分母是**已完成**的任务，不是全部任务 —— 一个没完成的任务不构成
       交付，谈不上"引入了回归"。
    2. 分母里只算**缺陷数据可得**的任务 —— "查过了，没查出缺陷"是知识，
       "没查过"不是。把没查过的算进来，等于宣称"那一批交付没有问题"，
       而系统其实根本没看过。

    一个缺陷数据都拿不到时报 ``baseline_missing``，不报 0。
    """
    completed = [item for item in outcomes if item.completed]
    if not completed:
        return rate(REGRESSION_DEFECT_RATE, 0, 0, empty_state=MetricState.NO_SAMPLES)
    known = [item for item in completed if item.introduced_defects is not None]
    if not known:
        return rate(
            REGRESSION_DEFECT_RATE,
            0,
            0,
            empty_state=MetricState.BASELINE_MISSING,
            detail={
                "reason": "缺陷数据尚不可得",
                "completed_tasks": len(completed),
                "tasks_with_defect_data": 0,
            },
        )
    with_defects = sum(1 for item in known if (item.introduced_defects or 0) > 0)
    return rate(
        REGRESSION_DEFECT_RATE,
        with_defects,
        len(known),
        detail={
            "completed_tasks": len(completed),
            "tasks_with_defect_data": len(known),
            "introduced_defects": sum(item.introduced_defects or 0 for item in known),
        },
    )


__all__ = [
    "REGRESSION_DEFECT_RATE",
    "REWORK_RATE",
    "TASK_SUCCESS_RATE",
    "TaskOutcome",
    "regression_defect_rate",
    "rework_rate",
    "task_success_rate",
]
