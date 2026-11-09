"""循环 B 的测试构造器。

设计依据：docs/tasks.md TASK-P1-061（T1 ① 全链路跑通并产出 ``EvolutionRoadmap``）。

**打桩件是"真的能跑"的对象，不是随手捏的假货。** 打桩落库口返回的**行 id 是
UUID 字符串** —— 因为真实实现返回的就是 uuid，而挂错行的后果是"任务挂到了
别的决策上"且**不会报错**。打桩件用假的行 id，测试就永远查不出这类错。

**打桩落库口会记下每一次写入。** "这一轮到底在库里留下了什么"是验收重点 ——
候选、决策、任务、快照、周期结束，五样都要能读回来对账。

**打桩执行口会报"我干完了"。** 它模拟的是**循环 A** 交回来的结论 —— 循环 B
看不见循环 A 怎么干活，只看得见它的结论（设计文档 §4：两个循环不能合并）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from core.evolution.assessment import GapSeverity, GoalGap, OverallStatus
from core.evolution.candidate import DiscoveryResult, EvolutionCandidate
from core.evolution.decision import (
    AnalysisKind,
    DecisionPolicy,
    DecisionRound,
    DecisionState,
    EvolutionDecision,
    FollowUpAnalysis,
)
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.evolution_loop import (
    CycleExecution,
    EvolutionPlan,
    LoopBState,
    ProjectObservation,
    ResourceAssignment,
    ResourceSchedule,
    TaskExecution,
    TriggerSource,
)
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.metrics.cycle import CycleStatus
from core.planning.roadmap import DecisionApproval, EvolutionRoadmap, TaskDraft, TaskType
from core.planning.stages import GapAnalysis
from core.task.outcome import TaskOutcome
from orchestration.loop_b import LoopBHandlers, PolicyDecisionMaker

#: 打桩项目 id。
PROJECT_ID: Final[str] = "p-demo"

#: 打桩周期 id。
CYCLE_ID: Final[str] = "cycle-0007"

#: 打桩候选 id。
CANDIDATE_ID: Final[str] = "CAND-0001"

#: 打桩任务标识。
TASK_KEY: Final[str] = "T-0001"


def demo_candidate(
    *,
    candidate_id: str = CANDIDATE_ID,
    title: str = "把演进循环的落库口补齐",
    risk: RiskLevel = RiskLevel.LOW,
    confidence: Decimal = Decimal("0.8"),
) -> EvolutionCandidate:
    """一份带证据的候选 —— **候选必须带证据**（R-030）。"""
    return EvolutionCandidate(
        id=candidate_id,
        category=OpportunityCategory.ENGINEERING,
        title=title,
        evidence=(
            Evidence(kind=EvidenceKind.ISSUE, ref="issue://core/evolution", note="落库口缺失"),
        ),
        hypothesis="补上落库口之后，这一轮的产出就能被查到",
        expected_outcome=("这一轮的候选与决策能在库里查到",),
        risk=risk,
        confidence=confidence,
        estimated_effort="1d",
    )


def demo_scored(
    candidate: EvolutionCandidate,
    *,
    value: Decimal = Decimal("0.7"),
    impact: Decimal = Decimal("0.6"),
    urgency: Decimal = Decimal("0.4"),
    cost: Decimal = Decimal("0.3"),
) -> ScoredCandidate:
    """给候选评个分。"""
    factors = PriorityFactors.from_candidate(
        candidate,
        value=value,
        impact=impact,
        urgency=urgency,
        cost=cost,
    )
    return ScoredCandidate(
        candidate_id=candidate.id,
        category=candidate.category,
        factors=factors,
    )


def demo_gaps(*, project_id: str = PROJECT_ID, snapshot: str = "snap-1") -> GapAnalysis:
    """一份目标差距。"""
    return GapAnalysis(
        project_id=project_id,
        snapshot=snapshot,
        overall_status=OverallStatus.EVOLVING,
        gaps=(
            GoalGap(
                id="gap-1",
                description="这一轮的产出查不到",
                evidence=(
                    Evidence(
                        kind=EvidenceKind.ISSUE,
                        ref="issue://core/evolution",
                        note="落库口缺失",
                    ),
                ),
                severity=GapSeverity.MEDIUM,
            ),
        ),
    )


@dataclass
class StubJournal:
    """把循环 B 的写入记在内存里的假落库口。

    **它实现的是 :class:`core.evolution.evolution_loop.EvolutionJournalPort` 整个
    接口** —— 不是"够用就行"的那几行。多写一个方法不会有人发现，少写一个
    方法则要到运行时才炸。

    **行 id 是 UUID 字符串。** 真实实现返回的就是 uuid；打桩件用别的形状，
    "挂错行"这类错误在测试里就永远查不出来。
    """

    cycles: list[dict[str, str]] = field(default_factory=list)
    candidates: list[EvolutionCandidate] = field(default_factory=list)
    decisions: list[DecisionRound] = field(default_factory=list)
    roadmaps: list[EvolutionRoadmap] = field(default_factory=list)
    snapshots: list[dict[str, object]] = field(default_factory=list)
    closed: list[dict[str, object]] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)
    _candidate_rows: int = 0

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    async def open_cycle(self, *, project_id: str, cycle_id: str, trigger: str) -> str:
        self._count("open_cycle")
        self.cycles.append({"project_id": project_id, "cycle_id": cycle_id, "trigger": trigger})
        return f"cycle-row-{len(self.cycles)}"

    async def record_candidates(
        self,
        cycle_ref: str,
        candidates: Sequence[EvolutionCandidate],
    ) -> Mapping[str, str]:
        self._count("record_candidates")
        rows: dict[str, str] = {}
        for candidate in candidates:
            self._candidate_rows += 1
            rows[candidate.id] = f"cand-row-{self._candidate_rows}"
        self.candidates.extend(candidates)
        return rows

    async def record_decisions(
        self,
        cycle_ref: str,
        decisions: DecisionRound,
        candidate_rows: Mapping[str, str],
    ) -> Mapping[str, str]:
        self._count("record_decisions")
        self.decisions.append(decisions)
        return {
            decision.id: f"dec-row-{index}"
            for index, decision in enumerate(decisions.decisions, start=1)
        }

    async def land_tasks(
        self,
        roadmap: EvolutionRoadmap,
        decision_rows: Mapping[str, str],
    ) -> Mapping[str, str]:
        self._count("land_tasks")
        self.roadmaps.append(roadmap)
        return {task.key: f"task-row-{index}" for index, task in enumerate(roadmap.tasks, start=1)}

    async def record_snapshot(
        self,
        *,
        project_id: str,
        version: int,
        commit: str,
        branch: str,
        metrics: Mapping[str, float],
    ) -> str:
        self._count("record_snapshot")
        self.snapshots.append(
            {
                "project_id": project_id,
                "version": version,
                "commit": commit,
                "branch": branch,
                "metrics": dict(metrics),
            }
        )
        return f"snap-row-{len(self.snapshots)}"

    async def close_cycle(
        self,
        cycle_ref: str,
        *,
        status: CycleStatus,
        attainment_before: Decimal | None,
        attainment_after: Decimal | None,
        evidence_refs: Sequence[str],
    ) -> None:
        self._count("close_cycle")
        self.closed.append(
            {
                "cycle_ref": cycle_ref,
                "status": status,
                "attainment_before": attainment_before,
                "attainment_after": attainment_after,
                "evidence_refs": tuple(evidence_refs),
            }
        )


@dataclass
class StubHandlers:
    """打桩的各步处理器。

    **一个对象实现九个端口。** 它们的方法名互不冲突，合成一个更省事；
    "哪个端口归谁"由 :class:`orchestration.loop_b.LoopBHandlers` 的字段名
    决定，不靠这里。

    **决策默认走真的决策策略。** 打桩件自己编一个决策，就把
    :class:`core.evolution.decision.DecisionPolicy` 的三条门槛绕过去了 ——
    而那三条门槛正是"证据不足先 defer""风险太高要评审""价值太低不投入"。

    **要测"不值得做"那条路时，把 :attr:`decisions` 预置成一份全是 defer 的
    决策。** 那比"给策略调一个离谱的阈值"更接近真实：真实系统里 defer
    来自证据不足，不是来自阈值被人调坏了。
    """

    journal: StubJournal = field(default_factory=StubJournal)
    candidate: EvolutionCandidate = field(default_factory=demo_candidate)
    decisions: DecisionRound | None = None
    #: 打桩执行口报"验收通过"。改成 ``False`` 就走失败那条路。
    accepted: bool = True
    calls: dict[str, int] = field(default_factory=dict)

    def _count(self, name: str) -> int:
        self.calls[name] = self.calls.get(name, 0) + 1
        return self.calls[name]

    @property
    def handlers(self) -> LoopBHandlers:
        return LoopBHandlers(
            observer=self,
            analyzer=self,
            discoverer=self,
            evaluator=self,
            decider=self,
            planner=self,
            scheduler=self,
            engineering=self,
            gap_updater=self,
            journal=self.journal,
        )

    # -- 各步 ---------------------------------------------------------------

    async def observe(self, state: LoopBState) -> ProjectObservation:
        self._count("observe")
        return ProjectObservation(
            project_id=state.project_id,
            commit="c" * 40,
            branch="main",
            goal="让项目持续演进",
            tech_stack=("python",),
            metrics={"coverage": 0.82},
            attainment=Decimal("0.7"),
            previous_attainment=Decimal("0.6"),
            latest_snapshot_version=3,
        )

    async def analyze(self, state: LoopBState) -> GapAnalysis:
        self._count("analyze")
        return demo_gaps(project_id=state.project_id)

    async def discover(self, state: LoopBState) -> DiscoveryResult:
        self._count("discover")
        return DiscoveryResult(candidates=(self.candidate,))

    async def evaluate(self, state: LoopBState) -> tuple[ScoredCandidate, ...]:
        self._count("evaluate")
        return (demo_scored(self.candidate),)

    async def decide(self, state: LoopBState) -> DecisionRound:
        self._count("decide")
        if self.decisions is not None:
            return self.decisions
        maker = PolicyDecisionMaker(DecisionPolicy())
        return await maker.decide(state)

    async def plan(self, state: LoopBState) -> EvolutionPlan:
        self._count("plan")
        decisions = state.decisions
        if decisions is None:
            raise AssertionError("规划之前必须先有决策")
        recorded = decisions.recorded
        if not recorded:
            return EvolutionPlan()
        first = recorded[0]
        return EvolutionPlan(
            drafts=(
                TaskDraft(
                    key=TASK_KEY,
                    decision_id=first.id,
                    task_type=TaskType.FEATURE,
                    title=self.candidate.title,
                    description="把落库口补齐",
                    rationale=first.rationale,
                ),
            ),
            approvals=(
                DecisionApproval(
                    decision_id=first.id,
                    candidate_id=first.candidate_id,
                    approved=True,
                    reason="证据够硬，值得做",
                ),
            ),
        )

    async def schedule(self, state: LoopBState) -> ResourceSchedule:
        self._count("schedule")
        roadmap = state.roadmap
        if roadmap is None:
            raise AssertionError("排期之前必须先有路线图")
        return ResourceSchedule(
            assignments=tuple(
                ResourceAssignment(task_key=task.key, wave=1, executor="stub-executor")
                for task in roadmap.tasks
            ),
            budget=Decimal("10"),
        )

    async def execute(self, state: LoopBState) -> CycleExecution:
        self._count("execute")
        roadmap = state.roadmap
        if roadmap is None:
            raise AssertionError("执行之前必须先有路线图")
        return CycleExecution(
            tasks=tuple(
                TaskExecution(
                    task_key=task.key,
                    task_id=state.task_rows.get(task.key, ""),
                    accepted=self.accepted,
                    outcome=TaskOutcome.COMPLETED if self.accepted else TaskOutcome.FAILED,
                    reason="" if self.accepted else "验收没通过",
                )
                for task in roadmap.tasks
            )
        )

    async def update(self, state: LoopBState) -> GapAnalysis:
        self._count("update")
        return demo_gaps(project_id=state.project_id, snapshot="snap-2")


def defer_all(candidate: EvolutionCandidate, *, cycle_id: str = CYCLE_ID) -> DecisionRound:
    """一份"全是 defer"的决策轮 —— 用来走"不值得做"那条路。

    **defer 必须带补充分析项**（领域模型的硬约束），所以这里给每条都配一条
    测量项 —— 不然构造不出来。
    """
    scored = demo_scored(candidate)
    return DecisionRound(
        cycle_id=cycle_id,
        decisions=(
            EvolutionDecision(
                id=f"EVD-{cycle_id}-001",
                candidate_id=candidate.id,
                decision=DecisionState.DEFER,
                rationale="证据还不够，先补一次测量",
                requested_analysis=(
                    FollowUpAnalysis(
                        kind=AnalysisKind.MEASUREMENT,
                        description="对候选做一次可复现的测量，确认问题是否存在",
                    ),
                ),
                factors=scored.factors,
            ),
        ),
    )


def initial_state(**overrides: object) -> LoopBState:
    """一份循环 B 的初始状态。"""
    state = LoopBState(
        project_id=PROJECT_ID,
        cycle_id=CYCLE_ID,
        trigger=TriggerSource.SCHEDULE,
    )
    if not overrides:
        return state
    return state.model_copy(update=overrides)


__all__ = [
    "CANDIDATE_ID",
    "CYCLE_ID",
    "PROJECT_ID",
    "TASK_KEY",
    "StubHandlers",
    "StubJournal",
    "defer_all",
    "demo_candidate",
    "demo_gaps",
    "demo_scored",
    "initial_state",
]
