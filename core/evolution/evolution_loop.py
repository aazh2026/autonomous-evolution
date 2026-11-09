"""循环 B（项目自主演进循环）的契约（纯领域层）。

设计依据：docs/design.md §4（循环 B 的节点链、"持续演进应该由事件驱动，而不是
死循环"的六种触发源、"这两个循环不能合并成一个无限执行 Prompt"）、§3.3
（Observe / Discover / Prioritize 三层、决策三态）、§5（五阶段决策链）；
docs/tasks.md TASK-P1-061（R-006 不引入 Temporal、R-027 三层、R-074 节点链、
R-075 两循环不得合并、R-078 六维差异）。

**这一层只回答"每一步的输入是什么、产出是什么"。** 它不知道底下是
LangGraph、不知道执行器是谁、也不知道循环 A 长什么样 —— 那些在
:mod:`orchestration.loop_b`。分开的理由与 :mod:`core.task.execution_loop`
一样：契约写在编排层，别的调用方（非 LangGraph 的那条路）就用不上；而
"这一步的产出对不对"是**领域知识**，写在领域层才能被单独校验与复用。

**三个刻意的选择。**

**一、状态里没有墙钟时间。** 状态要被写进 checkpoint，还要被回放 runner
（TASK-P1-065）原样重放 —— 里面出现一次 ``datetime.now()``，"同一脚本回放
两次产生完全相同的记录"这条就永远做不到。时间戳属于**事件**，不属于**状态**。

**二、状态里的每一样东西都是 Pydantic 模型或枚举。** checkpoint 的序列化器
（msgpack）只放行白名单里的类型，而白名单是从**状态模式本身**算出来的
（:mod:`orchestration.langgraph.serialization`）：Pydantic 模型与枚举收，
**dataclass 不收**。所以这里没有 dataclass。

**三、循环 B 不碰执行器。** 状态里没有执行器、没有会话、没有 diff ——
那些是**循环 A** 的东西（:mod:`core.task.execution_loop`）。循环 B 只知道一个
口：把计划交出去，剩下的由循环 A 承接。这不是分工上的客套，是 design.md §4
"这两个循环不能合并成一个无限执行 Prompt"的**结构性**落地：一旦循环 B 自己
能执行代码，"Coding Agent 不断创造新的开发任务，让项目陷入无意义的自我修改"
那条风险就再也拦不住了。

**触发源有六个，不是六个"定时器"。** design.md §4 明写"持续演进应该由事件驱动，
而不是死循环"，并给了六种触发源。每一轮演进都必须说得出**是被什么触发的** ——
说不出的那一轮，事后没人能判断"该不该在这一轮做这件事"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.evolution.candidate import DiscoveryResult, EvolutionCandidate
from core.evolution.decision import DecisionRound
from core.evolution.priority import ActualOutcome, ScoredCandidate
from core.metrics.cycle import CycleCounts, CycleStatus, CycleVerdict, classify_cycle
from core.planning.roadmap import DecisionApproval, EvolutionRoadmap, TaskDraft
from core.planning.stages import GapAnalysis
from core.task.outcome import TaskOutcome


class LoopBNode(StrEnum):
    """循环 B 的十五个节点 —— **顺序就是设计文档 §4 的顺序**。

    枚举成员的声明顺序与 :data:`LOOP_B_ORDER` 一致；两者不一致时
    ``tests/unit/core/test_evolution_loop.py`` 会转红。

    带问号的节点（``WORTH_DOING``）是**分支点** —— 它的结论决定这一轮要不要
    继续往下规划。
    """

    OBSERVE_PROJECT = "observe_project"
    BUILD_PROJECT_SNAPSHOT = "build_project_snapshot"
    ANALYZE_GOAL_GAP = "analyze_goal_gap"
    DISCOVER_CANDIDATES = "discover_candidates"
    GATHER_EVIDENCE = "gather_evidence"
    EVALUATE_VALUE_RISK_COST = "evaluate_value_risk_cost"
    WORTH_DOING = "worth_doing"
    RECORD_DEFER_REJECT = "record_defer_reject"
    GENERATE_EVOLUTION_PLAN = "generate_evolution_plan"
    DECOMPOSE_INTO_TASKS = "decompose_into_tasks"
    DEPENDENCY_AND_RESOURCE_SCHEDULING = "dependency_and_resource_scheduling"
    EXECUTE_ENGINEERING_CYCLE = "execute_engineering_cycle"
    EVALUATE_ACTUAL_OUTCOME = "evaluate_actual_outcome"
    UPDATE_PROJECT_KNOWLEDGE = "update_project_knowledge"
    UPDATE_GOAL_GAP = "update_goal_gap"


#: 十五个节点的固定次序 —— 与 design.md §4 的节点链逐字对应。
LOOP_B_ORDER: Final[tuple[LoopBNode, ...]] = (
    LoopBNode.OBSERVE_PROJECT,
    LoopBNode.BUILD_PROJECT_SNAPSHOT,
    LoopBNode.ANALYZE_GOAL_GAP,
    LoopBNode.DISCOVER_CANDIDATES,
    LoopBNode.GATHER_EVIDENCE,
    LoopBNode.EVALUATE_VALUE_RISK_COST,
    LoopBNode.WORTH_DOING,
    LoopBNode.RECORD_DEFER_REJECT,
    LoopBNode.GENERATE_EVOLUTION_PLAN,
    LoopBNode.DECOMPOSE_INTO_TASKS,
    LoopBNode.DEPENDENCY_AND_RESOURCE_SCHEDULING,
    LoopBNode.EXECUTE_ENGINEERING_CYCLE,
    LoopBNode.EVALUATE_ACTUAL_OUTCOME,
    LoopBNode.UPDATE_PROJECT_KNOWLEDGE,
    LoopBNode.UPDATE_GOAL_GAP,
)


class TriggerSource(StrEnum):
    """触发源 —— 六种，与 design.md §4 的表逐条对应。

    **取值与数据库那一列的默认口径一致**（``evolution_cycle.trigger`` 的默认值
    是 ``schedule``）。词表只有一份，两边才不会各写各的。

    **六个不是"六种定时器"。** 定时触发只是其中一种：代码事件（提交、合并
    请求）、质量事件（持续集成失败、覆盖率下降）、运行事件（性能退化、异常
    增加）、外部事件（用户反馈、需求变化）、知识事件（依赖漏洞、技术升级）
    各有各的响应方式 —— 事件驱动与定期扫描结合，才能既不漏局部变化，也不漏
    跨模块的长期积累。
    """

    #: 定时触发：每日、每周的全项目扫描。
    SCHEDULE = "schedule"
    #: 代码事件：提交、合并请求 —— 重新分析变更影响。
    CODE = "code"
    #: 质量事件：持续集成失败、覆盖率下降 —— 自动分析和修复。
    QUALITY = "quality"
    #: 运行事件：性能退化、异常增加 —— 创建诊断任务。
    RUNTIME = "runtime"
    #: 外部事件：用户反馈、需求变化 —— 重新规划。
    EXTERNAL = "external"
    #: 知识事件：依赖漏洞、技术升级 —— 评估是否需要演进。
    KNOWLEDGE = "knowledge"


#: 每种触发源在设计文档里的名字。留着它是为了让"这轮是被什么触发的"这句话
#: 在读记录的人脑子里有对应的画面，而不是六个英文单词。
TRIGGER_LABELS: Final[MappingProxyType[TriggerSource, str]] = MappingProxyType(
    {
        TriggerSource.SCHEDULE: "定时触发",
        TriggerSource.CODE: "代码事件",
        TriggerSource.QUALITY: "质量事件",
        TriggerSource.RUNTIME: "运行事件",
        TriggerSource.EXTERNAL: "外部事件",
        TriggerSource.KNOWLEDGE: "知识事件",
    }
)


class ProjectObservation(BaseModel):
    """观察项目的产物 —— 项目现在是什么样子。

    **快照不是观察，观察是快照的来源。** 观察是一次**读**：此刻的提交、分支、
    度量、目标、技术栈。快照是观察的结果被**固定下来**之后的东西（见
    :class:`SnapshotView`）。两者混成一个模型，"这一轮看到的是哪一刻"就再也
    说不清了。

    **目标达成度可以没有。** 没有就写没有（``None``）—— 拿一个默认值填上，
    等于凭空断言"项目离目标还有一半"，而那会让"有没有变好"这件事永远算不出
    差值（:mod:`core.metrics.cycle` 的 ``classify_cycle`` 靠它判定结局）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    #: 观察时刻的提交。
    commit: str = Field(min_length=1)
    #: 观察时刻的分支。
    branch: str = ""
    #: 项目目标（人类可读的原文）。
    goal: str = ""
    #: 技术栈。
    tech_stack: tuple[str, ...] = ()
    #: 观察到的度量值。
    metrics: dict[str, float] = Field(default_factory=dict)
    #: 目标达成度（0~1）。``None`` 表示没有可比的前值。
    attainment: Decimal | None = Field(default=None, ge=0, le=1)
    #: **上一份**快照记下的目标达成度。差值就是这一轮的改善 ——
    #: 没有它就没有可比的前值，结局只能是"没有可验证的改善"。
    previous_attainment: Decimal | None = Field(default=None, ge=0, le=1)
    #: 这个项目**最新一份**快照的版本号。下一份快照的版本号是它加一。
    latest_snapshot_version: int = Field(default=0, ge=0)


class SnapshotView(BaseModel):
    """项目快照 —— 观察的定格。

    **版本号在项目内单调递增**（``project_snapshot`` 上有
    ``(project_id, version)`` 唯一约束）。重复的版本号会当场炸出来，而不是悄悄
    覆盖掉上一份快照 —— 覆盖掉的那一份，事后没人知道它曾经存在过。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    #: 这一份快照的版本号（1 起）。
    version: int = Field(ge=1)
    commit: str = Field(min_length=1)
    branch: str = ""
    metrics: dict[str, float] = Field(default_factory=dict)
    #: 目标达成度（0~1）。``None`` 表示没有可比的前值。
    attainment: Decimal | None = Field(default=None, ge=0, le=1)

    @classmethod
    def from_observation(cls, observation: ProjectObservation) -> SnapshotView:
        """由观察装配快照。版本号是"最新版本 + 1"。"""
        return cls(
            project_id=observation.project_id,
            version=observation.latest_snapshot_version + 1,
            commit=observation.commit,
            branch=observation.branch,
            metrics=dict(observation.metrics),
            attainment=observation.attainment,
        )


class CandidateEvidence(BaseModel):
    """一个候选的证据清单。

    **引用去重，但保留原有次序。** 去重是为了"这份证据被数了一次"；保留次序是
    因为证据的强弱有别 —— 第一条往往是直接指向问题的那一条。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    #: 证据引用（去重后，按发现次序）。
    refs: tuple[str, ...] = ()


class DroppedCandidate(BaseModel):
    """一个被丢掉的候选 —— 连同理由。

    **丢弃也要留痕。** 什么都不留的话，读的人分不清"判断为不成立"与"压根没
    看见"，而这两种情况的补救动作完全相反（与决策三态、趋势丢弃同一套理由）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EvidenceBundle(BaseModel):
    """一轮里所有候选的证据汇总。

    **它必须给每一个候选一个说法。** 汇总之后，候选要么在 :attr:`kept` 里，
    要么在 :attr:`dropped` 里 —— 谁都不许凭空消失。悄悄丢掉的候选，事后看着
    就是"这轮没发现什么"，而实际上它被发现过、只是没人说得清为什么没算数。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 留下的候选与它们的证据。
    kept: tuple[CandidateEvidence, ...] = ()
    #: 被丢掉的候选与理由。
    dropped: tuple[DroppedCandidate, ...] = ()
    #: 这一轮用到的全部证据引用（去重、排序）。
    refs: tuple[str, ...] = ()

    @property
    def accounted_for(self) -> frozenset[str]:
        """被说清楚的候选 id。"""
        return frozenset(item.candidate_id for item in self.kept) | frozenset(
            item.candidate_id for item in self.dropped
        )


class EvolutionPlan(BaseModel):
    """演进计划 —— 这一轮要做成哪些任务。

    **草稿与放行凭据放在一起。** 放行与否是对**某一条决策**的结论；两者放在
    同一个产物里，它们就不可能在传递过程中走散（与
    :class:`core.planning.stages.DecisionOutcome` 同一套理由）。

    **任务标识不许重复。** 重复的标识会让依赖边指向"哪一个"变得不确定 ——
    而依赖边指向的是标识，不是任务行。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 拆解草稿。
    drafts: tuple[TaskDraft, ...] = ()
    #: 放行凭据。
    approvals: tuple[DecisionApproval, ...] = ()

    @model_validator(mode="after")
    def _no_duplicate_keys(self) -> Self:
        keys = [draft.key for draft in self.drafts]
        if len(keys) != len(set(keys)):
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            raise ValueError(f"任务标识重复了：{'、'.join(duplicates)}")
        return self


class ResourceAssignment(BaseModel):
    """一份派工 —— 哪个任务、哪一波、谁来干、多少钱。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 路线图里的任务标识。
    task_key: str = Field(min_length=1)
    #: 第几波。同波可并行。
    wave: int = Field(ge=1)
    #: 承接这个任务的执行器（标识）。
    executor: str = ""
    #: 这个任务的预算上限。
    budget: Decimal | None = Field(default=None, ge=0)


class ResourceSchedule(BaseModel):
    """资源排期 —— 依赖与资源合在一起的那一份。

    **波次必须尊重依赖。** 后继任务的波次不得小于前驱 —— 否则"先做哪个"就
    排错了，而排错的后果是**在一件还没做完的事上开工**，且不会报错。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 每一份派工。
    assignments: tuple[ResourceAssignment, ...] = ()
    #: 这一轮的总预算。
    budget: Decimal | None = Field(default=None, ge=0)

    @property
    def by_key(self) -> Mapping[str, ResourceAssignment]:
        """按任务标识索引派工。"""
        return MappingProxyType({item.task_key: item for item in self.assignments})


class ScheduleError(RuntimeError):
    """排期与路线图对不上。"""


def check_resource_schedule(roadmap: EvolutionRoadmap, schedule: ResourceSchedule) -> None:
    """派工必须**覆盖每一个任务**，且**不得让后继早于前驱**。

    **两条都要断。** 少派一个任务，那个任务就没人做 —— 而排期表看上去仍然
    完整；让后继早于前驱开工，则是在一件还没做完的事上动土。
    """
    assigned = schedule.by_key
    missing = sorted({task.key for task in roadmap.tasks} - set(assigned))
    if missing:
        raise ScheduleError(f"这些任务没有派工：{'、'.join(missing)}")

    unknown = sorted(set(assigned) - {task.key for task in roadmap.tasks})
    if unknown:
        raise ScheduleError(f"派工里有路线图上没有的任务：{'、'.join(unknown)}")

    for edge in roadmap.dependencies:
        predecessor = assigned[edge.predecessor_key].wave
        successor = assigned[edge.successor_key].wave
        if successor <= predecessor:
            raise ScheduleError(
                f"任务 {edge.successor_key!r}（第 {successor} 波）不能早于它的前置 "
                f"{edge.predecessor_key!r}（第 {predecessor} 波）"
            )


class TaskExecution(BaseModel):
    """一个任务被执行之后的结果。

    **验收结论不来自执行器的自我报告。** :attr:`accepted`` 取的是**执行层判定**
    的结果（:func:`core.task.outcome.resolve_outcome` 只看执行器报的状态与独立
    验证的结论）—— "Agent 说自己做完了"永远不等于"任务完成"（design.md §3.4）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 路线图里的任务标识。
    task_key: str = Field(min_length=1)
    #: 落库后的任务行 id（UUID 字符串）。
    task_id: str = ""
    #: 验收是否通过。
    accepted: bool = False
    #: 执行层给出的结论。
    outcome: TaskOutcome | None = None
    #: 没通过时的理由。
    reason: str = ""


class CycleExecution(BaseModel):
    """一轮工程执行的结果 —— 循环 A 交回来的东西。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 逐个任务的结果，按排期次序。
    tasks: tuple[TaskExecution, ...] = ()

    @property
    def completed(self) -> int:
        """验收通过的任务数。"""
        return sum(1 for item in self.tasks if item.accepted)


class CycleOutcomeReport(BaseModel):
    """一轮演进的实际结果 —— 当初的判断准不准。

    **结局是**判定**出来的，不是填报的。** :attr:`verdict` 由
    :func:`core.metrics.cycle.classify_cycle` 给出，调用方不能自己填。

    **这份判定是"编排层"的判定；落库时仓储层会按库里实际的行数再判一次。**
    以落库的那一份为准 —— 那一份数的是真实行数，而这一份数的是编排层手里
    的执行结果。两者不一致时，差异本身就是一条信息。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 每个候选的实际收益。
    outcomes: tuple[ActualOutcome, ...] = ()
    #: 周期怎么结束的。
    status: CycleStatus = CycleStatus.COMPLETED
    #: 周期开始前的目标达成度。
    attainment_before: Decimal | None = Field(default=None, ge=0, le=1)
    #: 周期结束后的目标达成度。
    attainment_after: Decimal | None = Field(default=None, ge=0, le=1)
    #: 结局判定（结论 + 理由）。
    verdict: CycleVerdict | None = None


class KnowledgeUpdate(BaseModel):
    """项目知识的更新 —— 这一轮在库里留下了什么。

    **两样东西缺一不可。** 只有周期记录，回答不了"项目此刻是什么样"；只有
    快照，记录不了"这一轮做了什么"。两条都要留，事后才能把"这一轮的判断"与
    "这一轮之后的项目"对起来看。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 落库的周期行 id。
    cycle_ref: str = ""
    #: 落库的快照行 id。
    snapshot_ref: str = ""
    #: 落库的快照版本号。
    snapshot_version: int = Field(default=0, ge=0)
    #: 周期怎么结束的。
    status: CycleStatus = CycleStatus.COMPLETED


class LoopBState(BaseModel):
    """循环 B 累积的状态。

    **它是一个 Pydantic 模型，不是一个 dict** —— 因为它要被写进 checkpoint：
    一个"看起来像 dict、其实字段随时间变"的字典存下去就再也读不出类型了。

    **状态里没有执行器、没有会话、没有 diff。** 那些是循环 A 的东西。这里只有
    "计划"与"计划的执行结果" —— 循环 B 看得见循环 A 交回来的结论，看不见它
    怎么干活的。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    #: 这一轮演进的编号，如 ``cycle-0007``。
    cycle_id: str = Field(min_length=1)
    #: 是什么触发了这一轮。
    trigger: TriggerSource = TriggerSource.SCHEDULE
    #: 周期怎么结束的。被预算掐断的周期是 ``budget_exhausted``（TASK-P1-063）。
    status: CycleStatus = CycleStatus.COMPLETED

    observation: ProjectObservation | None = None
    #: **收尾时再观察一次**得到的观察结果 —— 这一轮干完活之后项目长什么样。
    #:
    #: **为什么要有第二次观察。** 达成度的差值要能说明"**这一轮**变好多少"，
    #: 而开工前的那次观察只给出"开工前是什么样" —— 拿它跟上一轮的快照比，
    #: 比出来的是**上一轮**的改善，不是这一轮的。差值晚一轮，等于每一轮都在
    #: 替上一轮记账。
    #:
    #: **它是可选的。** 没有第二次观察时，结局判定退回"上一次观察 vs 快照"
    #: 那套口径 —— 与这个字段出现之前完全一样。
    post_observation: ProjectObservation | None = None
    snapshot: SnapshotView | None = None
    gaps: GapAnalysis | None = None
    discovery: DiscoveryResult | None = None
    evidence: EvidenceBundle | None = None
    #: 评过分的候选。
    scored: tuple[ScoredCandidate, ...] = ()
    #: 这一轮的决策。
    decisions: DecisionRound | None = None
    #: 值不值得做 —— 分支点。
    worth_doing: bool = False
    plan: EvolutionPlan | None = None
    roadmap: EvolutionRoadmap | None = None
    schedule: ResourceSchedule | None = None
    execution: CycleExecution | None = None
    outcome: CycleOutcomeReport | None = None
    knowledge: KnowledgeUpdate | None = None
    #: 下一轮的目标差距。
    next_gaps: GapAnalysis | None = None

    #: 落库的周期行 id。
    cycle_ref: str = ""
    #: 候选领域 id → 候选行 id。
    candidate_rows: dict[str, str] = Field(default_factory=dict)
    #: 决策领域 id → 决策行 id。
    decision_rows: dict[str, str] = Field(default_factory=dict)
    #: 任务标识 → 任务行 id。
    task_rows: dict[str, str] = Field(default_factory=dict)
    #: 走过的节点（按顺序）。
    visited_nodes: tuple[LoopBNode, ...] = ()


def record_node(state: LoopBState, node: LoopBNode) -> LoopBState:
    """把"走到哪个节点了"记进状态。

    **节点可以重复出现。** 循环 B 的一轮里没有返修环，但"这一轮被触发了几次"
    这件事要看得出差别 —— 所以这是"走过的顺序"，不是"走过的集合"。
    """
    return state.model_copy(update={"visited_nodes": (*state.visited_nodes, node)})


def gather_evidence(discovery: DiscoveryResult) -> EvidenceBundle:
    """把这一轮所有候选的证据收拢成一份清单。

    **每个候选都要有说法。** 留下的写进 :attr:`EvidenceBundle.kept`，没有证据
    的写进 :attr:`EvidenceBundle.dropped` 并说清理由 —— 谁都不许凭空消失。

    **没有证据的候选不是候选，是猜测。** 它连"值不值得做"都没资格参与判断 ——
    让它进评估，等于让一句"我觉得应该做"混进一份看起来有依据的清单里。
    """
    kept: list[CandidateEvidence] = []
    dropped: list[DroppedCandidate] = []
    refs: list[str] = []

    for candidate in discovery.candidates:
        candidate_refs = tuple(dict.fromkeys(item.ref for item in candidate.evidence))
        if not candidate_refs:
            dropped.append(
                DroppedCandidate(
                    candidate_id=candidate.id,
                    reason="没有可核验的证据 —— 这是猜测，不是候选",
                )
            )
            continue
        kept.append(CandidateEvidence(candidate_id=candidate.id, refs=candidate_refs))
        for ref in candidate_refs:
            if ref not in refs:
                refs.append(ref)

    return EvidenceBundle(
        kept=tuple(kept),
        dropped=tuple(dropped),
        refs=tuple(sorted(refs)),
    )


def evaluate_outcome(state: LoopBState) -> CycleOutcomeReport:
    """把这一轮的执行结果与当初的判断对起来。

    **实际收益必须是一个数。** "做完了，感觉不错"没法用来校准任何东西 ——
    :class:`core.evolution.priority.ActualOutcome` 的 ``realized_value`` 落在
    0~1，与当初的 ``V`` 同一量纲，才能把当初的判断与实际结果放在一起比。

    **收益按"验收通过的任务数"折算。** 一个候选派生的任务全过了就算拿到了它
    承诺的收益；一个都没过就没有 —— 中间的折算按比例给，理由写在 note 里。

    **任务标识要换算成候选 id 才对得上。** 执行结果按路线图里的任务标识排，
    而实际收益是**按候选**记的 —— 中间隔着"一个候选派生出几个任务"这一步。

    **前值与后值怎么取，看有没有收尾观察**（见 :func:`_attainment_pair`）：
    有收尾观察时，差值是**这一轮**推进出来的；没有时，退回"上一份快照 vs
    这一轮观察"那套晚一轮的口径。没有可比的前值时前值就是 ``None``，于是
    结局是"没有可验证的改善"而不是"达成度没变" ——
    缺数据不是零（:func:`core.metrics.cycle.classify_cycle` 的第四条）。
    """
    decisions = state.decisions
    roadmap = state.roadmap
    execution = state.execution
    if decisions is None or roadmap is None or execution is None:
        raise ScheduleError("评估实际结果之前，决策、路线图与执行结果都必须在状态里")

    accepted_by_candidate: dict[str, list[bool]] = {}
    for item in execution.tasks:
        try:
            planned = roadmap.task_by_key(item.task_key)
        except KeyError as error:
            raise ScheduleError(f"执行结果里有路线图上没有的任务：{item.task_key!r}") from error
        accepted_by_candidate.setdefault(planned.candidate_id, []).append(item.accepted)

    outcomes: list[ActualOutcome] = []
    for decision in decisions.recorded:
        accepted = accepted_by_candidate.get(decision.candidate_id, [])
        if not accepted:
            continue
        passed = sum(1 for item in accepted if item)
        outcomes.append(
            ActualOutcome(
                candidate_id=decision.candidate_id,
                realized_value=Decimal(passed) / Decimal(len(accepted)),
                note=f"{len(accepted)} 个任务里通过了 {passed} 个",
            )
        )

    before, after = _attainment_pair(state)
    attainment_before = before
    attainment_after = after
    counts = _execution_counts(decisions, execution)
    verdict = classify_cycle(
        state.status,
        counts,
        attainment_before=attainment_before,
        attainment_after=attainment_after,
    )
    return CycleOutcomeReport(
        outcomes=tuple(outcomes),
        status=state.status,
        attainment_before=attainment_before,
        attainment_after=attainment_after,
        verdict=verdict,
    )


def _execution_counts(decisions: DecisionRound, execution: CycleExecution) -> CycleCounts:
    """这一轮的执行计数（编排层口径）。

    **这是编排层的口径，不是数据库的口径。** 数据库那边数的是真实行数
    （:meth:`infrastructure.database.repositories.CycleRepository.counts`），
    落库时以那一份为准。
    """
    return CycleCounts(
        candidates=len(decisions.decisions),
        decisions=decisions.total,
        tasks=len(execution.tasks),
        completed_tasks=execution.completed,
    )


def empty_cycle_outcome(state: LoopBState) -> CycleOutcomeReport:
    """一轮"什么都没做"的结局。

    **什么时候用。** "值不值得做"的答案是"不值得"时，这一轮不规划、不拆解、
    不执行 —— 但它**仍然是一轮**：决策被记下了，周期要结束，项目知识要更新。
    那条路上的结局由这里给出。

    **结局是 ``stalled``，不是"没有结论"。** 一个任务都没落地，这一轮就
    没有产出 —— 而"没有产出"是一条**结论**（"我们判断这轮不值得做"），
    不是"没有结论"。缺数据与零产出是两回事：缺数据是"说不清"，零产出是
    "说清了，什么都没落地"。

    **候选数与决策数仍然要数。** 那一轮发现了什么、决定了什么，都是真实发生
    的事 —— 只有"落地的产出"是零。把前两项也清零，周期记录就会少掉这一轮
    唯一说清的东西。
    """
    candidates = len(state.discovery.candidates) if state.discovery else 0
    decisions = state.decisions.total if state.decisions else 0
    counts = CycleCounts(
        candidates=candidates,
        decisions=decisions,
        tasks=0,
        completed_tasks=0,
    )
    before, after = _attainment_pair(state)
    attainment_before = before
    attainment_after = after
    verdict = classify_cycle(
        state.status,
        counts,
        attainment_before=attainment_before,
        attainment_after=attainment_after,
    )
    return CycleOutcomeReport(
        status=state.status,
        attainment_before=attainment_before,
        attainment_after=attainment_after,
        verdict=verdict,
    )


def _attainment_pair(state: LoopBState) -> tuple[Decimal | None, Decimal | None]:
    """这一轮的前后达成度 —— **(前值, 后值)**。

    **有收尾观察时，用收尾观察。** 收尾观察给出的是"这一轮干完活之后的
    达成度"，而它的 ``previous_attainment`` 是"上一次观察到的达成度"——
    也就是**这一轮开工前**的那个数。差值因此是**这一轮**推进出来的，
    而不是上一轮的。

    **没有收尾观察时，用开工前那次观察与快照。** 那正是这个字段出现之前的
    口径 —— 快照是开工时的定格，所以后值是"开工时的达成度"。这套口径下
    差值晚一轮（比的是这一轮与上一轮的开工时刻），但它至少有值可比，
    而"有值可比"比"差值晚一轮"更要紧。
    """
    post = state.post_observation
    if post is not None:
        return post.previous_attainment, post.attainment
    observation = state.observation
    snapshot = state.snapshot
    before = observation.previous_attainment if observation is not None else None
    after = snapshot.attainment if snapshot is not None else None
    return before, after


@runtime_checkable
class EvolutionJournalPort(Protocol):
    """循环 B 的落库口。

    **为什么是一个端口而不是直接写库。** 领域层不该知道数据库存在（分层：
    ``infrastructure → core``）；而"这一轮做了什么必须留下痕迹"是领域规则 ——
    所以规则写在领域层，写库的实现放在基础设施层
    （:mod:`infrastructure.database.evolution_journal`）。

    **写失败必须炸出来。** 与循环 A 的留痕口同一条理由（design.md §3.5D）：
    记录写不进去还往下跑，等于这一轮没有发生过 —— 而"没有发生过"这件事恰恰
    是事后才被发现的。

    **每一步都返回领域 id 与行 id 的对应关系。** 领域侧的标识（``"EVD-cycle-0007-002"``）
    与数据库侧的主键（uuid）是两回事，对应关系只有刚记下这些行的那一端知道 ——
    猜错了就是任务挂到了别的决策上，而**不会报错**。
    """

    async def open_cycle(self, *, project_id: str, cycle_id: str, trigger: str) -> str:
        """开一个周期，返回它的行 id。"""
        ...

    async def record_candidates(
        self,
        cycle_ref: str,
        candidates: Sequence[EvolutionCandidate],
    ) -> Mapping[str, str]:
        """登记这一轮发现的候选，返回「候选领域 id → 候选行 id」。"""
        ...

    async def record_decisions(
        self,
        cycle_ref: str,
        decisions: DecisionRound,
        candidate_rows: Mapping[str, str],
    ) -> Mapping[str, str]:
        """登记这一轮的决策，返回「决策领域 id → 决策行 id」。"""
        ...

    async def land_tasks(
        self,
        roadmap: EvolutionRoadmap,
        decision_rows: Mapping[str, str],
    ) -> Mapping[str, str]:
        """把路线图落成任务行，返回「任务标识 → 任务行 id」。"""
        ...

    async def record_snapshot(
        self,
        *,
        project_id: str,
        version: int,
        commit: str,
        branch: str,
        metrics: Mapping[str, float],
    ) -> str:
        """落一份项目快照，返回它的行 id。"""
        ...

    async def close_cycle(
        self,
        cycle_ref: str,
        *,
        status: CycleStatus,
        attainment_before: Decimal | None,
        attainment_after: Decimal | None,
        evidence_refs: Sequence[str],
    ) -> None:
        """结束周期，写入完整记录。"""
        ...


__all__ = [
    "LOOP_B_ORDER",
    "TRIGGER_LABELS",
    "CandidateEvidence",
    "CycleExecution",
    "CycleOutcomeReport",
    "DroppedCandidate",
    "EvidenceBundle",
    "EvolutionJournalPort",
    "EvolutionPlan",
    "KnowledgeUpdate",
    "LoopBNode",
    "LoopBState",
    "ProjectObservation",
    "ResourceAssignment",
    "ResourceSchedule",
    "ScheduleError",
    "SnapshotView",
    "TaskExecution",
    "TriggerSource",
    "check_resource_schedule",
    "empty_cycle_outcome",
    "evaluate_outcome",
    "gather_evidence",
    "record_node",
]
