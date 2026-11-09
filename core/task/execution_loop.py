"""循环 A（工程执行循环）的契约（纯领域层）。

设计依据：docs/design.md §4（循环 A 的节点链、"不能所有任务都走相同的验收
流程"）、§6（任务状态机与"修复失败必须有次数、成本和时间上限"）；
docs/tasks.md TASK-P1-060。

**这一层只回答"每一步的输入是什么、产出是什么"。** 它不知道底下是
LangGraph、不知道执行器是谁、也不知道验证引擎怎么跑 —— 那些在
:mod:`orchestration.loop_a`。分开的理由与 :mod:`core.planning.stages` 一样：
契约写在编排层，别的调用方（非 LangGraph 的那条路）就用不上；而"这一步的
产出对不对"是**领域知识**，写在领域层才能被单独校验与复用。

**三个刻意的选择。**

**一、状态里没有墙钟时间。** 状态要被写进 checkpoint，还要被回放 runner
（TASK-P1-065）原样重放 —— 里面出现一次 ``datetime.now()``，"同一脚本回放
两次产生完全相同的记录"这条就永远做不到。时间戳属于**事件**，不属于**状态**：
事件按序号排就够了（``ExecutionEvent.sequence``）。

**二、状态里的每一样东西都是 Pydantic 模型或枚举。** checkpoint 的序列化器
（msgpack）只放行白名单里的类型，而白名单是从**状态模式本身**算出来的
（:mod:`orchestration.langgraph.serialization`）：Pydantic 模型与枚举收，
**dataclass 不收**。所以验证结论在状态里是 :class:`VerificationOutcome`
（Pydantic），而不是 :class:`core.task.outcome.VerificationReport`
（dataclass）—— 后者存进去读回来会**退化成 dict**：字段名还在，类型没了，
而"字段名还在"这件事恰恰最危险：它看起来好好的。

**三、任务状态由状态机说了算。** 编排层不自己判断"这一步能不能走"——它调
:func:`core.task.state_machine.apply_action`，非法迁移当场炸出来。修复次数
上限也是那里的闸门（:class:`core.task.state_machine.RepairBudgetExhaustedError`），
而不是编排层自己数次数 —— 两处各数一次，早晚会对不上。

**验收重点按任务类型走。** design.md §4 明写"不能所有任务都走相同的验收
流程"：重构强调行为保持与回归测试，性能优化比较优化前后的基准，安全修复
需要漏洞复测。所以 :class:`ExecutionPlan` 带一个 :attr:`ExecutionPlan.acceptance_focus`，
不填就按任务类型从 :data:`core.planning.roadmap.ACCEPTANCE_FOCUS` 取 ——
**由类型决定，不由任务自己说**。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.planning.roadmap import ACCEPTANCE_FOCUS, TaskType
from core.task.outcome import (
    ExecutionStatus,
    TaskOutcome,
    VerificationReport,
    VerificationVerdict,
)
from core.task.state_machine import TaskAction, TaskState, TransitionContext, apply_action


class LoopANode(StrEnum):
    """循环 A 的十五个节点 —— **顺序就是设计文档 §4 的顺序**。

    枚举成员的声明顺序与 :data:`LOOP_A_ORDER` 一致；两者不一致时
    ``tests/unit/orchestration/test_loop_a.py`` 会转红。

    三个带问号的节点（``ACCEPTANCE_PASSED`` / ``CAN_AUTO_REPAIR``，
    以及合并闸门）是**分支点** —— 它们的出边由判定结果决定，不是固定的。
    """

    TASK_READY = "task_ready"
    TASK_PLANNER = "task_planner"
    SELECT_CODING_AGENT = "select_coding_agent"
    CREATE_ISOLATED_WORKSPACE = "create_isolated_workspace"
    AGENT_EXECUTES = "agent_executes"
    COLLECT_DIFF_AND_ARTIFACTS = "collect_diff_and_artifacts"
    INDEPENDENT_VERIFICATION = "independent_verification"
    ACCEPTANCE_PASSED = "acceptance_passed"
    INTEGRATION_AND_MERGE_GATE = "integration_and_merge_gate"
    FAILURE_DIAGNOSIS = "failure_diagnosis"
    CAN_AUTO_REPAIR = "can_auto_repair"
    CREATE_REPAIR_TASK = "create_repair_task"
    ESCALATE_OR_BLOCK = "escalate_or_block"
    UPDATE_PROJECT_STATE = "update_project_state"
    EXECUTION_COMPLETE = "execution_complete"


#: 十五个节点的固定次序 —— 与 design.md §4 的节点链逐字对应。
LOOP_A_ORDER: Final[tuple[LoopANode, ...]] = (
    LoopANode.TASK_READY,
    LoopANode.TASK_PLANNER,
    LoopANode.SELECT_CODING_AGENT,
    LoopANode.CREATE_ISOLATED_WORKSPACE,
    LoopANode.AGENT_EXECUTES,
    LoopANode.COLLECT_DIFF_AND_ARTIFACTS,
    LoopANode.INDEPENDENT_VERIFICATION,
    LoopANode.ACCEPTANCE_PASSED,
    LoopANode.INTEGRATION_AND_MERGE_GATE,
    LoopANode.FAILURE_DIAGNOSIS,
    LoopANode.CAN_AUTO_REPAIR,
    LoopANode.CREATE_REPAIR_TASK,
    LoopANode.ESCALATE_OR_BLOCK,
    LoopANode.UPDATE_PROJECT_STATE,
    LoopANode.EXECUTION_COMPLETE,
)


#: 修复次数的默认上限。与 :class:`core.task.state_machine.TransitionContext`
#: 的默认值一致 —— 编排层不另立一套口径。
DEFAULT_MAX_REPAIRS: Final[int] = 3


class FailureCategory(StrEnum):
    """失败归类 —— 这一类决定"能不能自己修"。

    三类而不是两类，是因为"不可修复"与"该由人来看"是两种不同的后续动作：
    前者可以自动放弃，后者得挂起等审批。混成一种，系统就会把授权问题
    当成技术失败一直修，或者反过来，把网络抖动当成要人介入。
    """

    #: 能自动修。
    AUTO_REPAIRABLE = "auto_repairable"
    #: 得人来看（授权、预算、口径这类事系统不该自己拍板）。
    NEEDS_HUMAN = "needs_human"
    #: 修不了 —— 再修也是白修。
    UNREPAIRABLE = "unrepairable"


class ExecutionPlan(BaseModel):
    """任务规划器的产物 —— 给 Agent 的施工说明。

    **验收重点不填就按任务类型补上。** design.md §4 明写"不能所有任务都走
    相同的验收流程"：重构强调行为保持与回归测试，性能优化比较优化前后的
    基准，安全修复需要漏洞复测。所以重点由**类型**决定（见
    :data:`core.planning.roadmap.ACCEPTANCE_FOCUS`），而不是每个任务自己说
    —— 自己说的话，任务就会挑对自己有利的那个重点。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    task_type: TaskType = TaskType.FEATURE
    steps: tuple[str, ...] = ()
    #: 这一类任务要拿什么来证明它做成了。
    acceptance_focus: str = ""
    timeout_seconds: int = Field(default=1800, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _fill_acceptance_focus(cls, data: object) -> object:
        """没写验收重点就按任务类型补上。

        **为什么是 ``before`` 而不是 ``after``。** ``mode="after"`` 的校验器
        返回的不是 ``self`` 时，Pydantic 在 ``__init__`` 那条路上会**把返回值
        丢掉** —— 只有 ``model_validate`` 才认它。于是同一个模型会因构造方式
        不同而有两副面孔：``ExecutionPlan(...)`` 造出来的验收重点是空的，
        ``ExecutionPlan.model_validate(...)`` 造出来的才是填好的。``before``
        在字段填充之前改输入，两条路走的是同一段逻辑。

        **只在该填的时候填。** 已经写了（非空字符串）就认它；写了但不是字符串
        就原样交回去，让 Pydantic 自己报类型错 —— 静默改掉一个类型错误的输入
        比报错更糟。
        """
        if not isinstance(data, Mapping):
            return data
        if "acceptance_focus" in data:
            raw_focus = data["acceptance_focus"]
            if not isinstance(raw_focus, str) or raw_focus.strip():
                return data
        raw_type = data.get("task_type", TaskType.FEATURE)
        try:
            task_type = TaskType(raw_type)
        except (TypeError, ValueError):
            return data
        filled: dict[str, object] = {str(key): value for key, value in data.items()}
        filled["acceptance_focus"] = ACCEPTANCE_FOCUS[task_type]
        return filled


class FailureDiagnosis(BaseModel):
    """失败诊断的产物：为什么这样，以及能不能自己修。

    :attr:`repair_hint` 是"该怎么修" —— 它是**建议**，不是命令：能不能用
    这条建议由 :attr:`category` 决定，而次数够不够由状态机的闸门决定。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(min_length=1)
    category: FailureCategory
    detail: str = ""
    #: 给下一轮执行的施工建议。
    repair_hint: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def auto_repairable(self) -> bool:
        """能不能走自动修复。"""
        return self.category is FailureCategory.AUTO_REPAIRABLE


class RepairTask(BaseModel):
    """一次修复的施工说明。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    #: 第几次修复。从 1 起算。
    attempt: int = Field(ge=1)
    instruction: str = Field(min_length=1)
    diagnosis: FailureDiagnosis


class WorkspaceInfo(BaseModel):
    """隔离工作区的信息。

    **工作区 id 与路径是两样东西。** id 是**授权**用的（授权五要素之一是
    ``workspace_id``），路径是**干活**用的 —— 授权不能拿路径当凭据，因为
    路径会变，id 不会。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    branch: str = Field(min_length=1)


class CollectedArtifacts(BaseModel):
    """收集到的改动与产物。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    branch: str = ""
    commit: str | None = None
    changed_files: int = Field(default=0, ge=0)
    diff_ref: str = ""


class MergeOutcome(BaseModel):
    """集成与合并的结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merged: bool
    commit: str = ""
    #: 没合并上的理由 —— 合并失败要进诊断，而诊断需要知道为什么。
    reason: str = ""


class VerificationOutcome(BaseModel):
    """独立验证结论的**可持久化**那一份。

    **为什么不是 :class:`core.task.outcome.VerificationReport`。** 那个是
    dataclass，而 checkpoint 的 msgpack 白名单只收 Pydantic 模型与枚举 ——
    dataclass 存进去读回来会退化成 dict（字段名还在，类型没了）。这里用
    Pydantic 模型装同一份内容，:meth:`to_report` 再把它变回领域层的结论类型。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: VerificationVerdict
    reasons: tuple[str, ...] = ()
    checks: tuple[tuple[str, bool], ...] = ()

    @classmethod
    def from_report(cls, report: VerificationReport) -> VerificationOutcome:
        """从领域层的结论转过来。"""
        return cls(verdict=report.verdict, reasons=report.reasons, checks=report.checks)

    def to_report(self) -> VerificationReport:
        """转回领域层的结论类型（判定时要用）。"""
        return VerificationReport(verdict=self.verdict, reasons=self.reasons, checks=self.checks)

    @property
    def passed(self) -> bool:
        """验证是否通过。"""
        return self.verdict is VerificationVerdict.PASSED


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """一次执行会话的落库记录。

    **授权快照跟着会话一起落库。** 不是为了运行，是为了**事后审计**："当时
    授权的是什么"必须能查出来 —— 只存在于内存里的授权，出事之后什么都证明
    不了。

    :attr:`task_id` 是任务行的 **UUID 字符串形式**：``agent_session.task_id``
    是指向 ``task.id`` 的外键，而任务表的主键是 UUID。它不是任务标题，也不是
    任务编号。
    """

    project_id: str
    task_id: str
    agent_id: str
    executor: str
    #: 执行器侧的会话标识。
    session_id: str
    authorization: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EventRecord:
    """一步执行的落库事件。

    **事件是"这一步做完了"的记录，不是状态。** 任务当前在哪个态，看状态；
    这一步做了什么、经过了哪些任务状态，看事件。
    """

    project_id: str
    task_id: str
    agent_id: str
    #: 落库的 ``agent_session`` 行 id；开不出会话的那几步是 ``None``。
    session_ref: str | None
    #: 执行器侧的会话标识。
    session_id: str
    event_type: str
    #: 会话内的顺序号 —— 幂等消费与回放都依赖它。
    sequence: int
    payload: Mapping[str, object] = field(default_factory=dict)
    causation_id: str | None = None
    correlation_id: str | None = None


@runtime_checkable
class SessionJournal(Protocol):
    """执行轨迹的落库口。

    **为什么是一个端口而不是直接写库。** 领域层不该知道数据库存在（分层：
    ``infrastructure → core``）；而"每一步都要留痕"这条规则是领域规则 ——
    所以规则写在领域层，写库的实现放在基础设施层（:mod:`infrastructure.
    database.execution_journal`）。

    **写失败必须炸出来。** 审计写入失败要 fail-closed（design.md §3.5D）——
    留痕写不进去还往下跑，等于这次执行没有痕迹。
    """

    async def open_session(self, record: SessionRecord) -> str:
        """开一个执行会话，返回它的行 id。"""
        ...

    async def record_event(self, event: EventRecord) -> None:
        """写一条执行事件。"""
        ...


class LoopAState(BaseModel):
    """循环 A 累积的状态。

    **它是一个 Pydantic 模型，不是一个 dict** —— 因为它要被写进 checkpoint：
    一个"看起来像 dict、其实字段随时间变"的字典存下去就再也读不出类型了。

    **任务状态由状态机推进，编排层不自己改。** :attr:`task_state` 只能通过
    :func:`advance_task_state` 变。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    project_id: str = ""
    agent_id: str = ""
    title: str = ""
    task_type: TaskType = TaskType.FEATURE
    #: 任务在工程流程里的位置 —— 由状态机推进。
    task_state: TaskState = TaskState.READY
    #: 是否已有绑定该 task_id 的验收契约。进入 RUNNING 的准入条件（R-051）。
    contract_bound: bool = False
    #: 修复次数上限。成本与时间上限由编排层负责（§6 三重上限，TASK-P1-063）。
    max_repairs: int = Field(default=DEFAULT_MAX_REPAIRS, ge=0)
    #: 上一轮执行花了多少钱。**修复预算按它估** —— 下一轮大约也是这个数。
    #:
    #: **是估计，不是账单。** 预算要在动手之前判，而动手之前没人知道这次要花
    #: 多少（等花完了再判就晚了）。账本记的是预计成本，已记的量只会往上走 ——
    #: 估低了是低估，不会把已经花掉的抹掉。
    execution_cost: Decimal = Field(default=Decimal(0), ge=0)

    plan: ExecutionPlan | None = None
    #: 选中的执行器标识。
    executor: str = ""
    workspace: WorkspaceInfo | None = None
    #: 执行器侧的会话标识。
    session_id: str = ""
    #: 落库的 ``agent_session`` 行 id。
    session_ref: str = ""
    #: 第几次尝试 —— 首跑是 1，每次返修 +1。
    attempt: int = Field(default=1, ge=1)
    execution_status: ExecutionStatus | None = None
    artifacts: CollectedArtifacts | None = None
    verification: VerificationOutcome | None = None
    #: 执行层的判定结果（由 :func:`core.task.outcome.resolve_outcome` 给出）。
    outcome: TaskOutcome | None = None
    #: 验收是否通过 —— 判定过了没有。
    accepted: bool = False
    merge: MergeOutcome | None = None
    diagnosis: FailureDiagnosis | None = None
    #: 诊断说这类失败能不能自己修。
    repair_allowed: bool = False
    repair_task: RepairTask | None = None
    #: 已用掉的修复次数。
    repair_count: int = Field(default=0, ge=0)
    #: 修复次数上限是否已用尽。
    repair_exhausted: bool = False
    #: 挂起给人的理由。
    blocked_reason: str = ""
    #: 已写进轨迹的事件数 —— 事件的顺序号由它派生。
    event_sequence: int = Field(default=0, ge=0)
    #: 走过的节点（按顺序；返修时 ``AGENT_EXECUTES`` 会再出现一次）。
    visited_nodes: tuple[LoopANode, ...] = ()


def advance_task_state(state: LoopAState, action: TaskAction) -> LoopAState:
    """走一步任务状态迁移，返回**新的**状态。

    **非法迁移抛错，不做静默兜底。** 状态机出错必须炸出来 —— 静默跳过会让
    任务停在错误的态上，而错误的状态比崩溃难查得多。

    **修复次数上限在这里生效。** :attr:`LoopAState.repair_count` 传进闸门，
    达上限时 :class:`core.task.state_machine.RepairBudgetExhaustedError`
    当场抛出来 —— 编排层接住它，转入升级处理。
    """
    context = TransitionContext(
        task_id=state.task_id,
        acceptance_contract_bound=state.contract_bound,
        repair_count=state.repair_count,
        max_repairs=state.max_repairs,
    )
    result = apply_action(state.task_state, action, context)
    return state.model_copy(update={"task_state": result.current})


def record_node(state: LoopAState, node: LoopANode) -> LoopAState:
    """把"走到哪个节点了"记进状态。

    **节点可以重复出现。** 返修那一轮会再走一遍 ``AGENT_EXECUTES`` ——
    所以这是"走过的顺序"，不是"走过的集合"。
    """
    return state.model_copy(update={"visited_nodes": (*state.visited_nodes, node)})


__all__ = [
    "DEFAULT_MAX_REPAIRS",
    "LOOP_A_ORDER",
    "CollectedArtifacts",
    "EventRecord",
    "ExecutionPlan",
    "FailureCategory",
    "FailureDiagnosis",
    "LoopANode",
    "LoopAState",
    "MergeOutcome",
    "RepairTask",
    "SessionJournal",
    "SessionRecord",
    "VerificationOutcome",
    "WorkspaceInfo",
    "advance_task_state",
    "record_node",
]
