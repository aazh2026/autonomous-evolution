"""TASK-P1-071 —— 指标输入与真实数据库行的形状是否对得上。

验证（T1）：指标层的输入记录**能不能真的从库里取到**，以及在取不到的那几个
字段上，指标是不是**如实说"算不出来"**。

**这个测试证明的不是"已经有了一条采集管线"。** 阶段一没有采集管线 ——
那是 TASK-P1-072（成本与耗时采集）与看板任务的事。这里要证明的是更靠下的
一环：**输入记录的每个字段都有确定的来源，来源缺了的时候指标不编数**。

为什么这件事值得单独打一个集成测试。指标层的输入记录（``Discovery`` /
``DecisionRecord`` / ``TaskOutcome``）是纯 Python 的 dataclass —— 它们可以被
任何东西填上，包括一个随手编的值。单测里喂进去的样本再漂亮，也证明不了
**库里的行**长得对。而"长得对"是会悄悄坏掉的：

- ``evolution_candidate.evidence`` 是 JSON，元素结构由调用方决定 ——
  证据引用到底在 ``ref`` 键还是别的键，只有对着真库看一眼才知道；
- ``evolution_decision.decision`` 是三态字符串 —— 指标层按 ``record``
  过滤"要去做的决策"，字符串对不上，过滤就静默失效；
- **缺陷数据目前没有来源** —— 阶段一的库里没有缺陷表，任务的
  ``introduced_defects`` 只能缺着。这时候回归缺陷率必须报"算不出来"，
  绝不能报 0。

最后一条是这一节真正要守的东西。"回归缺陷率 0%"与"没人查过缺陷"在报表
上长得一模一样，而前者会让读的人以为这批交付是干净的 —— 一个**假的安全
信号**，比"缺数据"危险得多。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import EvolutionCandidate, EvolutionCycle, EvolutionDecision, Project, Task
from core.task.state_machine import TaskState
from evaluation.common import MetricState, Verdict
from evaluation.evolution_evaluation.decision import (
    DecisionRecord,
    decision_adoption_rate,
    false_positive_rate,
)
from evaluation.evolution_evaluation.discovery import (
    Discovery,
    duplicate_discovery_rate,
    valid_discovery_rate,
)
from evaluation.project_evaluation.attainment import CycleAttainment, attainment_delta
from evaluation.task_evaluation.delivery import (
    TaskOutcome,
    regression_defect_rate,
    rework_rate,
    task_success_rate,
)
from infrastructure.database.repositories import (
    DECISION_RECORD,
    DECISION_REJECT,
    CandidateRepository,
    CycleRepository,
    DecisionRepository,
    TaskRepository,
)

pytestmark = pytest.mark.integration

_PROJECT_ID = "p-metric-inputs"


async def _seed_project(session: AsyncSession) -> None:
    project = Project(
        id=_PROJECT_ID,
        name="指标输入项目",
        goal={"objective": [{"id": "OBJ-1", "description": "让指标算得出来"}]},
        policy={"merge_to_main": "approval_required"},
        budget={"currency": "USD"},
        repository_url="https://example.invalid/metrics.git",
    )
    session.add(project)
    await session.flush()


async def _seed_chain(session: AsyncSession) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """造出候选 → 决策 → 任务这条链，返回（周期 UUID，候选 UUID 列表）。

    两个候选**共享一条证据引用** —— 重复发现率要靠它才有非零的分母。
    四条决策覆盖三态：record、record（被推翻）、defer、reject。
    三个任务：两个完成（一个返过工），一个没完成。
    """
    cycles = CycleRepository(session)
    row = await cycles.open_cycle(project_id=_PROJECT_ID, cycle_id="cycle-0001", trigger="schedule")

    candidate_repo = CandidateRepository(session)
    decision_repo = DecisionRepository(session)
    task_repo = TaskRepository(session)

    first = await candidate_repo.create(
        project_id=_PROJECT_ID,
        category="quality",
        title="候选一",
        evidence=[{"kind": "coverage_report", "ref": "artifact://report-shared"}],
        cycle_id=row.id,
    )
    second = await candidate_repo.create(
        project_id=_PROJECT_ID,
        category="quality",
        title="候选二",
        evidence=[
            {"kind": "coverage_report", "ref": "artifact://report-shared"},
            {"kind": "log", "ref": "artifact://log-1"},
        ],
        cycle_id=row.id,
    )

    recorded = await decision_repo.record(
        candidate_id=first.id,
        decision=DECISION_RECORD,
        rationale="证据充分",
        cycle_id=row.id,
    )
    overturned = await decision_repo.record(
        candidate_id=second.id,
        decision=DECISION_RECORD,
        rationale="证据充分",
        cycle_id=row.id,
        overridden_by="critic",
    )
    await decision_repo.record(
        candidate_id=second.id,
        decision="defer",
        rationale="信息不足",
        requested_analysis=["补充性能基线"],
        cycle_id=row.id,
    )
    await decision_repo.record(
        candidate_id=first.id,
        decision=DECISION_REJECT,
        rationale="重复发现",
        cycle_id=row.id,
    )

    done = await task_repo.create_from_decision(recorded.id, title="任务一", task_type="test")
    await task_repo.bind_acceptance_contract(
        done.id, {"task_id": str(done.id), "checks": ["unit_tests"]}
    )
    done.status = TaskState.COMPLETED.value
    done.repair_count = 1

    also_done = await task_repo.create_from_decision(
        overturned.id, title="任务二", task_type="test"
    )
    await task_repo.bind_acceptance_contract(
        also_done.id, {"task_id": str(also_done.id), "checks": ["unit_tests"]}
    )
    also_done.status = TaskState.COMPLETED.value

    await task_repo.create_from_decision(recorded.id, title="任务三", task_type="test")

    await session.flush()
    return row.id, [first.id, second.id]


async def _cycle_name_map(session: AsyncSession) -> dict[uuid.UUID, str]:
    """周期 UUID → 人类可读的周期编号。

    指标记录里的 ``cycle_id`` 用的是人类可读的编号（``cycle-0001``），而不是
    数据库主键 —— 主键换一批数据就变了，编号才是稳定的。
    """
    rows = await session.execute(select(EvolutionCycle.id, EvolutionCycle.cycle_id))
    return dict(rows.all())


def _evidence_refs(candidate: EvolutionCandidate) -> tuple[str, ...]:
    """从候选的证据 JSON 里取出证据引用。

    ``evidence`` 的元素结构由调用方决定（``CandidateRepository.create`` 只
    保证它非空），所以这里按 ``ref`` 键取值，缺键的元素跳过 —— 而不是
    让 ``KeyError`` 从指标层冒出来。
    """
    refs: list[str] = []
    for item in candidate.evidence:
        ref = item.get("ref")
        if isinstance(ref, str):
            refs.append(ref)
    return tuple(refs)


async def _load_candidates(session: AsyncSession) -> list[EvolutionCandidate]:
    rows = await session.execute(
        select(EvolutionCandidate).order_by(EvolutionCandidate.created_at, EvolutionCandidate.id)
    )
    return list(rows.scalars().all())


async def _load_decisions(session: AsyncSession) -> list[EvolutionDecision]:
    rows = await session.execute(
        select(EvolutionDecision).order_by(EvolutionDecision.created_at, EvolutionDecision.id)
    )
    return list(rows.scalars().all())


async def _load_tasks(session: AsyncSession) -> list[Task]:
    rows = await session.execute(select(Task).order_by(Task.created_at, Task.id))
    return list(rows.scalars().all())


async def _discoveries(
    session: AsyncSession,
    labels: Mapping[str, Verdict] | None = None,
) -> list[Discovery]:
    """把库里的候选变成自主发现层的输入记录。"""
    names = await _cycle_name_map(session)
    candidates = await _load_candidates(session)
    return [
        Discovery(
            candidate_id=str(candidate.id),
            cycle_id=names[candidate.cycle_id] if candidate.cycle_id is not None else "",
            title=candidate.title,
            evidence_refs=_evidence_refs(candidate),
            label=labels.get(str(candidate.id)) if labels is not None else None,
        )
        for candidate in candidates
    ]


async def _decision_records(
    session: AsyncSession,
    labels: Mapping[str, Verdict] | None = None,
) -> list[DecisionRecord]:
    """把库里的决策变成决策质量层的输入记录。"""
    decisions = await _load_decisions(session)
    return [
        DecisionRecord(
            decision_id=str(item.id),
            candidate_id=str(item.candidate_id),
            decision=item.decision,
            overridden_by=item.overridden_by,
            candidate_label=labels.get(str(item.candidate_id)) if labels is not None else None,
        )
        for item in decisions
    ]


async def _task_outcomes(session: AsyncSession) -> list[TaskOutcome]:
    """把库里的任务变成工程交付层的输入记录。

    **缺陷个数不填** —— 阶段一的库里没有缺陷表，填 0 等于凭空断言
    "这批交付没有引入缺陷"。
    """
    tasks = await _load_tasks(session)
    return [
        TaskOutcome(
            task_id=str(task.id),
            completed=task.status == TaskState.COMPLETED.value,
            repair_count=task.repair_count,
        )
        for task in tasks
    ]


class TestTheMetricInputsCanBeBuiltFromRealRows:
    """形状对得上 —— 指标层的每个输入字段都有确定的来源。"""

    async def test_the_evidence_refs_come_out_of_the_evidence_column(
        self, session: AsyncSession
    ) -> None:
        await _seed_project(session)
        await _seed_chain(session)

        candidates = await _load_candidates(session)

        refs = {str(candidate.id): _evidence_refs(candidate) for candidate in candidates}
        assert len(refs) == 2
        assert sorted(ref for group in refs.values() for ref in group) == [
            "artifact://log-1",
            "artifact://report-shared",
            "artifact://report-shared",
        ]

    async def test_the_decision_vocabulary_matches_what_the_metrics_filter_on(
        self, session: AsyncSession
    ) -> None:
        """指标层按 ``record`` 过滤"要去做的决策" —— 库里的取值必须对得上。"""
        await _seed_project(session)
        await _seed_chain(session)

        decisions = await _load_decisions(session)

        assert sorted(item.decision for item in decisions) == [
            "defer",
            "record",
            "record",
            "reject",
        ]

    async def test_the_task_status_marks_completion(self, session: AsyncSession) -> None:
        await _seed_project(session)
        await _seed_chain(session)

        tasks = await _load_tasks(session)

        completed = [item for item in tasks if item.status == TaskState.COMPLETED.value]
        assert len(completed) == 2
        assert len(tasks) == 3


class TestTheMetricsOverRealRows:
    """真库里的数据算出来的数是对的。"""

    async def test_the_duplicate_discovery_rate_uses_the_shared_evidence(
        self, session: AsyncSession
    ) -> None:
        await _seed_project(session)
        await _seed_chain(session)

        result = duplicate_discovery_rate(await _discoveries(session))

        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal(1)
        assert result.denominator == Decimal(2)
        assert result.value == Decimal("0.5")

    async def test_the_decision_adoption_rate_counts_the_overturned_proposal(
        self, session: AsyncSession
    ) -> None:
        await _seed_project(session)
        await _seed_chain(session)

        result = decision_adoption_rate(await _decision_records(session))

        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal(1)
        assert result.denominator == Decimal(2)
        assert result.value == Decimal("0.5")

    async def test_the_task_metrics_use_the_real_status_and_repair_count(
        self, session: AsyncSession
    ) -> None:
        await _seed_project(session)
        await _seed_chain(session)

        outcomes = await _task_outcomes(session)

        success = task_success_rate(outcomes)
        rework = rework_rate(outcomes)

        assert success.numerator == Decimal(2)
        assert success.denominator == Decimal(3)
        assert success.value == Decimal("0.6666666666666666666666666667")
        assert rework.numerator == Decimal(1)
        assert rework.denominator == Decimal(3)
        assert rework.detail["total_repairs"] == 1


class TestTheMetricsRefuseToInventWhatIsMissing:
    """取不到的数据，指标必须说"取不到"。"""

    async def test_without_human_labels_the_validity_rate_is_uncomputable(
        self, session: AsyncSession
    ) -> None:
        """人工标注基线还没有（TASK-P1-083）—— 报 0 会让看板显示"有效率 0%"。"""
        await _seed_project(session)
        await _seed_chain(session)

        result = valid_discovery_rate(await _discoveries(session))

        assert result.state is MetricState.BASELINE_MISSING
        assert result.value is None
        assert result.detail["total_candidates"] == 2
        assert result.detail["labelled_candidates"] == 0

    async def test_without_human_labels_the_false_positive_rate_is_uncomputable(
        self, session: AsyncSession
    ) -> None:
        await _seed_project(session)
        await _seed_chain(session)

        result = false_positive_rate(await _decision_records(session))

        assert result.state is MetricState.BASELINE_MISSING
        assert result.value is None
        assert result.detail["proposals"] == 2

    async def test_without_a_defect_source_the_regression_rate_is_uncomputable(
        self, session: AsyncSession
    ) -> None:
        """阶段一的库里没有缺陷表 —— 绝不能报"回归缺陷率 0%"。

        那是**假的安全信号**：读的人会以为这批交付是干净的，而系统其实
        从来没有看过缺陷。
        """
        await _seed_project(session)
        await _seed_chain(session)

        result = regression_defect_rate(await _task_outcomes(session))

        assert result.state is MetricState.BASELINE_MISSING
        assert result.value is None
        assert result.detail["completed_tasks"] == 2
        assert result.detail["tasks_with_defect_data"] == 0


class TestTheMetricsComputeOnceTheMissingDataArrives:
    """基线到位之后，同一批行就能算出数来。"""

    async def test_the_labels_make_the_validity_and_false_positive_rates_computable(
        self, session: AsyncSession
    ) -> None:
        """人工标注到位后的样子 —— 这里是 TASK-P1-083 交付的形状。"""
        await _seed_project(session)
        _, candidate_ids = await _seed_chain(session)

        labels: Mapping[str, Verdict] = {
            str(candidate_ids[0]): Verdict.VALID,
            str(candidate_ids[1]): Verdict.INVALID,
        }

        validity = valid_discovery_rate(await _discoveries(session, labels))
        assert validity.state is MetricState.COMPUTED
        assert validity.numerator == Decimal(1)
        assert validity.denominator == Decimal(2)

        false_positive = false_positive_rate(await _decision_records(session, labels))
        assert false_positive.state is MetricState.COMPUTED
        assert false_positive.numerator == Decimal(1)
        assert false_positive.denominator == Decimal(2)

    async def test_the_cycle_attainment_snapshots_flow_into_the_evolution_layer(
        self, session: AsyncSession
    ) -> None:
        """达成度快照存在周期的 ``metrics`` JSON 里 —— 取出来就是差值的输入。"""
        await _seed_project(session)
        cycle_uuid, _ = await _seed_chain(session)

        cycle = await session.get(EvolutionCycle, cycle_uuid)
        assert cycle is not None
        cycle.metrics = {
            **cycle.metrics,
            "attainment_before": "0.40",
            "attainment_after": "0.65",
        }
        await session.flush()

        attainment = CycleAttainment(
            cycle_id=cycle.cycle_id,
            attainment_before=Decimal(str(cycle.metrics["attainment_before"])),
            attainment_after=Decimal(str(cycle.metrics["attainment_after"])),
            outcome=cycle.outcome or "",
        )

        result = attainment_delta([attainment])

        assert result.state is MetricState.COMPUTED
        assert result.value == Decimal("0.25")
        assert result.detail["comparable_cycles"] == 1
