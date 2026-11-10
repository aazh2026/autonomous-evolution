"""按历史表现自适应调度 —— **执行器与模型一起挑，挑不出来就不跑**。

设计依据：docs/tasks.md ``TASK-P4-040``（交付物"执行器动态调度 —— 基于历史表现
的自适应调度"；验收条件"**调度器在实测中优于静态分配的对照组**"）、R-038（调度要
基于实测而不是预设）、``TASK-P4-030``（二维能力矩阵 —— **历史就是那张矩阵**）、
``TASK-P4-020``（多模型路由 —— 路由与选型共用一份决定）。

**一次选型，两个端口，一份决定。** 与 :mod:`orchestration.capability_resolver` 同
一条规矩：编排层要问两件事 —— 用哪个执行器
(:class:`~orchestration.loop_a.AgentSelectorPort`)、用哪个模型
(:class:`~orchestration.loop_a.ModelResolverPort`)。**分开问两次，就会选出两个互
相矛盾的组合** —— 那样凑起来的组合**从来没被一起测过**。

**与能力矩阵那一份的区别：决定是活的。** 能力矩阵是一张固定的表，选型每次都按
同一张表答；这里的调度器**每做一次决定，历史就多一条**，下一次的决定就可能换
人 —— 这正是"自适应"与"查表"的分别。

**历史从哪来。** 从**实测记录**来 —— 离线评测跑出来的那些运行（能力矩阵就是这
些记录切出来的）。:func:`load_observations` 读的就是它们。

**为什么失败要炸出来。** 挑不出来的时候，调度器会明说"挑不出来"
(:class:`~core.policy.scheduling.SchedulingDecision` 的 ``chosen`` 是 ``None``)。
这里把它变成异常，**不退回"随便挑一个"** —— 退回一个没证据的组合，等于拿未知的
风险去换"看起来能跑"。

**依赖方向。** 这一层只认 :mod:`core.policy.scheduling` 里的**纯数据**与**纯函
数**（观测、账本、调度器）。它不重算统计量，也不认识评测记录 —— "哪几格分不开"
是评测层算好的结论，作为**数据**带进来。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from core.planning.roadmap import TaskType
from core.policy.scheduling import (
    AdaptiveScheduler,
    Observation,
    SchedulingDecision,
    observation_from_dict,
)
from core.task.execution_loop import LoopAState

__all__ = [
    "AdaptiveSelection",
    "AdaptiveSelectionError",
    "CostCeiling",
    "load_observations",
]


class AdaptiveSelectionError(Exception):
    """按历史挑不出能跑的组合 —— **挑不出来就不跑**。

    **它不是"退化到默认"。** 退化到一个没证据的组合，等于拿未知的风险去换"看
    起来能跑" —— 而这件事恰好是自适应调度要防的。

    :param reason: 人话 —— 挑不出来的具体原因。
    :param rejected: 被排除的组合与理由 —— **排除必须带理由**，否则事后没人知道
        是它不行、是它没测过，还是它压根不在候选里。
    """

    def __init__(self, reason: str, rejected: Sequence[tuple[str, str]] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rejected = tuple(rejected)


#: 单次成本上限的来源 —— **按项目与任务类型给出上限**，给 ``None`` 表示不限。
#:
#: **为什么不写死。** 预算不是全局的：甲项目有钱可以用贵的那格，乙项目快没钱了
#: 只能在它自己允许的档位里退一档。
CostCeiling = Callable[[str, TaskType], Decimal | None]


@dataclass(frozen=True, slots=True)
class AdaptiveSelection:
    """按历史自适应地挑执行器与模型 —— **一个对象，两个端口**。

    :param scheduler: 核心层的调度器 —— **历史就在它里面**。
    :param ceiling: 单次成本上限的来源 —— **不给就是不限**。
    """

    #: 调度器 —— 历史与规矩都在这儿。
    scheduler: AdaptiveScheduler

    #: 单次成本上限的来源。
    ceiling: CostCeiling | None = None

    #: 已经做出的决定 —— **按 (项目, 任务类型) 索引**。
    #:
    #: **它是不参与相等与哈希的。** 两个选择器"选得一样不一样"看的是调度器与历
    #: 史，不是它们各自记住了什么。
    _decisions: dict[tuple[str, str], SchedulingDecision] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    # -- 两个端口 -----------------------------------------------------------

    async def select(self, state: LoopAState, candidates: Sequence[str]) -> str:
        """挑一个执行器 —— **挑不出来就报错**。

        **任务类型取自计划，不是取自状态。** 规划器写的是 :attr:`LoopAState.plan`，
        状态上那个 :attr:`LoopAState.task_type` 字段**没人写** —— 它停在默认值
        ``FEATURE``。用错了这里，挑出来的组合与 :meth:`resolve` 问的键就对不上，
        于是"执行器选了、模型挑不出来"。

        **任务编号取自状态。** 调度器按任务分别记证据 —— 同一个组合在这个任务上
        做过，比在别的任务上做过更贴近这次要做的事。

        **没有计划就没有任务类型。** 那就不选 —— 选了执行器也没有配套的模型，等
        于跑一个没测过的组合。
        """
        if state.plan is None:
            raise AdaptiveSelectionError("没有计划就没有任务类型，选出来的执行器没有配套模型")
        task_type = state.plan.task_type
        decision = self.scheduler.decide(
            state.task_id,
            candidates=tuple(candidates),
            maximum_cost=self._limit(state.project_id, task_type),
        )
        if decision.chosen is None:
            raise AdaptiveSelectionError(
                f"项目 {state.project_id} 的 {task_type.value} 任务挑不出组合 —— {decision.reason}",
                [(str(item.choice), item.reason) for item in decision.rejected],
            )
        self._decisions[(state.project_id, task_type.value)] = decision
        return decision.executor

    async def resolve(self, *, project_id: str, task_type: TaskType) -> str:
        """挑一个模型 —— **从上一次的决定里出**。

        **它不会自己去挑。** 模型必须与执行器来自**同一格** —— 分开挑就会挑出一
        个没被一起测过的组合。

        **缓存里没有就是没有。** 这说明调用顺序不对（先问模型、后问执行器），或
        者选型压根没跑过。两种情况都不该猜一个模型顶上。
        """
        decision = self._decisions.get((project_id, task_type.value))
        if decision is None or decision.chosen is None:
            raise AdaptiveSelectionError(
                f"项目 {project_id} 的 {task_type.value} 任务还没有选过执行器 —— "
                "执行器与模型必须来自同一格，先选执行器再问模型"
            )
        return decision.model

    # -- 观测 ---------------------------------------------------------------

    def record(self, observation: Observation) -> None:
        """把一次运行结果记回历史 —— **历史变长的唯一入口**。

        **这是"自适应"落到实处的那个动作。** 跑完之后把结果记下来，下一次决定
        就能用上 —— 而不只是"查一张固定的表"。
        """
        self.scheduler.record(observation)

    @property
    def decisions(self) -> Mapping[tuple[str, str], SchedulingDecision]:
        """已经做出的决定 —— **按 (项目, 任务类型) 索引**。

        **留痕是为了事后能回答"这次为什么用这个组合"。** 决定不会自愈 —— 上一轮
        为什么选它，得能查。
        """
        return dict(self._decisions)

    def reason_for(self, project_id: str, task_type: TaskType) -> str:
        """上一次调度的理由 —— 人话。"""
        decision = self._decisions.get((project_id, task_type.value))
        if decision is None:
            return ""
        return decision.reason

    # -- 内部 ---------------------------------------------------------------

    def _limit(self, project_id: str, task_type: TaskType) -> Decimal | None:
        """这次的单次成本上限 —— **项目上限与规矩上限里更紧的那个**。

        **两个上限都是硬闸，取更紧的。** 项目说"这个任务最多花这么多"是项目的事；
        规矩说"单次最多花这么多"是全局的事。**只认其中一个，等于另一个形同虚
        设。**
        """
        policy_limit = self.scheduler.policy.maximum_cost
        project_limit = self.ceiling(project_id, task_type) if self.ceiling is not None else None
        if policy_limit is None:
            return project_limit
        if project_limit is None:
            return policy_limit
        return min(policy_limit, project_limit)


def load_observations(path: Path) -> tuple[Observation, ...]:
    """读一份实测记录 —— **运行时调度器的历史就是它**。

    **格式由 :func:`core.policy.scheduling.observation_to_dict` 说了算**，这里只
    管读回。两边各写一份转换，迟早会对不上，而对不上的那几列会静悄悄地变成 0。

    **读坏了就说坏了。** 把坏文件读成"没有历史"，调度器会退化成冷启动 —— 而冷启
    动在设了成本上限时是**直接拒绝**的；更糟的是没设上限时它会拿冷启动偏好当
    结论，看起来像是"调度过了"。
    """
    observations: list[Observation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as error:
            raise ValueError(f"观测文件 {path} 里有一行不是 JSON：{error}") from error
        if not isinstance(parsed, Mapping):
            raise ValueError(f"观测文件 {path} 里的每一行必须是对象")
        observations.append(observation_from_dict(dict(parsed)))
    return tuple(observations)
