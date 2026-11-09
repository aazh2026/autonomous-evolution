"""决策图的单元测试（TASK-P1-050）。

设计依据：docs/tasks.md TASK-P1-050、docs/design.md §5。

**这里验的是"图"的三件事。**

1. **每个节点都产出结构化对象**（T1 ①）—— 不是一句自由文本。这条不是
   "模型返回了什么"的意思，而是"图把什么放进了状态的哪个字段、放进去的是
   不是该阶段的领域类型"。真去问模型，答案每次都不一样；所以这一步盯的是
   **契约**：节点的产出类型由领域层定死，图只负责把它放对位置。
2. **打桩节点能跑通整图**（T1 ②）—— 端到端产出一份 ``EvolutionRoadmap``。
3. **编排器不校验状态** —— 这条是**反着写的**：证明 LangGraph 本身拦不住
   类型不对的写入，从而说明契约为什么必须放在领域层，而不是"我们已经防住
   了所以不用测"。
"""

from __future__ import annotations

import pytest
from langgraph.graph import END, START, StateGraph

from core.evolution.assessment import ProjectAssessment
from core.evolution.candidate import DiscoveryResult
from core.planning.roadmap import EvolutionRoadmap
from core.planning.stages import (
    STAGE_ORDER,
    DecisionOutcome,
    GapAnalysis,
    PipelineState,
    Stage,
    StageContractError,
    StageHandlers,
)
from orchestration.langgraph.decision_graph import (
    build_decision_graph,
    run_decision_graph,
)
from tests.support.decision_stages import (
    RecordingHandlers,
    assessment,
    gap_analysis,
    project_context,
)


def _state() -> PipelineState:
    return PipelineState(cycle_id="cycle-0007", context=project_context())


class TestEachNodeProducesAStructuredObject:
    """T1 ①：五个节点各产出符合 schema 的结构化对象。"""

    @pytest.mark.parametrize(
        ("stage", "field", "expected_type"),
        [
            (Stage.UNDERSTAND, "assessment", ProjectAssessment),
            (Stage.DIAGNOSE, "gaps", GapAnalysis),
            (Stage.EXPLORE, "discovery", DiscoveryResult),
            (Stage.DECIDE, "outcome", DecisionOutcome),
            (Stage.PLAN, "roadmap", EvolutionRoadmap),
        ],
    )
    async def test_a_node_writes_a_domain_object_onto_its_own_field(
        self, stage: Stage, field: str, expected_type: type[object]
    ) -> None:
        """每个节点写的都是**领域对象**，不是自由文本、也不是裸字典。

        **为什么盯类型而不只是盯"有东西"。** 状态里的 ``assessment`` 字段
        填一句"项目还需要改进"，下游读的时候不会报错 —— 它会照着这句
        自由文本继续算，最后得到一份看起来完整的错误计划。**类型就是这件事
        的最后一道防线**。
        """
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        updates: dict[str, dict[str, object]] = {}
        async for chunk in graph.astream(_state(), stream_mode="updates"):
            node, update = next(iter(chunk.items()))
            updates[node] = update

        assert stage.value in updates
        artifact = updates[stage.value][field]
        assert isinstance(artifact, expected_type)
        assert not isinstance(artifact, str)

    async def test_the_five_nodes_run_in_the_fixed_order(self) -> None:
        """节点跑出来的次序就是契约里的次序。"""
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        seen: list[str] = []
        async for chunk in graph.astream(_state(), stream_mode="updates"):
            seen.extend(chunk)

        assert seen == [stage.value for stage in STAGE_ORDER]

    async def test_a_node_writes_the_stage_it_completed(self) -> None:
        """节点除了产物，还要写"我跑到哪了"。

        **少了这一条，从 checkpoint 恢复就会把整条链重跑一遍** —— 而"重跑"
        对像"调模型"这样的节点不是白干一遍，是又花钱又可能给出另一个结论。
        """
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        completed: list[tuple[Stage, ...]] = []
        async for chunk in graph.astream(_state(), stream_mode="updates"):
            update = next(iter(chunk.values()))
            completed.append(update["completed_stages"])

        assert completed == [
            (Stage.UNDERSTAND,),
            (Stage.UNDERSTAND, Stage.DIAGNOSE),
            (Stage.UNDERSTAND, Stage.DIAGNOSE, Stage.EXPLORE),
            (Stage.UNDERSTAND, Stage.DIAGNOSE, Stage.EXPLORE, Stage.DECIDE),
            STAGE_ORDER,
        ]


class TestTheWholeGraphRuns:
    """T1 ②：用打桩节点跑通整图，端到端产出 ``EvolutionRoadmap``。"""

    async def test_the_run_produces_a_roadmap(self) -> None:
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        final = await run_decision_graph(graph, _state())

        assert isinstance(final.roadmap, EvolutionRoadmap)
        assert final.roadmap.cycle_id == "cycle-0007"
        assert len(final.roadmap.tasks) == 1

    async def test_every_stage_ran_exactly_once(self) -> None:
        """五个阶段各跑一次 —— 不是"至少一次"。"""
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        await run_decision_graph(graph, _state())

        assert handlers.calls == {
            "understand": 1,
            "diagnose": 1,
            "explore": 1,
            "decide": 1,
            "plan": 1,
        }

    async def test_the_final_state_carries_every_artifact(self) -> None:
        """跑完之后，五个产物都在状态里。"""
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        final = await run_decision_graph(graph, _state())

        assert final.assessment is not None
        assert final.gaps is not None
        assert final.discovery is not None
        assert final.outcome is not None
        assert final.roadmap is not None
        assert final.completed_stages == STAGE_ORDER

    async def test_the_roadmap_only_contains_approved_decisions(self) -> None:
        """路线图里的每个任务都要有对应的放行凭据。

        **这是"凭据跟着决策走"的用处。** 规划阶段拿到的凭据不是可有可无的
        参考，而是"这条决策准不准做"的判据 —— 没有凭据的任务不该进路线图。
        """
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        final = await run_decision_graph(graph, _state())

        assert final.outcome is not None
        assert final.roadmap is not None
        approved = {
            approval.decision_id for approval in final.outcome.approvals if approval.approved
        }
        for task in final.roadmap.tasks:
            assert task.decision_id in approved


class TestTheOrchestratorDoesNotValidateTheState:
    """**编排器不校验状态** —— 所以契约必须在领域层。

    这一节是**反着写的**：它证明 LangGraph 拦不住类型不对的写入。
    不是为了说"我们防住了"，而是为了说"**只有领域层能拦住**"——
    少这一节，这条结论就只是断言，不是实测。
    """

    async def test_langgraph_accepts_a_free_text_where_a_model_belongs(self) -> None:
        """把一句中文塞进 ``assessment`` 字段，编排器照跑不误。

        **这是实测出来的，不是推的。** 如果哪天 LangGraph 开始校验了，
        这条用例会红 —— 那时契约就可以从领域层挪走一份（**但不能整个挪走**：
        领域层还要给非 LangGraph 的调用方用）。
        """
        graph = StateGraph(PipelineState)
        graph.add_node("bad", lambda _state: {"assessment": "我是一句自由文本"})
        graph.add_edge(START, "bad")
        graph.add_edge("bad", END)
        compiled = graph.compile()

        result = await compiled.ainvoke(_state())

        assert result["assessment"] == "我是一句自由文本"

    async def test_the_domain_contract_is_what_stops_it(self) -> None:
        """同样一个错，在领域层被拦住了。"""
        handlers = RecordingHandlers()
        handlers.fail_on = "understand"
        graph = build_decision_graph(handlers.handlers)

        with pytest.raises(RuntimeError):
            await run_decision_graph(graph, _state())

    async def test_a_stage_will_not_accept_another_stage_s_artifact(self) -> None:
        """**图的节点也要过契约。** 处理器交回来一份类型不对的产物时，
        节点不会把它塞进状态 —— 它会报错停下。

        契约在 ``core/planning/stages.py``，图只是调用它；这条用例盯的是
        **图确实调用了它**。
        """

        class WrongUnderstander:
            def understand(self, _context: object) -> object:
                return "我是一句自由文本"

        handlers = StageHandlers(
            understand=WrongUnderstander(),  # type: ignore[arg-type]
            diagnose=RecordingHandlers().handlers.diagnose,
            explore=RecordingHandlers().handlers.explore,
            decide=RecordingHandlers().handlers.decide,
            plan=RecordingHandlers().handlers.plan,
        )
        graph = build_decision_graph(handlers)

        with pytest.raises(StageContractError) as caught:
            await run_decision_graph(graph, _state())

        assert caught.value.stage is Stage.UNDERSTAND
        assert "类型不对" in caught.value.message


class TestTheStagesStayInOrder:
    """次序不对就报错 —— 早跑一步，后面那份产物就建在空地基上。"""

    async def test_a_misordered_run_is_refused(self) -> None:
        """把 ``completed_stages`` 预填成"诊断已跑过"，理解阶段就该被拒。

        **这是恢复时的真实情形**：checkpoint 里存着上一次跑到一半的状态，
        而节点的执行顺序由图决定 —— 两者对不上时，**必须报错而不是照跑**。
        """
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)
        state = PipelineState(
            cycle_id="cycle-0007",
            context=project_context(),
            completed_stages=(Stage.DIAGNOSE,),
        )

        with pytest.raises(StageContractError):
            await run_decision_graph(graph, state)


class TestTheStateIsCarriedFaithfully:
    """状态里带的东西不能走样。"""

    async def test_the_gap_analysis_keeps_the_gaps_from_the_assessment(self) -> None:
        """诊断是**收窄**，不是重写 —— 差距一条不少地传下去。"""
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        final = await run_decision_graph(graph, _state())

        assert final.gaps is not None
        assert final.assessment is not None
        assert final.gaps.gaps == final.assessment.goal_gap

    async def test_the_round_trip_keeps_the_project_identity(self) -> None:
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        final = await run_decision_graph(graph, _state())

        assert final.context.project_id == "p-demo"
        assert final.gaps is not None
        assert final.gaps.project_id == "p-demo"
        assert final.roadmap is not None
        assert final.roadmap.cycle_id == "cycle-0007"


class TestTheGraphShape:
    """图的结构本身。"""

    async def test_the_graph_has_five_nodes(self) -> None:
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        drawn = graph.get_graph()

        assert set(drawn.nodes) >= {"understand", "diagnose", "explore", "decide", "plan"}

    async def test_the_graph_starts_at_understand_and_ends_at_plan(self) -> None:
        """入口是认知，出口是规划 —— 中间的次序由边定死。"""
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        drawn = graph.get_graph()

        edges = {
            (edge.source, edge.target)
            for edge in drawn.edges
            if edge.source != "__start__" and edge.target != "__end__"
        }
        assert ("understand", "diagnose") in edges
        assert ("diagnose", "explore") in edges
        assert ("explore", "decide") in edges
        assert ("decide", "plan") in edges


class TestTheGapAnalysisIsNarrowed:
    """诊断阶段留下的东西，正是探索阶段要用的东西。"""

    async def test_the_explore_stage_sees_only_the_gaps(self) -> None:
        """探索阶段拿到的输入里没有机会 —— 机会是探索自己找的。"""
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        final = await run_decision_graph(graph, _state())

        assert final.gaps is not None
        assert not hasattr(final.gaps, "opportunities")

    async def test_the_assessment_is_still_around(self) -> None:
        """收窄不是丢弃 —— 认知阶段的结论还留在状态里。"""
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        final = await run_decision_graph(graph, _state())

        assert final.assessment is not None
        assert final.assessment.opportunities


class TestTheRoadmapIsBuiltFromTheApprovedDecision:
    """规划阶段的产物与决策对得上。"""

    async def test_the_task_points_at_the_decision_that_produced_it(self) -> None:
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        final = await run_decision_graph(graph, _state())

        assert final.roadmap is not None
        assert final.outcome is not None
        decided = {item.id for item in final.outcome.decisions.decisions}
        for task in final.roadmap.tasks:
            assert task.decision_id in decided


class TestTheHandlersAreTheOnlyDifference:
    """图不该知道处理器是谁 —— 换个实现，图照样跑。"""

    async def test_a_different_handler_set_gives_a_different_roadmap(self) -> None:
        """**证明注入是真的。** 换个处理器，图照样跑通，只是结果不同。"""

        class OtherPlan:
            def plan(self, _outcome: DecisionOutcome) -> EvolutionRoadmap:
                return EvolutionRoadmap(cycle_id="cycle-0099")

        base = RecordingHandlers()
        handlers = StageHandlers(
            understand=base.handlers.understand,
            diagnose=base.handlers.diagnose,
            explore=base.handlers.explore,
            decide=base.handlers.decide,
            plan=OtherPlan(),
        )
        graph = build_decision_graph(handlers)

        final = await run_decision_graph(graph, _state())

        assert final.roadmap is not None
        assert final.roadmap.cycle_id == "cycle-0099"
        assert final.roadmap.tasks == ()


class TestTheGraphRunsWithoutACheckpointer:
    """不带持久化也要跑得通 —— 持久化是可选的。"""

    async def test_the_graph_runs_with_no_checkpointer(self) -> None:
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers)

        final = await run_decision_graph(graph, _state())

        assert final.roadmap is not None


class TestTheStageProductsAreWhatTheDomainExpects:
    """打桩处理器交回来的东西，领域层认。"""

    def test_the_stub_products_satisfy_the_domain_rules(self) -> None:
        """每个产物本身也要合规 —— 图只管类型，**内容**由领域模型自己把关。"""
        assert assessment().goal_gap[0].evidence
        assert gap_analysis().gaps
