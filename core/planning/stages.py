"""Director 五阶段的契约（纯领域层）。

设计依据：docs/design.md §5（五阶段：认知 → 诊断 → 探索 → 决策 → 规划）、
docs/tasks.md TASK-P1-050。

**这里放什么、不放什么。** 这一层只回答三件事：每个阶段的输入是什么、产出
是什么、产出对不对。它**不知道编排器是谁** —— 底下是 LangGraph、是一串
``await``、还是人手敲的调用，契约都是同一份。

**为什么契约要写在领域层，而不是编排层。** 编排器**不校验**状态：实测把一个
整数字段写成字符串，编排器照跑不误，最后返回一份类型不对的状态（见
``tests/unit/orchestration/test_decision_graph.py`` 里盯着这件事的那条用例）。
所以"这一步的产出对不对"必须由我们自己判 —— 而判据是领域知识（这条差距有没有
证据、这份决策是不是 ``record``），它属于领域层。编排层只负责**按顺序调用**
与**持久化**。

**五阶段各自产出什么。**

    认知 UNDERSTAND  项目快照、目标、历史      → ProjectAssessment
    诊断 DIAGNOSE    目标差距、质量指标          → GapAnalysis
    探索 EXPLORE     差距                        → DiscoveryResult
    决策 DECIDE      候选、预算、约束            → DecisionOutcome
    规划 PLAN        已批准的决策                → EvolutionRoadmap

**"认知"与"诊断"为什么要拆成两个产物。** ``ProjectAssessment`` 一份里同时装着
项目状态、目标差距与演进机会 —— 那是**给人看的全貌**。但流水线上它们是两道
工序：认知回答"项目现在是什么样子"，诊断回答"离目标还差什么"，而机会是**探索**
的产物，不是认知的。所以诊断阶段把 ``ProjectAssessment`` **收窄**成
:class:`GapAnalysis`（只留状态、差距与暂缓项），机会留给下一段。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, Protocol, overload

from pydantic import BaseModel, ConfigDict, Field

from core.evolution.assessment import (
    DeferredOpportunity,
    GoalGap,
    OverallStatus,
    ProjectAssessment,
)
from core.evolution.candidate import DiscoveryResult
from core.evolution.decision import DecisionRound
from core.planning.roadmap import DecisionApproval, EvolutionRoadmap, PlanningError
from core.project.models import ProjectGoal


class Stage(StrEnum):
    """决策链上的五个阶段。"""

    UNDERSTAND = "understand"
    DIAGNOSE = "diagnose"
    EXPLORE = "explore"
    DECIDE = "decide"
    PLAN = "plan"


#: 五阶段的固定次序 —— 认知 → 诊断 → 探索 → 决策 → 规划。
#:
#: **次序不是约定，是依赖。** 后一段的输入是前一段的产出：诊断读认知的结论、
#: 探索读诊断的差距、决策读探索的候选、规划读决策的结论。次序乱了，产物就
#: 对不上 —— 而对不上时**不会报错**，只会得到一份看起来完整的错误计划。
STAGE_ORDER: Final[tuple[Stage, ...]] = (
    Stage.UNDERSTAND,
    Stage.DIAGNOSE,
    Stage.EXPLORE,
    Stage.DECIDE,
    Stage.PLAN,
)


class StageContractError(PlanningError):
    """某个阶段没有满足它的契约。

    **这类错误必须响亮。** 阶段契约被违反时，流水线的产出就是一份错的计划 ——
    而错的计划不会自己报错：它看起来结构完整，只是接在错误的输入上。所以
    契约一破就地停下，而不是"接着往下跑看看"。
    """

    def __init__(self, stage: Stage, message: str) -> None:
        super().__init__(f"阶段 {stage.value} 的契约没满足：{message}")
        self.stage = stage
        self.message = message


class ProjectContext(BaseModel):
    """认知阶段的输入：项目快照、目标与历史。

    **目标是必填的，不是可选项。** "离目标还有多远"这个问题，没有目标就
    没有答案 —— 而一份没有目标的项目评估（``overall_status`` 恒为某个默认
    值、差距恒为空）看起来和真的评估一模一样。目标六要素不齐时
    :class:`~core.project.models.ProjectGoal` 自己就会拒绝，所以这条链上
    不会出现"目标缺失但流程照跑"。

    **历史只是一串文本。** 决策链要读的是"这个项目走过什么"，而在阶段一的
    库里那是**已经发生过的事**（提交、决策、周期摘要），不是可以被模型编
    出来的结论。所以这里只给引用与文本，事实由取事实的那一端去查。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    goal: ProjectGoal
    history: tuple[str, ...] = ()


class GapAnalysis(BaseModel):
    """诊断阶段的产物：项目离目标还有多远，以及哪些事现在还不该做。

    **这是 ``ProjectAssessment`` 的收窄，不是它的复制。** 认知阶段那份评估
    里还有演进机会与建议 —— 那是**探索**的原料。诊断只回答"差距在哪、什么
    时候动"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    overall_status: OverallStatus
    gaps: tuple[GoalGap, ...] = ()
    deferred: tuple[DeferredOpportunity, ...] = ()

    @classmethod
    def from_assessment(cls, assessment: ProjectAssessment) -> GapAnalysis:
        """从认知阶段的评估里取出诊断结论。"""
        return cls(
            project_id=assessment.project_id,
            snapshot=assessment.snapshot,
            overall_status=assessment.overall_status,
            gaps=assessment.goal_gap,
            deferred=assessment.deferred,
        )


class DecisionOutcome(BaseModel):
    """决策阶段的产物：这一轮的决策，以及每条决策的放行凭据。

    **凭据跟着决策走。** 放行与否是对**某一条决策**的结论，不是对整轮的
    结论 —— 同一轮里有的决策该做、有的该缓。凭据与决策放在同一个产物里，
    它们就不可能在传递过程中走散。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions: DecisionRound
    approvals: tuple[DecisionApproval, ...] = ()

    def approval_for(self, decision_id: str) -> DecisionApproval | None:
        """某条决策的放行凭据。没有就是没有 —— 不猜。"""
        for approval in self.approvals:
            if approval.decision_id == decision_id:
                return approval
        return None


class PipelineState(BaseModel):
    """五阶段流水线累积的状态。

    **它是一个 Pydantic 模型，不是一个 dict。** 因为它要被存进 checkpoint ——
    一个"看起来像 dict、其实字段随时间变"的字典存下去就再也读不出类型了。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cycle_id: str = Field(min_length=1)
    context: ProjectContext
    assessment: ProjectAssessment | None = None
    gaps: GapAnalysis | None = None
    discovery: DiscoveryResult | None = None
    outcome: DecisionOutcome | None = None
    roadmap: EvolutionRoadmap | None = None
    completed_stages: tuple[Stage, ...] = ()


#: 每个阶段产出落在状态的哪个字段上。
_STAGE_FIELDS: Final[dict[Stage, str]] = {
    Stage.UNDERSTAND: "assessment",
    Stage.DIAGNOSE: "gaps",
    Stage.EXPLORE: "discovery",
    Stage.DECIDE: "outcome",
    Stage.PLAN: "roadmap",
}

#: 每个阶段**产出**必须是什么类型。
_STAGE_ARTIFACT_TYPES: Final[dict[Stage, type[object]]] = {
    Stage.UNDERSTAND: ProjectAssessment,
    Stage.DIAGNOSE: GapAnalysis,
    Stage.EXPLORE: DiscoveryResult,
    Stage.DECIDE: DecisionOutcome,
    Stage.PLAN: EvolutionRoadmap,
}

#: 每个阶段的**输入**取自状态的哪个字段。
#:
#: **这张表与 ``_STAGE_FIELDS`` 是一对。** 上一个阶段的产出落在哪个字段，
#: 下一个阶段就从哪个字段读 —— 两张表对不上，输入就会取错（而取错的输入
#: 类型还对得上，只是不对题）。
_STAGE_INPUT_FIELDS: Final[dict[Stage, str]] = {
    Stage.UNDERSTAND: "context",
    Stage.DIAGNOSE: "assessment",
    Stage.EXPLORE: "gaps",
    Stage.DECIDE: "discovery",
    Stage.PLAN: "outcome",
}


def next_stage(state: PipelineState) -> Stage | None:
    """下一个该跑的阶段。跑完了就是 ``None``。"""
    for stage in STAGE_ORDER:
        if stage not in state.completed_stages:
            return stage
    return None


def stage_field(stage: Stage) -> str:
    """这个阶段的产物落在状态的哪个字段上。"""
    return _STAGE_FIELDS[stage]


@overload
def stage_input(state: PipelineState, stage: Literal[Stage.UNDERSTAND]) -> ProjectContext: ...


@overload
def stage_input(state: PipelineState, stage: Literal[Stage.DIAGNOSE]) -> ProjectAssessment: ...


@overload
def stage_input(state: PipelineState, stage: Literal[Stage.EXPLORE]) -> GapAnalysis: ...


@overload
def stage_input(state: PipelineState, stage: Literal[Stage.DECIDE]) -> DiscoveryResult: ...


@overload
def stage_input(state: PipelineState, stage: Literal[Stage.PLAN]) -> DecisionOutcome: ...


def stage_input(state: PipelineState, stage: Stage) -> object:
    """这个阶段的输入。

    **缺了就是状态不自洽，就地报错。** 典型的情形是一份坏掉的 checkpoint：
    "认知阶段标记为已完成，评估却不在状态里"。那样的状态不能被接着跑下去 ——
    跑下去得到的是一份"看起来完整"的错计划。

    **为什么放在领域层。** "每个阶段读哪个字段"是契约的一部分，与编排器是谁
    无关；而编排层要是自己写一遍，就得在每个节点里重复一遍判断 —— 而那些
    判断迟早与领域层的那份对不上。

    **为什么类型是逐阶段写死的。** 调用方拿到的东西**就是**该阶段的输入类型，
    不用再自己断言一次；而取错字段这件事在类型上就露出来了（``GapAnalysis``
    传给要 ``ProjectAssessment`` 的处理器，mypy 直接报错）。
    """
    field = _STAGE_INPUT_FIELDS[stage]
    value = getattr(state, field)
    if value is None:
        raise StageContractError(stage, f"缺少输入：{field} 不在状态里")
    return value


def advance(state: PipelineState, stage: Stage, artifact: object) -> PipelineState:
    """记录某个阶段的产物，返回**新的**状态。

    **四条契约，破了就报错。**

    1. **次序**：这个阶段必须是下一个该跑的阶段。早跑一步，后面那份产物就
       建在空地基上。
    2. **类型**：产物必须是对应阶段该有的类型。编排器不校验状态，类型不对
       的话，最后得到的是一份"结构完整但内容错位"的状态。
    3. **只跑一次**：同一个阶段不跑第二遍 —— 重跑会把已经定下来的结论换成
       另一个样子，而状态里只留最后一份，前一份就此消失。
    4. **不越级**：跑完这一步之后，那一步的产物必须真的在状态里。

    **第五条契约（不缺输入）在 :func:`stage_input` 里。** 那是"跑之前"的检查，
    这一条是"跑完之后"的检查 —— 分开守，因为坏的状态有两种：一种是产物不对
    （这里守），一种是输入压根不在（``stage_input`` 守）。
    """
    expected = next_stage(state)
    if expected is None:
        raise StageContractError(stage, "五个阶段都已经跑完了")
    if stage != expected:
        raise StageContractError(stage, f"下一个该跑的是 {expected.value}")

    artifact_type = _STAGE_ARTIFACT_TYPES[stage]
    if not isinstance(artifact, artifact_type):
        raise StageContractError(
            stage,
            f"产出类型不对：{type(artifact).__name__}，该是 {artifact_type.__name__}",
        )

    field = _STAGE_FIELDS[stage]
    updated = state.model_copy(update={field: artifact})
    return updated.model_copy(update={"completed_stages": (*state.completed_stages, stage)})


# ---------------------------------------------------------------- 处理器端口


class Understander(Protocol):
    """认知阶段的处理器：项目快照与目标 → 项目评估。"""

    def understand(self, context: ProjectContext) -> ProjectAssessment: ...


class Diagnoser(Protocol):
    """诊断阶段的处理器：项目评估 → 差距分析。"""

    def diagnose(self, assessment: ProjectAssessment) -> GapAnalysis: ...


class Explorer(Protocol):
    """探索阶段的处理器：差距 → 演进候选。"""

    def explore(self, gaps: GapAnalysis) -> DiscoveryResult: ...


class Decider(Protocol):
    """决策阶段的处理器：候选 → 决策与放行凭据。"""

    def decide(self, discovery: DiscoveryResult) -> DecisionOutcome: ...


class Planner(Protocol):
    """规划阶段的处理器：已批准的决策 → 路线图。"""

    def plan(self, outcome: DecisionOutcome) -> EvolutionRoadmap: ...


@dataclass(frozen=True)
class StageHandlers:
    """五个阶段各一个处理器。

    **为什么是注入而不是内部 new。** 测试要能拿打桩节点跑通整图（design.md
    的 T1 ②），而真实实现要走角色化 Prompt（TASK-P1-057）—— 两者的差别只在
    这五个对象里，图本身不该知道。
    """

    understand: Understander
    diagnose: Diagnoser
    explore: Explorer
    decide: Decider
    plan: Planner


def draft_keys(roadmap: EvolutionRoadmap) -> tuple[str, ...]:
    """路线图里所有任务的标识（按排期顺序）。"""
    return tuple(slot.key for slot in roadmap.schedule)


__all__ = [
    "STAGE_ORDER",
    "Decider",
    "DecisionOutcome",
    "Diagnoser",
    "Explorer",
    "GapAnalysis",
    "Planner",
    "ProjectContext",
    "Stage",
    "StageContractError",
    "StageHandlers",
    "Understander",
    "advance",
    "draft_keys",
    "next_stage",
    "stage_field",
    "stage_input",
]
