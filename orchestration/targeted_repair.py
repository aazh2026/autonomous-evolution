"""定向修复的编排 —— 分类完了，还得问全局账本。

设计依据：docs/design.md §6（"修复失败必须有次数、成本和时间上限"）；
docs/tasks.md TASK-P2-015（"自动定位失败原因并生成 Targeted 修复指令，
**修复次数与预算严格受全局配额限制**"）、TASK-P1-063（三重上限全局共享）、
R-098、I-04。

**这一层把两半接上。** :mod:`agents.diagnosis.failure_classifier` 负责
"哪儿坏了、改哪儿"，:class:`orchestration.repair.RepairCoordinator` 负责
"能不能修、修几次、花多少"。**配额只在后者那头生效** —— 分类器压根不知道
账本的存在，它给出的结论只是一句"这是语法错，改这三行"。

**为什么分类器不能自己问账本。** 分类是纯函数：同样的失败记录，同样的
结论，不该因为"额度还有没有"而变。**额度一变、结论就变，等于让"修什么"
取决于"还剩多少钱"** —— 那不是分类，那是调度。

**不能自动修的压根不占额度。** 额度是给"真的要修"这件事留的；没修就占一
格，等于凭空少了一截却没人真的修过。这条在
:class:`orchestration.repair.RepairCoordinator` 里已经实现（它先判归类再
问账本），这一层**不重复实现** —— 直接把诊断交给它。

**这一层不落状态。** 与 :mod:`orchestration.gates` 同一套做法：算结论、
给迁移，写状态的是调用方。两边都写就会有一边写丢。
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.diagnosis.failure_classifier import (
    Classification,
    RepairTarget,
    ToolFailure,
    classify,
)
from orchestration.repair import RepairCoordinator, RepairOutcome, RepairRequest


@dataclass(frozen=True, slots=True)
class TargetedRepairPlan:
    """一次定向修复的结论。

    :attr:`outcome` 是**配额判完之后**的结论 —— 它带着撞到的那一条上限
    （:attr:`RepairOutcome.limit`）与这次预留之后的全局用量
    （:attr:`RepairOutcome.usage`）。分类本身不管这些。
    """

    #: 归类结果。
    classification: Classification
    #: 配额判完之后的结论。
    outcome: RepairOutcome

    @property
    def allowed(self) -> bool:
        """准不准修。"""
        return self.outcome.allowed

    @property
    def targets(self) -> tuple[RepairTarget, ...]:
        """要改的地方。**被拒时也在** —— 挂起也要说得清为什么。"""
        return self.classification.targets

    @property
    def instruction(self) -> str:
        """排给 Agent 的施工说明 —— **配额批了才有**。"""
        task = self.outcome.repair_task
        if task is None:
            return ""
        return task.instruction


class TargetedRepairPlanner:
    """把归类接上有界修复。

    :param coordinator: 有界修复的协调器（:class:`orchestration.repair.
        RepairCoordinator`）。**一个项目一个实例** —— 它记着那本全局账。

    **它只做两件事**：归类，然后把归类结果翻译成协调器认的诊断问它。
    **配额怎么算、撞了哪一条上限、要不要落状态，都不是它的事。**
    """

    __slots__ = ("_coordinator",)

    def __init__(self, coordinator: RepairCoordinator) -> None:
        self._coordinator = coordinator

    @property
    def coordinator(self) -> RepairCoordinator:
        """协调器 —— 账在那儿。"""
        return self._coordinator

    async def plan(self, request: RepairRequest, *, failure: ToolFailure) -> TargetedRepairPlan:
        """归类 + 问配额。

        :param request: 这次修复的请求（谁、哪个项目、修过几次、花多少）。
        :param failure: 这次失败的机器可读记录。
        """
        classification = classify(failure)
        outcome = await self._coordinator.plan_diagnosed(
            request,
            diagnosis=classification.to_diagnosis(),
        )
        return TargetedRepairPlan(classification=classification, outcome=outcome)


__all__ = [
    "TargetedRepairPlan",
    "TargetedRepairPlanner",
]
