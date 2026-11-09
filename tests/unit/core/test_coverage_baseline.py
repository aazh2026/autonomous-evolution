"""覆盖率基线的口径 —— **阈值按模块各自的基线定，不许靠降基线过关**。

设计依据：docs/tasks.md TASK-P1-042（T1 ① 修改某模块基线后，该模块的覆盖率
阈值同步变化，其他模块不受影响；T1 ② 尝试下调基线被拒绝并留下审计记录；
T1 ③ 阈值为 max(模块基线, 全局地板)，不小于全局地板；T2 阶段一周期结束时，
基线下调次数 = 0）。

**这一层只测口径，不测落库。** 落库是
:mod:`tests.integration.test_coverage_baseline_store` 的事。口径必须能被单独
证伪 —— 口径错了，存得再稳也是错的。

**每条断言都在盯一件"看起来差不多但含义相反"的事。** "没有基线"与"基线是
0"、"被拒"与"没试"、"这次测砸了"与"要降标准" —— 这些在报表上常常长得
一模一样，而这正是要守住的地方。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.task.coverage_baseline import (
    GLOBAL_COVERAGE_FLOOR,
    BaselineAuditSink,
    BaselineUpdateKind,
    BaselineUpdateOutcome,
    BaselineUpdateRequest,
    CoverageBaselineError,
    CoverageBaselineService,
    CoverageBaselineSet,
    CoverageBaselineStore,
    LoweringApproval,
    LoweringAttempt,
    ModuleBaseline,
)

#: 固定的时间 —— 审批要带时刻，测试里不该出现"现在"。
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class InMemoryStore:
    """一个内存里的基线存取口 —— **按项目分开**。

    **它必须按项目存**：所有模块共用一份基线的话，"这个项目的阈值"这件事
    就没有意义了，而那正是要测的东西。
    """

    def __init__(self) -> None:
        self._rows: dict[str, CoverageBaselineSet] = {}
        self.saved: list[CoverageBaselineSet] = []

    async def load(self, project_id: str) -> CoverageBaselineSet:
        return self._rows.get(project_id, CoverageBaselineSet(project_id=project_id))

    async def save(self, baselines: CoverageBaselineSet) -> CoverageBaselineSet:
        self._rows[baselines.project_id] = baselines
        self.saved.append(baselines)
        return baselines


class RecordingAuditSink:
    """一个把下调尝试记下来的审计口 —— **记下来的顺序也是证据**。"""

    def __init__(self, *, failing: bool = False) -> None:
        self.attempts: list[LoweringAttempt] = []
        self._failing = failing

    def record_lowering_attempt(self, attempt: LoweringAttempt) -> None:
        if self._failing:
            raise RuntimeError("审计写不进去")
        self.attempts.append(attempt)


def _service(
    store: InMemoryStore,
    audit: RecordingAuditSink,
) -> CoverageBaselineService:
    return CoverageBaselineService(store=store, audit=audit)


def _request(
    module: str,
    coverage: str,
    *,
    project_id: str = "proj-1",
    requested_by: str = "agent-1",
    approval: LoweringApproval | None = None,
) -> BaselineUpdateRequest:
    return BaselineUpdateRequest(
        project_id=project_id,
        module=module,
        coverage=Decimal(coverage),
        requested_by=requested_by,
        approval=approval,
    )


def _approval(
    module: str,
    target: str,
    *,
    project_id: str = "proj-1",
    approver: str = "human-1",
) -> LoweringApproval:
    return LoweringApproval(
        project_id=project_id,
        module=module,
        target_coverage=Decimal(target),
        approver=approver,
        approved_at=NOW,
    )


class TestTheThreshold:
    """T1 ③ —— **阈值 = max(模块基线, 全局地板)**。"""

    async def test_a_module_without_a_baseline_gets_the_floor(self) -> None:
        """没有基线不是"要求 0" —— 是"要求至少达到地板"。"""
        service = _service(InMemoryStore(), RecordingAuditSink())
        assert await service.threshold("proj-1", "tinylib.cli") == GLOBAL_COVERAGE_FLOOR

    async def test_a_higher_module_baseline_wins(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.93"))
        assert await service.threshold("proj-1", "tinylib.stats") == Decimal("0.930000")

    async def test_a_lower_module_baseline_cannot_go_below_the_floor(self) -> None:
        """模块基线压不过全局地板 —— 地板是底线。"""
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        # 先把某个模块的基线抬到地板之上，再看地板还在不在。
        await service.update(_request("tinylib.stats", "0.93"))
        store._rows["proj-1"] = CoverageBaselineSet(
            project_id="proj-1",
            modules={"tinylib.cli": ModuleBaseline(module="tinylib.cli", coverage=Decimal("0.10"))},
        )
        assert await service.threshold("proj-1", "tinylib.cli") == GLOBAL_COVERAGE_FLOOR

    async def test_the_threshold_is_never_below_the_floor(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        for module in ("a", "b", "c"):
            await service.threshold("proj-1", module)
        thresholds = await service.thresholds("proj-1")
        assert all(value >= GLOBAL_COVERAGE_FLOOR for value in thresholds.values())

    async def test_the_floor_can_be_raised(self) -> None:
        """地板往上调是收紧 —— 允许。"""
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        raised = await store.load("proj-1")
        await store.save(raised.with_floor(Decimal("0.90")))
        assert await service.threshold("proj-1", "anything") == Decimal("0.900000")

    async def test_the_floor_cannot_be_lowered(self) -> None:
        """地板往下调就是"把标准降下来换通过" —— 不允许。"""
        store = InMemoryStore()
        current = await store.load("proj-1")
        with pytest.raises(CoverageBaselineError):
            current.with_floor(Decimal("0.10"))

    async def test_the_floor_cannot_be_lowered_back(self) -> None:
        """**地板只往上调** —— 调到比全局地板高、但比现在的地板低也不行。

        这一条盯的是"往回调一档"：0.87 仍然高于全局地板 0.85，可是它比
        现在的地板 0.90 松 —— 往回调一档也是放松标准，不在权限之内。
        """
        store = InMemoryStore()
        current = await store.load("proj-1")
        raised = current.with_floor(Decimal("0.90"))
        with pytest.raises(CoverageBaselineError):
            raised.with_floor(Decimal("0.87"))

    async def test_a_floor_below_the_global_one_is_refused(self) -> None:
        """地板不能低于全局地板 —— 那也是往下调。"""
        with pytest.raises(CoverageBaselineError):
            CoverageBaselineSet(project_id="proj-1", floor=Decimal("0.10"))


class TestTheModuleIsolation:
    """T1 ① —— **改一个模块的基线，其他模块不受影响**。"""

    async def test_changing_one_module_moves_only_its_threshold(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))
        await service.update(_request("tinylib.cli", "0.91"))

        await service.update(_request("tinylib.stats", "0.95"))

        assert await service.threshold("proj-1", "tinylib.stats") == Decimal("0.950000")
        assert await service.threshold("proj-1", "tinylib.cli") == Decimal("0.910000")

    async def test_another_module_s_baseline_is_untouched(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))
        await service.update(_request("tinylib.cli", "0.91"))

        await service.update(_request("tinylib.stats", "0.95"))

        current = await store.load("proj-1")
        other = current.module_baseline("tinylib.cli")
        assert other is not None
        assert other.coverage == Decimal("0.910000")

    async def test_the_threshold_follows_the_baseline(self) -> None:
        """阈值不是另存的一份 —— 它是算出来的。"""
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))
        assert await service.threshold("proj-1", "tinylib.stats") == Decimal("0.900000")
        await service.update(_request("tinylib.stats", "0.96"))
        assert await service.threshold("proj-1", "tinylib.stats") == Decimal("0.960000")


class TestTheLowering:
    """T1 ② —— **下调要审批，被拒不留下审计记录**。"""

    async def test_a_lowering_without_approval_is_refused(self) -> None:
        store = InMemoryStore()
        audit = RecordingAuditSink()
        service = _service(store, audit)
        await service.update(_request("tinylib.stats", "0.90"))

        outcome = await service.update(_request("tinylib.stats", "0.80"))

        assert outcome.kind is BaselineUpdateKind.REFUSED
        assert outcome.refused

    async def test_a_refused_lowering_leaves_the_baseline_alone(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        await service.update(_request("tinylib.stats", "0.80"))

        current = await store.load("proj-1")
        baseline = current.module_baseline("tinylib.stats")
        assert baseline is not None
        assert baseline.coverage == Decimal("0.900000")

    async def test_a_refused_lowering_has_no_effective_value(self) -> None:
        """被拒时 ``effective`` 是 ``None`` —— 没有"落下去"这回事。"""
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        outcome = await service.update(_request("tinylib.stats", "0.80"))

        assert outcome.effective is None

    async def test_a_refused_lowering_is_audited(self) -> None:
        """T1 ② 的落点 —— 拒绝了也要留痕。"""
        store = InMemoryStore()
        audit = RecordingAuditSink()
        service = _service(store, audit)
        await service.update(_request("tinylib.stats", "0.90"))

        await service.update(_request("tinylib.stats", "0.80", requested_by="agent-9"))

        assert len(audit.attempts) == 1
        attempt = audit.attempts[0]
        assert attempt.module == "tinylib.stats"
        assert attempt.requested_by == "agent-9"
        assert attempt.approved is False

    async def test_the_audit_record_says_what_was_refused(self) -> None:
        """审计要能自己读懂 —— 从多少降到多少。"""
        store = InMemoryStore()
        audit = RecordingAuditSink()
        service = _service(store, audit)
        await service.update(_request("tinylib.stats", "0.90"))

        await service.update(_request("tinylib.stats", "0.80"))

        attempt = audit.attempts[0]
        assert attempt.current == Decimal("0.900000")
        assert attempt.proposed == Decimal("0.80")

    async def test_a_refused_lowering_does_not_count_as_a_lowering(self) -> None:
        """**被拒的不算下调** —— T2 看的是实际发生的下调。"""
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        await service.update(_request("tinylib.stats", "0.80"))

        current = await store.load("proj-1")
        assert current.lowering_count == 0

    async def test_an_approved_lowering_goes_through(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        outcome = await service.update(
            _request("tinylib.stats", "0.80", approval=_approval("tinylib.stats", "0.80"))
        )

        assert outcome.kind is BaselineUpdateKind.LOWERED
        assert outcome.effective == Decimal("0.800000")

    async def test_an_approved_lowering_is_counted(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        await service.update(
            _request("tinylib.stats", "0.80", approval=_approval("tinylib.stats", "0.80"))
        )

        current = await store.load("proj-1")
        assert current.lowering_count == 1

    async def test_an_approved_lowering_is_audited_too(self) -> None:
        """成功的也要记 —— 只记失败的话，账就不完整了。"""
        store = InMemoryStore()
        audit = RecordingAuditSink()
        service = _service(store, audit)
        await service.update(_request("tinylib.stats", "0.90"))

        await service.update(
            _request("tinylib.stats", "0.80", approval=_approval("tinylib.stats", "0.80"))
        )

        assert len(audit.attempts) == 1
        assert audit.attempts[0].approved is True

    async def test_an_approval_for_another_module_does_not_apply(self) -> None:
        """批的是"当时那一份" —— 拿 A 模块的批准去降 B 模块，不认。"""
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        outcome = await service.update(
            _request("tinylib.stats", "0.80", approval=_approval("tinylib.cli", "0.80"))
        )

        assert outcome.kind is BaselineUpdateKind.REFUSED

    async def test_an_approval_for_another_target_does_not_apply(self) -> None:
        """批的是"降到某个数" —— 批 0.80 不等于批 0.70。"""
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        outcome = await service.update(
            _request("tinylib.stats", "0.80", approval=_approval("tinylib.stats", "0.70"))
        )

        assert outcome.kind is BaselineUpdateKind.REFUSED

    async def test_an_approval_for_another_project_does_not_apply(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        outcome = await service.update(
            _request(
                "tinylib.stats",
                "0.80",
                approval=_approval("tinylib.stats", "0.80", project_id="proj-2"),
            )
        )

        assert outcome.kind is BaselineUpdateKind.REFUSED

    async def test_a_raising_update_needs_no_approval(self) -> None:
        """往上调是收紧 —— 不需要谁批。"""
        store = InMemoryStore()
        audit = RecordingAuditSink()
        service = _service(store, audit)
        await service.update(_request("tinylib.stats", "0.90"))

        outcome = await service.update(_request("tinylib.stats", "0.95"))

        assert outcome.kind is BaselineUpdateKind.RAISED
        assert audit.attempts == []

    async def test_a_first_baseline_is_not_a_lowering(self) -> None:
        """新模块没有"原来是多少" —— 建基线不需要审批。"""
        store = InMemoryStore()
        audit = RecordingAuditSink()
        service = _service(store, audit)

        outcome = await service.update(_request("tinylib.new", "0.10"))

        assert outcome.kind is BaselineUpdateKind.RAISED
        assert outcome.previous is None
        assert audit.attempts == []

    async def test_the_same_coverage_changes_nothing(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        outcome = await service.update(_request("tinylib.stats", "0.90"))

        assert outcome.kind is BaselineUpdateKind.UNCHANGED


class TestTheFailClosedAudit:
    """**审计写不下来 = 这件事没发生过。**"""

    async def test_a_failing_audit_blocks_the_lowering(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink(failing=True))
        await service.update(_request("tinylib.stats", "0.90"))

        with pytest.raises(RuntimeError):
            await service.update(
                _request("tinylib.stats", "0.80", approval=_approval("tinylib.stats", "0.80"))
            )

    async def test_a_failing_audit_leaves_the_baseline_alone(self) -> None:
        """顺序是"先写审计、后改基线" —— 审计失败，基线不能动。"""
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink(failing=True))
        await service.update(_request("tinylib.stats", "0.90"))

        with pytest.raises(RuntimeError):
            await service.update(
                _request("tinylib.stats", "0.80", approval=_approval("tinylib.stats", "0.80"))
            )

        current = await store.load("proj-1")
        baseline = current.module_baseline("tinylib.stats")
        assert baseline is not None
        assert baseline.coverage == Decimal("0.900000")

    async def test_a_failing_audit_does_not_count_a_lowering(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink(failing=True))
        await service.update(_request("tinylib.stats", "0.90"))

        with pytest.raises(RuntimeError):
            await service.update(
                _request("tinylib.stats", "0.80", approval=_approval("tinylib.stats", "0.80"))
            )

        current = await store.load("proj-1")
        assert current.lowering_count == 0


class TestTheSync:
    """随代码演进的那条路 —— **只往上调**。"""

    async def test_a_measurement_above_the_baseline_raises_it(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))

        outcomes = await service.sync_with_measurements(
            project_id="proj-1",
            coverage_by_module={"tinylib.stats": Decimal("0.95")},
            requested_by="agent-1",
        )

        assert outcomes[0].kind is BaselineUpdateKind.RAISED
        assert await service.threshold("proj-1", "tinylib.stats") == Decimal("0.950000")

    async def test_a_measurement_below_the_baseline_does_not_lower_it(self) -> None:
        """测砸了不等于标准可以降 —— 基线是"已经做到过的高度"。"""
        store = InMemoryStore()
        audit = RecordingAuditSink()
        service = _service(store, audit)
        await service.update(_request("tinylib.stats", "0.90"))

        outcomes = await service.sync_with_measurements(
            project_id="proj-1",
            coverage_by_module={"tinylib.stats": Decimal("0.70")},
            requested_by="agent-1",
        )

        assert outcomes[0].kind is BaselineUpdateKind.UNCHANGED
        assert await service.threshold("proj-1", "tinylib.stats") == Decimal("0.900000")

    async def test_a_low_measurement_is_not_an_audit_noise(self) -> None:
        """每次"没达标"都当成下调请求，审计表会被自己的噪声淹掉。"""
        store = InMemoryStore()
        audit = RecordingAuditSink()
        service = _service(store, audit)
        await service.update(_request("tinylib.stats", "0.90"))

        await service.sync_with_measurements(
            project_id="proj-1",
            coverage_by_module={"tinylib.stats": Decimal("0.70")},
            requested_by="agent-1",
        )

        assert audit.attempts == []

    async def test_a_new_module_gets_a_baseline_from_the_measurement(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())

        outcomes = await service.sync_with_measurements(
            project_id="proj-1",
            coverage_by_module={"tinylib.new": Decimal("0.95")},
            requested_by="agent-1",
        )

        assert outcomes[0].kind is BaselineUpdateKind.RAISED
        assert await service.threshold("proj-1", "tinylib.new") == Decimal("0.950000")

    async def test_a_measurement_out_of_range_is_refused(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())

        with pytest.raises(CoverageBaselineError):
            await service.sync_with_measurements(
                project_id="proj-1",
                coverage_by_module={"tinylib.stats": Decimal("1.5")},
                requested_by="agent-1",
            )

    async def test_the_sync_leaves_the_other_modules_alone(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.cli", "0.92"))

        await service.sync_with_measurements(
            project_id="proj-1",
            coverage_by_module={"tinylib.stats": Decimal("0.95")},
            requested_by="agent-1",
        )

        assert await service.threshold("proj-1", "tinylib.cli") == Decimal("0.920000")


class TestTheT2Count:
    """T2 —— **阶段一周期结束时，基线下调次数 = 0**。"""

    async def test_a_cycle_without_lowering_leaves_the_count_at_zero(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))
        await service.sync_with_measurements(
            project_id="proj-1",
            coverage_by_module={"tinylib.stats": Decimal("0.95")},
            requested_by="agent-1",
        )

        current = await store.load("proj-1")
        assert current.lowering_count == 0

    async def test_a_refused_lowering_leaves_the_count_at_zero(self) -> None:
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))
        await service.update(_request("tinylib.stats", "0.80"))

        current = await store.load("proj-1")
        assert current.lowering_count == 0

    async def test_the_count_is_per_project(self) -> None:
        """一个项目下调过，不该让另一个项目的计数也变。"""
        store = InMemoryStore()
        service = _service(store, RecordingAuditSink())
        await service.update(_request("tinylib.stats", "0.90"))
        await service.update(
            _request("tinylib.stats", "0.80", approval=_approval("tinylib.stats", "0.80"))
        )

        other = await store.load("proj-2")
        assert other.lowering_count == 0


class TestThePorts:
    """端口的实现得对得上 —— **协议是运行时可判的**。"""

    def test_the_in_memory_store_satisfies_the_store_port(self) -> None:
        assert isinstance(InMemoryStore(), CoverageBaselineStore)

    def test_the_recording_sink_satisfies_the_audit_port(self) -> None:
        assert isinstance(RecordingAuditSink(), BaselineAuditSink)

    def test_the_service_satisfies_nothing_specific(self) -> None:
        """服务本身不是端口 —— 它是被用的东西。"""
        assert not isinstance(
            _service(InMemoryStore(), RecordingAuditSink()), CoverageBaselineStore
        )


class TestTheDocument:
    """落盘与读回 —— **读回来要一样**。"""

    def test_the_document_round_trip_is_equal(self) -> None:
        original = CoverageBaselineSet(
            project_id="proj-1",
            floor=Decimal("0.90"),
            modules={
                "tinylib.stats": ModuleBaseline(
                    module="tinylib.stats",
                    coverage=Decimal("0.912345"),
                    sample_count=3,
                    source="pytest:3",
                )
            },
            lowering_count=1,
        )
        restored = CoverageBaselineSet.from_document(original.to_document())
        assert restored.to_document() == original.to_document()

    def test_the_coverage_is_written_as_a_string(self) -> None:
        """YAML/JSON 里裸写的 0.1 是浮点数 —— 覆盖率差最后几位就差判。"""
        original = CoverageBaselineSet(
            project_id="proj-1",
            modules={
                "tinylib.stats": ModuleBaseline(
                    module="tinylib.stats", coverage=Decimal("0.912345")
                )
            },
        )
        assert "0.912345" in str(original.to_document())

    def test_an_unknown_key_is_refused(self) -> None:
        """少写一个字段会让那一列静默消失。"""
        with pytest.raises(CoverageBaselineError):
            CoverageBaselineSet.from_document(
                {
                    "project_id": "proj-1",
                    "floor": "0.85",
                    "lowering_count": 0,
                    "modules": {},
                    "unexpected": 1,
                }
            )

    def test_a_module_key_must_match_the_module_name(self) -> None:
        """键与名字对不上 —— 那是两份数据。"""
        with pytest.raises(CoverageBaselineError):
            CoverageBaselineSet.from_document(
                {
                    "project_id": "proj-1",
                    "floor": "0.85",
                    "lowering_count": 0,
                    "modules": {
                        "tinylib.stats": {
                            "module": "tinylib.cli",
                            "coverage": "0.9",
                            "sample_count": 1,
                            "source": "",
                        }
                    },
                }
            )


class TestTheValidation:
    """入口处的校验 —— **坏数据要当场暴露**。"""

    def test_a_blank_module_is_refused(self) -> None:
        with pytest.raises(CoverageBaselineError):
            ModuleBaseline(module="  ", coverage=Decimal("0.9"))

    def test_a_coverage_above_one_is_refused(self) -> None:
        with pytest.raises(CoverageBaselineError):
            ModuleBaseline(module="a", coverage=Decimal("1.5"))

    def test_a_negative_sample_count_is_refused(self) -> None:
        with pytest.raises(CoverageBaselineError):
            ModuleBaseline(module="a", coverage=Decimal("0.9"), sample_count=-1)

    def test_a_blank_approver_is_refused(self) -> None:
        with pytest.raises(CoverageBaselineError):
            LoweringApproval(
                project_id="proj-1",
                module="a",
                target_coverage=Decimal("0.8"),
                approver="  ",
                approved_at=NOW,
            )

    def test_a_blank_requester_is_refused(self) -> None:
        with pytest.raises(CoverageBaselineError):
            _request("a", "0.9", requested_by="  ")

    def test_the_outcome_says_whether_the_baseline_moved(self) -> None:
        raised = BaselineUpdateOutcome(
            kind=BaselineUpdateKind.RAISED,
            project_id="proj-1",
            module="a",
            previous=Decimal("0.8"),
            proposed=Decimal("0.9"),
            effective=Decimal("0.9"),
        )
        refused = BaselineUpdateOutcome(
            kind=BaselineUpdateKind.REFUSED,
            project_id="proj-1",
            module="a",
            previous=Decimal("0.9"),
            proposed=Decimal("0.8"),
            effective=None,
        )
        assert raised.applied
        assert not refused.applied
        assert refused.refused
