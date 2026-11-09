"""循环 B（项目自主演进循环）的编排（LangGraph）。

设计依据：docs/design.md §4（循环 B 的节点链、六个触发源、"这两个循环不能
合并成一个无限执行 Prompt"）、docs/tasks.md TASK-P1-061（T1 ① 全链路跑通并产出
``EvolutionRoadmap``；T1 ② 循环 B 不直接执行代码；T1 ③ 循环 B 的执行由循环 A
承接；T1 ④ 循环 B 结束后项目知识被更新）。

    Observe Project → Build Project Snapshot → Analyze Goal Gap
    → Discover Evolution Candidates → Gather Evidence
    → Evaluate Value / Risk / Cost → Worth Doing? → Record / Defer / Reject
    → Generate Evolution Plan → Decompose into Tasks
    → Dependency & Resource Scheduling → Execute Engineering Cycle
    → Evaluate Actual Outcome → Update Project Knowledge → Update Goal Gap

**这张图只做三件事：按次序调用各步、把状态存下来、把每一步的结论写进库里。**
每一步的契约在 :mod:`core.evolution.evolution_loop` —— 那里才是"这一步的产出
对不对"的判据所在。

**为什么契约不放在图里。** LangGraph **不校验**状态：把一个整数字段写成字符串，
它照跑不误。所以"这一步的产出对不对"必须由我们自己判 —— 而判据是领域知识。

**三个刻意的结构。**

**一、循环 B 不碰执行器。** 本模块**不 import 任何 ``executors.*``** ——
执行代码是循环 A 的事。循环 B 与循环 A 之间只有一个口
（:class:`EngineeringCyclePort`），它唯一的实现
（:class:`LoopAEngineeringCycle`）把计划交给循环 A 的入口
（:func:`orchestration.loop_a.run_loop_a`）。这不是分工上的客套：design.md §4
明写"这两个循环不能合并成一个无限执行 Prompt"，而"合并"最容易发生的形式就是
**演进循环自己长出一双手去改代码** —— 一旦它能改代码，"Coding Agent 不断创造
新的开发任务，让项目陷入无意义的自我修改"那条风险就再也拦不住了。

**二、分支点在"Worth Doing?"，分流在"Record / Defer / Reject"之后。**
design.md §4 把分支画在"Worth Doing?"上；实现上把分流放在紧随其后的
"Record / Defer / Reject"之后，因为**决策一定要被记下来** —— 哪怕结论是
"不做"（R-034：不行动是合法决策；R-035：record / defer / reject 三态）。
先记下"这一轮我们决定什么都不做、为什么"，再决定要不要继续往下规划。

**三、每一轮都要说得清是被什么触发的。** :class:`TriggerSource` 有六个取值
（design.md §4 的六种触发源）。说得清的触发源，事后才判断得出"该不该在这一轮
做这件事"。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from core.evolution.candidate import DiscoveryResult
from core.evolution.decision import DecisionPolicy, DecisionRound, DecisionState, decide_round
from core.evolution.evolution_loop import (
    LOOP_B_ORDER,
    CycleExecution,
    CycleOutcomeReport,
    EvidenceBundle,
    EvolutionJournalPort,
    EvolutionPlan,
    KnowledgeUpdate,
    LoopBNode,
    LoopBState,
    ProjectObservation,
    ResourceSchedule,
    SnapshotView,
    TaskExecution,
    check_resource_schedule,
    empty_cycle_outcome,
    evaluate_outcome,
    gather_evidence,
    record_node,
)
from core.evolution.priority import ScoredCandidate
from core.planning.roadmap import EvolutionRoadmap, TaskDecomposer
from core.planning.stages import GapAnalysis
from core.task.execution_loop import LoopAState
from core.task.outcome import TaskOutcome
from orchestration.loop_a import LoopAHandlers, build_loop_a_graph, run_loop_a


class LoopBExecutionError(RuntimeError):
    """循环 B 的编排被违反 —— 比如评了一个没被发现过的候选。"""


# --------------------------------------------------------------------------
# 处理器端口
# --------------------------------------------------------------------------


@runtime_checkable
class ProjectObserverPort(Protocol):
    """项目观察者：项目现在是什么样子。"""

    async def observe(self, state: LoopBState) -> ProjectObservation: ...


@runtime_checkable
class GapAnalyzerPort(Protocol):
    """目标差距分析器：离目标还有多远。

    **它收窄的是认知阶段的评估，不是复制它。** 认知那份里还有演进机会与建议
    —— 那是探索的原料（:class:`core.planning.stages.GapAnalysis`）。
    """

    async def analyze(self, state: LoopBState) -> GapAnalysis: ...


@runtime_checkable
class CandidateDiscovererPort(Protocol):
    """演进机会发现器。

    **发现器只产出候选，不做决策。** 它没有权力决定"这件事要不要做" ——
    那是决策层的事（R-030：Candidate ≠ Task）。
    """

    async def discover(self, state: LoopBState) -> DiscoveryResult: ...


@runtime_checkable
class CandidateEvaluatorPort(Protocol):
    """价值 / 风险 / 成本的评估者。

    **价值、影响、紧迫、成本是判断，不是观察** —— 评估者自己给，编排层不猜。
    风险与置信度则取候选自己的（:meth:`core.evolution.priority.
    PriorityFactors.from_candidate`）：那两样是**证据的属性**，另填一个数就是
    自己跟自己打架。
    """

    async def evaluate(self, state: LoopBState) -> tuple[ScoredCandidate, ...]: ...


@runtime_checkable
class DecisionPort(Protocol):
    """决策者：这一轮哪些候选值得做。

    **产出的是决策，不是任务。** 只有 ``record`` 才会派生任务（R-035）。
    """

    async def decide(self, state: LoopBState) -> DecisionRound: ...


@runtime_checkable
class EvolutionPlannerPort(Protocol):
    """规划者：这一轮要做成哪些任务，以及这些决策放不放行。

    **放行凭据由它一并给出** —— 放行是对**某一条决策**的结论，拆开传就会走散。
    """

    async def plan(self, state: LoopBState) -> EvolutionPlan: ...


@runtime_checkable
class SchedulerPort(Protocol):
    """资源调度者：谁来做、哪一波做、多少钱。

    **依赖那一半由路线图给**（依赖无环、波次已排好）；这一层补的是**资源**：
    哪个执行器有空、预算还剩多少。
    """

    async def schedule(self, state: LoopBState) -> ResourceSchedule: ...


@runtime_checkable
class EngineeringCyclePort(Protocol):
    """工程执行循环的入口 —— **循环 B 只认这一个口**。

    **这是两个循环之间唯一的接口。** 循环 B 不执行代码；它把计划交出去，
    拿回"做完了没有"的结论。唯一的实现
    （:class:`LoopAEngineeringCycle`）转手交给循环 A 的入口。
    """

    async def execute(self, state: LoopBState) -> CycleExecution: ...


@runtime_checkable
class GoalGapUpdaterPort(Protocol):
    """目标差距的更新者：这一轮做完之后，项目离目标还有多远。

    **它产出的是**下一轮**的差距。** 这一轮的差距在"分析目标差距"那一步就
    定下来了；做完之后不改，那下一轮拿到的就是一份过期的诊断。
    """

    async def update(self, state: LoopBState) -> GapAnalysis: ...


@dataclass(frozen=True, slots=True)
class PolicyDecisionMaker:
    """按领域里的决策策略作决策 —— 领域规则的直接包装。

    **为什么把策略放在这里而不是散在各处。** "值不值得做"这件事的判据是
    :class:`core.evolution.decision.DecisionPolicy`（置信度不足就 defer、风险
    太高就 defer、价值太低就 reject）。策略只有一份，各处各判一次，早晚会对
    不上。
    """

    policy: DecisionPolicy

    async def decide(self, state: LoopBState) -> DecisionRound:
        """给这一轮的候选作决策。"""
        return decide_round(self.policy, state.scored, cycle_id=state.cycle_id)


@dataclass(frozen=True, slots=True)
class LoopBHandlers:
    """循环 B 的各步处理器。

    **为什么是注入而不是内部 new。** 测试要能拿打桩跑通整图（T1），而真实实现
    要走角色化 Prompt 与真的数据库。两者的差别只在这些对象里，图本身不该知道。

    **这里没有执行器。** 有执行器的地方是循环 A（:class:`LoopAHandlers`）——
    循环 B 拿着的那一份是 :class:`LoopAEngineeringCycle`，它把活交给循环 A。
    """

    observer: ProjectObserverPort
    analyzer: GapAnalyzerPort
    discoverer: CandidateDiscovererPort
    evaluator: CandidateEvaluatorPort
    decider: DecisionPort
    planner: EvolutionPlannerPort
    scheduler: SchedulerPort
    engineering: EngineeringCyclePort
    gap_updater: GoalGapUpdaterPort
    journal: EvolutionJournalPort


# --------------------------------------------------------------------------
# 各步
# --------------------------------------------------------------------------


def _visited(state: LoopBState, node: LoopBNode) -> dict[str, object]:
    """走过的节点。"""
    return {"visited_nodes": record_node(state, node).visited_nodes}


async def _observe_project(state: LoopBState, handlers: LoopBHandlers) -> dict[str, object]:
    """观察项目 —— 并**把这一轮开出来**。

    **周期在开始时就要落库。** 候选与决策挂在它身上（``cycle_id`` 外键）；事后
    补记的周期挂不住那一轮已经产生的东西，那一轮的产出就永远算不进它的计数里。

    **触发源跟着周期一起落库。** 说不清"这一轮是被什么触发的"，事后就没人能
    判断"该不该在这一轮做这件事"。
    """
    observation = await handlers.observer.observe(state)
    cycle_ref = await handlers.journal.open_cycle(
        project_id=state.project_id,
        cycle_id=state.cycle_id,
        trigger=state.trigger.value,
    )
    return {
        "observation": observation,
        "cycle_ref": cycle_ref,
        **_visited(state, LoopBNode.OBSERVE_PROJECT),
    }


async def _build_project_snapshot(
    state: LoopBState,
    _handlers: LoopBHandlers,
) -> dict[str, object]:
    """建项目快照 —— 把观察定格下来。

    **版本号是"最新版本 + 1"。** 重复的版本号会被数据库的唯一约束挡下来 ——
    悄悄覆盖掉上一份快照，事后没人知道它曾经存在过。
    """
    observation = state.observation
    if observation is None:
        raise LoopBExecutionError("没有观察结果就建不了快照")
    snapshot = SnapshotView.from_observation(observation)
    return {"snapshot": snapshot, **_visited(state, LoopBNode.BUILD_PROJECT_SNAPSHOT)}


async def _analyze_goal_gap(state: LoopBState, handlers: LoopBHandlers) -> dict[str, object]:
    """分析目标差距 —— 离目标还有多远，哪些事现在还不该做。"""
    gaps = await handlers.analyzer.analyze(state)
    return {"gaps": gaps, **_visited(state, LoopBNode.ANALYZE_GOAL_GAP)}


async def _discover_candidates(state: LoopBState, handlers: LoopBHandlers) -> dict[str, object]:
    """发现演进候选 —— 并把它们登记下来。

    **候选立刻落库。** 决策要挂在候选上（外键），而决策是在下一步作出的 ——
    等到那一步再回头补候选，这一轮的候选就挂不住决策了。
    """
    discovery = await handlers.discoverer.discover(state)
    candidate_rows = await handlers.journal.record_candidates(
        state.cycle_ref,
        discovery.candidates,
    )
    return {
        "discovery": discovery,
        "candidate_rows": dict(candidate_rows),
        **_visited(state, LoopBNode.DISCOVER_CANDIDATES),
    }


async def _gather_evidence(state: LoopBState, _handlers: LoopBHandlers) -> dict[str, object]:
    """收拢证据 —— 这一轮每个候选的证据，以及那些没有证据的候选。

    **每个候选都要有说法。** 留下的写进清单，没有证据的写进"被丢掉"并说清
    理由 —— 谁都不许凭空消失（悄悄丢掉的候选，事后看着就是"这轮没发现什么"）。
    """
    discovery = state.discovery
    if discovery is None:
        raise LoopBExecutionError("没有发现结果就收不了证据")
    evidence: EvidenceBundle = gather_evidence(discovery)
    return {"evidence": evidence, **_visited(state, LoopBNode.GATHER_EVIDENCE)}


async def _evaluate_value_risk_cost(
    state: LoopBState,
    handlers: LoopBHandlers,
) -> dict[str, object]:
    """评估价值 / 风险 / 成本。"""
    scored = await handlers.evaluator.evaluate(state)
    known = {item.id for item in state.discovery.candidates} if state.discovery else set()
    unknown = sorted({item.candidate_id for item in scored} - known)
    if unknown:
        raise LoopBExecutionError(f"评了没被发现过的候选：{'、'.join(unknown)}")
    return {
        "scored": tuple(scored),
        **_visited(state, LoopBNode.EVALUATE_VALUE_RISK_COST),
    }


async def _worth_doing(state: LoopBState, handlers: LoopBHandlers) -> dict[str, object]:
    """值不值得做 —— **这一轮的分支点**。

    **判据是"有没有一条 record 决策"。** 不是"分数够不够高" —— 分数高但风险
    超线的候选会被 defer，而 defer 就是这一轮不做。
    """
    decisions = await handlers.decider.decide(state)
    worth_doing = any(item.decision is DecisionState.RECORD for item in decisions.decisions)
    return {
        "decisions": decisions,
        "worth_doing": worth_doing,
        **_visited(state, LoopBNode.WORTH_DOING),
    }


async def _record_defer_reject(state: LoopBState, handlers: LoopBHandlers) -> dict[str, object]:
    """记录 / 推迟 / 拒绝 —— 把这一轮的决策落库。

    **三态都要记。** defer 与 reject 同样是合法结局（R-034）；只记"做了什么"
    而不记"为什么不做"，事后读的人分不清"判断为不值得"与"压根没看见"。
    """
    decisions = state.decisions
    if decisions is None:
        raise LoopBExecutionError("没有决策就没有可记录的东西")
    decision_rows = await handlers.journal.record_decisions(
        state.cycle_ref,
        decisions,
        state.candidate_rows,
    )
    return {
        "decision_rows": dict(decision_rows),
        **_visited(state, LoopBNode.RECORD_DEFER_REJECT),
    }


async def _generate_evolution_plan(
    state: LoopBState,
    handlers: LoopBHandlers,
) -> dict[str, object]:
    """生成演进计划 —— 这一轮要做成哪些任务。"""
    plan = await handlers.planner.plan(state)
    return {"plan": plan, **_visited(state, LoopBNode.GENERATE_EVOLUTION_PLAN)}


async def _decompose_into_tasks(state: LoopBState, handlers: LoopBHandlers) -> dict[str, object]:
    """拆解成任务 —— 并把它们落成任务行。

    **拆解用的是领域里的拆解器**（:class:`core.planning.roadmap.TaskDecomposer`），
    编排层不自己判断"哪些决策该变成任务"：只有 ``record`` 且**被放行**的决策
    才派生任务 —— 少一道门槛，任务就会从没有决策来源的地方冒出来。

    **落地走的是仓储层的唯一入口**（``TaskRepository.land_roadmap``）—— 那里会
    再查一遍"决策是不是 record、是不是被推翻"。
    """
    plan = state.plan
    decisions = state.decisions
    if plan is None or decisions is None:
        raise LoopBExecutionError("没有计划或没有决策就没法拆解")
    roadmap: EvolutionRoadmap = TaskDecomposer().decompose(
        cycle_id=state.cycle_id,
        decision_round=decisions,
        approvals=plan.approvals,
        drafts=plan.drafts,
    )
    task_rows = await handlers.journal.land_tasks(roadmap, state.decision_rows)
    return {
        "roadmap": roadmap,
        "task_rows": dict(task_rows),
        **_visited(state, LoopBNode.DECOMPOSE_INTO_TASKS),
    }


async def _dependency_and_resource_scheduling(
    state: LoopBState,
    handlers: LoopBHandlers,
) -> dict[str, object]:
    """依赖与资源调度 —— 谁来做、哪一波做。

    **排期要与路线图对得上。** 少派一个任务，那个任务就没人做 —— 而排期表看
    上去仍然完整；让后继早于前驱开工，则是在一件还没做完的事上动土。两条都
    要断（:func:`core.evolution.evolution_loop.check_resource_schedule`）。
    """
    roadmap = state.roadmap
    if roadmap is None:
        raise LoopBExecutionError("没有路线图就没法排期")
    schedule = await handlers.scheduler.schedule(state)
    check_resource_schedule(roadmap, schedule)
    return {"schedule": schedule, **_visited(state, LoopBNode.DEPENDENCY_AND_RESOURCE_SCHEDULING)}


async def _execute_engineering_cycle(
    state: LoopBState,
    handlers: LoopBHandlers,
) -> dict[str, object]:
    """执行工程循环 —— **把活交给循环 A**。

    **这一步不执行代码。** 它调用的是 :class:`EngineeringCyclePort`，而它唯一
    的实现转手交给循环 A 的入口（:func:`orchestration.loop_a.run_loop_a`）。
    """
    execution = await handlers.engineering.execute(state)
    return {"execution": execution, **_visited(state, LoopBNode.EXECUTE_ENGINEERING_CYCLE)}


async def _evaluate_actual_outcome(
    state: LoopBState,
    _handlers: LoopBHandlers,
) -> dict[str, object]:
    """评估实际结果 —— 当初的判断准不准。

    **结局是判定出来的，不是填报的**（:func:`core.evolution.evolution_loop.
    evaluate_outcome`）。落库时仓储层会按库里实际的行数再判一次，以那一份为准。
    """
    report: CycleOutcomeReport = evaluate_outcome(state)
    return {"outcome": report, **_visited(state, LoopBNode.EVALUATE_ACTUAL_OUTCOME)}


async def _update_project_knowledge(
    state: LoopBState,
    handlers: LoopBHandlers,
) -> dict[str, object]:
    """更新项目知识 —— 落一份快照，结束这一轮。

    **两样东西一起落。** 只有周期记录，回答不了"项目此刻是什么样"；只有快照，
    记录不了"这一轮做了什么"。两条都在，事后才能把"这一轮的判断"与"这一轮
    之后的项目"对起来看。

    **"不值得做"的那一轮也走这里。** 那条路上没有执行，所以没有"评估实际
    结果"的产物 —— 结局由 :func:`core.evolution.evolution_loop.
    empty_cycle_outcome` 给出（``stalled``：这一轮什么都没落地）。
    **"我们决定什么都不做"也是一条结论**，而只有把它记下来，下一轮才读得到
    "上一轮为什么没动"。

    **结局要写回状态。** 不写回的话，"不值得做"那条路走完之后，状态里就没有
    这一轮的判定 —— 而状态是这一轮唯一的留痕：周期记录里存的是**仓库数出来的
    计数**，不是这一轮的判定过程。
    """
    snapshot = state.snapshot
    if snapshot is None:
        raise LoopBExecutionError("没有快照就更新不了项目知识")
    report = state.outcome if state.outcome is not None else empty_cycle_outcome(state)
    snapshot_ref = await handlers.journal.record_snapshot(
        project_id=state.project_id,
        version=snapshot.version,
        commit=snapshot.commit,
        branch=snapshot.branch,
        metrics=snapshot.metrics,
    )
    await handlers.journal.close_cycle(
        state.cycle_ref,
        status=report.status,
        attainment_before=report.attainment_before,
        attainment_after=report.attainment_after,
        evidence_refs=state.evidence.refs if state.evidence else (),
    )
    knowledge = KnowledgeUpdate(
        cycle_ref=state.cycle_ref,
        snapshot_ref=snapshot_ref,
        snapshot_version=snapshot.version,
        status=report.status,
    )
    return {
        "outcome": report,
        "knowledge": knowledge,
        **_visited(state, LoopBNode.UPDATE_PROJECT_KNOWLEDGE),
    }


async def _update_goal_gap(state: LoopBState, handlers: LoopBHandlers) -> dict[str, object]:
    """更新目标差距 —— 下一轮的诊断。"""
    next_gaps = await handlers.gap_updater.update(state)
    return {"next_gaps": next_gaps, **_visited(state, LoopBNode.UPDATE_GOAL_GAP)}


def _route_after_decisions(state: LoopBState) -> str:
    """决策记完之后往哪走。

    **值得做 → 继续规划；不值得做 → 直接更新知识。** "不值得做"的那条路**不是
    什么都不做** —— 它照样落快照、照样结束周期、照样更新下一轮的目标差距。
    一轮"决定什么都不做"也是一轮，不留痕的话，它就只存在于某个人的记忆里。
    """
    if state.worth_doing:
        return "generate_evolution_plan"
    return "update_project_knowledge"


# --------------------------------------------------------------------------
# 循环 A 的承接
# --------------------------------------------------------------------------


class LoopAEngineeringCycle:
    """把循环 B 的计划交给**循环 A** 去执行。

    **这是两个循环之间唯一的接口。** 循环 B 自己不会写一行代码；它把路线图
    摊开，按排期次序一个任务一个任务地交给循环 A 的入口
    （:func:`orchestration.loop_a.run_loop_a`），拿回每个任务的验收结论。

    **为什么逐个任务交接，而不是"整批交出去"。** 路线图里有依赖边：后继任务
    必须等前置任务做完。整批交出去等于让循环 A 在一件还没做完的事上开工，而
    那种错误**不会报错** —— 每一件任务都看起来正常地做完了。

    **验收契约由调用方说了算。** :attr:`contract_bound_of` 回答"这个任务行上
    是否绑了验收契约"（R-051 的准入条件）。契约没绑，循环 A 会当场拒绝开工 ——
    那道闸门不能由这里代答。
    """

    __slots__ = ("_agent_id", "_contract_bound_of", "_graph", "_handlers")

    def __init__(
        self,
        handlers: LoopAHandlers,
        *,
        contract_bound_of: Callable[[str], bool],
        agent_id: str = "director",
    ) -> None:
        self._handlers = handlers
        self._contract_bound_of = contract_bound_of
        self._agent_id = agent_id
        self._graph = build_loop_a_graph(handlers)

    async def execute(self, state: LoopBState) -> CycleExecution:
        """按排期次序把每个任务交给循环 A。"""
        roadmap = state.roadmap
        if roadmap is None:
            raise LoopBExecutionError("没有路线图就没有可执行的任务")

        results: list[TaskExecution] = []
        for key in roadmap.execution_order:
            planned = roadmap.task_by_key(key)
            task_id = state.task_rows.get(key)
            if not task_id:
                raise LoopBExecutionError(f"任务 {key!r} 没有落到库里 —— 没有任务行就没有执行")
            loop_state = LoopAState(
                task_id=task_id,
                project_id=state.project_id,
                agent_id=self._agent_id,
                title=planned.title,
                task_type=planned.task_type,
                contract_bound=self._contract_bound_of(task_id),
            )
            result = await run_loop_a(self._graph, loop_state)
            accepted = result.outcome is TaskOutcome.COMPLETED
            results.append(
                TaskExecution(
                    task_key=key,
                    task_id=task_id,
                    accepted=accepted,
                    outcome=result.outcome,
                    reason="" if accepted else "循环 A 没有判定这个任务完成",
                )
            )
        return CycleExecution(tasks=tuple(results))


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

    **二、不能用 ``Callable[...]`` 标注。** mypy **不把 ``Callable`` 类型
    当作回调协议的实现**：写成
    ``Callable[[LoopBState], Awaitable[dict[str, object]]]`` 就过不了
    ``add_node`` 的 ``_Node`` 协议检查。而一个**类实例**带着异步 ``__call__``
    是过得去的 —— 协议检查看的是实例的 ``__call__`` 方法本身。
    """

    __slots__ = ("_handler", "_handlers")

    def __init__(
        self,
        handler: Callable[[LoopBState, LoopBHandlers], Awaitable[dict[str, object]]],
        handlers: LoopBHandlers,
    ) -> None:
        self._handler = handler
        self._handlers = handlers

    async def __call__(self, state: LoopBState) -> dict[str, object]:
        return await self._handler(state, self._handlers)


def build_loop_b_graph(
    handlers: LoopBHandlers,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[LoopBState, None, LoopBState, LoopBState]:
    """把十五个节点接成一张有向图。

    ``checkpointer`` 传了就带持久化：中途异常之后可以从上一个 checkpoint 接着
    跑，**已经跑完的节点不会重跑**。

    **分支用条件边。** 条件边读的是状态里已经算好的判定结果（``worth_doing``），
    而不是让路由函数再去问一遍处理器 —— 判定的输入在节点里已经被固定下来，
    路由时再判一次就多了一个能出分歧的地方。
    """
    graph = StateGraph(LoopBState)
    graph.add_node("observe_project", _BoundNode(_observe_project, handlers))
    graph.add_node("build_project_snapshot", _BoundNode(_build_project_snapshot, handlers))
    graph.add_node("analyze_goal_gap", _BoundNode(_analyze_goal_gap, handlers))
    graph.add_node("discover_candidates", _BoundNode(_discover_candidates, handlers))
    graph.add_node("gather_evidence", _BoundNode(_gather_evidence, handlers))
    graph.add_node(
        "evaluate_value_risk_cost",
        _BoundNode(_evaluate_value_risk_cost, handlers),
    )
    graph.add_node("worth_doing", _BoundNode(_worth_doing, handlers))
    graph.add_node("record_defer_reject", _BoundNode(_record_defer_reject, handlers))
    graph.add_node(
        "generate_evolution_plan",
        _BoundNode(_generate_evolution_plan, handlers),
    )
    graph.add_node("decompose_into_tasks", _BoundNode(_decompose_into_tasks, handlers))
    graph.add_node(
        "dependency_and_resource_scheduling",
        _BoundNode(_dependency_and_resource_scheduling, handlers),
    )
    graph.add_node(
        "execute_engineering_cycle",
        _BoundNode(_execute_engineering_cycle, handlers),
    )
    graph.add_node(
        "evaluate_actual_outcome",
        _BoundNode(_evaluate_actual_outcome, handlers),
    )
    graph.add_node(
        "update_project_knowledge",
        _BoundNode(_update_project_knowledge, handlers),
    )
    graph.add_node("update_goal_gap", _BoundNode(_update_goal_gap, handlers))

    graph.add_edge(START, "observe_project")
    graph.add_edge("observe_project", "build_project_snapshot")
    graph.add_edge("build_project_snapshot", "analyze_goal_gap")
    graph.add_edge("analyze_goal_gap", "discover_candidates")
    graph.add_edge("discover_candidates", "gather_evidence")
    graph.add_edge("gather_evidence", "evaluate_value_risk_cost")
    graph.add_edge("evaluate_value_risk_cost", "worth_doing")
    graph.add_edge("worth_doing", "record_defer_reject")
    graph.add_conditional_edges(
        "record_defer_reject",
        _route_after_decisions,
        {
            "generate_evolution_plan": "generate_evolution_plan",
            "update_project_knowledge": "update_project_knowledge",
        },
    )
    graph.add_edge("generate_evolution_plan", "decompose_into_tasks")
    graph.add_edge("decompose_into_tasks", "dependency_and_resource_scheduling")
    graph.add_edge(
        "dependency_and_resource_scheduling",
        "execute_engineering_cycle",
    )
    graph.add_edge("execute_engineering_cycle", "evaluate_actual_outcome")
    graph.add_edge("evaluate_actual_outcome", "update_project_knowledge")
    graph.add_edge("update_project_knowledge", "update_goal_gap")
    graph.add_edge("update_goal_gap", END)

    return graph.compile(checkpointer=checkpointer)


def recursion_limit() -> int:
    """LangGraph 的递归上限。

    **循环 B 没有返修环** —— 十五个节点走一遍就到底了，所以上限就是节点数
    加一点余量。不按这个算，就会撞上默认的 25 与 ``GraphRecursionError``：
    主链 15 个节点加上分支的两条路，留 5 的余量刚好够。
    """
    return len(LOOP_B_ORDER) + 5


def _run_config(config: RunnableConfig | None) -> RunnableConfig:
    """跑图时的配置 —— 递归上限按节点数算。"""
    base: dict[str, object] = dict(config) if config else {}
    base.setdefault("recursion_limit", recursion_limit())
    return cast(RunnableConfig, base)


async def run_loop_b(
    graph: CompiledStateGraph[LoopBState, None, LoopBState, LoopBState],
    state: LoopBState | None,
    *,
    config: RunnableConfig | None = None,
) -> LoopBState:
    """跑完这张图，返回**领域状态**。

    ``state`` 传 ``None`` 就是**从 checkpoint 接着跑**。

    **为什么要多这一步。** LangGraph 1.x 的 ``ainvoke`` 返回的是**通道字典**，
    不是状态模型 —— 即使状态模式本身是个 Pydantic 模型也一样。出了这张图，
    状态仍然是**领域对象**；字典回到模型时再校验一遍，某个通道里塞了个类型
    不对的值就会当场炸掉，而不是让它一路流到下游。
    """
    result = await graph.ainvoke(state, config=_run_config(config))
    return LoopBState(**result)


__all__ = [
    "CandidateDiscovererPort",
    "CandidateEvaluatorPort",
    "DecisionPort",
    "EngineeringCyclePort",
    "EvolutionPlannerPort",
    "GapAnalyzerPort",
    "GoalGapUpdaterPort",
    "LoopAEngineeringCycle",
    "LoopBExecutionError",
    "LoopBHandlers",
    "PolicyDecisionMaker",
    "ProjectObserverPort",
    "SchedulerPort",
    "build_loop_b_graph",
    "recursion_limit",
    "run_loop_b",
]
