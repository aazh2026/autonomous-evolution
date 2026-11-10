"""TASK-P1-082 —— M2 自主闭环验收的落库集成验证（T1 / T2）。

覆盖 TASK-P1-082 的全部 T2 判据：
① 系统自主产出的候选数 ≥ 1 且全部有证据引用；
② 至少 1 个候选走了 defer/reject 路径（证明"不行动"能力真实生效）；
③ 完成的变更通过了独立验证（VerificationRun 存在且 result = passed）；
④ 目标达成度差值可计算且 > 0（周期结局真实判定为 improved）。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.director.m2_budget import check_m2_budget
from apps.director.m2_cycle import (
    M2Config,
    M2CycleResult,
    run_m2_cycle,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_DIR = REPO_ROOT / "samples" / "pilot_repo"
PROJECT_FILE = PILOT_DIR / "project.yaml"


async def _run_m2(engine: AsyncEngine, tmp_path: Path) -> M2CycleResult:
    """在隔离目录跑一轮 M2 自主闭环。"""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    config = M2Config(
        project_file=PROJECT_FILE,
        pilot_source=PILOT_DIR,
        work_root=tmp_path / "m2-work",
        python_executable=sys.executable,
    )
    return await run_m2_cycle(config, session_factory)


async def _one(session: AsyncSession, sql: str, **params: object) -> dict[str, Any]:
    """跑一句查询，拿回唯一的这一行。"""
    result = await session.execute(text(sql), params)
    row = result.mappings().one_or_none()
    assert row is not None, f"查不到这一行：{sql}，参数={params}"
    return dict(row)


async def _all(session: AsyncSession, sql: str, **params: object) -> list[dict[str, Any]]:
    """跑一句查询，拿回全部行。"""
    result = await session.execute(text(sql), params)
    return [dict(r) for r in result.mappings().all()]


class TestM2AutonomousCycleIntegration:
    """M2 自主闭环核心验收断言集。"""

    async def test_t2_criterion_1_candidates_with_evidence(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """T2 ① —— 自主发掘候选数 ≥ 1 且全带证据引用。"""
        result = await _run_m2(engine, tmp_path)

        assert len(result.candidates) >= 2, "自主发掘的候选数必须 ≥ 2"

        rows = await _all(
            session,
            "select id::text, category, title, status, confidence, evidence "
            "from evolution_candidate where cycle_id = :cycle_id",
            cycle_id=result.records["evolution_cycle"],
        )
        assert len(rows) >= 2
        for row in rows:
            assert row["evidence"], f"候选 {row['title']} 必须落库其证据引用"
            assert isinstance(row["evidence"], list)
            assert len(row["evidence"]) >= 1

    async def test_t2_criterion_2_inaction_decision_taken(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """T2 ② —— 至少 1 个候选走了 defer/reject 路径。"""
        result = await _run_m2(engine, tmp_path)

        dec_rows = await _all(
            session,
            "select id::text, candidate_id::text, decision, rationale, requested_analysis "
            "from evolution_decision where cycle_id = :cycle_id",
            cycle_id=result.records["evolution_cycle"],
        )
        decisions_by_state = {r["decision"]: r for r in dec_rows}

        # 必须同时有 record 与 defer 两种决策
        assert "record" in decisions_by_state, "必须有决定执行的候选"
        assert "defer" in decisions_by_state, "必须有决定推迟（不行动）的候选"

        defer_row = decisions_by_state["defer"]
        assert defer_row["requested_analysis"], "defer 决策必须明确附带补充分析要求"

        # 验证只有 record 决策派生了任务，defer 决策绝不派生任务
        task_rows = await _all(
            session,
            "select id::text, decision_id::text, title from task where cycle_id = :cycle_id",
            cycle_id=result.records["evolution_cycle"],
        )
        assert len(task_rows) == 1, "只应有一个任务派生出来"
        assert task_rows[0]["decision_id"] == decisions_by_state["record"]["id"]

    async def test_t2_criterion_3_independent_verification_passed(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """T2 ③ —— 完成的变更通过了独立验证。"""
        result = await _run_m2(engine, tmp_path)

        assert result.accepted is True
        assert result.merged is True
        assert result.merge_commit

        v_run = await _one(
            session,
            "select id::text, task_id::text, cycle_id::text, result, verified_by "
            "from verification_run where task_id = :task_id",
            task_id=result.task_id,
        )
        assert v_run["result"] == "passed"
        assert v_run["verified_by"] == "m2-independent-verifier"

        artifact = await _one(
            session,
            "select id::text, type, hash from artifact where task_id = :task_id",
            task_id=result.task_id,
        )
        assert artifact["type"] == "execution_trace"
        assert artifact["hash"]

    async def test_t2_criterion_4_goal_attainment_delta_positive(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """T2 ④ —— 目标达成度差值可计算且 > 0，结局判定为 improved。"""
        result = await _run_m2(engine, tmp_path)

        assert result.attainment_before is not None
        assert result.attainment_after is not None
        assert result.attainment_delta is not None
        assert result.attainment_delta > Decimal("0"), (
            f"目标达成度差值必须 > 0，当前为 {result.attainment_delta}"
        )
        assert result.cycle_outcome == "improved"

        cycle_row = await _one(
            session,
            "select id::text, cycle_id, status, outcome, metrics "
            "from evolution_cycle where id = :id",
            id=result.cycle_ref,
        )
        assert cycle_row["status"] == "completed"
        assert cycle_row["outcome"] == "improved"
        assert cycle_row["metrics"].get("attainment_delta") == float(result.attainment_delta)

    async def test_budget_within_baseline(self, engine: AsyncEngine, tmp_path: Path) -> None:
        """预算与时长验证 —— 耗时与成本落在 TASK-P1-072 基线 2 倍以内。"""
        result = await _run_m2(engine, tmp_path)
        checks = check_m2_budget(
            repo_root=REPO_ROOT,
            cost=result.cost,
            elapsed_seconds=result.elapsed_seconds,
        )
        for check in checks:
            assert check.within, f"{check.metric} 超出基线 2 倍：{check.message()}"
