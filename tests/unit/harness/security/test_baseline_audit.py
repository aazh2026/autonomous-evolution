"""基线下调的审计留痕 —— **用的是审计那本册子，不另起一本**。

设计依据：docs/tasks.md TASK-P1-042（T1 ②"尝试下调基线被拒绝并留下审计
记录"）、TASK-P1-045（R-062 / R-063 的四要素）、docs/design.md §3.5D。

**这里盯的是"翻译"对不对。** 领域层给的是"一次下调尝试"，审计册子认识的是
"谁、在哪个任务上、对什么做了什么、结果如何"。翻译丢了要素，被拒的尝试就
会变成一条读不懂的记录 —— 而"读不懂"等于"没人会去看"。

**四要素缺一不可。** 主体是提下调的人，任务是这次尝试发生在哪个任务上，
资源是那个模块的基线，结果与判定分开放 —— **被拒是"没做成"，不是"做砸了"**。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.task.coverage_baseline import LoweringAttempt
from harness.security.audit import (
    NOT_APPLICABLE,
    AuditDecision,
    AuditEvent,
    AuditLog,
    AuditQuery,
    AuditResult,
    AuditStore,
    AuditWriteError,
    InMemoryAuditStore,
)
from harness.security.baseline_audit import (
    LOWER_BASELINE_OPERATION,
    RESOURCE_PREFIX,
    CoverageBaselineAuditSink,
)

NOW = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


class FailingStore(InMemoryAuditStore):
    """一个写不进去的审计存储。"""

    def append(self, _event: AuditEvent) -> None:
        """写不进去 —— 名字带下划线是因为它确实用不上。"""
        raise OSError("磁盘满了")


def _sink(store: AuditStore | None = None) -> tuple[CoverageBaselineAuditSink, InMemoryAuditStore]:
    memory = store if isinstance(store, InMemoryAuditStore) else InMemoryAuditStore()
    log = AuditLog(
        memory,
        clock=lambda: NOW,
        id_factory=lambda: "event-1",
    )
    return CoverageBaselineAuditSink(log), memory


def _events(memory: InMemoryAuditStore) -> tuple[AuditEvent, ...]:
    """取审计册子里的记录 —— **走公开的查询口**。

    直接摸 ``_events`` 的话，测试就会跟着存储的实现走；查询口才是稳定的
    契约。
    """
    return memory.query(AuditQuery(operation=LOWER_BASELINE_OPERATION))


def _attempt(
    *,
    approved: bool = False,
    task_id: str = "task-1",
    current: Decimal | None = Decimal("0.900000"),
) -> LoweringAttempt:
    return LoweringAttempt(
        project_id="proj-1",
        module="tinylib.stats",
        current=current,
        proposed=Decimal("0.80"),
        requested_by="agent-1",
        task_id=task_id,
        approved=approved,
        reason="没有对应的批准",
    )


class TestTheFourElements:
    """R-062 / R-063 —— **四要素一个都不能少**。"""

    def test_the_subject_is_the_requester(self) -> None:
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt())
        event = _events(memory)[0]
        assert event.agent_id == "agent-1"

    def test_the_task_is_the_task_it_happened_on(self) -> None:
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt(task_id="task-42"))
        event = _events(memory)[0]
        assert event.task_id == "task-42"

    def test_a_missing_task_uses_a_placeholder_not_an_empty_string(self) -> None:
        """**空串会让读的人以为"漏填了"** —— 占位值说清的是"不适用"。"""
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt(task_id=""))
        event = _events(memory)[0]
        assert event.task_id == NOT_APPLICABLE
        assert event.task_id != ""

    def test_the_resource_is_the_module_baseline(self) -> None:
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt())
        event = _events(memory)[0]
        assert event.resource == f"{RESOURCE_PREFIX}:tinylib.stats"

    def test_the_operation_names_the_lowering(self) -> None:
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt())
        event = _events(memory)[0]
        assert event.operation == LOWER_BASELINE_OPERATION

    def test_the_project_is_recorded(self) -> None:
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt())
        event = _events(memory)[0]
        assert event.project_id == "proj-1"

    def test_the_workspace_is_a_placeholder(self) -> None:
        """基线不是工作空间里的东西 —— 用占位值，不留空。"""
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt())
        event = _events(memory)[0]
        assert event.workspace_id == NOT_APPLICABLE


class TestTheVerdict:
    """**被拒是"没做成"，不是"做砸了"。**"""

    def test_a_refused_attempt_is_recorded_as_denied(self) -> None:
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt(approved=False))
        event = _events(memory)[0]
        assert event.result is AuditResult.DENIED
        assert event.decision is AuditDecision.DENY

    def test_an_approved_attempt_is_recorded_as_allowed(self) -> None:
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt(approved=True))
        event = _events(memory)[0]
        assert event.result is AuditResult.SUCCESS
        assert event.decision is AuditDecision.ALLOW

    def test_a_refused_attempt_is_still_recorded(self) -> None:
        """拒绝了也要记 —— **"为什么没做"往往正是后来要复盘的东西**。"""
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt(approved=False))
        assert len(_events(memory)) == 1

    def test_the_reason_names_the_module_and_the_numbers(self) -> None:
        """**这条记录要能自己读懂** —— 从多少降到多少。"""
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt())
        event = _events(memory)[0]
        assert "tinylib.stats" in event.reason
        assert "0.900000" in event.reason
        assert "0.80" in event.reason

    def test_the_reason_says_which_way_it_went(self) -> None:
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt(approved=False))
        refused = _events(memory)[0].reason
        sink.record_lowering_attempt(_attempt(approved=True))
        allowed = _events(memory)[1].reason
        assert "不予放行" in refused
        assert "放行" in allowed

    def test_a_missing_previous_baseline_is_spelled_out(self) -> None:
        """新模块没有"原来是多少" —— 但那也得说清楚，不能只写个空。"""
        sink, memory = _sink()
        sink.record_lowering_attempt(_attempt(current=None))
        event = _events(memory)[0]
        assert "没有基线" in event.reason


class TestTheFailClosed:
    """**审计写不下来 = 这件事没发生过。**"""

    def test_a_failing_store_makes_the_sink_raise(self) -> None:
        sink, _ = _sink(FailingStore())
        with pytest.raises(AuditWriteError):
            sink.record_lowering_attempt(_attempt())

    def test_the_error_is_not_swallowed(self) -> None:
        """吞掉异常等于告诉调用方"记下来了"，而实际上没有。"""
        sink, _ = _sink(FailingStore())
        with pytest.raises(AuditWriteError):
            sink.record_lowering_attempt(_attempt(approved=True))
