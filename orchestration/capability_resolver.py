"""按实测能力矩阵选型 —— **执行器与模型一起挑，挑不出来就不跑**。

设计依据：docs/tasks.md ``TASK-P4-030``（交付物"执行器 × 模型的二维能力矩阵"；
验收条件"能力矩阵由实测数据得出，且**能指导自动选型**"）、``TASK-P4-020``（多模型
路由 —— 路由按"这个角色要多强的模型"挑）、R-111、I-17。

**一次选型，两个端口，一份决定。** 编排层要问两件事：这次用哪个执行器
(:class:`~orchestration.loop_a.AgentSelectorPort`)、这次用哪个模型
(:class:`~orchestration.loop_a.ModelResolverPort`)。**分开问两次，就会选出两个
互相矛盾的组合** —— 矩阵里最强的那格是"claude-code × 某个模型"，按执行器选
挑出 claude-code、按模型选挑出另一个模型，凑起来的组合**根本没测过**。

所以这里**一个对象实现两个端口**，共享同一份决定：先按矩阵定下"用哪一格"，
执行器与模型都从这一格里出。**这是"矩阵指导选型"最要紧的一步** —— 不是把矩阵
摆在一边让人看，是让矩阵**决定**跑什么。

**为什么失败要炸出来。** 矩阵里没有证据（没测过 / 全被成本上限挡了 / 候选表
里根本没有矩阵上的执行器）时，:func:`core.policy.capability.recommend` 会明说
"挑不出来"。这里把它变成异常，**不退回"随便挑一个"** —— 退回一个没测过的组
合，比不选更糟：钱照花、能力照降，而日志里什么异常都没有。

**挑不出来模型也不猜。** :meth:`MatrixCapabilitySelection.resolve` 在缓存里
找不到决定时同样报错 —— 那说明调用顺序不对（先问模型后问执行器），而不是"要不
就随便挑一个模型吧"。这正是 ``TASK-P4-020`` 立下的规矩：装上了路由就不许静默
兜底。

**依赖方向。** 这一层只认 :mod:`core.policy.capability` 里的**纯数据**与**纯函
数**（格子、矩阵、选型）。它不重算统计量，也不认识评测记录 —— "哪几格分不开"
是评测层算好的结论，作为**数据**带进来。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

from core.planning.roadmap import TaskType
from core.policy.capability import (
    CapabilityCell,
    CapabilityMatrix,
    SelectionOutcome,
    SelectionRequest,
    cell_from_dict,
    recommend,
)
from core.task.execution_loop import LoopAState

__all__ = [
    "CapabilitySelectionError",
    "CostCeiling",
    "MatrixCapabilitySelection",
    "load_capability_matrix",
]


class CapabilitySelectionError(Exception):
    """按实测矩阵挑不出能跑的组合 —— **挑不出来就不跑**。

    **它不是"退化到默认"。** 退到一个没测过的组合，等于拿"未知的风险"去换
    "看起来能跑" —— 而这件事恰好是能力矩阵要防的。

    :param reason: 人话 —— 挑不出来的具体原因。
    :param rejected: 被排除的格子与理由 —— **排除必须带理由**，否则事后没人
        知道是它不行、是它没测过，还是它压根不在候选里。
    """

    def __init__(self, reason: str, rejected: Sequence[tuple[CapabilityCell, str]] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rejected = tuple(rejected)


#: 单次成本上限的来源 —— **按项目与任务类型给出上限**，给 ``None`` 表示不限。
#:
#: **为什么不写死。** 预算不是全局的：甲项目有钱可以用贵的那格，乙项目快没钱
#: 了只能在它自己允许的档位里退一档（TASK-P4-020 的"上限来自项目余额"）。
CostCeiling = Callable[[str, TaskType], Decimal | None]

#: 选型请求的默认值 —— **用模块级单例当默认值**，避免每次构造都重造一个。
DEFAULT_SELECTION_REQUEST: SelectionRequest = SelectionRequest()


@dataclass(frozen=True, slots=True)
class MatrixCapabilitySelection:
    """按实测矩阵选执行器与模型 —— **一个对象，两个端口**。

    :param matrix: 实测出来的那张矩阵。
    :param request: 选型请求的默认值 —— 候选表由编排层给，所以**候选执行器
        不在这里**，而是在 :meth:`select` 的参数里；这里的 ``executors`` 只当
        兜底（编排层没给候选时用）。
    :param ceiling: 单次成本上限的来源 —— **不给就是不限**。
    """

    matrix: CapabilityMatrix
    request: SelectionRequest = DEFAULT_SELECTION_REQUEST
    ceiling: CostCeiling | None = None

    #: 已经做出的决定 —— **按 (项目, 任务类型) 索引**。
    #:
    #: **它是不参与相等与哈希的。** 两个选择器"选得一样不一样"看的是矩阵与请求，
    #: 不是它们各自记住了什么。
    _decisions: dict[tuple[str, str], SelectionOutcome] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    # -- 选型 ---------------------------------------------------------------

    def outcome_for(
        self,
        *,
        project_id: str,
        task_type: TaskType,
        candidates: Sequence[str] = (),
    ) -> SelectionOutcome:
        """按矩阵算一次选型 —— **纯函数**。

        **算得出的每一格都要有实测值。** 没有实测值的格子被排除（由
        :func:`core.policy.capability.recommend` 负责），所以这一步不会挑出
        一个没测过的组合。

        **成本上限按项目取。** 上限来自这个项目还剩多少额度，而不是某个全局
        数字；上限是**硬闸** —— 宁可这次不跑，也不要花掉不属于这个项目的钱。
        """
        query = replace(self.request, executors=tuple(candidates) or self.request.executors)
        if self.ceiling is not None:
            limit = self.ceiling(project_id, task_type)
            if limit is not None:
                query = replace(query, max_cost=limit)
        return recommend(self.matrix, query)

    # -- 两个端口 -----------------------------------------------------------

    async def select(self, state: LoopAState, candidates: Sequence[str]) -> str:
        """挑一个执行器 —— **挑不出来就报错**。

        **任务类型取自计划，不是取自状态。** 规划器写的是 :attr:`LoopAState.plan`，
        状态上那个 :attr:`LoopAState.task_type` 字段**没人写** —— 它停在默认值
        ``FEATURE``。用错了这里挑出来的格子，与 :meth:`resolve` 问的键就对不上，
        于是"执行器选了、模型挑不出来"。

        **没有计划就没有任务类型。** 那就不选 —— 因为选了执行器也没有配套的模
        型，等于跑一个没测过的组合。
        """
        if state.plan is None:
            raise CapabilitySelectionError("没有计划就没有任务类型，选出来的执行器没有配套模型")
        task_type = state.plan.task_type
        outcome = self.outcome_for(
            project_id=state.project_id,
            task_type=task_type,
            candidates=candidates,
        )
        chosen = outcome.chosen
        if chosen is None:
            raise CapabilitySelectionError(
                f"项目 {state.project_id} 的 {task_type.value} 任务挑不出组合 —— {outcome.reason}",
                [(item.cell, item.reason) for item in outcome.rejected],
            )
        self._decisions[(state.project_id, task_type.value)] = outcome
        return chosen.executor

    async def resolve(self, *, project_id: str, task_type: TaskType) -> str:
        """挑一个模型 —— **从上一次的决定里出**。

        **它不会自己去挑。** 模型必须与执行器来自**同一格** —— 分开挑就会挑出
        一个没测过的组合，那正是二维矩阵要防的事。

        **缓存里没有就是没有。** 这说明调用顺序不对（先问模型、后问执行器），
        或者选型压根没跑过。两种情况都不该猜一个模型顶上。
        """
        outcome = self._decisions.get((project_id, task_type.value))
        if outcome is None or outcome.chosen is None:
            raise CapabilitySelectionError(
                f"项目 {project_id} 的 {task_type.value} 任务还没有选过执行器 —— "
                "执行器与模型必须来自同一格，先选执行器再问模型"
            )
        return outcome.chosen.model

    # -- 观测 ---------------------------------------------------------------

    @property
    def decisions(self) -> Mapping[tuple[str, str], SelectionOutcome]:
        """已经做出的决定 —— **按 (项目, 任务类型) 索引**。

        **留痕是为了事后能回答"这次为什么用这个组合"。** 矩阵会更新，决定不会
        自愈 —— 上一轮为什么选它，得能查。
        """
        return dict(self._decisions)

    def reason_for(self, project_id: str, task_type: TaskType) -> str:
        """上一次选型的理由 —— 人话。"""
        outcome = self._decisions.get((project_id, task_type.value))
        if outcome is None:
            return ""
        return outcome.reason


def load_capability_matrix(path: Path) -> CapabilityMatrix:
    """读一份实测矩阵 —— **运行时拿它做选型**。

    **格式由评测层写、由这里读。** 一格长什么样只有一处定义
    （:func:`core.policy.capability.cell_to_dict`），两边各写一份转换，迟早会
    对不上，而对不上的那几列会静悄悄地变成 0。

    **读坏了就说读坏了。** 把坏文件读成"空矩阵"，选型会悄悄退化成"挑不出来"
    —— 那比直接报错难查得多。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("cells", [])
    if not isinstance(raw, list):
        raise ValueError(f"矩阵文件 {path} 里的 cells 必须是列表")
    cells: list[CapabilityCell] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError(f"矩阵文件 {path} 里的每一格必须是对象")
        cells.append(cell_from_dict(item))
    return CapabilityMatrix.of(cells)
