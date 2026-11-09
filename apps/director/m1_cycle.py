"""M1 单轮闭环的**脑子** —— 手工注入的那一个候选。

设计依据：docs/tasks.md TASK-P1-081（"手工注入一个 Candidate，走完 决策 →
任务 → 执行 → 独立验证 → 记录 全链"）；TASK-P1-082（M2 自主闭环用同一个
驱动器换一份配方）。

**这一层放的是"每一轮都一样"的那部分吗 —— 不是。** 那一部分在
:mod:`apps.director.cycle`：观察 → 快照 → 差距 → 发现 → 证据 → 评估 →
决策 → 计划 → 拆解 → 排期 → 循环 A → 评估结果 → 更新知识 → 更新差距，
十五个节点、同一套闸门、同一套落库。**这个模块只提供那一份里的"脑子"**：

- **观察** —— 看 git 的 HEAD 与目标的原文；
- **差距分析** —— 这一轮只有一条差距，就是那条候选；
- **发现候选** —— **手工注入的**那一个；
- **评分** —— 价值 0.7、影响 0.6、紧急度 0.5、成本 0.3；
- **轮后更新差距** —— 修掉的那条不再留着。

**M1 与 M2 换的是脑子，不是链路。** M2 的脑子在 :mod:`apps.director.
m2_discovery` —— 它自己去仓库里找问题，而不是被人塞一条。

**候选本身为什么是人话标题。** Critic 的第一问（问题是否真实存在）靠**词面
匹配**去测试报告里找失败用例 —— 中文标题与英文用例 id 对不上，那一问的结论
会是"未核实"。**未核实不否决**，所以链路照走；这一问的缺口写在
``docs/demo/m1-walkthrough.md`` 里，不靠改标题把它藏起来。M2 的候选标题
改成了 pytest 的 node id，正是为了把那一问从"未核实"变成"通过" —— 但那是
M2 的事（:mod:`apps.director.m2_discovery` 里有为什么）。
"""

from __future__ import annotations

from dataclasses import dataclass
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
    run_cycle,
)
from apps.director.m1_executor import EXECUTOR_NAME, M1PatchExecutor
from apps.director.patching import run_git
from core.evolution.assessment import GapSeverity, GoalGap, OverallStatus
from core.evolution.candidate import DiscoveryResult, EvolutionCandidate
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.evolution_loop import LoopBState, ProjectObservation
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.planning.stages import GapAnalysis

#: M1 的配置 —— 就是通用配置。
#:
#: **为什么是别名而不是子类。** M1 与 M2 的配置项**完全一样** —— 变的只是
#: 脑子（配方）。抄一份子类出来，两边就会各有一份字段清单，而改一处忘另一
#: 处这种事，**要等到某一天两边跑出不一样的行为才被发现**。
M1Config = CycleConfig

#: M1 的结论 —— 就是通用结论。
M1CycleResult = CycleResult

#: 试点项目 id —— 与 ``samples/pilot_repo/project.yaml`` 里的一致。
PILOT_PROJECT_ID: Final[str] = "pilot-tinylib"

#: 演示里记录的那一轮的周期 id。**不是每次运行都用它** —— 见
#: :func:`apps.director.cycle.new_cycle_id`。
M1_CYCLE_ID: Final[str] = "cycle-m1-0001"

#: 手工注入的候选 id。
M1_CANDIDATE_ID: Final[str] = "CAND-M1-001"

#: 任务标识（领域侧）—— 路线图里的 key。
M1_TASK_KEY: Final[str] = "T-M1-001"

#: 试点仓库里那条缺陷的编号 —— PILOT-001。
PILOT_ISSUE: Final[str] = "PILOT-001"

#: 这一轮用到的差距 id —— 差距分析与轮后更新都指着它。
M1_GAP_ID: Final[str] = "gap-m1-001"


def m1_candidate() -> EvolutionCandidate:
    """**手工注入的那一个候选** —— 试点仓库的 PILOT-001。

    **证据指向真的文件。** 两条 ``file`` 证据指的路径在仓库里都存在 ——
    Critic 的第二问（证据是否充分）会去仓库里核，核不到就是**否决**，
    而否决意味着这条决策拿不到放行凭据，任务也就派生不出来。

    **置信度给到 0.9。** 这是"证据有多硬"，不是"我有多想做" ——
    策略的第一道闸就是置信度（低于 0.3 直接 defer），而这条候选有两条
    可核的文件证据。

    **标题是人话，不是用例名。** 这条标题是给人看的（"空输入是一句用法错误，
    不该是一段 traceback"）；代价是 Critic 的第一问（问题是否真实存在）靠
    **词面匹配**去测试报告里找失败用例 —— 中文标题与英文用例 id 对不上，
    那一问的结论会是"未核实"。**未核实不否决**（见
    :mod:`agents.critic.report` 的否决清单），所以链路照走；这一问的缺口
    写在 ``docs/demo/m1-walkthrough.md`` 里，不靠改标题把它藏起来。
    """
    return EvolutionCandidate(
        id=M1_CANDIDATE_ID,
        category=OpportunityCategory.QUALITY,
        title="CLI 在空输入时抛未捕获的 ValueError",
        evidence=(
            Evidence(
                kind=EvidenceKind.FILE,
                ref="src/tinylib/cli.py",
                note="stats 分支没有空输入守卫，直接把空序列交给 mean()",
            ),
            Evidence(
                kind=EvidenceKind.FILE,
                ref="known_issues/test_known_issues.py",
                note=f"{PILOT_ISSUE} 的复现用例：期望退出码 2，实际抛 ValueError",
            ),
        ),
        hypothesis="空输入是一句用法错误，不该是一段 traceback",
        expected_outcome=("cli.main(['stats', '']) 返回 2，不再抛 ValueError",),
        risk=RiskLevel.LOW,
        confidence=Decimal("0.9"),
        estimated_effort="0.5d",
    )


# --------------------------------------------------------------------------
# 循环 B 的各步
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Observer:
    """观察项目 —— 看 git 的 HEAD 与目标的原文。

    **它不报达成度。** M1 这一版不测量任何指标 —— 而"没有可比的前后达成
    度"意味着这一轮的结局只能是"没有可验证的改善"，那是诚实的答案
    （:func:`core.metrics.cycle.classify_cycle` 的第四条）。**把达成度算
    出来是 M2 的事**（TASK-P1-082 的 T2 ④）。
    """

    repo: Path
    goal: str
    tech_stack: tuple[str, ...]
    latest_snapshot_version: int

    async def observe(self, state: LoopBState) -> ProjectObservation:
        commit = run_git(self.repo, "rev-parse", "HEAD").strip()
        branch = run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        return ProjectObservation(
            project_id=state.project_id,
            commit=commit,
            branch=branch,
            goal=self.goal,
            tech_stack=self.tech_stack,
            metrics={},
            attainment=None,
            previous_attainment=None,
            latest_snapshot_version=self.latest_snapshot_version,
        )


@dataclass(frozen=True, slots=True)
class _Analyzer:
    """分析目标差距 —— 这一轮只有一条差距，就是那条候选。"""

    candidate: EvolutionCandidate

    async def analyze(self, state: LoopBState) -> GapAnalysis:
        snapshot = state.snapshot.version if state.snapshot else 0
        return GapAnalysis(
            project_id=state.project_id,
            snapshot=f"snap-{snapshot}",
            overall_status=OverallStatus.EVOLVING,
            gaps=(
                GoalGap(
                    id=M1_GAP_ID,
                    description=self.candidate.title,
                    evidence=self.candidate.evidence,
                    severity=GapSeverity.MEDIUM,
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _Discoverer:
    """发现候选 —— **手工注入的那一个**。

    **它照样走发现器这个口。** 候选从这里出去，落库口才会登记它、决策才会
    挂在它身上。手工注入的是"候选从哪来"，不是"候选之后发生什么"。
    """

    candidate: EvolutionCandidate

    async def discover(self, state: LoopBState) -> DiscoveryResult:
        del state  # 手工注入的候选不看项目状态 —— 它就是那一个。
        return DiscoveryResult(candidates=(self.candidate,))


@dataclass(frozen=True, slots=True)
class _Evaluator:
    """评估价值 / 风险 / 成本。

    **风险与置信度取候选自己的**（:meth:`PriorityFactors.from_candidate`）——
    那两样是证据的属性。其余四项是判断，这里给的是**这一轮的判断**，
    写在代码里而不是藏在某次运行里。
    """

    value: Decimal = Decimal("0.7")
    impact: Decimal = Decimal("0.6")
    urgency: Decimal = Decimal("0.5")
    cost: Decimal = Decimal("0.3")

    async def evaluate(self, state: LoopBState) -> tuple[ScoredCandidate, ...]:
        discovery = state.discovery
        if discovery is None:
            return ()
        scored: list[ScoredCandidate] = []
        for candidate in discovery.candidates:
            factors = PriorityFactors.from_candidate(
                candidate,
                value=self.value,
                impact=self.impact,
                urgency=self.urgency,
                cost=self.cost,
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
class _GapUpdater:
    """更新目标差距 —— 这一轮修掉的那条差距不再留着。

    **整体状态仍然是"演进中"，不是"已达标"。** 修掉一条差距不等于目标达成
    —— 试点项目的四条关键结果里还有三条没达成。把它报成 ``AT_TARGET`` 就是
    拿一件小事冒充一件大事；而 ``OverallStatus`` 里**没有** ``STABLE``
    这个取值（只有 ``EVOLVING`` / ``AT_TARGET`` / ``BLOCKED`` / ``STALLED``），
    写一个不存在的枚举值连类型检查都过不去。
    """

    async def update(self, state: LoopBState) -> GapAnalysis:
        gaps = state.gaps
        if gaps is None:
            return GapAnalysis(
                project_id=state.project_id,
                snapshot="snap-unknown",
                overall_status=OverallStatus.EVOLVING,
            )
        remaining = tuple(gap for gap in gaps.gaps if gap.id != M1_GAP_ID)
        return GapAnalysis(
            project_id=gaps.project_id,
            snapshot=gaps.snapshot,
            overall_status=OverallStatus.EVOLVING,
            gaps=remaining,
        )


# --------------------------------------------------------------------------
# 配方与入口
# --------------------------------------------------------------------------


def m1_recipe() -> CycleRecipe:
    """M1 那一份配方 —— **换脑子不换链路**。

    **这一份配方里的每一项都能在 :mod:`apps.director.m2_cycle` 里找到对应
    物** —— 两边跑的是同一张图、同一套闸门、同一套落库。
    """
    candidate = m1_candidate()

    def brain(context: CycleContext) -> CycleBrain:
        project = context.project
        objective = (
            project.goal.objective[0].description if project.goal.objective else project.name
        )
        return CycleBrain(
            observer=_Observer(
                repo=context.repo,
                goal=objective,
                tech_stack=tuple(project.tech_stack),
                latest_snapshot_version=context.latest_snapshot_version,
            ),
            analyzer=_Analyzer(candidate=candidate),
            discoverer=_Discoverer(candidate=candidate),
            evaluator=_Evaluator(),
            gap_updater=_GapUpdater(),
        )

    return CycleRecipe(
        brain=brain,
        executors={EXECUTOR_NAME: M1PatchExecutor()},
        executor_name=EXECUTOR_NAME,
        task_instruction=(
            "在 stats 分支加上空输入守卫，返回退出码 2；"
            "把 PILOT-001 的复现用例搬回 tests/ 作为常规回归"
        ),
        task_key=M1_TASK_KEY,
        gap_id=M1_GAP_ID,
        verifier_name="m1-independent-verifier",
        approver="m1-approver",
        merge_comment="M1 演示：人工放行这一次合并",
        merge_message="M1：合入单轮闭环的产出",
        repo_email="m1@ases.local",
        repo_name="M1 驱动",
        cycle_prefix="cycle-m1",
    )


async def run_m1_cycle(
    config: M1Config,
    session_factory: async_sessionmaker[AsyncSession],
) -> M1CycleResult:
    """**跑完一整轮 M1 闭环** —— 从候选到验证通过。

    这一轮的全过程：

    1. 读项目定义（``project.yaml``）；
    2. 把试点仓库复制成一份独立的 git 仓库，**原目录一个字都不动**；
    3. 跑一遍复现套件，产出 Critic 要看的测试报告；
    4. 落项目行（外键的前置条件）；
    5. 走循环 B：观察 → 快照 → 差距 → **发现（手工注入的候选）** → 证据 →
       评估 → 决策 → 记录 → 计划 → 拆解落库 → 排期 → 执行 → 评估结果 →
       更新知识 → 更新差距；
    6. 循环 A 在拆解落库之后接手：工作区 → 执行 → **独立验证（真跑 pytest）**
       → 验收判定 → 合并闸门 → 回写 → 收尾；
    7. 汇总结论 —— 每一样都能对账。

    **要一个真的 Postgres。** T1 的验收口径是"每一步的 DB 记录可查" ——
    那些记录得**真的**在数据库里。链路里被替换掉的只有"写代码"那一段
    （见 :mod:`apps.director.m1_executor`），落库不是。

    :param config: 这一轮的配置。
    :param session_factory: 数据库会话工厂。
    :raises RuntimeError: 链路本身跑不动时（那一轮没发生，不是"没做"）。
    """
    return await run_cycle(config, m1_recipe(), session_factory)


__all__ = [
    "M1_CANDIDATE_ID",
    "M1_CYCLE_ID",
    "M1_GAP_ID",
    "M1_TASK_KEY",
    "PILOT_ISSUE",
    "PILOT_PROJECT_ID",
    "M1Config",
    "M1CycleResult",
    "m1_candidate",
    "m1_recipe",
    "run_m1_cycle",
]
