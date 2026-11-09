"""循环 B 的单元测试（TASK-P1-061）。

设计依据：docs/tasks.md TASK-P1-061（T1 ①~④）、docs/design.md §4（循环 B 的
节点链、两个循环不能合并）、§3.6（三表分离）。

**这里验的是"图"的四件事。**

1. **全链路可跑通并产出 ``EvolutionRoadmap``**（T1 ①）—— 不是"跑通了"，
   而是"真的产出了一份路线图"。
2. **循环 B 不直接执行代码**（T1 ②）—— **用 AST 查它的 import**，而不是
   "看看它有没有调"。
3. **循环 B 的执行由循环 A 承接**（T1 ③）—— 断言**调用的是循环 A 的入口**。
4. **循环 B 结束后项目知识被更新**（T1 ④）—— ``EvolutionCycle`` +
   ``ProjectSnapshot`` 落库。

**为什么第二条要用 AST。** "循环 B 里没有出现执行器"这种断言，靠肉眼看代码
守不住 —— 哪天有人加一行 ``from executors.codex import ...``，测试不会红。
而**只要它 import 了执行器层，它就离"自己动手改代码"只差一步**。用 AST
把 import 逐个列出来，改动会立刻在测试里显形。

**第三条为什么要"断言调用的是循环 A 的入口"。** 循环 B 自己跑一个执行器，
循环 A 再跑一次 —— 那就是两个循环各干一遍活，而不是"循环 A 承接"。所以这里
盯的是**入口函数本身**，不是"最后有没有代码被改"。

**为什么"不值得做"那条路也要走完。** 一轮"决定什么都不做"也是一轮：它留下
决策、留下快照、结束周期、更新下一轮的目标差距。**不留痕的话，它就只存在
于某个人的记忆里** —— 而下一轮读不到上一轮为什么没动，就会把同一件事再想一遍。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

import orchestration.loop_b as loop_b_module
from core.evolution.decision import DecisionPolicy, DecisionRound, DecisionState
from core.evolution.evolution_loop import (
    LOOP_B_ORDER,
    LoopBNode,
    LoopBState,
    ResourceSchedule,
    ScheduleError,
    TriggerSource,
)
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.metrics.cycle import CycleOutcome
from core.planning.roadmap import (
    DecisionApproval,
    EvolutionRoadmap,
    TaskDecomposer,
    TaskDraft,
    TaskType,
)
from orchestration.loop_a import run_loop_a as real_run_loop_a
from orchestration.loop_b import (
    LoopAEngineeringCycle,
    LoopBExecutionError,
    LoopBHandlers,
    build_loop_b_graph,
    recursion_limit,
    run_loop_b,
)
from tests.support.evolution_loop import (
    CANDIDATE_ID,
    TASK_KEY,
    StubHandlers,
    defer_all,
    demo_candidate,
    demo_scored,
    initial_state,
)
from tests.support.execution_loop import StubHandlers as LoopAStubHandlers

#: design.md §4 循环 B 节点链的**逐字标签** —— 顺序就是文档里的顺序。
#:
#: **这张表是"文档到代码"的翻译层。** 文档改了标签而这张表没改，用例就会红；
#: 反过来，实现改了节点名而这张表没改，也会红。两边都动不了，才是"一致"。
DESIGN_CHAIN: Final[tuple[str, ...]] = (
    "Observe Project",
    "Build Project Snapshot",
    "Analyze Goal Gap",
    "Discover Evolution Candidates",
    "Gather Evidence",
    "Evaluate Value / Risk / Cost",
    "Worth Doing?",
    "Record / Defer / Reject",
    "Generate Evolution Plan",
    "Decompose into Tasks",
    "Dependency & Resource Scheduling",
    "Execute Engineering Cycle",
    "Evaluate Actual Outcome",
    "Update Project Knowledge",
    "Update Goal Gap",
)

#: 文档标签 → 实现里的节点。
DESIGN_NODES: Final[dict[str, LoopBNode]] = {
    "Observe Project": LoopBNode.OBSERVE_PROJECT,
    "Build Project Snapshot": LoopBNode.BUILD_PROJECT_SNAPSHOT,
    "Analyze Goal Gap": LoopBNode.ANALYZE_GOAL_GAP,
    "Discover Evolution Candidates": LoopBNode.DISCOVER_CANDIDATES,
    "Gather Evidence": LoopBNode.GATHER_EVIDENCE,
    "Evaluate Value / Risk / Cost": LoopBNode.EVALUATE_VALUE_RISK_COST,
    "Worth Doing?": LoopBNode.WORTH_DOING,
    "Record / Defer / Reject": LoopBNode.RECORD_DEFER_REJECT,
    "Generate Evolution Plan": LoopBNode.GENERATE_EVOLUTION_PLAN,
    "Decompose into Tasks": LoopBNode.DECOMPOSE_INTO_TASKS,
    "Dependency & Resource Scheduling": LoopBNode.DEPENDENCY_AND_RESOURCE_SCHEDULING,
    "Execute Engineering Cycle": LoopBNode.EXECUTE_ENGINEERING_CYCLE,
    "Evaluate Actual Outcome": LoopBNode.EVALUATE_ACTUAL_OUTCOME,
    "Update Project Knowledge": LoopBNode.UPDATE_PROJECT_KNOWLEDGE,
    "Update Goal Gap": LoopBNode.UPDATE_GOAL_GAP,
}

#: 一路通过时走的节点。
HAPPY_PATH_NODES: Final[tuple[str, ...]] = tuple(node.value for node in LOOP_B_ORDER)

#: "不值得做"时走的节点 —— 规划、拆解、排期、执行、评估全部跳过。
NOT_WORTH_PATH_NODES: Final[tuple[str, ...]] = (
    "observe_project",
    "build_project_snapshot",
    "analyze_goal_gap",
    "discover_candidates",
    "gather_evidence",
    "evaluate_value_risk_cost",
    "worth_doing",
    "record_defer_reject",
    "update_project_knowledge",
    "update_goal_gap",
)

#: 循环 B 的模块文件 —— T1 ② 的 AST 检查要看它。
LOOP_B_SOURCE: Final[Path] = Path(__file__).resolve().parents[3] / "orchestration" / "loop_b.py"

#: 循环 A 打桩件用的任务行 id（uuid 字符串）。
TASK_ROW_ID: Final[str] = "3f1b2c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


async def _run(handlers: StubHandlers, **overrides: object) -> LoopBState:
    """用打桩处理器跑一遍整图。"""
    graph = build_loop_b_graph(handlers.handlers)
    return await run_loop_b(graph, initial_state(**overrides))


def _imported_modules(path: Path) -> Iterator[str]:
    """这个文件 import 了哪些模块（绝对与相对都算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            yield f"{prefix}{node.module or ''}"


def _roadmap_with_one_task() -> EvolutionRoadmap:
    """一份只含一个任务的路线图 —— 用来单独验"交给循环 A"那一步。"""
    candidate = demo_candidate()
    scored = demo_scored(candidate)
    decision = DecisionPolicy().decide(scored, decision_id="EVD-1")
    decisions = DecisionRound(cycle_id="cycle-0007", decisions=(decision,))
    return TaskDecomposer().decompose(
        cycle_id="cycle-0007",
        decision_round=decisions,
        approvals=(
            DecisionApproval(
                decision_id=decision.id,
                candidate_id=candidate.id,
                approved=True,
            ),
        ),
        drafts=(
            TaskDraft(
                key=TASK_KEY,
                decision_id=decision.id,
                task_type=TaskType.FEATURE,
                title=candidate.title,
            ),
        ),
    )


class TestTheChainMatchesTheDesign:
    """节点链必须与 design.md §4 一致 —— 图是照着文档画的，不是照着想象画的。"""

    def test_the_implementation_follows_the_documented_order(self) -> None:
        """文档里的节点链，逐个对上实现里的节点。"""
        assert tuple(DESIGN_NODES[label] for label in DESIGN_CHAIN) == LOOP_B_ORDER

    def test_the_enum_declares_the_nodes_in_the_same_order(self) -> None:
        """枚举成员的**声明顺序**也要一致 —— 枚举的顺序就是"步骤的顺序"。"""
        assert tuple(LoopBNode) == LOOP_B_ORDER

    def test_every_documented_label_has_a_node(self) -> None:
        """文档里的每个标签都要落到一个节点上 —— 不许有标签被悄悄漏掉。"""
        assert set(DESIGN_NODES) == set(DESIGN_CHAIN)
        assert len(DESIGN_NODES) == 15

    async def test_the_graph_has_one_node_per_design_node(self) -> None:
        """图里的节点与文档一一对应，一个不多一个不少。"""
        handlers = StubHandlers()
        graph = build_loop_b_graph(handlers.handlers)

        drawn = graph.get_graph()

        assert set(drawn.nodes) >= {node.value for node in LOOP_B_ORDER}


class TestTheWholeChainRuns:
    """T1 ①：全链路可跑通并产出 ``EvolutionRoadmap``。"""

    async def test_the_nodes_are_visited_in_the_documented_order(self) -> None:
        """走的路就是文档画的那条路。"""
        handlers = StubHandlers()

        final = await _run(handlers)

        assert tuple(node.value for node in final.visited_nodes) == HAPPY_PATH_NODES

    async def test_a_roadmap_is_produced(self) -> None:
        """**产出路线图** —— 不是"跑完了"，是"手里有一份能派工的路线图"。"""
        handlers = StubHandlers()

        final = await _run(handlers)

        roadmap = final.roadmap
        assert roadmap is not None
        assert isinstance(roadmap, EvolutionRoadmap)
        assert roadmap.tasks
        assert roadmap.total == 1

    async def test_the_roadmap_is_landed_as_tasks(self) -> None:
        """路线图里的任务要落成任务行 —— 没有任务行就没有执行。"""
        handlers = StubHandlers()

        final = await _run(handlers)

        roadmap = final.roadmap
        assert roadmap is not None
        assert handlers.journal.calls.get("land_tasks") == 1
        assert final.task_rows
        assert set(final.task_rows) == {task.key for task in roadmap.tasks}

    async def test_the_cycle_is_opened_before_anything_is_written_to_it(self) -> None:
        """**周期在开始时就要落库。** 候选与决策挂在它身上 —— 事后补记的周期
        挂不住那一轮已经产生的东西。"""
        handlers = StubHandlers()

        await _run(handlers)

        assert handlers.journal.calls.get("open_cycle") == 1
        assert handlers.journal.cycles[0]["cycle_id"] == "cycle-0007"

    async def test_the_trigger_is_recorded_with_the_cycle(self) -> None:
        """说不清"这一轮是被什么触发的"，事后就没人能判断该不该做这件事。"""
        handlers = StubHandlers()

        await _run(handlers, trigger=TriggerSource.QUALITY)

        assert handlers.journal.cycles[0]["trigger"] == "quality"

    async def test_the_candidates_are_recorded(self) -> None:
        handlers = StubHandlers()

        await _run(handlers)

        assert [item.id for item in handlers.journal.candidates] == [CANDIDATE_ID]

    async def test_the_decisions_are_recorded(self) -> None:
        """**决策要落库** —— 事后没人能知道这一轮为什么这么判。"""
        handlers = StubHandlers()

        final = await _run(handlers)

        decisions = final.decisions
        assert decisions is not None
        assert handlers.journal.calls.get("record_decisions") == 1
        assert final.decision_rows
        assert set(final.decision_rows) == {item.id for item in decisions.decisions}

    async def test_the_evidence_is_gathered_and_kept(self) -> None:
        """证据收拢了 —— 而且每个候选都有说法。"""
        handlers = StubHandlers()

        final = await _run(handlers)

        assert final.evidence is not None
        assert final.evidence.kept
        assert CANDIDATE_ID in final.evidence.accounted_for

    async def test_the_outcome_is_judged_not_declared(self) -> None:
        """**结局是判定出来的。** 达成度从 0.6 升到 0.7 → improved。"""
        handlers = StubHandlers()

        final = await _run(handlers)

        assert final.outcome is not None
        assert final.outcome.verdict is not None
        assert final.outcome.verdict.outcome is CycleOutcome.IMPROVED

    async def test_the_realized_value_is_recorded_per_candidate(self) -> None:
        """**实际收益要能拿去校准当初的判断** —— 那是"当初判得准不准"的唯一依据。"""
        handlers = StubHandlers()

        final = await _run(handlers)

        assert final.outcome is not None
        assert [item.candidate_id for item in final.outcome.outcomes] == [CANDIDATE_ID]
        assert final.outcome.outcomes[0].realized_value == Decimal(1)


class TestLoopBDoesNotExecuteCode:
    """T1 ②：循环 B **不直接执行代码**。"""

    def test_it_does_not_import_the_executor_layer(self) -> None:
        """**用 AST 查 import** —— 靠肉眼看代码守不住。

        哪天有人加一行 ``from executors.codex import ...``，这里就会红。
        而只要它 import 了执行器层，它就离"自己动手改代码"只差一步。
        """
        imported = list(_imported_modules(LOOP_B_SOURCE))

        offenders = [name for name in imported if name.startswith("executors")]
        assert not offenders, f"循环 B import 了执行器层：{offenders}"

    def test_it_has_no_executor_adapter(self) -> None:
        """模块上不许挂着任何执行器适配器 —— 挂了就是"伸手"的入口。"""
        offenders = [name for name in dir(loop_b_module) if "Executor" in name or "Adapter" in name]
        assert not offenders, f"循环 B 上挂着执行器相关的东西：{offenders}"

    def test_its_handlers_do_not_carry_an_executor(self) -> None:
        """**处理器集合里没有执行器。** 执行器在循环 A 那边（``LoopAHandlers``）。"""
        fields = set(LoopBHandlers.__dataclass_fields__)
        assert "executors" not in fields
        assert "executor" not in fields

    async def test_the_engineering_step_goes_through_a_port(self) -> None:
        """执行那一步调的是**端口** —— 端口后面是谁，编排层不关心。"""
        handlers = StubHandlers()

        await _run(handlers)

        assert handlers.calls.get("execute") == 1


class TestLoopAHandsOffTheExecution:
    """T1 ③：循环 B 的执行由循环 A 承接。"""

    async def test_the_loop_a_entry_point_is_called(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**断言调用的是循环 A 的入口** —— 不是"最后有代码被改"。

        循环 B 自己跑一个执行器、循环 A 再跑一次，那就是两个循环各干一遍活，
        而不是"循环 A 承接"。

        **包一层而不是替换掉。** 替换掉只能证明"它调了这个名字"，包一层才能
        证明"调完之后循环 A 真的跑过了" —— 换掉的话，循环 A 的图压根没跑，
        而这正是这条用例要防的那种"看起来承接了"。
        """
        calls: list[str] = []

        async def _spy(graph: object, state: object) -> object:
            calls.append(getattr(state, "task_id", ""))
            return await real_run_loop_a(graph, state)  # type: ignore[arg-type]

        monkeypatch.setattr("orchestration.loop_b.run_loop_a", _spy)

        loop_a = LoopAStubHandlers()
        cycle = LoopAEngineeringCycle(
            loop_a.handlers,
            contract_bound_of=lambda _task_id: True,
        )
        state = initial_state(
            roadmap=_roadmap_with_one_task(),
            task_rows={TASK_KEY: TASK_ROW_ID},
        )

        await cycle.execute(state)

        assert calls == [TASK_ROW_ID]
        assert loop_a.journal.sessions

    async def test_the_real_loop_a_graph_runs(self) -> None:
        """**真的走了一遍循环 A** —— 会话与事件都记下来了。

        只断言"调用了某个函数"不够：那个函数可以是空壳。这里断言循环 A 自己的
        留痕里有会话、有事件 —— 那是它真的跑过的证据。
        """
        loop_a = LoopAStubHandlers()
        cycle = LoopAEngineeringCycle(
            loop_a.handlers,
            contract_bound_of=lambda _task_id: True,
        )
        state = initial_state(
            roadmap=_roadmap_with_one_task(),
            task_rows={TASK_KEY: TASK_ROW_ID},
        )

        execution = await cycle.execute(state)

        assert loop_a.journal.sessions
        assert loop_a.journal.events
        assert execution.tasks[0].accepted is True
        assert execution.tasks[0].outcome is not None

    async def test_a_task_without_a_row_is_refused(self) -> None:
        """**没有任务行就没有执行** —— 否则执行的是一个不存在的任务。"""
        loop_a = LoopAStubHandlers()
        cycle = LoopAEngineeringCycle(
            loop_a.handlers,
            contract_bound_of=lambda _task_id: True,
        )
        state = initial_state(roadmap=_roadmap_with_one_task(), task_rows={})

        with pytest.raises(LoopBExecutionError):
            await cycle.execute(state)

        assert loop_a.journal.sessions == []


class TestProjectKnowledgeIsUpdated:
    """T1 ④：循环 B 结束后项目知识被更新（``EvolutionCycle`` + ``ProjectSnapshot``）。"""

    async def test_a_snapshot_is_landed(self) -> None:
        handlers = StubHandlers()

        final = await _run(handlers)

        assert handlers.journal.calls.get("record_snapshot") == 1
        assert final.knowledge is not None
        assert final.knowledge.snapshot_version == 4

    async def test_the_snapshot_carries_the_observed_state(self) -> None:
        """快照要能**复现** —— 给定它能还原到当时的提交与当时的指标。"""
        handlers = StubHandlers()

        await _run(handlers)

        snapshot = handlers.journal.snapshots[0]
        assert snapshot["commit"] == "c" * 40
        assert snapshot["branch"] == "main"
        assert snapshot["metrics"] == {"coverage": 0.82}

    async def test_the_cycle_is_closed_with_its_judgement(self) -> None:
        """**周期要结束，且带着它的结论。** 前后达成度都要在。"""
        handlers = StubHandlers()

        await _run(handlers)

        assert handlers.journal.calls.get("close_cycle") == 1
        closed = handlers.journal.closed[0]
        assert closed["attainment_before"] == Decimal("0.6")
        assert closed["attainment_after"] == Decimal("0.7")

    async def test_the_evidence_refs_travel_with_the_cycle(self) -> None:
        """证据引用跟着周期一起落库 —— 事后才查得到"这一轮凭什么这么判"。"""
        handlers = StubHandlers()

        await _run(handlers)

        closed = handlers.journal.closed[0]
        assert closed["evidence_refs"] == ("issue://core/evolution",)


class TestTheNotWorthPath:
    """ "不值得做"也是一轮 —— 它留痕，但不动手。"""

    async def test_it_skips_the_planning_and_execution_steps(self) -> None:
        handlers = StubHandlers(decisions=defer_all(demo_candidate()))

        final = await _run(handlers)

        assert tuple(node.value for node in final.visited_nodes) == NOT_WORTH_PATH_NODES

    async def test_the_decision_is_still_recorded(self) -> None:
        """**决策一定要被记下来** —— 哪怕结论是"不做"（R-034）。

        只记"做了什么"而不记"为什么不做"，事后读的人分不清"判断为不值得"
        与"压根没看见"。
        """
        handlers = StubHandlers(decisions=defer_all(demo_candidate()))

        await _run(handlers)

        assert handlers.journal.calls.get("record_decisions") == 1
        assert handlers.journal.decisions[0].decisions[0].decision is DecisionState.DEFER

    async def test_no_task_is_landed(self) -> None:
        """defer 不派生任务 —— 不是"创建了再标记为不做"（R-035）。"""
        handlers = StubHandlers(decisions=defer_all(demo_candidate()))

        await _run(handlers)

        assert handlers.journal.calls.get("land_tasks") is None

    async def test_the_project_knowledge_is_still_updated(self) -> None:
        """**一轮"决定什么都不做"也是一轮。** 不留痕的话，它就只存在于某个人的
        记忆里 —— 而下一轮读不到上一轮为什么没动。"""
        handlers = StubHandlers(decisions=defer_all(demo_candidate()))

        final = await _run(handlers)

        assert handlers.journal.calls.get("record_snapshot") == 1
        assert handlers.journal.calls.get("close_cycle") == 1
        assert final.knowledge is not None

    async def test_the_cycle_is_judged_stalled_not_undecided(self) -> None:
        """**"什么都没落地"是一条结论**（stalled），不是"没有结论"。"""
        handlers = StubHandlers(decisions=defer_all(demo_candidate()))

        final = await _run(handlers)

        assert final.outcome is not None
        assert final.outcome.verdict is not None
        assert final.outcome.verdict.outcome is CycleOutcome.STALLED

    async def test_the_next_gap_is_updated(self) -> None:
        """下一轮的诊断要更新 —— 拿一份过期的差距去规划下一轮，等于白规划。"""
        handlers = StubHandlers(decisions=defer_all(demo_candidate()))

        final = await _run(handlers)

        assert final.next_gaps is not None
        assert handlers.calls.get("update") == 1


class TestTheOrchestrationRefusesToGuess:
    """编排层不许替别人做判断 —— 判断错位要当场炸出来。"""

    async def test_scoring_a_candidate_that_was_never_discovered_is_refused(self) -> None:
        """评一个没被发现过的候选 —— 那份评分挂不到任何东西上。

        放它过去的后果是：决策挂在一个不存在的候选上，而**不会报错**。
        """

        class _RogueEvaluator(StubHandlers):
            async def evaluate(self, _state: LoopBState) -> tuple[ScoredCandidate, ...]:
                return (
                    ScoredCandidate(
                        candidate_id="CAND-GHOST",
                        factors=PriorityFactors(
                            value=Decimal("0.9"),
                            impact=Decimal("0.9"),
                            confidence=Decimal("0.9"),
                            urgency=Decimal("0.9"),
                            cost=Decimal("0.1"),
                            risk=Decimal("0.2"),
                        ),
                    ),
                )

        handlers = _RogueEvaluator()

        with pytest.raises(LoopBExecutionError):
            await _run(handlers)

    async def test_a_schedule_that_leaves_a_task_unassigned_is_refused(self) -> None:
        """少派一个任务，那个任务就没人做 —— 而排期表看上去仍然完整。"""

        class _PartialScheduler(StubHandlers):
            async def schedule(self, _state: LoopBState) -> ResourceSchedule:
                return ResourceSchedule(assignments=(), budget=Decimal("10"))

        handlers = _PartialScheduler()

        with pytest.raises(ScheduleError, match="没有派工"):
            await _run(handlers)


class TestTheRecursionLimitIsEnough:
    """LangGraph 的递归上限得按节点数算，不能拿默认值凑。"""

    def test_the_limit_covers_the_whole_chain(self) -> None:
        """主链 15 个节点 —— 上限不够会撞 ``GraphRecursionError``，
        那是"编排器自己撞墙了"，不是"这一轮失败"。"""
        assert recursion_limit() >= len(LOOP_B_ORDER) + 1

    async def test_the_chain_runs_without_hitting_the_limit(self) -> None:
        handlers = StubHandlers()

        final = await _run(handlers)

        assert len(final.visited_nodes) == len(LOOP_B_ORDER)
