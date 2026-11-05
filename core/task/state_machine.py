"""Task 状态机（14 态）。

设计依据：docs/design.md §6「建议的核心状态机」（14 个状态 + Re-plan / Re-evaluate
两个动作；"同一任务重复提交必须幂等"、"修复失败必须有次数、成本和时间上限"）、
docs/tasks.md TASK-P1-021。

关于迁移表的来源，需要说明一件事：design.md §6 的状态机只以 mermaid 图形式存在，
而该图在导出时**只保留了状态名，边（迁移）全部丢失**。因此本模块的迁移表是按
状态语义重建的，而不是从设计文档抄来的。重建依据：

- 状态名本身的语义（DISCOVERED → EVALUATING → APPROVED → PLANNED → READY → RUNNING …）
- §6 的四条实现细节（幂等、恢复、取消传播、修复三重上限）
- R-051（验收契约必须在 READY→RUNNING **之前**存在并绑定 task_id）
- R-034/R-035（defer 语义：DEFERRED 必须能重新评估）
- 阶段三的"重规划"需求（Re-plan → PLANNED）

**待裁决**：这张迁移表应回写进 design.md §6（用 mermaid 源码或文字表格），
避免实现与文档长期分叉。已记入 docs/tasks.md 第 6 节待裁决项。

三条硬约束：

1. **非法迁移抛错**，不做静默兜底。状态机出错必须炸出来 —— 静默跳过会让任务
   停在错误的态上，而错误的状态比崩溃难查得多。
2. **幂等**：目标状态等于当前状态时是合法空操作（``changed=False``）。
   消息重投、worker 重启后的重放都靠这条兜住。``changed`` 标志告诉调用方
   **不要重复执行副作用**。
3. **准入闸门**：进入 RUNNING 前必须已有绑定 ``task_id`` 的验收契约。
   这是 gate，不是事后报告 —— 引擎级强制，不依赖调用方自觉。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class TaskState(StrEnum):
    """14 个任务状态。"""

    DISCOVERED = "DISCOVERED"
    EVALUATING = "EVALUATING"
    DEFERRED = "DEFERRED"
    APPROVED = "APPROVED"
    PLANNED = "PLANNED"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    PASSED = "PASSED"
    REPAIRING = "REPAIRING"
    BLOCKED = "BLOCKED"
    INTEGRATING = "INTEGRATING"
    MERGED = "MERGED"
    COMPLETED = "COMPLETED"


class TaskAction(StrEnum):
    """任务动作。动作 → 目标状态是多对一的（REPLAN 与 PLAN 都指向 PLANNED）。"""

    EVALUATE = "evaluate"
    DEFER = "defer"
    APPROVE = "approve"
    PLAN = "plan"
    REPLAN = "re_plan"
    READY = "ready"
    START = "start"
    VERIFY = "verify"
    PASS = "pass"
    REPAIR = "repair"
    BLOCK = "block"
    INTEGRATE = "integrate"
    MERGE = "merge"
    COMPLETE = "complete"
    REEVALUATE = "re_evaluate"


class TaskStateError(Exception):
    """状态机错误的基类。"""


class IllegalTransitionError(TaskStateError):
    """非法迁移 —— 该状态下不允许这个动作。"""

    def __init__(self, current: TaskState, action: TaskAction) -> None:
        allowed = sorted(a.value for a in ALLOWED_ACTIONS[current])
        super().__init__(
            f"状态 {current.value} 下不允许动作 {action.value}"
            f"（允许的动作：{', '.join(allowed) if allowed else '无'}）"
        )
        self.current = current
        self.action = action
        self.allowed = allowed


class TransitionGateError(TaskStateError):
    """迁移本身合法，但前置条件未满足。"""

    def __init__(self, message: str, *, gate: str) -> None:
        super().__init__(message)
        self.gate = gate


class AcceptanceContractRequiredError(TransitionGateError):
    """进入 RUNNING 前必须已绑定验收契约（R-051）。"""

    def __init__(self, task_id: str) -> None:
        super().__init__(
            f"任务 {task_id} 在进入 RUNNING 前必须已有绑定 task_id 的验收契约",
            gate="acceptance_contract",
        )
        self.task_id = task_id


class RepairBudgetExhaustedError(TransitionGateError):
    """有界修复次数已用尽（§6「修复失败必须有次数、成本和时间上限」）。"""

    def __init__(self, task_id: str, used: int, limit: int) -> None:
        super().__init__(
            f"任务 {task_id} 的修复次数已达上限 {limit}（已用 {used}）",
            gate="repair_count",
        )
        self.task_id = task_id
        self.used = used
        self.limit = limit


# --------------------------------------------------------------------------
# 迁移表：源状态 → 动作 → 目标状态
# --------------------------------------------------------------------------

_TRANSITION_TABLE: Final[dict[TaskState, dict[TaskAction, TaskState]]] = {
    TaskState.DISCOVERED: {
        TaskAction.EVALUATE: TaskState.EVALUATING,
    },
    TaskState.EVALUATING: {
        TaskAction.DEFER: TaskState.DEFERRED,
        TaskAction.APPROVE: TaskState.APPROVED,
    },
    TaskState.DEFERRED: {
        TaskAction.REEVALUATE: TaskState.EVALUATING,
    },
    TaskState.APPROVED: {
        TaskAction.PLAN: TaskState.PLANNED,
        TaskAction.REEVALUATE: TaskState.EVALUATING,
    },
    TaskState.PLANNED: {
        TaskAction.READY: TaskState.READY,
        # 从 PLANNED 重新规划没有意义 —— 计划刚生成。此时的"重规划"请求
        # 应来自 READY 之后的某个态（计划已失效）。
    },
    TaskState.READY: {
        TaskAction.START: TaskState.RUNNING,
        TaskAction.REPLAN: TaskState.PLANNED,
        TaskAction.BLOCK: TaskState.BLOCKED,
    },
    TaskState.RUNNING: {
        TaskAction.VERIFY: TaskState.VERIFYING,
        TaskAction.BLOCK: TaskState.BLOCKED,
    },
    TaskState.VERIFYING: {
        TaskAction.PASS: TaskState.PASSED,
        TaskAction.REPAIR: TaskState.REPAIRING,
        TaskAction.BLOCK: TaskState.BLOCKED,
        TaskAction.REPLAN: TaskState.PLANNED,
    },
    TaskState.PASSED: {
        TaskAction.INTEGRATE: TaskState.INTEGRATING,
        TaskAction.BLOCK: TaskState.BLOCKED,
        TaskAction.REPLAN: TaskState.PLANNED,
    },
    TaskState.REPAIRING: {
        TaskAction.VERIFY: TaskState.VERIFYING,
        TaskAction.BLOCK: TaskState.BLOCKED,
    },
    TaskState.BLOCKED: {
        TaskAction.REEVALUATE: TaskState.EVALUATING,
        TaskAction.REPLAN: TaskState.PLANNED,
    },
    TaskState.INTEGRATING: {
        TaskAction.MERGE: TaskState.MERGED,
        TaskAction.REPAIR: TaskState.REPAIRING,
        TaskAction.BLOCK: TaskState.BLOCKED,
    },
    TaskState.MERGED: {
        TaskAction.COMPLETE: TaskState.COMPLETED,
    },
    TaskState.COMPLETED: {},
}

TRANSITIONS: Final[MappingProxyType[TaskState, MappingProxyType[TaskAction, TaskState]]] = (
    MappingProxyType(
        {state: MappingProxyType(actions) for state, actions in _TRANSITION_TABLE.items()}
    )
)

ALLOWED_ACTIONS: Final[MappingProxyType[TaskState, frozenset[TaskAction]]] = MappingProxyType(
    {state: frozenset(actions) for state, actions in _TRANSITION_TABLE.items()}
)

#: 终态 —— 没有出边。
TERMINAL_STATES: Final[frozenset[TaskState]] = frozenset(
    state for state, actions in _TRANSITION_TABLE.items() if not actions
)


@dataclass(frozen=True, slots=True)
class TransitionContext:
    """迁移的前置条件。

    这些是"准入闸门"，与迁移表本身无关 —— 迁移表说"能不能走"，上下文说
    "现在准不准走"。
    """

    task_id: str = ""
    #: 是否已有绑定该 task_id 的验收契约。进入 RUNNING 的必要条件（R-051）。
    acceptance_contract_bound: bool = False
    #: 已用掉的修复次数。
    repair_count: int = 0
    #: 修复次数上限。成本与时间上限由编排层负责（§6 三重上限）。
    max_repairs: int = 3


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """迁移结果。"""

    previous: TaskState
    current: TaskState
    action: TaskAction
    #: 是否真的发生了状态变化。``False`` 表示幂等空操作 —— 调用方**不应**
    #: 重复执行副作用。
    changed: bool

    @property
    def is_idempotent_noop(self) -> bool:
        return not self.changed


def target_of(action: TaskAction) -> TaskState:
    """动作的目标状态。"""
    for actions in _TRANSITION_TABLE.values():
        if action in actions:
            return actions[action]
    raise TaskStateError(f"未知动作 {action.value}")


def can_apply(current: TaskState, action: TaskAction) -> bool:
    """该状态下动作是否合法（只看迁移表，不看闸门）。"""
    return action in ALLOWED_ACTIONS[current]


def apply_action(
    current: TaskState,
    action: TaskAction,
    context: TransitionContext | None = None,
) -> TransitionResult:
    """执行一次状态迁移。

    - 目标状态等于当前状态 → 幂等空操作，``changed=False``（**先判这一条**）
    - 动作不在该状态的允许集合里 → :class:`IllegalTransitionError`
    - 闸门不满足 → :class:`TransitionGateError`

    幂等检查必须排在允许集合检查**之前**：消息重投时，任务已经在目标态
    （例如任务已 RUNNING 又收到一次 START），此时再查允许集合会直接抛错 ——
    而重投恰恰是最需要被容忍的情况。
    """
    target = target_of(action)

    if target is current:
        # 幂等：重复投递同一动作不报错，也不重复触发副作用。
        return TransitionResult(previous=current, current=current, action=action, changed=False)

    if action not in ALLOWED_ACTIONS[current]:
        raise IllegalTransitionError(current, action)

    ctx = context or TransitionContext()
    _check_gates(target, ctx)

    return TransitionResult(previous=current, current=target, action=action, changed=True)


def _check_gates(target: TaskState, context: TransitionContext) -> None:
    """准入闸门。"""
    if target is TaskState.RUNNING and not context.acceptance_contract_bound:
        raise AcceptanceContractRequiredError(context.task_id)

    if target is TaskState.REPAIRING and context.repair_count >= context.max_repairs:
        raise RepairBudgetExhaustedError(
            task_id=context.task_id,
            used=context.repair_count,
            limit=context.max_repairs,
        )


def all_states() -> tuple[TaskState, ...]:
    """全部 14 个状态。"""
    return tuple(TaskState)


def legal_actions(current: TaskState) -> frozenset[TaskAction]:
    """某状态下允许的动作集合。"""
    return ALLOWED_ACTIONS[current]


def happy_path() -> tuple[TaskState, ...]:
    """从 DISCOVERED 到 COMPLETED 的一条完整合法路径。

    供测试与文档引用，避免各处手写路径时写错。
    """
    return (
        TaskState.DISCOVERED,
        TaskState.EVALUATING,
        TaskState.APPROVED,
        TaskState.PLANNED,
        TaskState.READY,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.PASSED,
        TaskState.INTEGRATING,
        TaskState.MERGED,
        TaskState.COMPLETED,
    )


__all__ = [
    "ALLOWED_ACTIONS",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "AcceptanceContractRequiredError",
    "IllegalTransitionError",
    "RepairBudgetExhaustedError",
    "TaskAction",
    "TaskState",
    "TaskStateError",
    "TransitionContext",
    "TransitionGateError",
    "TransitionResult",
    "all_states",
    "apply_action",
    "can_apply",
    "happy_path",
    "legal_actions",
    "target_of",
]
