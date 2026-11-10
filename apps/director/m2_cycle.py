"""M2 自主闭环驱动 —— 只给目标，系统自主完成演进周期。

设计依据：docs/tasks.md TASK-P1-082（M2 自主闭环验收）；docs/design.md §4、§9。

**M2 与 M1 的本质区别**：
- **M1**：候选由人工注入（单条候选），因无前后度量对比结局返回 ``no_change``；
- **M2**：候选由系统**自主发掘**（多条候选，含正常推进的缺陷候选与推迟的架构候选）；
- **真实度量达成度**：轮前轮后分别测量试点项目指标，计算出正向达成度差值（``delta > 0``）；
- **全链闭环**：独立验证通过、审批通过、代码合入、结局真实判定为 ``improved``。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.director.cycle import (
    CycleBrain,
    CycleConfig,
    CycleContext,
    CycleRecipe,
    CycleResult,
    pilot_actuals,
    run_cycle,
)
from apps.director.m2_discovery import (
    M2_DEFERRED_CANDIDATE_ID,
    M2_GAP_ID,
    M2_RECORD_CANDIDATE_ID,
    M2_TASK_KEY,
    M2Analyzer,
    M2Discoverer,
    M2Evaluator,
    M2GapUpdater,
)
from apps.director.m2_executor import EXECUTOR_NAME, M2PatchExecutor
from apps.director.m2_observer import M2Observer

#: M2 配置。
M2Config = CycleConfig

#: M2 结果。
M2CycleResult = CycleResult

#: 试点项目 ID。
PILOT_PROJECT_ID: Final[str] = "pilot-tinylib"

#: M2 演进周期默认 ID。
M2_CYCLE_ID: Final[str] = "cycle-m2-0001"


def m2_recipe() -> CycleRecipe:
    """构建 M2 自主闭环的配方。"""

    def brain(context: CycleContext) -> CycleBrain:
        project = context.project
        objective = (
            project.goal.objective[0].description if project.goal.objective else project.name
        )
        return CycleBrain(
            observer=M2Observer(
                repo=context.repo,
                project=project,
                goal=objective,
                tech_stack=tuple(project.tech_stack),
                latest_snapshot_version=context.latest_snapshot_version,
            ),
            analyzer=M2Analyzer(repo=context.repo, project=project),
            discoverer=M2Discoverer(repo=context.repo, project=project),
            evaluator=M2Evaluator(),
            gap_updater=M2GapUpdater(),
        )

    return CycleRecipe(
        brain=brain,
        executors={EXECUTOR_NAME: M2PatchExecutor()},
        executor_name=EXECUTOR_NAME,
        task_instruction=(
            "在 stats 分支加上空输入守卫，返回退出码 2；"
            "把 PILOT-001 的复现用例搬回 tests/ 作为常规回归"
        ),
        task_key=M2_TASK_KEY,
        gap_id=M2_GAP_ID,
        actuals=pilot_actuals,
        verifier_name="m2-independent-verifier",
        approver="m2-approver",
        merge_comment="M2 演示：自主闭环放行合并",
        merge_message="M2：自主闭环合入缺陷修复与回归用例",
        repo_email="m2@ases.local",
        repo_name="M2 驱动",
        cycle_prefix="cycle-m2",
    )


async def run_m2_cycle(
    config: M2Config,
    session_factory: async_sessionmaker[AsyncSession],
) -> M2CycleResult:
    """跑完一整轮 M2 自主闭环演进。

    从项目目标 YAML 开始，自主发掘候选、自主决策（包含 record 与 defer）、
    拆解派生任务、沙箱隔离执行、独立验证引擎验证、合并主分支并记录正向达成度收益。
    """
    return await run_cycle(config, m2_recipe(), session_factory)


__all__ = [
    "M2_CYCLE_ID",
    "M2_DEFERRED_CANDIDATE_ID",
    "M2_GAP_ID",
    "M2_RECORD_CANDIDATE_ID",
    "M2_TASK_KEY",
    "PILOT_PROJECT_ID",
    "M2Config",
    "M2CycleResult",
    "m2_recipe",
    "run_m2_cycle",
]
