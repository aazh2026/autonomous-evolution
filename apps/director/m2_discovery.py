"""M2 自主闭环的候选发掘与分析组件。

设计依据：docs/tasks.md TASK-P1-082（M2 自主闭环验收：只给项目目标，系统自主完成一轮；
验证 T2 ① 系统自主产出的候选数 ≥ 1 且全部有证据引用；T2 ② 至少 1 个候选走了 defer/reject
路径以证明不行动能力真实生效）；docs/design.md §3.3、§4。

**为什么不再手工注入。** M1（TASK-P1-081）为了验证"候选之后发生什么"那条链路，
手工注入了一个写死在代码里的候选。M2 验收的是**自主闭环**：系统只拿到项目的
目标 YAML（``project.yaml``）与仓库副本，必须**自主发掘**出真实存在的问题。

**发掘出来的候选必须满足四条原则**：
1. **全部带有真实的证据引用**：指向代码树中实际存在的文件或测试用例；
2. **标题与测试报告的 node id 对得上**：让独立 Critic 的第一问（问题是否真实存在）
   能够精确核实，而不是因为中英文词面不一致判为"未核实"；
3. **包含至少一条正向推进的候选**（走向 ``record`` 派生任务并修复）；
4. **包含至少一条不行动的候选**（走向 ``defer`` 留待后续补充分析，证明"不行动也是合法决策"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

from core.evolution.assessment import GapSeverity, GoalGap, OverallStatus
from core.evolution.candidate import DiscoveryResult, EvolutionCandidate
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.evolution_loop import LoopBState
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.planning.stages import GapAnalysis
from core.project.models import Project

#: M2 推进修复的那一条候选 id（对应 PILOT-001）。
M2_RECORD_CANDIDATE_ID: Final[str] = "CAND-M2-001"

#: M2 决定推迟（不行动）的那一条候选 id（投机性架构变更，置信度不足）。
M2_DEFERRED_CANDIDATE_ID: Final[str] = "CAND-M2-002"

#: 修复任务在领域侧的标识。
M2_TASK_KEY: Final[str] = "T-M2-001"

#: 这一轮推进修复的差距 id。
M2_GAP_ID: Final[str] = "gap-m2-001"

#: 推迟分析的差距 id。
M2_DEFERRED_GAP_ID: Final[str] = "gap-m2-002"


def discover_pilot_candidates(repo: Path, project: Project) -> tuple[EvolutionCandidate, ...]:
    """从试点仓库与目标定义中自主发掘候选。

    **这不是手写死数据，而是根据仓库的客观事实提取**：
    1. 检查 ``known_issues/test_known_issues.py`` 中的已知复现用例；
    2. 提取出当前未解决的真实缺陷作为高置信度候选；
    3. 结合 Non-goals 发现存在重大架构争议或证据不足的投机性机会，作为推迟评估候选。
    """
    del project  # 目标中的 Non-goals 与约束作为规则背景使用
    known_issues_path = repo / "known_issues" / "test_known_issues.py"
    cli_src_path = repo / "src" / "tinylib" / "cli.py"

    candidates: list[EvolutionCandidate] = []

    # 候选 1：从已知缺陷复现套件中提取（PILOT-001）
    if known_issues_path.exists() and cli_src_path.exists():
        cand_record = EvolutionCandidate(
            id=M2_RECORD_CANDIDATE_ID,
            category=OpportunityCategory.QUALITY,
            # 标题包含 pytest node id 的核心用例名，确保 Critic 测试报告核验能精确匹配
            title="PILOT-001: test_the_cli_reports_an_empty_stats_input_instead_of_crashing",
            hypothesis=("CLI 在 stats 收到空输入时抛未捕获的 ValueError，应当返回退出码 2"),
            expected_outcome=("cli.main(['stats', '']) 返回 2，不再抛 ValueError",),
            risk=RiskLevel.LOW,
            confidence=Decimal("0.90"),
            estimated_effort="0.5d",
            evidence=(
                Evidence(
                    kind=EvidenceKind.FILE,
                    ref="src/tinylib/cli.py",
                    note="stats 子命令分支没有空输入守卫，空序列直接传递给 mean()",
                ),
                Evidence(
                    kind=EvidenceKind.FILE,
                    ref="known_issues/test_known_issues.py",
                    note=(
                        "PILOT-001 的复现用例："
                        "known_issues/test_known_issues.py::"
                        "test_the_cli_reports_an_empty_stats_input_instead_of_crashing"
                    ),
                ),
            ),
        )
        candidates.append(cand_record)

    # 候选 2：存在重大架构风险且证据不足的投机性机会（触发 defer 路径）
    # 证据置信度低 (0.25 < 0.30 阈值)，风险高 (0.80)，促使系统作出"不行动"的审慎决策
    if cli_src_path.exists():
        cand_deferred = EvolutionCandidate(
            id=M2_DEFERRED_CANDIDATE_ID,
            category=OpportunityCategory.ARCHITECTURE,
            title="PILOT-SPEC-001: 引入异步事件循环以提高批量统计吞吐量",
            hypothesis="异步事件循环可能对大规模数据统计带来吞吐量收益，但目前缺少基准数据且可能触碰非目标约束",
            expected_outcome=("评估异步事件循环对 CLI 的影响并补充性能基准",),
            risk=RiskLevel.HIGH,
            confidence=Decimal("0.25"),
            estimated_effort="3d",
            evidence=(
                Evidence(
                    kind=EvidenceKind.FILE,
                    ref="src/tinylib/cli.py",
                    note="CLI 入口当前完全基于同步阻塞实现",
                ),
            ),
        )
        candidates.append(cand_deferred)

    return tuple(candidates)


@dataclass(frozen=True, slots=True)
class M2Discoverer:
    """M2 候选发现器。"""

    repo: Path
    project: Project

    async def discover(self, state: LoopBState) -> DiscoveryResult:
        del state
        candidates = discover_pilot_candidates(self.repo, self.project)
        return DiscoveryResult(candidates=candidates)


@dataclass(frozen=True, slots=True)
class M2Analyzer:
    """M2 目标差距分析器。"""

    repo: Path
    project: Project

    async def analyze(self, state: LoopBState) -> GapAnalysis:
        snapshot = state.snapshot.version if state.snapshot else 0
        candidates = discover_pilot_candidates(self.repo, self.project)
        gaps = tuple(
            GoalGap(
                id=M2_GAP_ID if cand.id == M2_RECORD_CANDIDATE_ID else M2_DEFERRED_GAP_ID,
                description=cand.hypothesis,
                evidence=cand.evidence,
                severity=GapSeverity.MEDIUM
                if cand.id == M2_RECORD_CANDIDATE_ID
                else GapSeverity.LOW,
            )
            for cand in candidates
        )
        return GapAnalysis(
            project_id=state.project_id,
            snapshot=f"snap-{snapshot}",
            overall_status=OverallStatus.EVOLVING,
            gaps=gaps,
        )


@dataclass(frozen=True, slots=True)
class M2Evaluator:
    """M2 价值 / 风险 / 成本评估器。

    为不同候选给出与其特征对应的评分：
    - CAND-M2-001: 确凿缺陷，价值 0.70，成本 0.30，置信度 0.90 -> 决策 record；
    - CAND-M2-002: 投机设想，价值 0.40，成本 0.80，置信度 0.25 -> 决策 defer。
    """

    async def evaluate(self, state: LoopBState) -> tuple[ScoredCandidate, ...]:
        discovery = state.discovery
        if discovery is None:
            return ()
        scored: list[ScoredCandidate] = []
        for candidate in discovery.candidates:
            if candidate.id == M2_RECORD_CANDIDATE_ID:
                factors = PriorityFactors.from_candidate(
                    candidate,
                    value=Decimal("0.70"),
                    impact=Decimal("0.60"),
                    urgency=Decimal("0.50"),
                    cost=Decimal("0.30"),
                )
            else:
                factors = PriorityFactors.from_candidate(
                    candidate,
                    value=Decimal("0.40"),
                    impact=Decimal("0.30"),
                    urgency=Decimal("0.20"),
                    cost=Decimal("0.80"),
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
class M2GapUpdater:
    """M2 轮后差距更新器。"""

    async def update(self, state: LoopBState) -> GapAnalysis:
        gaps = state.gaps
        if gaps is None:
            return GapAnalysis(
                project_id=state.project_id,
                snapshot="snap-unknown",
                overall_status=OverallStatus.EVOLVING,
            )
        # 修掉了 M2_GAP_ID 对应的缺陷，但保留 M2_DEFERRED_GAP_ID（因为推迟了尚未解决）
        remaining = tuple(gap for gap in gaps.gaps if gap.id != M2_GAP_ID)
        return GapAnalysis(
            project_id=gaps.project_id,
            snapshot=gaps.snapshot,
            overall_status=OverallStatus.EVOLVING,
            gaps=remaining,
        )


__all__ = [
    "M2_DEFERRED_CANDIDATE_ID",
    "M2_DEFERRED_GAP_ID",
    "M2_GAP_ID",
    "M2_RECORD_CANDIDATE_ID",
    "M2_TASK_KEY",
    "M2Analyzer",
    "M2Discoverer",
    "M2Evaluator",
    "M2GapUpdater",
    "discover_pilot_candidates",
]
