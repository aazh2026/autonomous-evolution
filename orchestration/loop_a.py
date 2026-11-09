"""循环 A（工程执行循环）的编排（LangGraph）。

设计依据：docs/design.md §4（循环 A 的节点链）、docs/tasks.md TASK-P1-060。

    Task Ready → Task Planner → Select Coding Agent → Create Isolated Workspace
    → Agent Executes → Collect Diff & Artifacts → Independent Verification
    → Acceptance Passed? → Integration & Merge Gate → Failure Diagnosis
    → Can Auto Repair? → Create Repair Task → Escalate/Block
    → Update Project State → Execution Complete

**这张图只做三件事：按次序调用各步、把状态存下来、把每一步写进轨迹。**
每一步的契约在 :mod:`core.task.execution_loop` —— 那里才是"这一步的产出对不对"
的判据所在。

**为什么契约不放在图里。** LangGraph **不校验**状态：把一个整数字段写成字符串，
它照跑不误（``tests/unit/orchestration/test_decision_graph.py`` 里有盯着这件事的
用例）。编排器要是对状态负责，就得在每个节点里重复一遍类型判断 —— 而那些判断
是**领域知识**，写在领域层才能被单独校验与复用。图只负责**顺序**与**持久化**。

**判定权在验证引擎，不在执行器。** "Agent Executes" 之后的每一句结论都由
:func:`core.task.outcome.resolve_outcome` 给出：它只看**执行器报的状态**与
**独立验证的结论**，不看执行器的自我报告（design.md §3.4）。所以"Agent 说它做完了"
永远不等于"任务完成"。

**三条分支，每一个都有明确的去处。**

======================  ======================================================
节点                    分支
======================  ======================================================
Acceptance Passed?      通过 → 合并闸门；没通过 → 失败诊断
Integration & Merge     合上了 → 回写项目状态；没合上 → 失败诊断
Can Auto Repair?        能修 → 造修复任务；不能 → 升级挂起
======================  ======================================================

**修复上限有两条，缺一不可。** 每任务那条由状态机守（``VERIFYING → REPAIRING``
那条迁移上挂着 ``RepairBudgetExhaustedError`` 闸门）—— 计数只有一处，两处各数
一次早晚会对不上。全局那条由 :class:`orchestration.repair.RepairCoordinator` 守
—— **按项目共享的次数 + 成本 + 时限**（TASK-P1-063）。每任务独立的上限不是
上限，是配额：两个任务各修 3 次，看着都没超，合起来就是 6 次，而上限说的是 5
次。任一条响了 → 不再自动重试，转入升级挂起（``Escalate/Block``）。

**为什么每一步都写事件。** design.md §3.5D 要求审计写入 fail-closed，而
"每一步都留痕"是事后能回答"它到底走到哪一步、为什么停在这"的前提。事件按
顺序号排，状态里不存墙钟时间 —— 这样同一份脚本回放两次，产生的是同一串事件
（TASK-P1-065 的前提）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, cast, runtime_checkable

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from core.task.execution_loop import (
    DEFAULT_MAX_REPAIRS,
    LOOP_A_ORDER,
    CollectedArtifacts,
    EventRecord,
    ExecutionPlan,
    FailureDiagnosis,
    LoopANode,
    LoopAState,
    MergeOutcome,
    SessionJournal,
    SessionRecord,
    VerificationOutcome,
    WorkspaceInfo,
    advance_task_state,
    record_node,
)
from core.task.outcome import ExecutionStatus, TaskOutcome, VerificationReport, resolve_outcome
from core.task.state_machine import (
    AcceptanceContractRequiredError,
    TaskAction,
    TaskState,
)
from executors.base import (
    AgentSession,
    ExecutionHandle,
    ExecutionResult,
    ExecutionTask,
    SessionConfig,
)
from orchestration.repair import RepairCoordinator, RepairRequest

#: 一轮修复要走过的节点数 —— 用来算 LangGraph 的递归上限。
#:
#: agent_executes → collect_diff_and_artifacts → independent_verification
#: → acceptance_passed → failure_diagnosis → can_auto_repair → create_repair_task
_REPAIR_CYCLE_NODES: Final[int] = 7


class LoopAExecutionError(RuntimeError):
    """循环 A 的编排被违反 —— 比如选了个不存在的执行器。"""


@dataclass(frozen=True, slots=True)
class _StatusOnly:
    """只暴露状态 —— 判定函数刻意只看得见这一个成员。

    **这是 :class:`core.task.outcome.StatusSource` 协议的形状**：判定过程
    拿不到执行器的自我报告，所以"Agent 说自己做完了"在类型层面就够不着。
    """

    status: ExecutionStatus


# --------------------------------------------------------------------------
# 处理器端口
# --------------------------------------------------------------------------


@runtime_checkable
class TaskPlannerPort(Protocol):
    """任务规划器：任务 → 施工计划。"""

    async def plan(self, state: LoopAState) -> ExecutionPlan: ...


@runtime_checkable
class AgentSelectorPort(Protocol):
    """执行器选择器：在候选里挑一个。

    **候选是编排层给的。** 选择器不能凭空变出一个执行器 —— 它只能在
    编排层手里那一份里挑。挑不出来的后果见 :func:`_select_coding_agent`。
    """

    async def select(self, state: LoopAState, candidates: Sequence[str]) -> str: ...


@runtime_checkable
class WorkspacePort(Protocol):
    """隔离工作区的开辟者。"""

    async def create(self, state: LoopAState) -> WorkspaceInfo: ...


@runtime_checkable
class ExecutorPort(Protocol):
    """执行器 —— 编排层只需要这四个方法。

    **为什么不是整个 :class:`executors.base.CodingAgentAdapter`。** 适配器有
    七个方法，编排层一个都不用（事件流、中断、终止属于别处）。端口小一点，
    打桩就容易一点 —— 而"用打桩跑通整图"正是 T1 的要求。
    """

    @property
    def name(self) -> str: ...

    async def create_session(self, config: SessionConfig) -> AgentSession: ...

    async def execute(self, task: ExecutionTask) -> ExecutionHandle: ...

    async def collect_result(self, session_id: str) -> ExecutionResult: ...


@runtime_checkable
class ArtifactCollectorPort(Protocol):
    """产物收集者：把执行结果与工作区里的改动收上来。"""

    async def collect(self, state: LoopAState, result: ExecutionResult) -> CollectedArtifacts: ...


@runtime_checkable
class VerificationPort(Protocol):
    """独立验证引擎 —— **执行器之外的另一个东西**。

    design.md §3.5B：验证引擎独立于执行器。端口放在编排层而不是执行器后面，
    就是为了让"谁来验"这件事在结构上与"谁来干"分开。
    """

    async def verify(self, state: LoopAState) -> VerificationReport: ...


@runtime_checkable
class DiagnoserPort(Protocol):
    """失败诊断器：为什么会这样，以及能不能自己修。"""

    async def diagnose(self, state: LoopAState) -> FailureDiagnosis: ...


@runtime_checkable
class MergeGatePort(Protocol):
    """集成与合并闸门。"""

    async def merge(self, state: LoopAState) -> MergeOutcome: ...


@runtime_checkable
class ProjectStatePort(Protocol):
    """项目状态的回写者。"""

    async def update(self, state: LoopAState) -> None: ...


@dataclass(frozen=True, slots=True)
class LoopAHandlers:
    """循环 A 的各步处理器。

    **为什么是注入而不是内部 new。** 测试要能拿打桩 Agent 与打桩验证引擎跑通
    整图（T1），而真实实现要走执行器适配器与验证引擎。两者的差别只在这些
    对象里，图本身不该知道。

    :attr:`executors` 是**候选表**（标识 → 执行器）。选择器只能在这张表里挑。

    :attr:`repair_coordinator` 是**修复额度的账**（TASK-P1-063）。它是必填的 ——
    "可不带"意味着有人会不带，而不带就等于没有全局上限。它**按项目共用一个**
    ：两个任务各修 3 次，看着都没超，合起来 6 次，而上限说的是 5 次。
    """

    planner: TaskPlannerPort
    selector: AgentSelectorPort
    workspace: WorkspacePort
    collector: ArtifactCollectorPort
    verifier: VerificationPort
    diagnoser: DiagnoserPort
    merge_gate: MergeGatePort
    project_state: ProjectStatePort
    journal: SessionJournal
    executors: Mapping[str, ExecutorPort]
    repair_coordinator: RepairCoordinator


# --------------------------------------------------------------------------
# 留痕
# --------------------------------------------------------------------------


async def _record(
    state: LoopAState,
    handlers: LoopAHandlers,
    node: LoopANode,
    *,
    task_states: tuple[TaskState, ...] = (),
    payload: Mapping[str, object] | None = None,
    session_ref: str | None = None,
) -> dict[str, object]:
    """把这一步写进执行轨迹，并给出这一步的状态更新。

    **事件里带"这一步经过了哪些任务状态"。** 事后只看轨迹就能还原任务的
    状态迁移序列 —— 而轨迹是异常之后**还在**的那个东西。（合并闸门一步走过
    两个状态：``INTEGRATING`` 与 ``MERGED``。）

    **``session_ref`` 是"这一步属于哪一次执行"。** 不给就用状态里记着的那一个
    —— 而"起一次执行"那一步恰恰是**刚**开出会话的那一步：它要把新会话的
    行 id 显式传进来，否则这条事件会挂到上一次执行的会话上（第一轮则挂不上
    任何会话）。
    """
    sequence = state.event_sequence + 1
    visited = record_node(state, node)
    await handlers.journal.record_event(
        EventRecord(
            project_id=state.project_id,
            task_id=state.task_id,
            agent_id=state.agent_id,
            session_ref=session_ref or state.session_ref or None,
            session_id=state.session_id,
            event_type=node.value,
            sequence=sequence,
            payload={
                "node": node.value,
                "task_states": [item.value for item in task_states],
                **dict(payload or {}),
            },
        )
    )
    return {
        "event_sequence": sequence,
        "visited_nodes": visited.visited_nodes,
        "task_state": task_states[-1] if task_states else state.task_state,
    }


# --------------------------------------------------------------------------
# 各步
# --------------------------------------------------------------------------


async def _task_ready(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """任务就绪 —— 确认它停在 READY 上，且验收契约已绑定。

    **没有验收契约就不许开始。** R-051：契约是 READY→RUNNING 的准入闸门，
    不是事后报告。这里先查一遍（早失败，别白开工作区），状态机里还有一道
    （:class:`core.task.state_machine.AcceptanceContractRequiredError`）——
    两道闸门一处是"别浪费时间"，一处是"引擎级强制"。
    """
    if state.task_state is not TaskState.READY:
        raise LoopAExecutionError(f"循环 A 从 READY 起步，收到的是 {state.task_state.value}")
    if not state.contract_bound:
        raise AcceptanceContractRequiredError(state.task_id)
    return await _record(state, handlers, LoopANode.TASK_READY, payload={"task_id": state.task_id})


async def _task_planner(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """任务规划：任务 → 施工计划。"""
    plan = await handlers.planner.plan(state)
    update = await _record(
        state,
        handlers,
        LoopANode.TASK_PLANNER,
        payload={"task_type": plan.task_type.value, "acceptance_focus": plan.acceptance_focus},
    )
    update["plan"] = plan
    return update


async def _select_coding_agent(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """选执行器。"""
    candidates = tuple(sorted(handlers.executors))
    if not candidates:
        raise LoopAExecutionError("没有可用的执行器")
    chosen = await handlers.selector.select(state, candidates)
    if chosen not in handlers.executors:
        raise LoopAExecutionError(f"选出来的执行器 {chosen!r} 不在候选里：{candidates}")
    update = await _record(
        state,
        handlers,
        LoopANode.SELECT_CODING_AGENT,
        payload={"executor": chosen, "candidates": list(candidates)},
    )
    update["executor"] = chosen
    return update


async def _create_isolated_workspace(
    state: LoopAState,
    handlers: LoopAHandlers,
) -> dict[str, object]:
    """开一个隔离工作区。"""
    workspace = await handlers.workspace.create(state)
    update = await _record(
        state,
        handlers,
        LoopANode.CREATE_ISOLATED_WORKSPACE,
        payload={"workspace_id": workspace.workspace_id, "branch": workspace.branch},
    )
    update["workspace"] = workspace
    return update


async def _agent_executes(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """起一次执行。

    **首跑把任务从 READY 推到 RUNNING，返修时不动。** ``REPAIRING`` 状态没有
    ``START`` 出边 —— 返修那一轮是"继续修"，不是"重新开始一个任务"。硬套
    ``START`` 会撞上 ``IllegalTransitionError``。
    """
    if state.plan is None:
        raise LoopAExecutionError("没有计划就不许执行")
    if state.workspace is None:
        raise LoopAExecutionError("没有隔离工作区就不许执行")
    if state.executor not in handlers.executors:
        raise LoopAExecutionError(f"执行器 {state.executor!r} 不在候选表里")

    task_states: tuple[TaskState, ...] = ()
    if state.task_state is TaskState.READY:
        started = advance_task_state(state, TaskAction.START)
        task_states = (started.task_state,)

    executor = handlers.executors[state.executor]
    attempt = state.repair_count + 1
    instruction = state.plan.instruction
    if state.repair_task is not None:
        instruction = state.repair_task.instruction

    session = await executor.create_session(
        SessionConfig(workspace=state.workspace.path, branch=state.workspace.branch)
    )
    session_ref = await handlers.journal.open_session(
        SessionRecord(
            project_id=state.project_id,
            task_id=state.task_id,
            agent_id=state.agent_id,
            executor=executor.name,
            session_id=session.session_id,
            authorization={
                "agent_id": state.agent_id,
                "task_id": state.task_id,
                "project_id": state.project_id,
                "workspace_id": state.workspace.workspace_id,
                "expiry": None,
            },
        )
    )
    handle = await executor.execute(
        ExecutionTask(
            session_id=session.session_id,
            task_id=state.task_id,
            title=state.title or state.plan.task_id,
            instruction=instruction,
            timeout_seconds=state.plan.timeout_seconds,
            attempt=attempt,
        )
    )

    update = await _record(
        state,
        handlers,
        LoopANode.AGENT_EXECUTES,
        task_states=task_states,
        session_ref=session_ref,
        payload={
            "executor": executor.name,
            "attempt": attempt,
            "idempotency_key": handle.idempotency_key,
            "session_id": session.session_id,
        },
    )
    update["session_id"] = session.session_id
    update["session_ref"] = session_ref
    update["attempt"] = attempt
    return update


async def _collect_diff_and_artifacts(
    state: LoopAState,
    handlers: LoopAHandlers,
) -> dict[str, object]:
    """收集 Git Diff 与产物。"""
    executor = handlers.executors[state.executor]
    result = await executor.collect_result(state.session_id)
    artifacts = await handlers.collector.collect(state, result)
    update = await _record(
        state,
        handlers,
        LoopANode.COLLECT_DIFF_AND_ARTIFACTS,
        payload={
            "status": result.status.value,
            "branch": artifacts.branch,
            "commit": artifacts.commit or "",
            "changed_files": artifacts.changed_files,
        },
    )
    update["execution_status"] = result.status
    update["artifacts"] = artifacts
    # 这一轮花了多少钱 —— **下一轮修复的预算按它估**（TASK-P1-063）。
    update["execution_cost"] = result.execution.cost
    return update


async def _independent_verification(
    state: LoopAState,
    handlers: LoopAHandlers,
) -> dict[str, object]:
    """独立验证 —— 任务先进入 VERIFYING，再验。

    **迁移在验证之前。** 任务"正在验证"这件事本身是一个状态：验挂了、
    验崩了，任务都该停在 VERIFYING 而不是 RUNNING。
    """
    verifying = advance_task_state(state, TaskAction.VERIFY)
    report = await handlers.verifier.verify(state)
    outcome = VerificationOutcome.from_report(report)
    update = await _record(
        state,
        handlers,
        LoopANode.INDEPENDENT_VERIFICATION,
        task_states=(verifying.task_state,),
        payload={
            "verdict": outcome.verdict.value,
            "reasons": list(outcome.reasons),
            "checks": [list(check) for check in outcome.checks],
        },
    )
    update["verification"] = outcome
    return update


async def _acceptance_passed(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """验收通过了吗 —— 判定权在独立验证，不在执行器。

    **判定走 :func:`core.task.outcome.resolve_outcome`。** 它只看两样东西：
    执行器报的状态与独立验证的结论；执行器的自我报告压根不在输入里 ——
    类型层面就够不着（:class:`core.task.outcome.StatusSource` 协议只有一个成员）。
    """
    if state.execution_status is None:
        raise LoopAExecutionError("没有执行结果就没法判定")
    if state.verification is None:
        raise LoopAExecutionError("没有独立验证结论就没法判定")

    resolution = resolve_outcome(
        _StatusOnly(state.execution_status),
        state.verification.to_report(),
    )
    accepted = resolution.outcome is TaskOutcome.COMPLETED

    task_states: tuple[TaskState, ...] = ()
    if accepted:
        task_states = (advance_task_state(state, TaskAction.PASS).task_state,)

    update = await _record(
        state,
        handlers,
        LoopANode.ACCEPTANCE_PASSED,
        task_states=task_states,
        payload={"outcome": resolution.outcome.value, "reason": resolution.reason},
    )
    update["outcome"] = resolution.outcome
    update["accepted"] = accepted
    return update


async def _failure_diagnosis(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """失败诊断：为什么会这样，能不能自己修。"""
    diagnosis = await handlers.diagnoser.diagnose(state)
    update = await _record(
        state,
        handlers,
        LoopANode.FAILURE_DIAGNOSIS,
        payload={"category": diagnosis.category.value, "reason": diagnosis.reason},
    )
    update["diagnosis"] = diagnosis
    return update


async def _can_auto_repair(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """能不能自己修 —— 看诊断的归类。

    **这里不数次数。** 次数是状态机的闸门管的事；这里只回答"这类失败值不值得
    自动修"。两件事混在一起，就会出现"归类说能修、结果次数超了"这种说不清的
    状态。
    """
    if state.diagnosis is None:
        raise LoopAExecutionError("没有诊断就没法判断能不能修")
    allowed = state.diagnosis.auto_repairable
    update = await _record(
        state,
        handlers,
        LoopANode.CAN_AUTO_REPAIR,
        payload={"allowed": allowed, "category": state.diagnosis.category.value},
    )
    update["repair_allowed"] = allowed
    return update


async def _create_repair_task(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """真的修一次 —— **两道闸门一起守**：每任务那条在状态机里，全局那条在
    :class:`orchestration.repair.RepairCoordinator` 里。

    **任一条响了就停。** 每任务那条是"这个任务自己修了几次"，全局那条是"这个
    项目的所有任务一共修了多少次" —— 只看前者的挡不住并行的情况：两个任务各
    修 3 次，看着都没超，合起来就是 6 次，而上限说的是 5 次。

    **闸门响了就停。** 于是这一步不造修复任务，转入升级挂起 —— **不再自动
    重试**（T1 ③）。

    **成本按上一轮的实际开销估。** 预算要在动手之前判，而动手之前没人知道这
    次要花多少（:attr:`LoopAState.execution_cost` 是上一轮那次的实际开销）。
    """
    if state.diagnosis is None:
        raise LoopAExecutionError("没有诊断就修不了")

    outcome = await handlers.repair_coordinator.plan_diagnosed(
        RepairRequest(
            task_id=state.task_id,
            project_id=state.project_id,
            current_state=state.task_state,
            cost=state.execution_cost,
            repair_count=state.repair_count,
            max_repairs=state.max_repairs,
        ),
        diagnosis=state.diagnosis,
    )

    if not outcome.allowed:
        update = await _record(
            state,
            handlers,
            LoopANode.CREATE_REPAIR_TASK,
            payload={
                "exhausted": True,
                "reason": outcome.reason,
                "limit": outcome.limit.value if outcome.limit is not None else "",
            },
        )
        update["repair_exhausted"] = True
        update["blocked_reason"] = outcome.reason
        update["repair_allowed"] = False
        return update

    if outcome.repair_task is None:
        # 重投：任务已经在修复中。**不重复造任务**，也不重复触发副作用。
        update = await _record(
            state,
            handlers,
            LoopANode.CREATE_REPAIR_TASK,
            payload={"redelivered": True, "reason": outcome.reason},
        )
        update["repair_allowed"] = False
        return update

    repair = outcome.repair_task
    update = await _record(
        state,
        handlers,
        LoopANode.CREATE_REPAIR_TASK,
        task_states=(outcome.transition.current,),
        payload={"attempt": repair.attempt, "instruction": repair.instruction},
    )
    update["repair_task"] = repair
    update["repair_count"] = repair.attempt
    update["repair_allowed"] = False
    return update


async def _escalate_or_block(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """升级挂起 —— 任务停下来等人。

    **挂起的原因要留痕。** "为什么停在这儿"是事后最常被问的那个问题；
    没有理由的 ``BLOCKED`` 只是一句没有线索的结论。
    """
    reason = state.blocked_reason or "自动修复不可用，转人工处理"
    blocked = advance_task_state(state, TaskAction.BLOCK)
    update = await _record(
        state,
        handlers,
        LoopANode.ESCALATE_OR_BLOCK,
        task_states=(blocked.task_state,),
        payload={"reason": reason},
    )
    update["blocked_reason"] = reason
    return update


async def _integration_and_merge_gate(
    state: LoopAState,
    handlers: LoopAHandlers,
) -> dict[str, object]:
    """集成与合并闸门 —— 合不上的也算一次失败。

    **合并前先进 INTEGRATING。** 合并是个会失败的动作（冲突、被拒、门没开），
    所以它必须占一个状态；合上了再走 ``MERGED``。
    """
    integrating = advance_task_state(state, TaskAction.INTEGRATE)
    merge = await handlers.merge_gate.merge(state)

    task_states: tuple[TaskState, ...] = (integrating.task_state,)
    if merge.merged:
        task_states = (
            integrating.task_state,
            advance_task_state(integrating, TaskAction.MERGE).task_state,
        )

    update = await _record(
        state,
        handlers,
        LoopANode.INTEGRATION_AND_MERGE_GATE,
        task_states=task_states,
        payload={"merged": merge.merged, "commit": merge.commit, "reason": merge.reason},
    )
    update["merge"] = merge
    return update


async def _update_project_state(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """回写项目状态 —— 合上了与挂起了都要回写。

    **挂起也要回写。** "这个任务卡住了"本身就是项目状态的一部分 ——
    只在成功时回写，看板上就永远看不到卡住的任务。
    """
    await handlers.project_state.update(state)
    return await _record(
        state,
        handlers,
        LoopANode.UPDATE_PROJECT_STATE,
        payload={"task_state": state.task_state.value},
    )


async def _execution_complete(state: LoopAState, handlers: LoopAHandlers) -> dict[str, object]:
    """收尾 —— 合上了就把任务结掉。

    **挂起的不结。** ``BLOCKED`` 任务没有完成可言，它停在那里等人。
    """
    task_states: tuple[TaskState, ...] = ()
    task_state = state.task_state
    if task_state is TaskState.MERGED:
        task_state = advance_task_state(state, TaskAction.COMPLETE).task_state
        task_states = (task_state,)
    update = await _record(
        state,
        handlers,
        LoopANode.EXECUTION_COMPLETE,
        task_states=task_states,
        payload={"task_state": task_state.value},
    )
    return update


# --------------------------------------------------------------------------
# 分支
# --------------------------------------------------------------------------


def _route_after_acceptance(state: LoopAState) -> str:
    """验收通过 → 合并；没通过 → 失败诊断。"""
    if state.accepted:
        return "integration_and_merge_gate"
    return "failure_diagnosis"


def _route_after_merge(state: LoopAState) -> str:
    """合上了 → 回写项目状态；没合上 → 失败诊断。"""
    if state.merge is not None and state.merge.merged:
        return "update_project_state"
    return "failure_diagnosis"


def _route_after_diagnosis(state: LoopAState) -> str:
    """能自己修 → 造修复任务；不能 → 升级挂起。"""
    if state.repair_allowed:
        return "create_repair_task"
    return "escalate_or_block"


def _route_after_repair_task(state: LoopAState) -> str:
    """次数用尽 → 升级挂起；否则回去再执行一轮。"""
    if state.repair_exhausted:
        return "escalate_or_block"
    return "agent_executes"


# --------------------------------------------------------------------------
# 图
# --------------------------------------------------------------------------


class _BoundNode:
    """把处理器绑成"只吃状态"的异步节点。

    **为什么是类，不是闭包，也不是 ``Callable[...]`` 标注。** 两件事各自有
    理由，合起来才必须是这个形状。

    **一、必须是真异步函数。** LangGraph 靠 ``inspect.iscoroutinefunction``
    判断节点是不是异步的：一个**返回协程的普通函数**会被当成同步节点，于是
    协程对象被当成"状态更新"塞进通道 —— 报出来的是
    ``InvalidUpdateError: Expected dict, got <coroutine object ...>``。
    这里的 ``__call__`` 本身就是 ``async def``，判定不会走偏；而
    ``is_async_callable`` 也会去看实例的 ``__call__``，所以它同样认得。

    **二、不能用 ``Callable[...]`` 标注。** mypy **不把 ``Callable`` 类型
    当作回调协议的实现**：写成
    ``Callable[[LoopAState], Awaitable[dict[str, object]]]`` 就过不了
    ``add_node`` 的 ``_Node`` 协议检查（报的是
    ``No overload variant of "add_node" ... matches argument types``）。
    而一个**类实例**带着异步 ``__call__`` 是过得去的 —— 协议检查看的是
    实例的 ``__call__`` 方法本身。

    **签名只有 ``state`` 一个参数。** LangGraph 会照着签名决定要不要注入
    ``config`` / ``store`` / ``writer``；多一个关键字参数，就多一处可能对不
    上的地方。
    """

    __slots__ = ("_handler", "_handlers")

    def __init__(
        self,
        handler: Callable[[LoopAState, LoopAHandlers], Awaitable[dict[str, object]]],
        handlers: LoopAHandlers,
    ) -> None:
        self._handler = handler
        self._handlers = handlers

    async def __call__(self, state: LoopAState) -> dict[str, object]:
        return await self._handler(state, self._handlers)


def build_loop_a_graph(
    handlers: LoopAHandlers,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[LoopAState, None, LoopAState, LoopAState]:
    """把十五个节点接成一张有向图。

    ``checkpointer`` 传了就带持久化：中途异常之后可以从上一个 checkpoint 接着
    跑，**已经跑完的节点不会重跑**。

    **三个分支点用条件边。** 条件边读的是状态里已经算好的判定结果
    （``accepted`` / ``merge`` / ``repair_allowed`` / ``repair_exhausted``），
    而不是让路由函数再去问一遍处理器 —— 判定的输入在节点里已经被固定下来，
    路由时再判一次就多了一个能出分歧的地方。
    """
    graph = StateGraph(LoopAState)
    graph.add_node("task_ready", _BoundNode(_task_ready, handlers))
    graph.add_node("task_planner", _BoundNode(_task_planner, handlers))
    graph.add_node("select_coding_agent", _BoundNode(_select_coding_agent, handlers))
    graph.add_node(
        "create_isolated_workspace",
        _BoundNode(_create_isolated_workspace, handlers),
    )
    graph.add_node("agent_executes", _BoundNode(_agent_executes, handlers))
    graph.add_node(
        "collect_diff_and_artifacts",
        _BoundNode(_collect_diff_and_artifacts, handlers),
    )
    graph.add_node(
        "independent_verification",
        _BoundNode(_independent_verification, handlers),
    )
    graph.add_node("acceptance_passed", _BoundNode(_acceptance_passed, handlers))
    graph.add_node(
        "integration_and_merge_gate",
        _BoundNode(_integration_and_merge_gate, handlers),
    )
    graph.add_node("failure_diagnosis", _BoundNode(_failure_diagnosis, handlers))
    graph.add_node("can_auto_repair", _BoundNode(_can_auto_repair, handlers))
    graph.add_node("create_repair_task", _BoundNode(_create_repair_task, handlers))
    graph.add_node("escalate_or_block", _BoundNode(_escalate_or_block, handlers))
    graph.add_node("update_project_state", _BoundNode(_update_project_state, handlers))
    graph.add_node("execution_complete", _BoundNode(_execution_complete, handlers))

    graph.add_edge(START, "task_ready")
    graph.add_edge("task_ready", "task_planner")
    graph.add_edge("task_planner", "select_coding_agent")
    graph.add_edge("select_coding_agent", "create_isolated_workspace")
    graph.add_edge("create_isolated_workspace", "agent_executes")
    graph.add_edge("agent_executes", "collect_diff_and_artifacts")
    graph.add_edge("collect_diff_and_artifacts", "independent_verification")
    graph.add_edge("independent_verification", "acceptance_passed")
    graph.add_conditional_edges(
        "acceptance_passed",
        _route_after_acceptance,
        {
            "integration_and_merge_gate": "integration_and_merge_gate",
            "failure_diagnosis": "failure_diagnosis",
        },
    )
    graph.add_conditional_edges(
        "integration_and_merge_gate",
        _route_after_merge,
        {
            "update_project_state": "update_project_state",
            "failure_diagnosis": "failure_diagnosis",
        },
    )
    graph.add_edge("failure_diagnosis", "can_auto_repair")
    graph.add_conditional_edges(
        "can_auto_repair",
        _route_after_diagnosis,
        {
            "create_repair_task": "create_repair_task",
            "escalate_or_block": "escalate_or_block",
        },
    )
    graph.add_conditional_edges(
        "create_repair_task",
        _route_after_repair_task,
        {
            "agent_executes": "agent_executes",
            "escalate_or_block": "escalate_or_block",
        },
    )
    graph.add_edge("escalate_or_block", "update_project_state")
    graph.add_edge("update_project_state", "execution_complete")
    graph.add_edge("execution_complete", END)

    return graph.compile(checkpointer=checkpointer)


def recursion_limit(max_repairs: int) -> int:
    """LangGraph 的递归上限 —— 按"主链 + 每轮修复"算。

    **默认的 25 不够用。** 主链 11 个节点，每轮返修再加 7 个 —— 修到第三轮
    就是 32 个，超了会撞 ``GraphRecursionError``。而那不是"任务失败"，是
    "编排器自己撞墙了"：任务其实还能修，只是图不让自己走完。所以上限得按
    修复次数算出来，而不是拿默认值凑。
    """
    return len(LOOP_A_ORDER) + max_repairs * _REPAIR_CYCLE_NODES + 5


def _run_config(config: RunnableConfig | None, state: LoopAState | None) -> RunnableConfig:
    """跑图时的配置 —— 递归上限按修复次数算。"""
    base: dict[str, object] = dict(config) if config else {}
    max_repairs = state.max_repairs if state is not None else DEFAULT_MAX_REPAIRS
    base.setdefault("recursion_limit", recursion_limit(max_repairs))
    return cast(RunnableConfig, base)


async def run_loop_a(
    graph: CompiledStateGraph[LoopAState, None, LoopAState, LoopAState],
    state: LoopAState | None,
    *,
    config: RunnableConfig | None = None,
) -> LoopAState:
    """跑完这张图，返回**领域状态**。

    ``state`` 传 ``None`` 就是**从 checkpoint 接着跑**。

    **为什么要多这一步。** LangGraph 1.x 的 ``ainvoke`` 返回的是**通道字典**，
    不是状态模型 —— 即使状态模式本身是个 Pydantic 模型也一样。出了这张图，
    状态仍然是**领域对象**；字典回到模型时再校验一遍，某个通道里塞了个类型
    不对的值就会当场炸掉，而不是让它一路流到下游。
    """
    result = await graph.ainvoke(state, config=_run_config(config, state))
    return LoopAState(**result)


__all__ = [
    "AgentSelectorPort",
    "ArtifactCollectorPort",
    "DiagnoserPort",
    "ExecutorPort",
    "LoopAExecutionError",
    "LoopAHandlers",
    "MergeGatePort",
    "ProjectStatePort",
    "TaskPlannerPort",
    "VerificationPort",
    "WorkspacePort",
    "build_loop_a_graph",
    "recursion_limit",
    "run_loop_a",
]
