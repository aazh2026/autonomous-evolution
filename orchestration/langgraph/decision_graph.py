"""Director 五阶段决策图（LangGraph）。

设计依据：docs/design.md §5（五阶段：认知 → 诊断 → 探索 → 决策 → 规划）、
docs/tasks.md TASK-P1-050。

    START → understand → diagnose → explore → decide → plan → END

**这张图只做两件事：按顺序调用五个阶段，以及把状态存下来。** 阶段的契约在
``core/planning/stages.py`` —— 那里才是"这一步的产出对不对"的判据所在。

**为什么契约不放在图里。** LangGraph **不校验**状态：把一个整数字段写成
字符串，它照跑不误，最后返回一份类型不对的状态（``tests/unit/orchestration/
test_decision_graph.py`` 里有盯着这件事的用例）。编排器要是对状态负责，就得
在每个节点里重复一遍类型判断 —— 而那些判断是**领域知识**，写在领域层才能被
单独校验与复用。图只负责**顺序**与**持久化**。

**为什么节点是注入的。** 测试要能拿打桩节点跑通整图（design.md 的 T1 ②），
真实实现要走角色化 Prompt（TASK-P1-057）。两者的差别只在五个处理器对象里，
图本身不该知道。

**状态是 Pydantic 模型。** 因为它要被写进 checkpoint：一个"看起来像 dict、
其实字段随时间变"的字典存下去就再也读不出类型了，而恢复时读不出类型的
状态等于没有状态。
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from core.planning.stages import (
    PipelineState,
    Stage,
    StageHandlers,
    advance,
    stage_field,
    stage_input,
)


def _commit(state: PipelineState, stage: Stage, artifact: object) -> dict[str, object]:
    """把一个阶段的产物与"已完成到哪"一起写回状态。

    **两个字段都要写。** 只写产物的话，状态里"已完成到哪"永远是空的 ——
    下一次 ``advance`` 于是认为第一阶段还没跑过、次序检查形同虚设，
    而从 checkpoint 恢复时更是会把整条链重跑一遍。
    """
    updated = advance(state, stage, artifact)
    field = stage_field(stage)
    return {field: getattr(updated, field), "completed_stages": updated.completed_stages}


def _understand(state: PipelineState, handlers: StageHandlers) -> dict[str, object]:
    """认知阶段：项目快照与目标 → 项目评估。"""
    artifact = handlers.understand.understand(stage_input(state, Stage.UNDERSTAND))
    return _commit(state, Stage.UNDERSTAND, artifact)


def _diagnose(state: PipelineState, handlers: StageHandlers) -> dict[str, object]:
    """诊断阶段：项目评估 → 差距分析。"""
    artifact = handlers.diagnose.diagnose(stage_input(state, Stage.DIAGNOSE))
    return _commit(state, Stage.DIAGNOSE, artifact)


def _explore(state: PipelineState, handlers: StageHandlers) -> dict[str, object]:
    """探索阶段：差距 → 演进候选。"""
    artifact = handlers.explore.explore(stage_input(state, Stage.EXPLORE))
    return _commit(state, Stage.EXPLORE, artifact)


def _decide(state: PipelineState, handlers: StageHandlers) -> dict[str, object]:
    """决策阶段：候选 → 决策与放行凭据。"""
    artifact = handlers.decide.decide(stage_input(state, Stage.DECIDE))
    return _commit(state, Stage.DECIDE, artifact)


def _plan(state: PipelineState, handlers: StageHandlers) -> dict[str, object]:
    """规划阶段：已批准的决策 → 路线图。"""
    artifact = handlers.plan.plan(stage_input(state, Stage.PLAN))
    return _commit(state, Stage.PLAN, artifact)


def build_decision_graph(
    handlers: StageHandlers,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[PipelineState, None, PipelineState, PipelineState]:
    """把五个阶段接成一张有向图。

    ``checkpointer`` 传了就带持久化：中途异常之后可以从上一个 checkpoint 接着
    跑，**已经跑完的阶段不会重跑**（design.md 的 T1 ③）。
    """
    graph = StateGraph(PipelineState)
    graph.add_node("understand", lambda state: _understand(state, handlers))
    graph.add_node("diagnose", lambda state: _diagnose(state, handlers))
    graph.add_node("explore", lambda state: _explore(state, handlers))
    graph.add_node("decide", lambda state: _decide(state, handlers))
    graph.add_node("plan", lambda state: _plan(state, handlers))

    graph.add_edge(START, "understand")
    graph.add_edge("understand", "diagnose")
    graph.add_edge("diagnose", "explore")
    graph.add_edge("explore", "decide")
    graph.add_edge("decide", "plan")
    graph.add_edge("plan", END)

    return graph.compile(checkpointer=checkpointer)


async def run_decision_graph(
    graph: CompiledStateGraph[PipelineState, None, PipelineState, PipelineState],
    state: PipelineState | None,
    *,
    config: RunnableConfig | None = None,
) -> PipelineState:
    """跑完这张图，返回**领域状态**。

    ``state`` 传 ``None`` 就是**从 checkpoint 接着跑** —— LangGraph 认这个
    约定：给了输入就是开新一轮，不给就是续上一次（design.md 的 T1 ③）。

    **为什么要多这一步。** LangGraph 1.x 的 ``invoke`` 返回的是**通道字典**，
    不是状态模型 —— 即使状态模式本身是个 Pydantic 模型也一样（实测返回
    ``dict``；改用 ``version="v2"`` 换来的又是一个包装类型 ``GraphOutput``）。
    于是"跑完了"与"手上是一份 ``PipelineState``"之间差着一步，而这一步没人
    会替我们做。编排层把它补上：出了这张图，状态仍然是**领域对象**。

    **顺手多看一眼类型。** 字典回到模型时会再校验一遍 —— 某个通道里塞了个
    类型不对的值（编排器自己不校验状态，见上），这里就会当场炸掉，而不是
    让它一路流到下游。
    """
    result = await graph.ainvoke(state, config=config)
    return PipelineState(**result)


__all__ = ["build_decision_graph", "run_decision_graph"]
