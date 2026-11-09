"""循环 A 的单元测试（TASK-P1-060）。

设计依据：docs/tasks.md TASK-P1-060、docs/design.md §4（循环 A 的节点链）、
§3.4（判定权在验证引擎）、§3.5D（留痕 fail-closed）。

**这里验的是"图"的三件事。**

1. **全链路状态迁移序列符合设计文档节点顺序**（T1 ①）—— 不是"跑通了"，
   而是"走的路跟文档画的一样"。
2. **验证失败会进入失败诊断并产生修复任务**（T1 ②）。
3. **修复次数超上限就停**（T1 ③）—— 停在 ``BLOCKED``，不再自动重试。
4. **每一步都留痕**（T1 ④）—— ``AgentSession`` 与 ``ExecutionEvent``。

**为什么留痕要用"轨迹"当断言对象。** 打桩的落库口把每一步都记下来了，于是
节点顺序与任务状态迁移序列都能**从轨迹里读出来** —— 而不是从图的结构里
推出来。轨迹是出事之后还在的那个东西：图的结构说明"应该怎么走"，轨迹说明
"实际走了什么"。

**打桩执行器会报"我做完了"。** 这正是要验的东西 —— 执行器的自我报告不参与
判定（design.md §3.4）：打桩报完成而打桩验证判失败时，任务仍应走失败路径。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import pytest

from core.task.execution_loop import (
    LOOP_A_ORDER,
    FailureCategory,
    LoopANode,
    LoopAState,
)
from core.task.outcome import ExecutionStatus, TaskOutcome, VerificationVerdict
from core.task.state_machine import AcceptanceContractRequiredError, TaskState
from orchestration.loop_a import (
    LoopAExecutionError,
    build_loop_a_graph,
    recursion_limit,
    run_loop_a,
)
from tests.support.execution_loop import StubExecutor, StubHandlers, ready_state

#: design.md §4 循环 A 节点链的**逐字标签** —— 顺序就是文档里的顺序。
#:
#: **这张表是"文档到代码"的翻译层。** 文档改了标签而这张表没改，用例就会红；
#: 反过来，实现改了节点名而这张表没改，也会红。两边都动不了，才是"一致"。
DESIGN_CHAIN: Final[tuple[str, ...]] = (
    "Task Ready",
    "Task Planner",
    "Select Coding Agent",
    "Create Isolated Workspace",
    "Agent Executes",
    "Collect Git Diff & Artifacts",
    "Independent Verification",
    "Acceptance Passed?",
    "Integration & Merge Gate",
    "Failure Diagnosis",
    "Can Auto Repair?",
    "Create Repair Task",
    "Escalate / Block",
    "Update Project State",
    "Execution Complete",
)

#: 文档标签 → 实现里的节点。
DESIGN_NODES: Final[dict[str, LoopANode]] = {
    "Task Ready": LoopANode.TASK_READY,
    "Task Planner": LoopANode.TASK_PLANNER,
    "Select Coding Agent": LoopANode.SELECT_CODING_AGENT,
    "Create Isolated Workspace": LoopANode.CREATE_ISOLATED_WORKSPACE,
    "Agent Executes": LoopANode.AGENT_EXECUTES,
    "Collect Git Diff & Artifacts": LoopANode.COLLECT_DIFF_AND_ARTIFACTS,
    "Independent Verification": LoopANode.INDEPENDENT_VERIFICATION,
    "Acceptance Passed?": LoopANode.ACCEPTANCE_PASSED,
    "Integration & Merge Gate": LoopANode.INTEGRATION_AND_MERGE_GATE,
    "Failure Diagnosis": LoopANode.FAILURE_DIAGNOSIS,
    "Can Auto Repair?": LoopANode.CAN_AUTO_REPAIR,
    "Create Repair Task": LoopANode.CREATE_REPAIR_TASK,
    "Escalate / Block": LoopANode.ESCALATE_OR_BLOCK,
    "Update Project State": LoopANode.UPDATE_PROJECT_STATE,
    "Execution Complete": LoopANode.EXECUTION_COMPLETE,
}

#: 一路通过时走的节点。
HAPPY_PATH_NODES: Final[tuple[str, ...]] = (
    "task_ready",
    "task_planner",
    "select_coding_agent",
    "create_isolated_workspace",
    "agent_executes",
    "collect_diff_and_artifacts",
    "independent_verification",
    "acceptance_passed",
    "integration_and_merge_gate",
    "update_project_state",
    "execution_complete",
)

#: 一路通过时的任务状态迁移序列。
#:
#: **注意少了两个状态。** ``READY`` 是起点（不是迁移出来的），``BLOCKED``
#: 是失败路径的终点 —— 成功的路上不经过它。
HAPPY_PATH_STATES: Final[tuple[TaskState, ...]] = (
    TaskState.RUNNING,
    TaskState.VERIFYING,
    TaskState.PASSED,
    TaskState.INTEGRATING,
    TaskState.MERGED,
    TaskState.COMPLETED,
)


async def _run(handlers: StubHandlers, **overrides: object) -> LoopAState:
    """用打桩处理器跑一遍整图。"""
    graph = build_loop_a_graph(handlers.handlers)
    return await run_loop_a(graph, ready_state(**overrides))


class _RogueSelector(StubHandlers):
    """挑一个不在候选表里的执行器。"""

    async def select(self, state: LoopAState, candidates: Sequence[str]) -> str:
        """返回一个**长得像**候选、却不在候选表里的名字。

        拿真候选与任务拼一个影子名字 —— 它看着像个正经选项，编排层手里
        却没有，于是必须被拒。
        """
        return f"{candidates[0]}-{state.agent_id}-shadow"


class TestTheChainMatchesTheDesign:
    """节点链必须与 design.md §4 一致 —— 图是照着文档画的，不是照着想象画的。"""

    def test_the_implementation_follows_the_documented_order(self) -> None:
        """文档里的节点链，逐个对上实现里的节点。"""
        assert tuple(DESIGN_NODES[label] for label in DESIGN_CHAIN) == LOOP_A_ORDER

    def test_the_enum_declares_the_nodes_in_the_same_order(self) -> None:
        """枚举成员的**声明顺序**也要一致 —— 枚举的顺序就是"步骤的顺序"。"""
        assert tuple(LoopANode) == LOOP_A_ORDER

    def test_every_documented_label_has_a_node(self) -> None:
        """文档里的每个标签都要落到一个节点上 —— 不许有标签被悄悄漏掉。"""
        assert set(DESIGN_NODES) == set(DESIGN_CHAIN)
        assert len(DESIGN_NODES) == 15

    async def test_the_graph_has_one_node_per_design_node(self) -> None:
        """图里的节点与文档一一对应，一个不多一个不少。"""
        handlers = StubHandlers()
        graph = build_loop_a_graph(handlers.handlers)

        drawn = graph.get_graph()

        assert set(drawn.nodes) >= {node.value for node in LOOP_A_ORDER}


class TestTheHappyPath:
    """T1 ①：全链路状态迁移序列符合设计文档节点顺序。"""

    async def test_the_nodes_are_visited_in_the_documented_order(self) -> None:
        """走的路就是文档画的那条路。"""
        handlers = StubHandlers()

        await _run(handlers)

        assert handlers.journal.nodes == HAPPY_PATH_NODES

    async def test_the_task_states_migrate_in_the_documented_order(self) -> None:
        """任务状态一路推进到 ``COMPLETED``，中间一步都不跳。"""
        handlers = StubHandlers()

        await _run(handlers)

        assert handlers.journal.task_states == HAPPY_PATH_STATES

    async def test_the_task_ends_completed(self) -> None:
        handlers = StubHandlers()

        final = await _run(handlers)

        assert final.task_state is TaskState.COMPLETED
        assert final.outcome is TaskOutcome.COMPLETED
        assert final.accepted is True

    async def test_the_merge_gate_is_what_moves_the_task_to_merged(self) -> None:
        """``MERGED`` 是合并闸门给的 —— 没合上就没有 ``MERGED``。"""
        handlers = StubHandlers()

        final = await _run(handlers)

        assert final.merge is not None
        assert final.merge.merged is True
        assert final.merge.commit == "c0ffee"

    async def test_a_failing_merge_does_not_complete_the_task(self) -> None:
        """**合不上就不算完成。** 合并冲突不是"小问题"。"""
        handlers = StubHandlers(merged=False)

        final = await _run(handlers)

        assert final.task_state is not TaskState.COMPLETED
        assert final.merge is not None
        assert final.merge.merged is False


class TestAFailedVerificationGoesToDiagnosisAndRepair:
    """T1 ②：验证失败 → 进入 Failure Diagnosis → 产生修复任务。"""

    async def test_a_failed_verification_goes_to_the_failure_diagnosis(self) -> None:
        """验证没过 → 先诊断，不是直接重试。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])

        await _run(handlers)

        nodes = handlers.journal.nodes
        assert "failure_diagnosis" in nodes
        assert nodes.index("failure_diagnosis") < nodes.index("create_repair_task")

    async def test_a_repair_task_is_created_with_the_diagnosis(self) -> None:
        """修复任务带着诊断走 —— 不是一句"再试一次"。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED, VerificationVerdict.PASSED])

        final = await _run(handlers)

        assert final.repair_task is not None
        assert final.repair_task.attempt == 1
        assert final.repair_task.instruction == handlers.repair_hint
        assert final.repair_task.diagnosis.category is FailureCategory.AUTO_REPAIRABLE

    async def test_the_task_moves_to_repairing_before_it_re_executes(self) -> None:
        """修复这一轮走的是 ``REPAIRING``，不是重新 ``START`` 一次。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED, VerificationVerdict.PASSED])

        await _run(handlers)

        assert TaskState.REPAIRING in handlers.journal.task_states
        assert handlers.journal.task_states.count(TaskState.RUNNING) == 1

    async def test_the_second_round_runs_the_agent_again(self) -> None:
        """修复之后确实又跑了一遍 —— 而不是只在状态里记了一笔。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED, VerificationVerdict.PASSED])

        await _run(handlers)

        assert handlers.journal.nodes.count("agent_executes") == 2
        assert handlers.calls["verify"] == 2

    async def test_the_repair_uses_the_diagnosis_as_its_instruction(self) -> None:
        """下一轮拿到的施工说明来自诊断 —— 不是原指令再来一遍。"""
        handlers = StubHandlers(
            verdicts=[VerificationVerdict.FAILED, VerificationVerdict.PASSED],
            repair_hint="把那个失败的用例修好",
        )

        await _run(handlers)

        instructions = [task.instruction for task in handlers.executor.tasks]
        assert instructions[-1] == "把那个失败的用例修好"

    async def test_the_attempt_number_grows_with_the_repair_round(self) -> None:
        """第几次尝试要能对得上 —— 幂等键与它绑定。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED, VerificationVerdict.PASSED])

        await _run(handlers)

        assert [task.attempt for task in handlers.executor.tasks] == [1, 2]

    async def test_a_failure_that_needs_a_human_is_not_repaired(self) -> None:
        """该由人来看的失败不自动修 —— 系统不替人拍板。"""
        handlers = StubHandlers(
            verdicts=[VerificationVerdict.FAILED],
            diagnosis_category=FailureCategory.NEEDS_HUMAN,
        )

        final = await _run(handlers)

        assert final.task_state is TaskState.BLOCKED
        assert "create_repair_task" not in handlers.journal.nodes
        assert handlers.calls["diagnose"] == 1


class TestTheRepairBudgetStopsTheLoop:
    """T1 ③：修复次数超上限 → 进入 ``BLOCKED``，不再自动重试。"""

    async def test_the_task_is_blocked_when_the_budget_runs_out(self) -> None:
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])

        final = await _run(handlers)

        assert final.task_state is TaskState.BLOCKED
        assert final.repair_exhausted is True

    async def test_the_repair_count_stops_at_the_limit(self) -> None:
        """修到上限就停 —— 不是"再多修一次看看"。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])

        final = await _run(handlers)

        assert final.repair_count == final.max_repairs == 3

    async def test_no_further_attempt_is_made_after_the_budget_is_gone(self) -> None:
        """**不再自动重试** —— 次数用尽后执行器不再被调用。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])

        await _run(handlers)

        assert len(handlers.executor.tasks) == 4
        assert handlers.journal.nodes.count("agent_executes") == 4

    async def test_the_blocked_task_is_not_completed(self) -> None:
        """挂起的任务没有"完成"可言。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])

        final = await _run(handlers)

        assert final.task_state is not TaskState.COMPLETED
        assert final.outcome is not TaskOutcome.COMPLETED

    async def test_the_reason_for_blocking_is_recorded(self) -> None:
        """停下来的理由要能查出来 —— 没有理由的 ``BLOCKED`` 只是一句结论。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])

        final = await _run(handlers)

        assert final.blocked_reason

    async def test_the_budget_is_the_one_the_state_declares(self) -> None:
        """上限由状态说了算 —— 编排层不另立一套口径。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])

        final = await _run(handlers, max_repairs=1)

        assert final.repair_count == 1
        assert handlers.journal.nodes.count("agent_executes") == 2
        assert final.task_state is TaskState.BLOCKED


class TestEveryStepLeavesATrace:
    """T1 ④：每步都写入 ``AgentSession`` 与 ``ExecutionEvent``。

    **口径。** ``AgentSession`` 的语义是"**一次 Agent 执行**会话"，不是
    "一个节点" —— 所以每一次执行开一个会话，每个**节点**写一条事件。
    会话还没开出来的那几步（就绪、规划、选执行器、开工作区）还没发生执行，
    它们的事件的会话字段是空的 —— 而不是挂到某个不相干的会话上。
    """

    async def test_every_node_writes_exactly_one_event(self) -> None:
        """**每一步都留痕** —— 事件数与走过的节点数一样多。"""
        handlers = StubHandlers()

        final = await _run(handlers)

        assert len(handlers.journal.events) == len(final.visited_nodes)

    async def test_the_events_are_the_nodes_in_order(self) -> None:
        handlers = StubHandlers()

        await _run(handlers)

        assert tuple(event.event_type for event in handlers.journal.events) == HAPPY_PATH_NODES

    async def test_the_events_are_numbered_without_a_gap(self) -> None:
        """顺序号连续 —— 幂等消费与回放都靠它。"""
        handlers = StubHandlers()

        await _run(handlers)

        assert [event.sequence for event in handlers.journal.events] == list(
            range(1, len(handlers.journal.events) + 1)
        )

    async def test_every_agent_execution_opens_a_session(self) -> None:
        """**每次执行一个会话** —— 会话数与执行次数一样多。"""
        handlers = StubHandlers()

        await _run(handlers)

        assert len(handlers.journal.sessions) == 1
        assert handlers.journal.sessions[0].executor == "stub-executor"

    async def test_the_session_carries_the_authorization_snapshot(self) -> None:
        """授权快照跟着会话落库 —— 只在内存里的话，出事之后什么都证明不了。"""
        handlers = StubHandlers()

        await _run(handlers)

        authorization = handlers.journal.sessions[0].authorization
        assert authorization["agent_id"] == "agent-1"
        assert authorization["task_id"] == handlers.journal.sessions[0].task_id
        assert authorization["workspace_id"] == "ws-1"

    async def test_the_events_after_the_execution_belong_to_its_session(self) -> None:
        """事件挂在**它所属的那次执行**的会话上。"""
        handlers = StubHandlers()

        await _run(handlers)

        session_ref = handlers.journal.session_refs[0]
        by_type = {event.event_type: event for event in handlers.journal.events}
        assert by_type["agent_executes"].session_ref == session_ref
        assert by_type["independent_verification"].session_ref == session_ref
        assert by_type["execution_complete"].session_ref == session_ref

    async def test_the_steps_before_the_execution_have_no_session(self) -> None:
        """还没执行的那几步没有会话可挂 —— 空着比挂错好。"""
        handlers = StubHandlers()

        await _run(handlers)

        by_type = {event.event_type: event for event in handlers.journal.events}
        assert by_type["task_ready"].session_ref is None
        assert by_type["task_planner"].session_ref is None

    async def test_a_repair_round_opens_a_session_of_its_own(self) -> None:
        """返修是**另一次执行** —— 另开一个会话，别混进上一次。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED, VerificationVerdict.PASSED])

        await _run(handlers)

        assert len(handlers.journal.sessions) == 2
        first, second = handlers.journal.sessions
        assert first.session_id != second.session_id

        events = handlers.journal.events
        executes = [event for event in events if event.event_type == "agent_executes"]
        assert executes[0].session_ref == handlers.journal.session_refs[0]
        assert executes[1].session_ref == handlers.journal.session_refs[1]
        assert events[-1].session_ref == handlers.journal.session_refs[1]


class TestTheExecutorDoesNotDecideTheOutcome:
    """design.md §3.4：判定权在验证引擎，不在执行器。"""

    async def test_a_self_reported_success_is_not_a_verdict(self) -> None:
        """打桩执行器报"我做完了"，验证判失败 —— 任务就走失败路径。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])

        final = await _run(handlers)

        assert handlers.executor.tasks  # 执行器确实报了完成
        assert final.accepted is False
        assert final.task_state is not TaskState.COMPLETED

    async def test_a_failed_verification_is_not_a_completed_task(self) -> None:
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])

        final = await _run(handlers)

        assert final.outcome is TaskOutcome.FAILED

    async def test_an_executor_that_reports_failure_does_not_finish_the_task(self) -> None:
        """**执行器报失败，哪怕验证通过，任务也不能算完成。**

        验证引擎说"通过"只说明**它验的那部分**通过了；执行器说"我挂了"说的是
        **它自己那部分**没跑完。两句话同时成立时，任务就是没做完 ——
        把执行器的失败状态忽略掉，等于让"跑挂了"的任务蒙混过关。
        """
        handlers = StubHandlers(executor=StubExecutor(status=ExecutionStatus.FAILED))

        final = await _run(handlers)

        assert final.outcome is TaskOutcome.FAILED
        assert final.accepted is False
        assert final.task_state is not TaskState.COMPLETED


class TestTheLoopRefusesToStartWithoutAnAcceptanceContract:
    """R-051：没有绑定验收契约的任务不许开始执行。"""

    async def test_the_task_is_refused(self) -> None:
        handlers = StubHandlers()

        with pytest.raises(AcceptanceContractRequiredError):
            await _run(handlers, contract_bound=False)

    async def test_nothing_was_executed(self) -> None:
        """被拒之后一步都不许走 —— 白开工作区、白花钱。"""
        handlers = StubHandlers()

        with pytest.raises(AcceptanceContractRequiredError):
            await _run(handlers, contract_bound=False)

        assert handlers.executor.tasks == []

    async def test_no_workspace_is_opened_for_a_task_without_a_contract(self) -> None:
        """**拒绝要发生在开工作区之前。**

        "就绪"这一步查的就是"能不能开工"；查完了再开工作区，等于先花钱
        再发现不能干 —— 而闸门存在的意义正是"早失败，别白开工作区"。
        """
        handlers = StubHandlers()

        with pytest.raises(AcceptanceContractRequiredError):
            await _run(handlers, contract_bound=False)

        assert handlers.calls.get("create", 0) == 0

    async def test_a_task_that_is_not_ready_is_refused(self) -> None:
        """循环 A 从 ``READY`` 起步 —— 别的状态起步说明流程走岔了。"""
        handlers = StubHandlers()

        with pytest.raises(LoopAExecutionError):
            await _run(handlers, task_state=TaskState.RUNNING)


class TestTheChosenExecutorMustComeFromTheCandidates:
    """选择器只能在候选表里挑 —— 挑不出来就不许执行。"""

    async def test_an_executor_outside_the_candidates_is_refused(self) -> None:
        """编排层手里没有的执行器，谁都别想用上它。"""
        handlers = _RogueSelector()

        with pytest.raises(LoopAExecutionError):
            await _run(handlers)

    async def test_nothing_was_executed(self) -> None:
        """被拒之后一步都不许走。"""
        handlers = _RogueSelector()

        with pytest.raises(LoopAExecutionError):
            await _run(handlers)

        assert handlers.executor.tasks == []

    async def test_the_workspace_is_not_opened_for_a_rejected_choice(self) -> None:
        """**拒绝要发生在开工作区之前。** 白开一个工作区是白花钱 ——
        而且"选了不存在的执行器"这件事，在选的那一刻就该发现。
        """
        handlers = _RogueSelector()

        with pytest.raises(LoopAExecutionError):
            await _run(handlers)

        assert handlers.calls.get("create", 0) == 0


class TestTheGraphShape:
    """图的结构本身。"""

    async def test_the_main_chain_is_wired_in_order(self) -> None:
        handlers = StubHandlers()
        graph = build_loop_a_graph(handlers.handlers)

        drawn = graph.get_graph()

        edges = {
            (edge.source, edge.target)
            for edge in drawn.edges
            if edge.source != "__start__" and edge.target != "__end__"
        }
        assert ("task_ready", "task_planner") in edges
        assert ("agent_executes", "collect_diff_and_artifacts") in edges
        assert ("independent_verification", "acceptance_passed") in edges
        assert ("update_project_state", "execution_complete") in edges

    async def test_the_repair_edge_goes_back_to_the_execution(self) -> None:
        """返修的边指回"起一次执行" —— 这是这张图能自己转起来的唯一原因。"""
        handlers = StubHandlers()
        graph = build_loop_a_graph(handlers.handlers)

        drawn = graph.get_graph()

        edges = {
            (edge.source, edge.target)
            for edge in drawn.edges
            if edge.source != "__start__" and edge.target != "__end__"
        }
        assert ("create_repair_task", "agent_executes") in edges

    async def test_the_blocked_path_still_reaches_the_end(self) -> None:
        """挂起的任务也要把流程走完（回写项目状态 → 收尾）—— 不然看板上看不到。"""
        handlers = StubHandlers(verdicts=[VerificationVerdict.FAILED])
        graph = build_loop_a_graph(handlers.handlers)

        await run_loop_a(graph, ready_state())

        nodes = handlers.journal.nodes
        assert "escalate_or_block" in nodes
        assert nodes.index("escalate_or_block") < nodes.index("update_project_state")
        assert handlers.calls["update"] == 1


class TestTheRecursionLimitIsEnough:
    """LangGraph 的递归上限得按修复次数算，不能拿默认值凑。"""

    def test_the_limit_covers_the_whole_repair_budget(self) -> None:
        """修到上限也要走得完 —— 不然撞的是 ``GraphRecursionError``，
        那是"编排器自己撞墙了"，不是"任务失败"。"""
        assert recursion_limit(3) >= 15 + 3 * 7

    def test_a_bigger_budget_means_a_bigger_limit(self) -> None:
        assert recursion_limit(5) > recursion_limit(3)
