"""M2 自主闭环组件的单元测试。

验证 M2 的四大核心判据逻辑：
1. 自主发掘候选数 ≥ 1 且全带证据引用；
2. 存在走了 defer/reject 的不行动候选；
3. 目标达成度前后测量差值可计算且 > 0；
4. 缺陷修复后相关差距被清除，未解决的差距被保留。
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from apps.director.m2_discovery import (
    M2_DEFERRED_CANDIDATE_ID,
    M2_GAP_ID,
    M2_RECORD_CANDIDATE_ID,
    M2Analyzer,
    M2Discoverer,
    M2Evaluator,
    M2GapUpdater,
    discover_pilot_candidates,
)
from apps.director.m2_observer import measure_pilot_actuals
from core.evolution.decision import DecisionPolicy, DecisionState
from core.evolution.evolution_loop import LoopBState, TriggerSource
from core.project.models import Project, load_project

REPO_ROOT = Path(__file__).resolve().parents[3]
PILOT_DIR = REPO_ROOT / "samples" / "pilot_repo"
PROJECT_FILE = PILOT_DIR / "project.yaml"


@pytest.fixture
def pilot_project() -> Project:
    return load_project(PROJECT_FILE)


class TestM2CandidateDiscovery:
    """T2 ① —— 自主发掘候选与证据完整性。"""

    def test_discovery_produces_multiple_candidates_with_evidence(
        self, pilot_project: Project
    ) -> None:
        candidates = discover_pilot_candidates(PILOT_DIR, pilot_project)
        assert len(candidates) >= 2, "M2 自主发掘的候选数量必须 ≥ 2"

        # 验证每条候选都带非空证据
        for cand in candidates:
            assert cand.evidence, f"候选 {cand.id} 必须携带证据引用"
            for ev in cand.evidence:
                assert ev.ref, "证据引用不能为空"
                # 证据引用的文件必须在仓库中真实存在
                target = PILOT_DIR / ev.ref
                assert target.exists(), f"证据引用的文件 {ev.ref} 在试点仓库中不存在"

        # 验证包含已知缺陷候选与投机性架构候选
        ids = {c.id for c in candidates}
        assert M2_RECORD_CANDIDATE_ID in ids
        assert M2_DEFERRED_CANDIDATE_ID in ids

    @pytest.mark.asyncio
    async def test_discoverer_port_returns_candidates(self, pilot_project: Project) -> None:
        discoverer = M2Discoverer(repo=PILOT_DIR, project=pilot_project)
        state = LoopBState(project_id="test", cycle_id="c1", trigger=TriggerSource.SCHEDULE)
        result = await discoverer.discover(state)
        assert len(result.candidates) >= 2


class TestM2DecisionAndInaction:
    """T2 ② —— 至少 1 个候选走 defer/reject 路径。"""

    @pytest.mark.asyncio
    async def test_policy_decides_record_and_defer(self, pilot_project: Project) -> None:
        evaluator = M2Evaluator()
        state = LoopBState(
            project_id="test",
            cycle_id="c1",
            trigger=TriggerSource.SCHEDULE,
            discovery=await M2Discoverer(PILOT_DIR, pilot_project).discover(
                LoopBState(project_id="test", cycle_id="c1", trigger=TriggerSource.SCHEDULE)
            ),
        )
        scored_candidates = await evaluator.evaluate(state)
        assert len(scored_candidates) == 2

        policy = DecisionPolicy()
        decisions = [
            policy.decide(scored, decision_id=f"DEC-{scored.candidate_id}")
            for scored in scored_candidates
        ]

        dec_map = {d.candidate_id: d for d in decisions}
        # 候选 1（确凿缺陷）必须是 record
        assert dec_map[M2_RECORD_CANDIDATE_ID].decision is DecisionState.RECORD
        assert dec_map[M2_RECORD_CANDIDATE_ID].produces_task is True

        # 候选 2（低置信度投机设想）必须是 defer（不行动）
        assert dec_map[M2_DEFERRED_CANDIDATE_ID].decision is DecisionState.DEFER
        assert dec_map[M2_DEFERRED_CANDIDATE_ID].produces_task is False
        assert len(dec_map[M2_DEFERRED_CANDIDATE_ID].requested_analysis) >= 1, (
            "defer 决策必须明确产出后续补充分析项"
        )


class TestM2AttainmentMeasurement:
    """T2 ④ —— 目标达成度差值可计算且 > 0。"""

    def test_attainment_increases_after_fix(self, tmp_path: Path, pilot_project: Project) -> None:
        # 复制试点仓库以模拟轮前与轮后状态
        repo_copy = tmp_path / "repo"
        shutil.copytree(PILOT_DIR, repo_copy)

        # 1. 轮前测量
        actuals_pre, attain_pre = measure_pilot_actuals(repo_copy, pilot_project)
        assert actuals_pre["KR-003"].number == Decimal("0")
        assert actuals_pre["KR-002"].number == Decimal("0.000000")
        assert attain_pre > Decimal("0")

        # 2. 模拟合入修复用例
        test_cli = repo_copy / "tests" / "test_cli.py"
        test_cli.write_text("def test_cli(): pass\n", encoding="utf-8")

        # 3. 轮后测量
        actuals_post, attain_post = measure_pilot_actuals(repo_copy, pilot_project)
        assert actuals_post["KR-003"].number == Decimal("1")
        kr2_num = actuals_post["KR-002"].number
        assert kr2_num is not None and kr2_num > Decimal("0")
        assert attain_post > attain_pre, "轮后达成度必须高于轮前达成度"

        delta = attain_post - attain_pre
        assert delta > Decimal("0"), f"达成度差值必须 > 0，当前为 {delta}"


class TestM2GapUpdater:
    """轮后目标差距更新。"""

    @pytest.mark.asyncio
    async def test_gap_updater_clears_resolved_gap(self, pilot_project: Project) -> None:
        analyzer = M2Analyzer(PILOT_DIR, pilot_project)
        state = LoopBState(project_id="test", cycle_id="c1", trigger=TriggerSource.SCHEDULE)
        gaps = await analyzer.analyze(state)
        assert len(gaps.gaps) == 2

        updater = M2GapUpdater()
        updated_state = state.model_copy(update={"gaps": gaps})
        result = await updater.update(updated_state)

        # M2_GAP_ID 已被修掉，剩下的差距只有 M2_DEFERRED_GAP_ID
        assert len(result.gaps) == 1
        assert result.gaps[0].id != M2_GAP_ID
