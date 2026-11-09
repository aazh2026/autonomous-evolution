"""确定性演练 runner —— 从 YAML 脚本回放完整的演进周期。

设计依据：docs/tasks.md TASK-P1-065（T1 ① 同一脚本回放两次，产生**完全相同**
的状态迁移序列与 DB 记录；T1 ② 打桩 Agent 报告成功但打桩验证判失败 → 系统
进入修复路径；T1 ③ 打桩 Critic 否决 → 候选不进入 ``APPROVED``）；R-108。

**演练要演的是编排，不是模型。** 真实系统里模型每次的输出都不一样 ——
那正是它该有的样子。所以"回放两次完全一样"这件事，靠的不是把模型调成
确定性的，而是**把模型那一段换成脚本**：观察、差距、候选、评分、任务草稿
都从 YAML 里读，Agent 的产出、验证的结论、Critic 的裁决也按脚本给。

**被换掉的只有"会说话的那部分"。** 编排本身是真的：真的图、真的状态机、
真的落库口、真的策略。**决策规则不换** —— 脚本给的是评分，决策由真的
:class:`core.evolution.decision.DecisionPolicy` 算；**放行规则不换** ——
脚本给的是 Critic 的裁决，放行凭据由真的
:func:`agents.critic.approval.approval_for` 翻译。否则演练验的就是"打桩
自己跟自己玩"，而那正是打桩最容易骗过自己的地方。

**"完全相同"要有判据。** :class:`ReplayTranscript` 收拢这一轮留下的全部
痕迹：走过的节点（循环 B 的十五个、循环 A 的那些）、任务的状态迁移序列、
这一轮的决策与放行凭据、路线图上的任务与被跳过的决策及其理由、这一轮的
产出、以及**执行会话的开会时刻**。
:attr:`ReplayTranscript.digest` 把它们压成一个摘要 —— 两次回放的摘要必须
一样。

**DB 记录那一份在集成测试里比。** 行 id 是 uuid、时间戳是墙钟 —— 它们天生
不可重复，比它们等于比运气。所以比对的是**规范化之后**的行（uuid 换成
序号、时间戳丢掉），规范化规则写在集成测试里，与这里同一套口径。

**这一层放在 ``tests/`` 而不是 ``core/``。** 它是演练工具，不是生产代码 ——
生产代码里不该有一条"按脚本回放"的路径，那会让"真的跑一遍"和"演一遍"
变得分不清。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import yaml

from agents.critic.approval import approval_for
from agents.critic.checks import (
    CHECK_QUESTIONS,
    VETOING_CHECKS,
    CheckOutcome,
    CriticCheck,
    CriticEvidence,
    EvidenceKind,
    Verdict,
)
from agents.critic.report import CriticReport
from core.evolution.assessment import GapSeverity, GoalGap, OverallStatus
from core.evolution.candidate import DiscoveryResult, EvolutionCandidate
from core.evolution.decision import DecisionPolicy, DecisionRound, DecisionState
from core.evolution.evidence import Evidence as CandidateEvidence
from core.evolution.evidence import EvidenceKind as CandidateEvidenceKind
from core.evolution.evolution_loop import (
    EvolutionJournalPort,
    EvolutionPlan,
    LoopBState,
    ProjectObservation,
    ResourceAssignment,
    ResourceSchedule,
    TriggerSource,
)
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.planning.roadmap import DecisionApproval, TaskDraft, TaskType
from core.planning.stages import GapAnalysis
from core.task.execution_loop import (
    DEFAULT_MAX_REPAIRS,
    CollectedArtifacts,
    EventRecord,
    ExecutionPlan,
    FailureCategory,
    FailureDiagnosis,
    LoopAState,
    MergeOutcome,
    SessionJournal,
    SessionRecord,
    WorkspaceInfo,
)
from core.task.outcome import ExecutionStatus, VerificationReport, VerificationVerdict
from core.task.repair import InMemoryRepairLedger, RepairLimits
from executors.base import (
    AgentSession,
    ExecutionArtifacts,
    ExecutionHandle,
    ExecutionMetrics,
    ExecutionResult,
    ExecutionTask,
    SelfAssessment,
    SessionConfig,
    build_idempotency_key,
)
from orchestration.loop_a import LoopAHandlers
from orchestration.loop_b import (
    LoopAEngineeringCycle,
    LoopBHandlers,
    PolicyDecisionMaker,
    build_loop_b_graph,
    run_loop_b,
)
from orchestration.repair import RepairCoordinator

#: 演练里用的固定时刻 —— **不取墙钟时间**。
#:
#: 一个 ``datetime.now()`` 就会让两次回放产出不同的记录，而"回放两次完全
#: 一样"正是这个模块存在的理由。
REPLAY_TIME: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)

#: 打桩执行器的名字。
REPLAY_EXECUTOR: Final[str] = "replay-executor"

#: 打桩 Critic 否决时给的那条证据引用。
REPLAY_CRITIC_EVIDENCE: Final[str] = "report://replay/critic"


class ReplayScriptError(ValueError):
    """脚本写错了 —— 字段缺失、类型不对、或者引用了不存在的东西。"""


# --------------------------------------------------------------------------
# 脚本
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScriptedAttempt:
    """一次尝试的预置结果 —— 打桩 Agent 报什么。

    :attr:`status` 是**执行器的自我报告**。它不参与判定（design.md §3.4）：
    打桩报"做完了"而验证判失败，正是 T1 ② 要演的那出。
    """

    status: ExecutionStatus = ExecutionStatus.COMPLETED
    changed_files: int = 1
    branch: str = "feature/replay"
    commit: str = "0" * 40
    cost: Decimal = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class ScriptedVerification:
    """一次独立验证的预置结论 —— 打桩验证引擎说什么。

    **它与执行器的自我报告分开。** 两者的分歧正是"独立验证"这件事存在的
    理由；合在一起就等于没有独立验证。
    """

    verdict: VerificationVerdict = VerificationVerdict.PASSED
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScriptedCritic:
    """打桩 Critic 的预置裁决。

    :attr:`veto` 为真时，:attr:`check` 必须是**可否决的检查** ——
    :data:`agents.critic.checks.VETOING_CHECKS` 里那五项。四问拿不出硬事实，
    用它们否决等于让否决理由经不起复核。
    """

    veto: bool = False
    check: CriticCheck = CriticCheck.EVIDENCE_SUFFICIENT
    summary: str = "证据不足以支持这条候选"
    evidence_ref: str = REPLAY_CRITIC_EVIDENCE

    def __post_init__(self) -> None:
        if self.veto and self.check not in VETOING_CHECKS:
            raise ReplayScriptError(
                f"检查 {self.check.value} 不是可否决的检查 —— "
                f"可否决的是：{'、'.join(sorted(item.value for item in VETOING_CHECKS))}"
            )
        if not self.summary.strip():
            raise ReplayScriptError("Critic 的裁决要有一句话结论")


@dataclass(frozen=True, slots=True)
class ScriptedDraft:
    """一份任务草稿 —— 拆解的输入。

    **它引的是候选，不是决策。** 决策 id 是
    :func:`core.evolution.decision.decide_round` 按周期号生成的（``EVD-…-001``），
    脚本写死它就等于把脚本绑死在一个实现细节上 —— 决策 id 的生成规则一改，
    脚本全废。引候选则不会：候选 id 是脚本自己的。
    """

    key: str
    candidate_id: str
    task_type: TaskType = TaskType.FEATURE
    title: str = ""
    description: str = ""
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplayScript:
    """一份演练脚本 —— 回放的全部输入。

    **它只有输入，没有输出。** 脚本说"世界现在是什么样、Agent 会做什么、
    验证会怎么判、Critic 会不会否决"，不说"这一轮应该是什么结果"——
    结果由系统算出来。脚本里写结果的那种东西不是演练，是答案。
    """

    project_id: str
    cycle_id: str
    observation: ProjectObservation
    gaps: GapAnalysis
    trigger: TriggerSource = TriggerSource.SCHEDULE
    candidates: tuple[EvolutionCandidate, ...] = ()
    scores: tuple[ScoredCandidate, ...] = ()
    drafts: tuple[ScriptedDraft, ...] = ()
    critic: ScriptedCritic = field(default_factory=ScriptedCritic)
    attempts: tuple[ScriptedAttempt, ...] = ()
    verifications: tuple[ScriptedVerification, ...] = ()
    #: 失败归类 —— 决定"能不能自己修"。
    failure_category: FailureCategory = FailureCategory.AUTO_REPAIRABLE
    #: **项目级**修复次数上限 —— 全局账本的那条上限（TASK-P1-063）。
    #:
    #: **它不是每任务的上限。** 每任务那条是领域里的常数
    #: (:data:`core.task.execution_loop.DEFAULT_MAX_REPAIRS`)，由循环 A 的状态机
    #: 把关；脚本动不了它，也不该动 —— 全局账本才是"两个任务各修 3 次"
    #: 挡不住的那一条。
    repair_budget: int = DEFAULT_MAX_REPAIRS

    def __post_init__(self) -> None:
        known = {item.id for item in self.candidates}
        for draft in self.drafts:
            if draft.candidate_id not in known:
                raise ReplayScriptError(
                    f"任务草稿 {draft.key!r} 引用的候选 {draft.candidate_id!r} 不在脚本里"
                )
        scored = {item.candidate_id for item in self.scores}
        for candidate in self.candidates:
            if candidate.id not in scored:
                raise ReplayScriptError(f"候选 {candidate.id!r} 没有评分 —— 决策拿不到依据")

    # -- 从 YAML 读 ---------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path) -> ReplayScript:
        """从 YAML 文件读一份脚本。"""
        raw = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ReplayScriptError(f"脚本 {path} 的顶层必须是一个映射")
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ReplayScript:
        """从一份映射读脚本 —— YAML 的解析结果就是它。"""
        project_id = _required_str(data, "project_id")
        cycle_id = _required_str(data, "cycle_id")
        observation_raw = _required_mapping(data, "observation")
        gaps_raw = _required_list(data, "gaps")
        candidates_raw = _required_list(data, "candidates")
        scores_raw = _required_list(data, "scores")

        observation = ProjectObservation(
            project_id=project_id,
            commit=_required_str(observation_raw, "commit"),
            branch=str(observation_raw.get("branch", "")),
            goal=str(observation_raw.get("goal", "")),
            tech_stack=tuple(str(item) for item in observation_raw.get("tech_stack", ())),
            metrics={
                str(key): float(value)
                for key, value in dict(observation_raw.get("metrics", {})).items()
            },
            attainment=_optional_decimal(observation_raw.get("attainment")),
            previous_attainment=_optional_decimal(observation_raw.get("previous_attainment")),
            latest_snapshot_version=int(observation_raw.get("latest_snapshot_version", 0)),
        )

        gaps = GapAnalysis(
            project_id=project_id,
            snapshot=str(data.get("snapshot", "snap-replay")),
            overall_status=OverallStatus.EVOLVING,
            gaps=tuple(_gap_from_mapping(item) for item in gaps_raw),
        )

        candidates = tuple(_candidate_from_mapping(item) for item in candidates_raw)
        by_id = {item.id: item for item in candidates}
        scores = tuple(_score_from_mapping(item, by_id) for item in scores_raw)

        drafts = tuple(
            ScriptedDraft(
                key=_required_str(item, "key"),
                candidate_id=_required_str(item, "candidate_id"),
                task_type=TaskType(str(item.get("task_type", "feature"))),
                title=str(item.get("title", "")),
                description=str(item.get("description", "")),
                depends_on=tuple(str(dep) for dep in item.get("depends_on", ())),
            )
            for item in data.get("drafts", ())
        )

        critic_raw = dict(data.get("critic", {}) or {})
        critic = ScriptedCritic(
            veto=bool(critic_raw.get("veto", False)),
            check=CriticCheck(str(critic_raw.get("check", CriticCheck.EVIDENCE_SUFFICIENT.value))),
            summary=str(critic_raw.get("summary", "证据不足以支持这条候选")),
            evidence_ref=str(critic_raw.get("evidence_ref", REPLAY_CRITIC_EVIDENCE)),
        )

        attempts = tuple(
            ScriptedAttempt(
                status=ExecutionStatus(str(item.get("status", "completed"))),
                changed_files=int(item.get("changed_files", 1)),
                branch=str(item.get("branch", "feature/replay")),
                commit=str(item.get("commit", "0" * 40)),
                cost=Decimal(str(item.get("cost", "0.5"))),
            )
            for item in data.get("attempts", ())
        )

        verifications = tuple(
            ScriptedVerification(
                verdict=VerificationVerdict(str(item.get("verdict", "passed"))),
                reasons=tuple(str(reason) for reason in item.get("reasons", ())),
            )
            for item in data.get("verifications", ())
        )

        return cls(
            project_id=project_id,
            cycle_id=cycle_id,
            trigger=TriggerSource(str(data.get("trigger", TriggerSource.SCHEDULE.value))),
            observation=observation,
            gaps=gaps,
            candidates=candidates,
            scores=scores,
            drafts=drafts,
            critic=critic,
            attempts=attempts,
            verifications=verifications,
            failure_category=FailureCategory(
                str(data.get("failure_category", FailureCategory.AUTO_REPAIRABLE.value))
            ),
            repair_budget=int(data.get("repair_budget", DEFAULT_MAX_REPAIRS)),
        )


# --------------------------------------------------------------------------
# 打桩件
# --------------------------------------------------------------------------


class ScriptedExecutor:
    """打桩 Agent Adapter —— 按脚本返回预置的 diff 与状态。

    **它报"我做完了"。** 这不是 bug，是要演的东西：执行器的自我报告不参与
    判定（design.md §3.4），所以打桩报完成而打桩验证判失败时，任务仍应走
    失败路径（T1 ②）。

    **每次尝试取一份脚本。** 第一份是首跑，第二份是第一次返修 —— 用完了就
    重复最后一份，于是"一直修不好"这条路径也能被脚本描述。
    """

    __slots__ = ("_attempts", "_current", "_handle", "_name", "_opened", "_sessions", "_task_id")

    def __init__(
        self,
        attempts: Sequence[ScriptedAttempt] = (),
        *,
        name: str = REPLAY_EXECUTOR,
    ) -> None:
        self._attempts = tuple(attempts)
        self._name = name
        self._current: ScriptedAttempt = attempts[0] if attempts else ScriptedAttempt()
        self._sessions = 0
        self._task_id = ""
        self._opened: list[AgentSession] = []
        self._handle = ExecutionHandle(
            session_id="",
            task_id="",
            idempotency_key="",
            attempt=1,
            accepted_at=REPLAY_TIME,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def opened_sessions(self) -> tuple[AgentSession, ...]:
        """开过的那些会话 —— **带它们开会的时刻**。

        **时刻是留痕的一部分。** 会话开了几次、分别什么时候开的，都属于
        "这一轮发生了什么"。而 :data:`REPLAY_TIME` 是固定的 —— 所以这一份
        留痕两次回放必然一样；要是执行器改用墙钟，它就不再一样。
        """
        return tuple(self._opened)

    async def create_session(self, config: SessionConfig) -> AgentSession:
        self._sessions += 1
        session = AgentSession(
            session_id=f"{self._name}-session-{self._sessions:03d}",
            executor=self._name,
            workspace=config.workspace,
            created_at=REPLAY_TIME,
        )
        self._opened.append(session)
        return session

    async def execute(self, task: ExecutionTask) -> ExecutionHandle:
        # 第几次尝试决定取哪一份脚本 —— 返修时脚本给的是第二份。
        self._task_id = task.task_id
        index = min(task.attempt - 1, len(self._attempts) - 1) if self._attempts else 0
        self._current = self._attempts[index] if self._attempts else ScriptedAttempt()
        handle = ExecutionHandle(
            session_id=task.session_id,
            task_id=task.task_id,
            idempotency_key=build_idempotency_key(task.task_id, task.attempt),
            attempt=task.attempt,
            accepted_at=REPLAY_TIME,
        )
        self._handle = handle
        return handle

    async def collect_result(self, session_id: str) -> ExecutionResult:
        attempt = self._current
        return ExecutionResult(
            task_id=self._task_id,
            executor=self._name,
            status=attempt.status,
            artifacts=ExecutionArtifacts(
                branch=attempt.branch,
                commit=attempt.commit,
                changed_files=attempt.changed_files,
            ),
            execution=ExecutionMetrics(
                duration=Decimal("1"),
                tool_calls=1,
                cost=attempt.cost,
                currency="CNY",
            ),
            self_assessment=SelfAssessment(completed=True),
            # 幂等键跟着那次执行走 —— 每次尝试的键不一样（重试要能重做）。
            idempotency_key=self._handle.idempotency_key,
        )


class ScriptedVerifier:
    """打桩验证引擎 —— 按脚本给结论。

    **它与执行器分开，这是它存在的全部意义。** 打桩执行器报"做完了"，打桩
    验证报"没通过" —— 任务仍应走失败路径。若两者是同一个对象，验的就不是
    "独立验证"了。
    """

    __slots__ = ("_calls", "_verifications")

    def __init__(self, verifications: Sequence[ScriptedVerification] = ()) -> None:
        self._verifications = tuple(verifications)
        self._calls = 0

    async def verify(self, state: LoopAState) -> VerificationReport:
        index = min(self._calls, len(self._verifications) - 1) if self._verifications else 0
        self._calls += 1
        scripted = self._verifications[index] if self._verifications else ScriptedVerification()
        reasons = scripted.reasons
        if scripted.verdict is not VerificationVerdict.PASSED and not reasons:
            reasons = ("单元测试没通过",)
        checks = (("unit_tests", scripted.verdict is VerificationVerdict.PASSED),)
        return VerificationReport(
            verdict=scripted.verdict,
            reasons=reasons,
            checks=checks,
        )


@dataclass
class ScriptedLoopAHandlers:
    """循环 A 的打桩处理器。

    **只有"会看世界"的那几步是打桩的。** 状态机、闸门、修复额度都是真的 ——
    演练要演的就是它们。
    """

    executor: ScriptedExecutor
    verifier: ScriptedVerifier
    failure_category: FailureCategory = FailureCategory.AUTO_REPAIRABLE
    repair_hint: str = "修好那个失败的用例"

    async def plan(self, state: LoopAState) -> ExecutionPlan:
        return ExecutionPlan(
            task_id=state.task_id,
            instruction="把功能做完",
            task_type=state.task_type,
        )

    async def select(self, state: LoopAState, candidates: Sequence[str]) -> str:
        return candidates[0]

    async def create(self, state: LoopAState) -> WorkspaceInfo:
        return WorkspaceInfo(
            workspace_id="ws-replay",
            path="/tmp/ases-replay",
            branch="feature/replay",
        )

    async def collect(self, state: LoopAState, result: ExecutionResult) -> CollectedArtifacts:
        return CollectedArtifacts(
            branch=result.artifacts.branch or "",
            commit=result.artifacts.commit,
            changed_files=result.artifacts.changed_files,
        )

    async def diagnose(self, state: LoopAState) -> FailureDiagnosis:
        return FailureDiagnosis(
            reason="单元测试没通过",
            category=self.failure_category,
            repair_hint=self.repair_hint,
        )

    async def merge(self, state: LoopAState) -> MergeOutcome:
        return MergeOutcome(merged=True, commit="c0ffee")

    async def update(self, state: LoopAState) -> None:
        return None


@dataclass
class ScriptedLoopBHandlers:
    """循环 B 的打桩处理器 —— **"会说话的那几步"**。

    **决策与放行走真的规则。** 评分给出去之后，决策由
    :class:`core.evolution.decision.DecisionPolicy` 算；Critic 的裁决给出去
    之后，放行凭据由 :func:`agents.critic.approval.approval_for` 翻译。
    脚本只提供"模型会给出的东西"，不提供"系统该得出的结论"。
    """

    script: ReplayScript
    critic_reports: list[CriticReport] = field(default_factory=list)
    approvals: list[DecisionApproval] = field(default_factory=list)

    async def observe(self, state: LoopBState) -> ProjectObservation:
        return self.script.observation

    async def analyze(self, state: LoopBState) -> GapAnalysis:
        return self.script.gaps

    async def discover(self, state: LoopBState) -> DiscoveryResult:
        return DiscoveryResult(candidates=self.script.candidates)

    async def evaluate(self, state: LoopBState) -> tuple[ScoredCandidate, ...]:
        return self.script.scores

    async def decide(self, state: LoopBState) -> DecisionRound:
        maker = PolicyDecisionMaker(DecisionPolicy())
        return await maker.decide(state)

    async def plan(self, state: LoopBState) -> EvolutionPlan:
        decisions = state.decisions
        if decisions is None:
            raise ReplayScriptError("规划之前必须先有决策")
        recorded = decisions.recorded
        if not recorded:
            return EvolutionPlan()

        by_candidate = {item.candidate_id: item for item in decisions.decisions}
        approvals: list[DecisionApproval] = []
        for decision in recorded:
            report = self._critic_report(decision.candidate_id)
            self.critic_reports.append(report)
            approval = approval_for(decision, report)
            self.approvals.append(approval)
            approvals.append(approval)

        drafts = tuple(
            TaskDraft(
                key=draft.key,
                decision_id=by_candidate[draft.candidate_id].id,
                task_type=draft.task_type,
                title=draft.title or by_candidate[draft.candidate_id].candidate_id,
                description=draft.description,
                rationale=by_candidate[draft.candidate_id].rationale,
                depends_on=draft.depends_on,
            )
            for draft in self.script.drafts
            if draft.candidate_id in by_candidate
            and by_candidate[draft.candidate_id].decision is DecisionState.RECORD
        )
        return EvolutionPlan(drafts=drafts, approvals=tuple(approvals))

    async def schedule(self, state: LoopBState) -> ResourceSchedule:
        roadmap = state.roadmap
        if roadmap is None:
            raise ReplayScriptError("排期之前必须先有路线图")
        return ResourceSchedule(
            assignments=tuple(
                ResourceAssignment(task_key=task.key, wave=1, executor=REPLAY_EXECUTOR)
                for task in roadmap.tasks
            ),
            budget=Decimal("10"),
        )

    async def update(self, state: LoopBState) -> GapAnalysis:
        return self.script.gaps

    def _critic_report(self, candidate_id: str) -> CriticReport:
        """按脚本造一份九项齐全的审查报告。

        **九项一项都不能少** —— 报告模型会当场拒绝少一项的报告。少一项不是
        "少查了一项"，是"读的人会以为查过了"。
        """
        scripted = self.script.critic
        outcomes: list[CheckOutcome] = []
        for check in CriticCheck:
            if scripted.veto and check is scripted.check:
                outcomes.append(
                    CheckOutcome(
                        check=check,
                        verdict=Verdict.FAILED,
                        summary=scripted.summary,
                        evidence=(
                            CriticEvidence(
                                kind=EvidenceKind.TEST_REPORT,
                                reference=scripted.evidence_ref,
                                detail=scripted.summary,
                            ),
                        ),
                        vetoes=True,
                    )
                )
                continue
            outcomes.append(
                CheckOutcome(
                    check=check,
                    verdict=Verdict.PASSED,
                    summary=f"{CHECK_QUESTIONS[check]} —— 演练脚本：查过了，没问题",
                    evidence=(
                        CriticEvidence(
                            kind=EvidenceKind.CANDIDATE,
                            reference=candidate_id,
                            detail="演练脚本预置",
                        ),
                    ),
                )
            )
        return CriticReport(candidate_id=candidate_id, outcomes=tuple(outcomes))


@dataclass
class RecordingSessionJournal:
    """套在真的落库口外面 —— 把事件抄一份。

    **它不改写任何东西，只多记一份。** 抄下来的事件就是"状态迁移序列"的
    来源：合并闸门一步走过两个状态（``INTEGRATING`` 与 ``MERGED``），事件
    里带着这一步经过了哪些状态。
    """

    inner: SessionJournal
    events: list[EventRecord] = field(default_factory=list)

    async def open_session(self, record: SessionRecord) -> str:
        return await self.inner.open_session(record)

    async def record_event(self, event: EventRecord) -> None:
        self.events.append(event)
        await self.inner.record_event(event)

    @property
    def nodes(self) -> tuple[str, ...]:
        """走过的节点（按事件顺序）。"""
        return tuple(event.event_type for event in self.events)

    @property
    def task_states(self) -> tuple[str, ...]:
        """任务的状态迁移序列 —— 把每个事件带过的状态摊平。"""
        states: list[str] = []
        for event in self.events:
            raw = event.payload.get("task_states", ())
            if not isinstance(raw, (list, tuple)):
                continue
            for value in raw:
                states.append(str(value))
        return tuple(states)


# --------------------------------------------------------------------------
# 留痕
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayTranscript:
    """一次回放留下的可比较痕迹。

    **它收的是"事实"，不是"评价"。** 走过哪些节点、任务怎么迁移、哪条决策
    被放行、哪条被跳过 —— 全是从状态与落库口里读出来的，不掺解释。

    :attr:`digest` 是这些事实的摘要。**两次回放的摘要必须一样** —— 那就是
    "回放两次完全一样"的判据。
    """

    #: 循环 B 走过的节点。
    loop_b_nodes: tuple[str, ...]
    #: 循环 A 走过的节点（按事件顺序；返修时节点会重复出现）。
    loop_a_nodes: tuple[str, ...]
    #: 任务的状态迁移序列。
    task_states: tuple[str, ...]
    #: 这一轮的决策：(决策 id, 决策态)。
    decisions: tuple[tuple[str, str], ...]
    #: 放行凭据：(决策 id, 放不放行, 理由)。
    approvals: tuple[tuple[str, bool, str], ...]
    #: 进了路线图的任务：(标识, 标题)。
    planned_tasks: tuple[tuple[str, str], ...]
    #: 被跳过的决策：(决策 id, 理由)。
    skipped: tuple[tuple[str, str], ...]
    #: 任务的执行结局：(任务标识, 结局)。
    outcomes: tuple[tuple[str, str], ...]
    #: 开过的执行会话：(会话标识, 开会时刻)。
    #:
    #: **时刻进留痕，是为了让"没有墙钟"这条性质变成可验的。** 打桩执行器用
    #: 的是 :data:`REPLAY_TIME`（固定时刻）—— 要是它哪天改用 ``now()``，
    #: 这一份留痕两次回放就不再一样，而"一样"正是这个模块存在的理由。
    sessions: tuple[tuple[str, str], ...]
    #: 这一轮的结局。
    cycle_status: str

    @property
    def digest(self) -> str:
        """这些事实的摘要 —— 两次回放要比的就是它。"""
        payload = json.dumps(
            {
                "loop_a_nodes": list(self.loop_a_nodes),
                "loop_b_nodes": list(self.loop_b_nodes),
                "task_states": list(self.task_states),
                "decisions": [list(item) for item in self.decisions],
                "approvals": [[item[0], item[1], item[2]] for item in self.approvals],
                "planned_tasks": [list(item) for item in self.planned_tasks],
                "skipped": [list(item) for item in self.skipped],
                "outcomes": [list(item) for item in self.outcomes],
                "sessions": [list(item) for item in self.sessions],
                "cycle_status": self.cycle_status,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


class ReplayRunner:
    """把一份脚本回放成一次真实的演进周期。

    :param journal: 循环 B 的落库口。**内存的或真的都行** —— 回放的是编排，
        落库口是别人家的实现。
    :param session_journal: 循环 A 的执行轨迹落库口。
    :param contract_bound_of: 回答"这个任务行上绑了验收契约没有"。**默认一律
        当作已绑** —— 验收契约的准入门有它自己的验收（TASK-P1-062），演练脚本
        不重复演它；否则演练会停在准入门上，而那不是演练要验的东西。

    **回放的是真的图。** 循环 B 的十五个节点与循环 A 的十五个节点都是真的
    LangGraph 图，状态机、闸门、修复额度也都是真的 —— 只有"会说话的那部分"
    换成了脚本。
    """

    __slots__ = ("_contract_bound_of", "_journal", "_session_journal")

    def __init__(
        self,
        *,
        journal: EvolutionJournalPort,
        session_journal: SessionJournal,
        contract_bound_of: Callable[[str], bool] | None = None,
    ) -> None:
        self._journal = journal
        self._session_journal = session_journal
        self._contract_bound_of = contract_bound_of or (lambda _task_id: True)

    async def replay(self, script: ReplayScript) -> ReplayTranscript:
        """回放一份脚本，返回这次回放的留痕。"""
        loop_b_handlers = ScriptedLoopBHandlers(script=script)
        executor = ScriptedExecutor(script.attempts)
        verifier = ScriptedVerifier(script.verifications)
        loop_a_handlers = ScriptedLoopAHandlers(
            executor=executor,
            verifier=verifier,
            failure_category=script.failure_category,
        )
        recording = RecordingSessionJournal(inner=self._session_journal)
        loop_a = LoopAHandlers(
            planner=loop_a_handlers,
            selector=loop_a_handlers,
            workspace=loop_a_handlers,
            collector=loop_a_handlers,
            verifier=verifier,
            diagnoser=loop_a_handlers,
            merge_gate=loop_a_handlers,
            project_state=loop_a_handlers,
            journal=recording,
            executors={executor.name: executor},
            repair_coordinator=RepairCoordinator(
                InMemoryRepairLedger(),
                limits=RepairLimits(max_attempts=script.repair_budget),
            ),
        )
        engineering = LoopAEngineeringCycle(
            loop_a,
            contract_bound_of=self._contract_bound_of,
        )
        handlers = LoopBHandlers(
            observer=loop_b_handlers,
            analyzer=loop_b_handlers,
            discoverer=loop_b_handlers,
            evaluator=loop_b_handlers,
            decider=loop_b_handlers,
            planner=loop_b_handlers,
            scheduler=loop_b_handlers,
            engineering=engineering,
            gap_updater=loop_b_handlers,
            journal=self._journal,
        )
        graph = build_loop_b_graph(handlers)
        state = await run_loop_b(
            graph,
            LoopBState(
                project_id=script.project_id,
                cycle_id=script.cycle_id,
                trigger=script.trigger,
            ),
        )
        return _transcript_of(state, loop_b_handlers, recording, executor)


def _transcript_of(
    state: LoopBState,
    handlers: ScriptedLoopBHandlers,
    recording: RecordingSessionJournal,
    executor: ScriptedExecutor,
) -> ReplayTranscript:
    """把回放结束时的状态翻译成一份留痕。"""
    decisions: tuple[tuple[str, str], ...] = ()
    if state.decisions is not None:
        decisions = tuple((item.id, item.decision.value) for item in state.decisions.decisions)
    approvals = tuple((item.decision_id, item.approved, item.reason) for item in handlers.approvals)
    planned: tuple[tuple[str, str], ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    if state.roadmap is not None:
        planned = tuple((task.key, task.title) for task in state.roadmap.tasks)
        skipped = tuple((item.decision_id, item.reason) for item in state.roadmap.skipped)
    outcomes: tuple[tuple[str, str], ...] = ()
    if state.execution is not None:
        # 结论可能压根没有（执行层没给出判定）—— 记成 "unknown"，不记成空串：
        # 空串与"判定为某个空值"在读的人眼里是一回事。
        outcomes = tuple(
            (item.task_key, item.outcome.value if item.outcome else "unknown")
            for item in state.execution.tasks
        )
    return ReplayTranscript(
        loop_b_nodes=tuple(node.value for node in state.visited_nodes),
        loop_a_nodes=recording.nodes,
        task_states=recording.task_states,
        decisions=decisions,
        approvals=approvals,
        planned_tasks=planned,
        skipped=skipped,
        outcomes=outcomes,
        sessions=tuple(
            (item.session_id, item.created_at.isoformat()) for item in executor.opened_sessions
        ),
        cycle_status=state.outcome.status.value if state.outcome else state.status.value,
    )


# --------------------------------------------------------------------------
# YAML 的小工具
# --------------------------------------------------------------------------


def _required_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ReplayScriptError(f"脚本缺 {key!r}，或者它不是一个映射")
    return value


def _required_list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ReplayScriptError(f"脚本缺 {key!r}，或者它不是一个列表")
    return value


def _required_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReplayScriptError(f"脚本缺 {key!r}，或者它不是一段非空文字")
    return value


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _gap_from_mapping(item: Any) -> GoalGap:
    if not isinstance(item, dict):
        raise ReplayScriptError("差距必须是映射")
    return GoalGap(
        id=_required_str(item, "id"),
        description=_required_str(item, "description"),
        evidence=tuple(_evidence_from_mapping(raw) for raw in item.get("evidence", ())),
        severity=GapSeverity(str(item.get("severity", GapSeverity.MEDIUM.value))),
    )


def _evidence_from_mapping(item: Any) -> CandidateEvidence:
    if not isinstance(item, dict):
        raise ReplayScriptError("证据必须是映射")
    return CandidateEvidence(
        kind=CandidateEvidenceKind(str(item.get("kind", "issue"))),
        ref=_required_str(item, "ref"),
        note=str(item.get("note", "")),
    )


def _candidate_from_mapping(item: Any) -> EvolutionCandidate:
    if not isinstance(item, dict):
        raise ReplayScriptError("候选必须是映射")
    return EvolutionCandidate(
        id=_required_str(item, "id"),
        category=OpportunityCategory(str(item.get("category", "engineering"))),
        title=_required_str(item, "title"),
        evidence=tuple(_evidence_from_mapping(raw) for raw in item.get("evidence", ())),
        hypothesis=str(item.get("hypothesis", "把这件事做完之后，这一轮就能被验证")),
        expected_outcome=tuple(str(raw) for raw in item.get("expected_outcome", ())),
        risk=RiskLevel(str(item.get("risk", RiskLevel.LOW.value))),
        confidence=Decimal(str(item.get("confidence", "0.8"))),
        estimated_effort=str(item.get("estimated_effort", "")),
    )


def _score_from_mapping(
    item: Any,
    candidates: Mapping[str, EvolutionCandidate],
) -> ScoredCandidate:
    """一份评分。

    **因子由候选算，不在这里填。** 置信度与风险系数是**候选的属性** ——
    另填一个数就是自己跟自己打架（:meth:`core.evolution.priority.
    PriorityFactors.from_candidate`）。
    """
    if not isinstance(item, dict):
        raise ReplayScriptError("评分必须是映射")
    candidate_id = _required_str(item, "candidate_id")
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise ReplayScriptError(f"评分引用的候选 {candidate_id!r} 不在脚本里")
    return ScoredCandidate(
        candidate_id=candidate_id,
        category=candidate.category,
        factors=PriorityFactors.from_candidate(
            candidate,
            value=Decimal(str(item.get("value", "0.5"))),
            impact=Decimal(str(item.get("impact", "0.5"))),
            urgency=Decimal(str(item.get("urgency", "0.5"))),
            cost=Decimal(str(item.get("cost", "0.5"))),
        ),
    )


__all__ = [
    "REPLAY_CRITIC_EVIDENCE",
    "REPLAY_EXECUTOR",
    "REPLAY_TIME",
    "RecordingSessionJournal",
    "ReplayRunner",
    "ReplayScript",
    "ReplayScriptError",
    "ReplayTranscript",
    "ScriptedAttempt",
    "ScriptedCritic",
    "ScriptedDraft",
    "ScriptedExecutor",
    "ScriptedLoopAHandlers",
    "ScriptedLoopBHandlers",
    "ScriptedVerification",
    "ScriptedVerifier",
]
