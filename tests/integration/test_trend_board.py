"""TASK-P3-017 —— 趋势看板能不能真的从库里把四层指标读出来。

验证（T2）：能导出 ≥7 天连续运行的四层指标趋势。

**这个测试跑的是真东西** —— 真 PostgreSQL、真周期、真候选、真决策、真任务、
真验收、真账本流水。它验的是**读数这一层**能不能把库里的行摆成看板要的形状，
而不是"看板的算法对不对"（那件事由 ``tests/unit/evaluation/test_trend.py``
用纯函数证伪）。

**七天里有一天故意没有周期。** 那一天不能变成"那天是平的" —— 它必须仍然有
一个点，值与前一天相同（累计口径），而**它必须存在**。少一天就是少一天，
而"不见了"在图上读起来与"那天是平的"完全一样。

**账本里有一笔被拒绝的花费。** 它是"系统试图超预算但被拦住了"的证据，不是
花掉的钱 —— 成本曲线里不能有它。

**还有一个没闭合的周期。** 它没有结局，底下的任务也还没有最终状态 ——
拿它算指标等于把"还在跑"当成"没跑完"，所以它不进看板。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.entities import Project, Task
from core.entities.evidence import VerificationRun
from core.metrics.cycle import CycleStatus
from core.policy.budget import BudgetExhausted, BudgetMetric, SpendRequest
from core.task.state_machine import TaskState
from evaluation.common import MetricLayer, MetricState
from evaluation.trend import (
    COST_CUMULATIVE,
    VERIFICATION_PASS_RATE,
    TrendBoard,
    TrendWindow,
    build_board,
)
from infrastructure.database.budget import PostgresBudgetLedger
from infrastructure.database.repositories import (
    DECISION_RECORD,
    CandidateRepository,
    CycleRepository,
    DecisionRepository,
    TaskRepository,
)
from infrastructure.database.trend import PostgresTrendReader
from orchestration.dashboard import TrendDashboard

pytestmark = pytest.mark.integration

_PROJECT_ID = "p-trend"

#: 看板的窗口 —— 验收要求"≥7 天连续运行"。
_END = date(2026, 11, 10)
_DAYS = 7

#: 种了周期的那些天 —— **11-06 故意空着**。
_SEEDED_DAYS = (
    date(2026, 11, 3),
    date(2026, 11, 4),
    date(2026, 11, 5),
    date(2026, 11, 7),
    date(2026, 11, 8),
    date(2026, 11, 9),
    date(2026, 11, 10),
)

#: 任务在第几天之后不再完成 —— 前四天完成，后三天没完成。
_COMPLETED_UNTIL_INDEX = 4

#: 每天的花费。
_DAILY_COST = Decimal("1.00")


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """一个会话工厂 —— 看板的读取口用它。"""
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_project(session: AsyncSession) -> None:
    project = Project(
        id=_PROJECT_ID,
        name="趋势看板项目",
        goal={"objective": [{"id": "OBJ-1", "description": "让趋势看得见"}]},
        policy={"merge_to_main": "approval_required"},
        budget={"currency": "USD"},
        repository_url="https://example.invalid/trend.git",
    )
    session.add(project)
    await session.flush()


async def _seed_day(session: AsyncSession, *, day: date, index: int) -> None:
    """种一天：候选 → 决策 → 任务 → 验收。

    **任务归到周期上要穿过决策** —— 任务表上没有 ``cycle_id`` 列，所以这条
    链路（候选挂周期、决策挂候选、任务挂决策）是这条用例真正在铺的路。
    """
    cycles = CycleRepository(session)
    candidates = CandidateRepository(session)
    decisions = DecisionRepository(session)
    tasks = TaskRepository(session)

    started = datetime.combine(day, time(hour=9), tzinfo=UTC)
    ended = datetime.combine(day, time(hour=11), tzinfo=UTC)
    cycle = await cycles.open_cycle(
        project_id=_PROJECT_ID,
        cycle_id=f"cycle-{index:04d}",
        trigger="schedule",
        started_at=started,
    )

    candidate = await candidates.create(
        project_id=_PROJECT_ID,
        category="quality",
        title=f"第 {index} 天的候选",
        evidence=[{"kind": "coverage_report", "ref": f"artifact://report-{index}"}],
        cycle_id=cycle.id,
    )
    decision = await decisions.record(
        candidate_id=candidate.id,
        decision=DECISION_RECORD,
        rationale="证据充分",
        cycle_id=cycle.id,
    )

    task = await tasks.create_from_decision(
        decision.id,
        title=f"第 {index} 天的任务",
        task_type="test",
    )
    completed = index < _COMPLETED_UNTIL_INDEX
    if completed:
        await tasks.bind_acceptance_contract(task.id, {"task_id": str(task.id), "checks": ["unit"]})
        task.status = TaskState.COMPLETED.value

    session.add(
        VerificationRun(
            task_id=task.id,
            cycle_id=cycle.id,
            checks=[{"name": "unit", "result": "passed" if completed else "failed"}],
            result="passed" if completed else "failed",
            verified_by="verifier",
            created_at=ended,
        )
    )

    await cycles.close_cycle(
        cycle.id,
        status=CycleStatus.COMPLETED,
        attainment_before=Decimal("0.40"),
        attainment_after=Decimal("0.60"),
        ended_at=ended,
    )
    await session.flush()


async def _seed_skipped_verification(session: AsyncSession) -> None:
    """一条"跳过"的验收 —— 它不进分母。

    **没跑的东西没有结论** —— 把它算成"没通过"等于因为没验而惩罚系统。
    """
    rows = await session.execute(select(Task).limit(1))
    task = rows.scalars().one()
    session.add(
        VerificationRun(
            task_id=task.id,
            cycle_id=None,
            checks=[{"name": "unit", "result": "skipped"}],
            result="skipped",
            verified_by="verifier",
            created_at=datetime.combine(date(2026, 11, 6), time(hour=12), tzinfo=UTC),
        )
    )
    await session.flush()


async def _seed_open_cycle(session: AsyncSession) -> None:
    """一个**还没闭合**的周期 —— 它不进看板。"""
    await CycleRepository(session).open_cycle(
        project_id=_PROJECT_ID,
        cycle_id="cycle-open",
        trigger="schedule",
        started_at=datetime.combine(date(2026, 11, 10), time(hour=12), tzinfo=UTC),
    )
    await session.flush()


async def _record_costs(factory: async_sessionmaker[AsyncSession]) -> None:
    """走**生产路径**记账：每天一笔，另加一笔被拒绝的。

    **被拒绝的那笔也留痕** —— 它是"系统试图超预算但被拦住了"的证据。
    而它**不是花掉的钱**，所以成本曲线里不能有它。
    """
    ledger = PostgresBudgetLedger(factory)
    for day in _SEEDED_DAYS:
        await ledger.consume(
            SpendRequest(
                scope=_PROJECT_ID,
                metric=BudgetMetric.TOTAL_COST,
                amount=_DAILY_COST,
                currency="USD",
                limit=None,
                moment=datetime.combine(day, time(hour=12), tzinfo=UTC),
            )
        )
    with pytest.raises(BudgetExhausted):
        await ledger.consume(
            SpendRequest(
                scope=_PROJECT_ID,
                metric=BudgetMetric.TOTAL_COST,
                amount=Decimal("5.00"),
                currency="USD",
                limit=Decimal("7.00"),
                moment=datetime.combine(date(2026, 11, 10), time(hour=13), tzinfo=UTC),
            )
        )


async def _seed_everything(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """把七天种齐 —— 含一个没有周期的日子、一条跳过的验收、一笔被拒的花费。"""
    await _seed_project(session)
    for index, day in enumerate(_SEEDED_DAYS):
        await _seed_day(session, day=day, index=index)
    await _seed_skipped_verification(session)
    await _seed_open_cycle(session)
    await session.commit()
    await _record_costs(factory)


async def _read_board(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[TrendWindow, TrendBoard]:
    """读出看板 —— 走真实的读取口。"""
    window = TrendWindow.last_days(_END, _DAYS)
    reader = PostgresTrendReader(factory)
    data = await reader.read(project_id=_PROJECT_ID, until=_END)
    return window, build_board(_PROJECT_ID, window, data)


class TestTheBoardIsReadFromRealRows:
    async def test_the_board_covers_seven_consecutive_days(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_everything(session, factory)

        window, board = await _read_board(factory)

        assert window.days == _DAYS
        for trend in board.trends:
            assert trend.days == _DAYS, f"{trend.metric_id} 的点数与窗口对不上"
            assert tuple(point.day for point in trend.points) == window.day_list()

    async def test_all_four_layers_are_present(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """四层一层都不能少 —— 少了项目演进层就答不了"项目是否真的变好"。"""
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)

        for layer in MetricLayer:
            assert board.by_layer(layer), f"看板缺了 {layer} 层"

    async def test_the_day_without_a_cycle_still_has_a_point(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """**没有周期的那天不是"那天是平的"** —— 它是一个点，值与前一天相同。"""
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        trend = board.trend("task_success_rate")

        gap = trend.point_on(date(2026, 11, 6))
        previous = trend.point_on(date(2026, 11, 5))

        assert gap.day == date(2026, 11, 6)
        assert gap.value == previous.value
        assert gap.detail["cycles_closed"] == 3

    async def test_an_unclosed_cycle_is_not_counted(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """**没闭合的周期没有结局** —— 它的任务也还没有最终状态。"""
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)

        point = board.trend("task_success_rate").point_on(_END)
        assert point.detail["cycles_closed"] == len(_SEEDED_DAYS)

    async def test_the_task_success_rate_comes_from_the_real_task_rows(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """七天的任务里四天完成了 —— 成功率是 4/7。

        **分母是全部任务，包括没跑完的。** 只拿"跑完的任务"当分母，会让
        一个跑不完就放弃的系统显示出很高的成功率。
        """
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        point = board.trend("task_success_rate").point_on(_END)

        assert point.state is MetricState.COMPUTED
        assert point.numerator == Decimal(4)
        assert point.denominator == Decimal(7)
        assert point.value == Decimal(4) / Decimal(7)

    async def test_the_decision_adoption_rate_comes_from_the_real_decisions(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        point = board.trend("decision_adoption_rate").point_on(_END)

        assert point.state is MetricState.COMPUTED
        assert point.numerator == Decimal(len(_SEEDED_DAYS))
        assert point.denominator == Decimal(len(_SEEDED_DAYS))

    async def test_the_duplicate_discovery_rate_sees_the_evidence_refs(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """每天的候选引用各自的报告 —— 没有重复。"""
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        point = board.trend("duplicate_discovery_rate").point_on(_END)

        assert point.state is MetricState.COMPUTED
        assert point.value == Decimal(0)
        assert point.denominator == Decimal(len(_SEEDED_DAYS))

    async def test_the_attainment_delta_comes_from_the_cycle_snapshots(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        point = board.trend("attainment_delta").point_on(_END)

        assert point.state is MetricState.COMPUTED
        assert point.value == Decimal("0.20")


class TestTheQualityAndCostDimensionsComeFromTheLedger:
    async def test_the_cost_equals_the_recorded_entries(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """**成本只认账本**，而且**被拒绝的那笔不算**。"""
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        point = board.trend(COST_CUMULATIVE).point_on(_END)

        assert point.state is MetricState.COMPUTED
        assert point.value == Decimal("7.00")
        assert point.detail["currency"] == "USD"
        assert point.detail["entries"] == len(_SEEDED_DAYS)

    async def test_the_cost_is_cumulative(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        trend = board.trend(COST_CUMULATIVE)

        assert trend.point_on(date(2026, 11, 5)).value == Decimal("3.00")
        assert trend.point_on(date(2026, 11, 10)).value == Decimal("7.00")

    async def test_the_verification_pass_rate_ignores_the_skipped_run(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """**没跑的东西没有结论** —— 跳过的验收不进分母。"""
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        point = board.trend(VERIFICATION_PASS_RATE).point_on(_END)

        assert point.state is MetricState.COMPUTED
        assert point.numerator == Decimal(4)
        assert point.denominator == Decimal(len(_SEEDED_DAYS))
        assert point.detail["skipped"] == 1


class TestTheMissingBaselinesSaySo:
    async def test_without_labels_the_validity_rate_is_uncomputable(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """没有人工标注不是"有效发现率 0%"。"""
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        point = board.trend("valid_discovery_rate").point_on(_END)

        assert point.state is MetricState.BASELINE_MISSING
        assert point.value is None

    async def test_without_a_defect_source_the_regression_rate_is_uncomputable(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """阶段一的库里没有缺陷表 —— 绝不能报"回归缺陷率 0%"。"""
        await _seed_everything(session, factory)

        _, board = await _read_board(factory)
        point = board.trend("regression_defect_rate").point_on(_END)

        assert point.state is MetricState.BASELINE_MISSING
        assert point.value is None


class TestTheDashboardWiring:
    async def test_the_dashboard_exports_json_and_markdown(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_everything(session, factory)

        dashboard = TrendDashboard(source=PostgresTrendReader(factory), label_store=None)
        export = await dashboard.build(
            project_id=_PROJECT_ID,
            window=TrendWindow.last_days(_END, _DAYS),
        )

        payload = json.loads(export.to_json())
        assert payload["window"]["days"] == _DAYS
        assert len(payload["trends"]) == 10

        markdown = export.to_markdown()
        assert "跨周期趋势看板" in markdown
        assert "备注" in markdown

    async def test_the_dashboard_says_the_baseline_is_missing(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """**少了标注基线这件事必须被看见** —— 否则读的人会把"基线未建立"
        当成"算出来是 0"。"""
        await _seed_everything(session, factory)

        dashboard = TrendDashboard(source=PostgresTrendReader(factory), label_store=None)
        export = await dashboard.build(
            project_id=_PROJECT_ID,
            window=TrendWindow.last_days(_END, _DAYS),
        )

        assert export.notes
        assert "基线未建立" in export.notes[0]

    async def test_the_cycle_rows_are_read_in_order(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """周期按闭合时间排 —— 重复发现率靠这个顺序判重。"""
        await _seed_everything(session, factory)

        reader = PostgresTrendReader(factory)
        data = await reader.read(project_id=_PROJECT_ID, until=_END)

        closed = [item.closed_on for item in data.cycles]
        assert closed == sorted(closed)
        assert len(data.cycles) == len(_SEEDED_DAYS)

    async def test_a_project_without_cycles_still_gets_a_board(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """什么都没有的时候，也得有看板 —— **空看板比没有看板有用**：
        它明确地说"这儿还没有数据"。"""
        await _seed_project(session)
        await session.commit()

        reader = PostgresTrendReader(factory)
        data = await reader.read(project_id="p-empty", until=_END)
        board = build_board("p-empty", TrendWindow.last_days(_END, _DAYS), data)

        assert board.days == _DAYS
        assert board.trend("task_success_rate").point_on(_END).state is MetricState.NO_SAMPLES


class TestTheProjectSnapshotHasNoTimestamp:
    """**一个已知缺口，写下来免得以后有人当成设计。**

    ``project_snapshot`` 表**没有时间戳列** —— 它记的是"第 N 版快照"，不是
    "什么时候的快照"。所以真正的覆盖率 / 质量趋势（从 KR 实际值算出来的
    那一层）目前**无法按天归集**。

    看板里的质量维度因此用的是**验收通过率**（``verification_run`` 有
    ``created_at``），而不是快照里的指标 —— **能用时间戳归集的那一份**，
    才是能画成曲线的那一份。
    """

    async def test_the_snapshot_table_has_no_timestamp_column(
        self,
        session: AsyncSession,
    ) -> None:
        snapshots = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'project_snapshot'"
            )
        )
        names = {row[0] for row in snapshots}

        assert names, "project_snapshot 表不在 —— 用例的前置条件不满足"
        assert "created_at" not in names
        assert "recorded_at" not in names

    async def test_the_verification_table_has_one(
        self,
        session: AsyncSession,
    ) -> None:
        """**对照**：验收表有时间戳 —— 所以质量维度能按天归集。"""
        snapshots = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'verification_run'"
            )
        )
        names = {row[0] for row in snapshots}

        assert "created_at" in names
