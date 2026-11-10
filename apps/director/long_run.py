"""连续 10 周期长跑 —— 把十轮演进接起来跑完。

设计依据：docs/tasks.md TASK-P3-018（"连续 10 周期长跑"；验收"连续 10 轮无
人工派发任务；每轮产出可验证证据；达成度呈非负趋势"）、R-130（首个里程碑）、
docs/design.md §4、§9、§12。

**这一版的发掘器是"读台账的扫描器"，不是模型。** 这件事得写在最前面，因为
它决定了这轮长跑**量到了什么、没量到什么**：

- **量到了**：链路能不能连续十轮自己走完 —— 每轮的观察、差距分析、候选、决策、
  任务派生、沙箱执行、独立验证、审批、合并、结局判定、落库，全是真的；
  达成度是**每轮重新实测**的（见 :mod:`apps.director.pilot_measure`）。
- **没量到**：**发掘质量** —— "系统能不能自己看出别人看不出的问题"这件事，
  由另一条线单独量（有效发现率与误报率，见 :mod:`evaluation.evolution_evaluation`
  与 ``evaluation/labels/pilot_labels.jsonl``）。

**为什么用台账当发掘源。** 试点仓库的 OBJ-001 就是"修掉已知缺陷清单里的全部
有效发现" —— 对这个项目来说，"下一件该做的事"有权威答案，就是台账里第一条
还开着的有效发现。**这不是把结论写死**：台账里的十二条有效发现与八条无效
发现都要由扫描器逐条判**"还开着没有"**（复现型看它还在不在失败清单里，复核
型看各自的覆盖测量），判据全在 :mod:`apps.director.pilot_measure` 里。

**一轮只推进一条。** 十轮对十条修复，一一对应。**一轮推多条会让"哪一条带来
了改善"这件事说不清** —— 而长跑要证明的恰恰是"每一轮都留下了可核对的账"。

**上一轮的仓库就是这一轮的输入。** 每一轮把上一轮合入主分支之后的那份仓库
复制一份当起点 —— 所以第十轮开始时，仓库里已经有前九轮的修复。**这不是模拟
"十轮独立跑"**，而是真正的连跑：第十轮面对的是**真的被修过九次**的仓库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.director.cycle import (
    CycleBrain,
    CycleConfig,
    CycleContext,
    CycleRecipe,
    CycleResult,
    new_cycle_id,
    run_cycle,
)
from apps.director.m1_executor import M1PatchExecutor
from apps.director.patching import run_git
from apps.director.pilot_fixes import FIX_BY_ISSUE, FIX_BY_TITLE, IssueFix
from apps.director.pilot_measure import (
    LedgerEntry,
    PilotMeasurement,
    measure_repo,
    parse_ledger,
)
from core.evolution.assessment import GapSeverity, GoalGap, OverallStatus
from core.evolution.candidate import DiscoveryResult, EvolutionCandidate
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.evolution_loop import LoopBState, ProjectObservation
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.planning.stages import GapAnalysis
from core.project.models import Project, load_project
from core.project.stop_conditions import GoalAttainmentCalculator
from core.task.outcome import VerificationVerdict
from evaluation.long_run import (
    ORIGIN_AUTONOMOUS,
    REQUIRED_ROUNDS,
    CandidateEvidence,
    CycleEvidence,
    LongRunReport,
    build_report,
    judge_cycle,
)
from executors.base import (
    AgentSession,
    ExecutionHandle,
    ExecutionMetrics,
    ExecutionResult,
    ExecutionTask,
    SessionConfig,
    build_idempotency_key,
)

#: 长跑执行器的名字 —— 落进会话行与事件载荷，事后查得出"是谁干的"。
EXECUTOR_NAME: Final[str] = "lr-local-patcher"

#: 达成度保留精度（4 位小数）—— 与 :mod:`apps.director.m2_observer` 一致。
ATTAINMENT_QUANT: Final[Decimal] = Decimal("0.0001")

#: 候选的置信度 —— 复现型发现有会失败的用例撑着，复核型发现有覆盖测量撑着。
CANDIDATE_CONFIDENCE: Final[Decimal] = Decimal("0.90")

#: 价值 / 影响 / 紧迫 / 成本 —— 这四条是判断，不是观察。
CANDIDATE_VALUE: Final[Decimal] = Decimal("0.70")
CANDIDATE_IMPACT: Final[Decimal] = Decimal("0.60")
CANDIDATE_URGENCY: Final[Decimal] = Decimal("0.50")
CANDIDATE_COST: Final[Decimal] = Decimal("0.30")

#: 试点项目 id。
PILOT_PROJECT_ID: Final[str] = "pilot-tinylib"


class LongRunError(RuntimeError):
    """长跑跑不下去 —— 理由必须说清楚。"""


class UnknownFixError(LongRunError):
    """这一轮选中的候选没有对应的修复 —— 执行器不知道该改什么。"""


@dataclass(slots=True)
class LongRunTracker:
    """跨轮次共享的那点东西。

    **为什么需要一个跨轮次的对象。** 观察者每一轮都是新造的（脑子由配方现场
    造），而"上一轮收尾时的达成度"必须跨轮次传下去 —— 少了它，第十轮的
    ``previous_attainment`` 就接不上第九轮。

    **实测结果按 HEAD 缓存。** 一轮里要量好几次（开工前、发现时、收尾后），
    而同一个 HEAD 上的仓库不会变 —— 同一个 HEAD 量两次就是白跑两遍测试。
    **缓存的键是 HEAD，不是路径**：仓库换了路径但内容没变时，重新量一次也不
    会错，而缓存过期了只是浪费几秒。
    """

    python_executable: str
    scratch: Path
    last_attainment: Decimal | None = None
    _cache: dict[str, PilotMeasurement] = field(default_factory=dict)

    def measure(self, repo: Path) -> PilotMeasurement:
        """量一遍仓库 —— 同一个 HEAD 只量一次。"""
        head = run_git(repo, "rev-parse", "HEAD").strip()
        cached = self._cache.get(head)
        if cached is not None:
            return cached
        measured = measure_repo(repo, self.python_executable, self.scratch / head[:8])
        self._cache[head] = measured
        return measured


@dataclass(frozen=True, slots=True)
class LongRunObserver:
    """长跑的项目观察者 —— 达成度是每轮**重新实测**的。

    **两次观察，两次实测。** 开工前量一次（这一轮改之前是什么样），收尾后再量
    一次（改完合入之后是什么样）。差值落在这一轮身上。

    **收尾的 ``previous_attainment`` 取开工前那个数。** 不是"上一轮收尾的数" ——
    那是两条不同的线：一条是"这一轮推进了多少"，一条是"十轮下来走了多远"。
    前者是这一轮的账，后者是长跑的总账。
    """

    repo: Path
    project: Project
    goal: str
    tech_stack: tuple[str, ...]
    latest_snapshot_version: int
    tracker: LongRunTracker

    async def observe(self, state: LoopBState) -> ProjectObservation:
        commit = run_git(self.repo, "rev-parse", "HEAD").strip()
        branch = run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        measurement = self.tracker.measure(self.repo)
        actuals = measurement.actuals()
        attainment = GoalAttainmentCalculator(self.project.goal).evaluate(actuals).overall
        attainment = attainment.quantize(ATTAINMENT_QUANT)

        if state.observation is None:
            previous: Decimal | None = self.tracker.last_attainment
        else:
            previous = state.observation.attainment
            self.tracker.last_attainment = attainment

        return ProjectObservation(
            project_id=state.project_id,
            commit=commit,
            branch=branch,
            goal=self.goal,
            tech_stack=self.tech_stack,
            metrics={key: float(value.number) for key, value in actuals.items() if value.number},
            attainment=attainment,
            previous_attainment=previous,
            latest_snapshot_version=self.latest_snapshot_version,
        )


@dataclass(frozen=True, slots=True)
class LongRunDiscoverer:
    """长跑的候选发现器 —— 台账扫描器。

    **选的是"第一条还开着的有效发现"。** 复现型看它还在不在失败清单里；复核
    型看各自的覆盖测量。**无效发现直接跳过** —— 台账里那八条无效发现是误报率
    的分母，不是待办事项。
    """

    repo: Path
    project: Project
    tracker: LongRunTracker

    async def discover(self, state: LoopBState) -> DiscoveryResult:
        measurement = self.tracker.measure(self.repo)
        entry = next_finding(self.repo, measurement)
        if entry is None:
            return DiscoveryResult(candidates=())
        fix = FIX_BY_ISSUE.get(entry.issue)
        if fix is None:
            raise LongRunError(f"台账里的 {entry.issue} 没有对应的修复 —— 长跑推进不了它")
        candidate = build_candidate(entry.issue, fix, state.cycle_id)
        return DiscoveryResult(candidates=(candidate,))


@dataclass(frozen=True, slots=True)
class LongRunAnalyzer:
    """长跑的目标差距分析器 —— 差距就是这一轮要修的那一条。"""

    repo: Path
    project: Project
    tracker: LongRunTracker

    async def analyze(self, state: LoopBState) -> GapAnalysis:
        measurement = self.tracker.measure(self.repo)
        entry = next_finding(self.repo, measurement)
        if entry is None:
            # **不是"已达目标"。** 台账上没有还开着的发现，只说明"眼下没有可推进的
            # 已知差距" —— 关键结果未必达标，所以不能写 AT_TARGET。
            return GapAnalysis(
                project_id=state.project_id,
                snapshot=f"snap-{state.snapshot.version if state.snapshot else 0}",
                overall_status=OverallStatus.STALLED,
            )
        fix = FIX_BY_ISSUE.get(entry.issue)
        if fix is None:
            raise LongRunError(f"台账里的 {entry.issue} 没有对应的修复 —— 长跑推进不了它")
        return GapAnalysis(
            project_id=state.project_id,
            snapshot=f"snap-{state.snapshot.version if state.snapshot else 0}",
            overall_status=OverallStatus.EVOLVING,
            gaps=(
                GoalGap(
                    id=f"gap-{state.cycle_id}-{entry.issue}",
                    description=fix.hypothesis,
                    evidence=tuple(
                        Evidence(kind=EvidenceKind.FILE, ref=ref, note=note)
                        for ref, note in fix.evidence
                    ),
                    severity=GapSeverity.HIGH,
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class LongRunEvaluator:
    """长跑的价值 / 风险 / 成本评估器。"""

    async def evaluate(self, state: LoopBState) -> tuple[ScoredCandidate, ...]:
        discovery = state.discovery
        if discovery is None:
            return ()
        scored: list[ScoredCandidate] = []
        for candidate in discovery.candidates:
            factors = PriorityFactors.from_candidate(
                candidate,
                value=CANDIDATE_VALUE,
                impact=CANDIDATE_IMPACT,
                urgency=CANDIDATE_URGENCY,
                cost=CANDIDATE_COST,
            )
            scored.append(
                ScoredCandidate(
                    candidate_id=candidate.id,
                    category=candidate.category,
                    factors=factors,
                )
            )
        return tuple(scored)


@dataclass(frozen=True, slots=True)
class LongRunGapUpdater:
    """轮后差距更新 —— 修掉的那条从差距里去掉。"""

    async def update(self, state: LoopBState) -> GapAnalysis:
        gaps = state.gaps
        if gaps is None:
            return GapAnalysis(
                project_id=state.project_id,
                snapshot="snap-unknown",
                overall_status=OverallStatus.STALLED,
            )
        return GapAnalysis(
            project_id=gaps.project_id,
            snapshot=gaps.snapshot,
            overall_status=OverallStatus.EVOLVING,
            gaps=(),
        )


class LongRunPatchExecutor(M1PatchExecutor):
    """按**候选标题**分发补丁的执行器。

    **按标题分发，不按编号。** 执行器拿到的只有任务标题 —— 那是这一轮系统自己
    选中的候选的标题。用编号做键，就得在标题里塞编号；而标题里塞了编号就与
    测试报告里的 node id 对不上，Critic 的第一问会退成"未核实"。
    """

    __slots__ = ("_applied",)

    def __init__(self) -> None:
        super().__init__(patches=())
        self._applied: dict[str, int] = {}

    @property
    def name(self) -> str:
        """执行器名字。"""
        return EXECUTOR_NAME

    async def create_session(self, config: SessionConfig) -> AgentSession:
        """开一次会话 —— 用长跑自己的执行器标识。"""
        session = await super().create_session(config)
        return AgentSession(
            session_id=session.session_id,
            executor=EXECUTOR_NAME,
            workspace=session.workspace,
            created_at=session.created_at,
        )

    async def execute(self, task: ExecutionTask) -> ExecutionHandle:
        """按这一轮选中的候选打补丁并提交。"""
        fix = FIX_BY_TITLE.get(task.title)
        if fix is None:
            raise UnknownFixError(f"候选标题 {task.title!r} 没有对应的修复 —— 执行器不知道该改什么")
        workspace, _branch = self._sessions[task.session_id]
        root = Path(workspace)
        for patch in fix.patches:
            patch.apply(root)
        idempotency_key = build_idempotency_key(task.task_id, task.attempt)
        run_git(root, "add", "-A")
        run_git(root, "commit", "-m", f"{task.title}\n\n幂等键：{idempotency_key}")
        self._tasks[task.session_id] = task.task_id
        self._applied[task.session_id] = len(fix.patches)
        return ExecutionHandle(
            session_id=task.session_id,
            task_id=task.task_id,
            idempotency_key=idempotency_key,
            attempt=task.attempt,
            accepted_at=datetime.now(UTC),
        )

    async def collect_result(self, session_id: str) -> ExecutionResult:
        """收结果 —— 报这一轮真改了几个文件、花了多少。"""
        result = await super().collect_result(session_id)
        return ExecutionResult(
            task_id=result.task_id,
            executor=EXECUTOR_NAME,
            status=result.status,
            artifacts=result.artifacts,
            execution=ExecutionMetrics(
                duration=result.execution.duration,
                tool_calls=self._applied.get(session_id, 0),
                cost=result.execution.cost,
                currency=result.execution.currency,
            ),
            self_assessment=result.self_assessment,
            idempotency_key=result.idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class LongRunOutcome:
    """长跑跑完之后的结论。

    :attr: rounds: 每一轮的结果 —— 落库的每一行都能对账。
    :attr: report: 按事先定好的判据给出的长跑报告。
    :attr: measurements: 每一轮收尾时的实测值 —— 覆盖率、用例数、缺陷数。
    """

    rounds: tuple[CycleResult, ...]
    report: LongRunReport
    measurements: tuple[PilotMeasurement, ...]


def next_finding(repo: Path, measurement: PilotMeasurement) -> LedgerEntry | None:
    """台账里**第一条还开着的有效发现**。

    **返回的是台账条目**（:class:`apps.director.pilot_measure.LedgerEntry`）。
    没有就返回 ``None`` —— 那意味着"台账上的事都做完了"，而不是"系统坏了"。
    """
    for entry in parse_ledger(repo):
        if not entry.valid:
            continue
        if entry.evidence_type == "repro":
            if entry.repro_test in measurement.failing_repro:
                return entry
            continue
        if _review_still_open(entry.issue, measurement):
            return entry
    return None


def _review_still_open(issue: str, measurement: PilotMeasurement) -> bool:
    """复核型发现是不是还开着。

    **判据不明的复核型发现不算还开着。** 没有判据就没有"还开着"这个事实 ——
    而长跑只推进判据明确的发现；推进一条自己都说不清开没开的发现，等于把
    "不知道"当成"待办"。
    """
    if issue == "PILOT-010":
        return measurement.cli_coverage == 0
    if issue == "PILOT-011":
        return not measurement.money_paths_covered
    if issue == "PILOT-012":
        return not measurement.boundary_covered
    return False


def build_candidate(issue: str, fix: IssueFix, cycle_id: str) -> EvolutionCandidate:
    """把一条修复变成一个候选。"""
    return EvolutionCandidate(
        id=f"CAND-{cycle_id}-{issue}",
        category=OpportunityCategory.QUALITY,
        title=fix.title,
        hypothesis=fix.hypothesis,
        expected_outcome=fix.expected_outcome,
        risk=RiskLevel.LOW,
        confidence=CANDIDATE_CONFIDENCE,
        estimated_effort="0.5d",
        evidence=tuple(
            Evidence(kind=EvidenceKind.FILE, ref=ref, note=note) for ref, note in fix.evidence
        ),
    )


def _task_key(candidate_id: str) -> str:
    """这一轮派生出来的任务的标识 —— **按候选定，不能写死**。

    **为什么不能写死成一个。** 任务标识是路线图里的 key，也是任务与决策、
    任务与执行结果之间那根线 —— 同一个标识出现两次，后一个会盖掉前一个，
    而盖掉这件事**不会报错**：路线图看上去仍然完整，只是少了一个任务（见
    :func:`apps.director.cycle.task_key_for`）。

    **带上候选 id 就是全局唯一的。** 候选 id 里含着周期 id，而周期 id 全局
    唯一（``evolution_cycle.cycle_id`` 上有唯一约束）—— 于是十轮十一个标识，
    事后查账分得清哪一轮是哪一轮。
    """
    return f"T-LR-{candidate_id}"


def long_run_recipe(
    *,
    repo: Path,
    tracker: LongRunTracker,
    project: Project,
    gap_id: str,
    task_instruction: str,
) -> CycleRecipe:
    """这一轮的配方 —— 脑子换成长跑那一套，链路一个字不动。"""

    def brain(context: CycleContext) -> CycleBrain:
        objective = (
            project.goal.objective[0].description if project.goal.objective else project.name
        )
        return CycleBrain(
            observer=LongRunObserver(
                repo=context.repo,
                project=project,
                goal=objective,
                tech_stack=tuple(project.tech_stack),
                latest_snapshot_version=context.latest_snapshot_version,
                tracker=tracker,
            ),
            analyzer=LongRunAnalyzer(repo=context.repo, project=project, tracker=tracker),
            discoverer=LongRunDiscoverer(repo=context.repo, project=project, tracker=tracker),
            evaluator=LongRunEvaluator(),
            gap_updater=LongRunGapUpdater(),
        )

    return CycleRecipe(
        brain=brain,
        executors={EXECUTOR_NAME: LongRunPatchExecutor()},
        executor_name=EXECUTOR_NAME,
        task_instruction=task_instruction,
        task_key=_task_key,
        gap_id=gap_id,
        actuals=lambda _project: tracker.measure(repo).actuals(),
        verifier_name="lr-independent-verifier",
        approver="lr-approver",
        merge_comment="长跑：放行这一轮的合并",
        merge_message="长跑：合入这一轮的修复与回归用例",
        repo_email="lr@ases.local",
        repo_name="长跑驱动",
        cycle_prefix="cycle-lr",
    )


@dataclass(frozen=True, slots=True)
class LongRunConfig:
    """一次长跑的配置 —— **跑在哪、跑多少轮**。

    **为什么打包成一个对象。** 参数一多，调用处就得靠位置去记"第三个是工作根
    目录" —— 而长跑要连着跑十轮，调用处写错一个位置就是十轮白跑。
    """

    project_file: Path
    pilot_source: Path
    work_root: Path
    python_executable: str
    rounds: int = REQUIRED_ROUNDS
    project_id: str = PILOT_PROJECT_ID


async def run_long_run(
    config: LongRunConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> LongRunOutcome:
    """连着跑若干轮 —— **每一轮的输入是上一轮合入之后的仓库**。

    **轮与轮之间不重来。** 第十轮开始时，仓库里已经有前九轮的修复 —— 第十轮
    面对的是**真的被修过九次**的仓库，而不是一份干净的拷贝。

    **每轮一个独立的工作根目录。** 轮次之间不共用工作区 —— 共用会让上一轮的
    散改动混进这一轮，而"这一轮改了什么"就说不清了。
    """
    project = load_project(config.project_file)
    tracker = LongRunTracker(
        python_executable=config.python_executable,
        scratch=config.work_root / "measure",
    )
    config.work_root.mkdir(parents=True, exist_ok=True)

    results: list[CycleResult] = []
    measurements: list[PilotMeasurement] = []
    source = config.pilot_source

    for index in range(1, config.rounds + 1):
        round_root = config.work_root / f"round-{index:02d}"
        round_root.mkdir(parents=True, exist_ok=True)
        round_repo = round_root / "repo"

        recipe = long_run_recipe(
            repo=round_repo,
            tracker=tracker,
            project=project,
            gap_id=f"gap-lr-{index:03d}",
            task_instruction=f"第 {index} 轮：修掉台账里下一条还开着的有效发现",
        )
        cycle_config = CycleConfig(
            project_file=config.project_file,
            pilot_source=source,
            work_root=round_root,
            python_executable=config.python_executable,
            project_id=config.project_id,
            cycle_id=new_cycle_id("cycle-lr"),
            executor_name=EXECUTOR_NAME,
        )
        result = await run_cycle(cycle_config, recipe, session_factory)
        results.append(result)
        measurements.append(tracker.measure(round_repo))
        source = round_repo

    report = build_report([judge_cycle(_evidence_of(result)) for result in results])
    return LongRunOutcome(
        rounds=tuple(results),
        report=report,
        measurements=tuple(measurements),
    )


def _evidence_of(result: CycleResult) -> CycleEvidence:
    """把一轮的结论收成**长跑判据**要的那份证据。

    **"有决策"不够，得是"被采纳的决策"。** 判据要求"一个决策"—— 而没被
    采纳的决策说明系统这一轮**选择了不做**。让九轮"决定不做"混进来，就
    没有十轮"推进"了。
    """
    candidates: list[CandidateEvidence] = []
    decisions = {decision_id: decision for decision_id, decision, _rationale in result.decisions}
    for candidate_id, title, _confidence in result.candidates:
        fix = FIX_BY_TITLE.get(title)
        evidence_refs = tuple(ref for ref, _note in fix.evidence) if fix is not None else ()
        decision = decisions.get(candidate_id, "")
        candidates.append(
            CandidateEvidence(
                candidate_id=candidate_id,
                title=title,
                evidence_refs=evidence_refs,
                decision=decision,
            )
        )
    return CycleEvidence(
        cycle_id=result.cycle_id,
        origin=ORIGIN_AUTONOMOUS,
        candidates=tuple(candidates),
        task_keys=(result.task_key,) if result.task_key else (),
        verification_passed=result.verdict is VerificationVerdict.PASSED,
        merged=result.merged,
        merge_commit=result.merge_commit,
        attainment_before=result.attainment_before,
        attainment_after=result.attainment_after,
    )


__all__ = [
    "ATTAINMENT_QUANT",
    "EXECUTOR_NAME",
    "PILOT_PROJECT_ID",
    "REQUIRED_ROUNDS",
    "LongRunError",
    "LongRunEvaluator",
    "LongRunGapUpdater",
    "LongRunObserver",
    "LongRunOutcome",
    "LongRunPatchExecutor",
    "LongRunTracker",
    "UnknownFixError",
    "build_candidate",
    "long_run_recipe",
    "next_finding",
    "run_long_run",
]
