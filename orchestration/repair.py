"""有界自动修复的编排（**全局**三重上限）。

设计依据：docs/design.md §6（"修复失败必须有次数、成本和时间上限"）；
docs/tasks.md TASK-P1-063（"三重上限为全局共享（次数 + 成本 + 时间），不是每
任务独立"）、T1 ①（修复次数达上限后停止并置 ``BLOCKED``）、T1 ②（修复成本超
预算后停止）、T1 ③（修复总时长超限后停止）、T1 ④（并行两个任务时全局上限不会
被各自突破）；R-098, I-04。

**协调器做三件事，顺序不能换。**

1. **归类** —— 这次失败能不能自己修（能自动修 / 需人工 / 不可修复）。
2. **问全局账本** —— 还有没有额度（次数 + 成本 + 时间，**按项目共享**）。
3. **算迁移** —— 准就 ``VERIFYING → REPAIRING``，不准就 ``VERIFYING → BLOCKED``。

**为什么"全局共享"不是一句口号。** 状态机里已经有一条**每任务**的修复次数闸门
（:class:`core.task.state_machine.RepairBudgetExhaustedError`）—— 两个任务各修
3 次，看着都没超，合起来就是 6 次，而上限说的是 5 次。**每任务独立的上限不是
上限，是配额**；要真上限，就得有一份**被共享**的账本，而共享的东西得有人负责
记 —— 就是这个协调器。

**协调器只算迁移，不落状态**（与 :mod:`orchestration.gates` 同一套做法）：结论交
出去，写状态的是调用方。两边都写就会有一边写丢。

**三重上限是三样不同的东西，缺一不可。** 次数用尽说明这条路走不通了；次数不多
但每次很贵，同样会拖垮项目；**时间这条最容易漏** —— 一次修复只要还在"修"，
它就一直占着执行器，不设时限，一个跑不出来的任务能把整轮演进拖到天亮。

**不能自动修的失败压根不占额度。** 额度是给"真的要修"这件事留的；没修就占一
格，等于凭空少了一截却没人真的修过。

**"预留"是一个动作，不是"查询"加"占用"两步。** 两步之间只要有让出控制权的缝
隙（``await``），两个并行的任务就会各自看到"还有 3 次"然后各自占用 —— 总数突
破了上限，而**没有一个任务报错**。所以额度账本给的是一个原子的
:meth:`core.task.repair.RepairLedger.reserve`。

**成本是"预计"的，不是"已花"的。** 预算要在**动手之前**判，而动手之前没人知
道这次要花多少 —— 等花完了再判就晚了。所以账本记的是预计成本，调用方给的估
计来自上一轮的实际开销（:attr:`core.task.execution_loop.LoopAState.
execution_cost`）。**账本本身是单调的**：已记的量只会往上走，估低了也只是低估，
不会把已经花掉的抹掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from core.task.execution_loop import DEFAULT_MAX_REPAIRS, FailureDiagnosis, RepairTask
from core.task.repair import (
    FailureSignal,
    RepairLedger,
    RepairLimit,
    RepairLimits,
    RepairUsage,
    build_repair_task,
    classify,
)
from core.task.state_machine import (
    RepairBudgetExhaustedError,
    TaskAction,
    TaskState,
    TransitionContext,
    TransitionResult,
    apply_action,
)


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    """一次"要不要修"的结论。

    :attr:`transition` 是**授权**去做这次迁移 —— 不是"已经迁移了"。状态由调用
    方落库（与 :mod:`orchestration.gates` 同一套做法：门只算迁移）。

    :attr:`limit` 是**撞到的那一条上限**（被拒时）—— 调用方要据此决定是补预算、
    提配额还是认账停下。说不出撞的是哪一条，"修复停了"就是一句没法行动的话。

    :attr:`repair_task` 是要排的那次修复（准修时才有）。**重投时为空** ——
    任务已经在 ``REPAIRING``，不重复造任务（也不重复触发副作用）。
    """

    #: 准不准修。
    allowed: bool
    #: 人话理由。
    reason: str
    #: 归类结果（可自动修复 / 需人工 / 不可修复）。
    diagnosis: FailureDiagnosis
    #: 该走哪条迁移。
    transition: TransitionResult
    #: 撞到的上限（被拒时）。
    limit: RepairLimit | None = None
    #: 要排的修复任务（准修时才有）。
    repair_task: RepairTask | None = None
    #: 这次预留之后的全局用量（问过账本时才有）。
    usage: RepairUsage | None = None

    @property
    def blocked(self) -> bool:
        """是不是"停下来挂起"。"""
        return not self.allowed


@dataclass(frozen=True, slots=True)
class RepairRequest:
    """一次修复请求 —— 协调器问的所有东西都在这里。

    **为什么打包成一个对象。** 一次请求要带七八样东西：谁、哪个项目、现在什么
    状态、这次预计花多少、这个任务已经修过几次、每任务上限是多少。散成八个
    关键字参数，调用方迟早会记错其中一个 —— 而记错"已经修过几次"这种事，
    错的方向是**多修一次**，正是要防的那一种。

    :attr:`project_id` 是**额度共享的范围** —— 这就是"全局"两个字落地的地方。
    :attr:`repair_count` 与 :attr:`max_repairs` 是**每任务那条闸门**的输入。
    """

    task_id: str
    project_id: str
    current_state: TaskState
    #: 这次修复**预计**要花的钱。
    cost: Decimal = Decimal(0)
    #: 这个任务**已经**修过几次。
    repair_count: int = 0
    #: 每个任务最多修几次。
    max_repairs: int = DEFAULT_MAX_REPAIRS


class RepairCoordinator:
    """有界自动修复的协调器 —— **全局三重上限**在这里生效。

    :param ledger: 额度账本（:class:`core.task.repair.RepairLedger`）。**同一
        个项目要共用同一本** —— 每任务一本账就等于没有上限。
    :param limits: 全局上限（:class:`core.task.repair.RepairLimits`）。

    **协调器是有状态的**（它记着账），所以**一个项目一个实例**，而
    :class:`orchestration.loop_a.LoopAHandlers` 持有一个 —— 循环 A 里的每个
    任务都问同一本账，这正是"全局"两个字的落地方式。

    **它不落状态。** 协调器算出 ``VERIFYING → BLOCKED`` 还是
    ``VERIFYING → REPAIRING``，写状态的是调用方 —— 两边都写就会有一边写丢。
    """

    __slots__ = ("_ledger", "_limits")

    def __init__(self, ledger: RepairLedger, *, limits: RepairLimits) -> None:
        self._ledger = ledger
        self._limits = limits

    @property
    def limits(self) -> RepairLimits:
        """全局上限。"""
        return self._limits

    @property
    def ledger(self) -> RepairLedger:
        """额度账本。"""
        return self._ledger

    async def plan(self, request: RepairRequest, *, signal: FailureSignal) -> RepairOutcome:
        """给一次失败算结论 —— **归类在这一步**。

        :param request: 这次修复的请求。
        :param signal: 这次失败的原始信号（结构化，不解析人话）。
        """
        return await self._decide(request, classify(signal))

    async def plan_diagnosed(
        self,
        request: RepairRequest,
        *,
        diagnosis: FailureDiagnosis,
    ) -> RepairOutcome:
        """已经有诊断时算结论。

        **诊断是谁给的它不管。** 外部诊断器（:class:`orchestration.loop_a.
        DiagnoserPort`）给的诊断，与这里归类出来的诊断是同一种东西 —— 区别只在
        前者可能更细（它看过执行日志），而**能不能自己修**这个判断两者是同一
        个口径。
        """
        return await self._decide(request, diagnosis)

    async def _decide(self, request: RepairRequest, diagnosis: FailureDiagnosis) -> RepairOutcome:
        # 一、不能自动修的压根不占额度。
        if not diagnosis.auto_repairable:
            return self._blocked(request, diagnosis=diagnosis, reason=diagnosis.reason)

        # 二、每任务那条闸门（状态机里的）。**先判它** —— 它不占全局额度，顺序
        #    反了就会白占一格。
        try:
            repairing = apply_action(
                request.current_state,
                TaskAction.REPAIR,
                TransitionContext(
                    task_id=request.task_id,
                    repair_count=request.repair_count,
                    max_repairs=request.max_repairs,
                ),
            )
        except RepairBudgetExhaustedError as error:
            return self._blocked(
                request,
                diagnosis=diagnosis,
                reason=str(error),
                limit=RepairLimit.COUNT,
            )

        if not repairing.changed:
            # 重投：任务已经在 REPAIRING（这条迁移是幂等空操作）。**不重复修、
            # 不重复造任务**，也不占额度。
            return RepairOutcome(
                allowed=True,
                reason="任务已经在修复中 —— 重投，不重复修复",
                diagnosis=diagnosis,
                transition=repairing,
            )

        # 三、全局账本：**一次原子预留**。批了才记，不批一点都不记。
        reservation = await self._ledger.reserve(
            scope=request.project_id,
            cost=request.cost,
            limits=self._limits,
        )
        if not reservation.granted:
            return self._blocked(
                request,
                diagnosis=diagnosis,
                reason=reservation.reason,
                limit=reservation.limit,
                usage=reservation.usage,
            )

        # 四、造修复任务。
        repair_task = build_repair_task(
            task_id=request.task_id,
            attempt=request.repair_count + 1,
            diagnosis=diagnosis,
        )
        return RepairOutcome(
            allowed=True,
            reason=(
                f"允许第 {repair_task.attempt} 次修复"
                f"（全局已用 {reservation.usage.attempts}/{self._limits.max_attempts} 次）"
            ),
            diagnosis=diagnosis,
            transition=repairing,
            repair_task=repair_task,
            usage=reservation.usage,
        )

    def _blocked(
        self,
        request: RepairRequest,
        *,
        diagnosis: FailureDiagnosis,
        reason: str,
        limit: RepairLimit | None = None,
        usage: RepairUsage | None = None,
    ) -> RepairOutcome:
        """挂起 —— 算一条 ``→ BLOCKED`` 的迁移。

        **理由要说得清是哪一条上限。** "修复停了"没法行动；"全局成本超了"才
        知道该批预算还是该改口径。
        """
        blocked = apply_action(
            request.current_state,
            TaskAction.BLOCK,
            TransitionContext(task_id=request.task_id),
        )
        return RepairOutcome(
            allowed=False,
            reason=reason,
            diagnosis=diagnosis,
            transition=blocked,
            limit=limit,
            usage=usage,
        )


#: 协调器给外部用的默认上限。与每任务那条闸门的默认值一致 —— 不另立口径。
DEFAULT_LIMITS: Final[RepairLimits] = RepairLimits(max_attempts=DEFAULT_MAX_REPAIRS)


__all__ = [
    "DEFAULT_LIMITS",
    "RepairCoordinator",
    "RepairOutcome",
    "RepairRequest",
]
