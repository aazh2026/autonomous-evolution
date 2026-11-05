"""TASK-P1-070 —— 演进周期记录与结局判定。

验证（T1 ②）：``outcome`` 取值 ∈ {improved, no_change, regressed, stalled}，
且四种结局**都表达得出来** —— 不是只有 improved 可达。

验证（T1 ③）：周期记录包含前后两次达成度，可算出差值。

这一层是纯逻辑，不需要数据库。落库那一侧的往返由
``tests/integration/test_cycle_recording.py`` 负责。

**为什么结局要分成四个而不是两个。** 只留 improved / regressed 时，
"这轮啥也没干成"就只能被硬塞进其中一个 —— 记录于是开始撒谎，而撒谎的方向
恰好是"看起来系统很能干"。四个取值各自对应一种**真实发生过**的情况：

- improved：达成度上升了；
- regressed：达成度下降了；
- no_change：跑完了，但**没有可验证的**改善；
- stalled：这一轮什么也没落地。

"不可判定"必须落在 no_change 而不是 improved：缺前值时算不出差值，
说成"变好了"是编的。TASK-P1-014 立下的那条规矩在这里同样成立 ——
**不可判定 ≠ 未触发**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.entities.evolution import EvolutionCycle as EvolutionCycleEntity
from core.metrics.cycle import (
    CycleCounts,
    CycleNotClosedError,
    CycleOutcome,
    CycleRecordError,
    CycleStatus,
    EvolutionCycle,
    classify_cycle,
)

_START = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
_END = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)


def _counts(
    *, candidates: int = 3, decisions: int = 2, tasks: int = 2, completed: int = 2
) -> CycleCounts:
    """一份"这一轮干了些事"的计数。"""
    return CycleCounts(
        candidates=candidates,
        decisions=decisions,
        tasks=tasks,
        completed_tasks=completed,
    )


def _record(**overrides: object) -> EvolutionCycle:
    """一份默认"变好了"的周期记录。"""
    kwargs: dict[str, object] = {
        "project_id": "p-1",
        "cycle_id": "cycle-0001",
        "trigger": "schedule",
        "started_at": _START,
        "ended_at": _END,
        "status": CycleStatus.COMPLETED,
        "counts": _counts(),
        "attainment_before": Decimal("0.40"),
        "attainment_after": Decimal("0.65"),
    }
    kwargs.update(overrides)
    return EvolutionCycle.build(**kwargs)  # type: ignore[arg-type]


class TestOutcomeVocabulary:
    """T1 ② —— 四个结局取值都表达得出来。"""

    def test_improved_when_attainment_rises(self) -> None:
        verdict = classify_cycle(
            CycleStatus.COMPLETED,
            _counts(),
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
        )

        assert verdict.outcome is CycleOutcome.IMPROVED

    def test_regressed_when_attainment_falls(self) -> None:
        verdict = classify_cycle(
            CycleStatus.COMPLETED,
            _counts(),
            attainment_before=Decimal("0.80"),
            attainment_after=Decimal("0.55"),
        )

        assert verdict.outcome is CycleOutcome.REGRESSED

    def test_no_change_when_attainment_stays(self) -> None:
        verdict = classify_cycle(
            CycleStatus.COMPLETED,
            _counts(),
            attainment_before=Decimal("0.60"),
            attainment_after=Decimal("0.60"),
        )

        assert verdict.outcome is CycleOutcome.NO_CHANGE

    def test_stalled_when_nothing_was_completed(self) -> None:
        """跑了但一个任务都没落地 —— 这一轮没有产出。"""
        verdict = classify_cycle(
            CycleStatus.COMPLETED,
            _counts(tasks=2, completed=0),
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.90"),
        )

        assert verdict.outcome is CycleOutcome.STALLED

    @pytest.mark.parametrize(
        "status",
        [CycleStatus.ABORTED, CycleStatus.BUDGET_EXHAUSTED, CycleStatus.BLOCKED],
    )
    def test_stalled_when_the_cycle_did_not_run_to_completion(self, status: CycleStatus) -> None:
        """没跑完的周期不能报"有改善"。

        哪怕达成度数字上是上升的 —— 一轮被预算掐断的周期，那个上升多半来自
        周期之外的东西，不该记在这一轮的账上。
        """
        verdict = classify_cycle(
            status,
            _counts(),
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.90"),
        )

        assert verdict.outcome is CycleOutcome.STALLED

    def test_all_four_outcomes_are_reachable(self) -> None:
        """四种结局都真的可达 —— 不是纸面上的枚举。"""
        reached = {
            classify_cycle(
                CycleStatus.COMPLETED,
                _counts(),
                attainment_before=Decimal("0.40"),
                attainment_after=Decimal("0.65"),
            ).outcome,
            classify_cycle(
                CycleStatus.COMPLETED,
                _counts(),
                attainment_before=Decimal("0.40"),
                attainment_after=Decimal("0.20"),
            ).outcome,
            classify_cycle(
                CycleStatus.COMPLETED,
                _counts(),
                attainment_before=Decimal("0.40"),
                attainment_after=Decimal("0.40"),
            ).outcome,
            classify_cycle(
                CycleStatus.COMPLETED,
                _counts(completed=0),
                attainment_before=Decimal("0.40"),
                attainment_after=Decimal("0.65"),
            ).outcome,
        }

        assert reached == set(CycleOutcome)


class TestUndecidableIsNotNoImprovement:
    """缺基线时不能编出"变好了"，也不能编出"没变"。

    这是 TASK-P1-014 那条规矩（**不可判定 ≠ 未触发**）在周期判定上的对应
    形态：算不出差值时，唯一诚实的说法是"没有可验证的改善"。
    """

    def test_missing_baseline_is_no_change_not_improved(self) -> None:
        verdict = classify_cycle(
            CycleStatus.COMPLETED,
            _counts(),
            attainment_before=None,
            attainment_after=Decimal("0.65"),
        )

        assert verdict.outcome is CycleOutcome.NO_CHANGE

    def test_missing_after_value_is_no_change(self) -> None:
        verdict = classify_cycle(
            CycleStatus.COMPLETED,
            _counts(),
            attainment_before=Decimal("0.40"),
            attainment_after=None,
        )

        assert verdict.outcome is CycleOutcome.NO_CHANGE

    def test_missing_baseline_keeps_the_record_honest_about_it(self) -> None:
        """缺基线的记录里，前值仍然留空 —— 读的人能看出这条缺基线。"""
        record = _record(attainment_before=None)

        assert record.attainment_before is None
        assert record.attainment_delta is None


class TestVerdictReason:
    """判定要带理由 —— 一个只有结论的记录没法复核。"""

    def test_reason_is_never_empty(self) -> None:
        verdict = classify_cycle(
            CycleStatus.COMPLETED,
            _counts(),
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
        )

        assert verdict.reason.strip()

    def test_reason_states_the_delta(self) -> None:
        verdict = classify_cycle(
            CycleStatus.COMPLETED,
            _counts(),
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
        )

        assert "0.25" in verdict.reason

    def test_reason_says_which_gate_stopped_it(self) -> None:
        """没跑完的周期，理由要指得出是哪个状态卡住的。"""
        verdict = classify_cycle(
            CycleStatus.BUDGET_EXHAUSTED,
            _counts(),
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
        )

        assert "budget_exhausted" in verdict.reason


class TestBuildDerivesTheOutcome:
    """结局由判定得出，不接受外部指定。"""

    def test_build_agrees_with_the_classifier(self) -> None:
        record = _record()

        expected = classify_cycle(
            CycleStatus.COMPLETED,
            _counts(),
            attainment_before=Decimal("0.40"),
            attainment_after=Decimal("0.65"),
        )
        assert record.outcome is expected.outcome
        assert record.outcome_reason == expected.reason

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            (
                {"attainment_before": Decimal("0.40"), "attainment_after": Decimal("0.20")},
                CycleOutcome.REGRESSED,
            ),
            (
                {"attainment_before": Decimal("0.40"), "attainment_after": Decimal("0.40")},
                CycleOutcome.NO_CHANGE,
            ),
            (
                {"attainment_before": None, "attainment_after": Decimal("0.90")},
                CycleOutcome.NO_CHANGE,
            ),
            (
                {"counts": CycleCounts(candidates=3, decisions=2, tasks=2, completed_tasks=0)},
                CycleOutcome.STALLED,
            ),
            (
                {"status": CycleStatus.BUDGET_EXHAUSTED},
                CycleOutcome.STALLED,
            ),
        ],
        ids=["regressed", "no_change", "no_baseline", "nothing_completed", "not_completed"],
    )
    def test_build_derives_the_outcome_from_the_evidence(
        self, overrides: dict[str, object], expected: CycleOutcome
    ) -> None:
        """``build`` 是唯一的构造入口 —— 它必须**每一次**都照证据判定。

        只断言"能得出 improved"是不够的：一个把结局写死成 improved 的
        ``build``，在所有正向用例上都会通过，而它恰好是最危险的那种实现 ——
        它让每一轮记录都好看。
        """
        record = _record(**overrides)

        assert record.outcome is expected

    def test_build_has_no_outcome_parameter(self) -> None:
        """构造函数里根本没有"结局"这个入参 —— 传不进来。

        要是哪天加上了，这条测试就会红：那时"某人声称的结果"就能冒充
        "判定结果"了。
        """
        assert "outcome" not in EvolutionCycle.build.__code__.co_varnames
        assert "outcome_reason" not in EvolutionCycle.build.__code__.co_varnames

    def test_build_still_accepts_every_field_explicitly(self) -> None:
        """反过来也要成立：结局之外的字段都得能填。

        少传一个达成度快照，恰好是把"这轮到底变没变好"的唯一依据弄丢 ——
        所以字段必须逐个列全，而不是收成一个 dict。
        """
        parameters = EvolutionCycle.build.__code__.co_varnames

        for field in (
            "project_id",
            "cycle_id",
            "trigger",
            "started_at",
            "ended_at",
            "status",
            "counts",
            "attainment_before",
            "attainment_after",
            "cost",
            "currency",
            "evidence_refs",
        ):
            assert field in parameters


class TestAttainmentDelta:
    """T1 ③ —— 记录带前后两次达成度，能算出差值。"""

    def test_record_carries_both_snapshots(self) -> None:
        record = _record()

        assert record.attainment_before == Decimal("0.40")
        assert record.attainment_after == Decimal("0.65")

    def test_delta_is_after_minus_before(self) -> None:
        record = _record()

        assert record.attainment_delta == Decimal("0.25")

    def test_delta_is_zero_when_unchanged(self) -> None:
        record = _record(attainment_after=Decimal("0.40"))

        assert record.attainment_delta == Decimal("0")

    def test_delta_is_negative_when_it_regressed(self) -> None:
        record = _record(attainment_after=Decimal("0.25"))

        assert record.attainment_delta == Decimal("-0.15")

    def test_delta_is_none_when_a_snapshot_is_missing(self) -> None:
        """缺数据不是"差值为 0"。"""
        assert _record(attainment_before=None).attainment_delta is None
        assert _record(attainment_after=None).attainment_delta is None

    def test_delta_is_computed_from_decimals_not_floats(self) -> None:
        """差值必须精确到分。

        浮点数会把"0.1 + 0.2"算成 0.30000000000000004；达成度差值算错
        一点点，就可能把"没变"判成"变好了"。
        """
        record = _record(attainment_before=Decimal("0.1"), attainment_after=Decimal("0.3"))

        assert record.attainment_delta == Decimal("0.2")
        assert record.attainment_delta != Decimal("0.30000000000000004")


class TestRecordValidation:
    """记录本身的校验 —— 不合法的记录当场拒绝。"""

    @pytest.mark.parametrize("value", ["1.5", "-0.1", "2"])
    def test_attainment_out_of_range_is_rejected(self, value: str) -> None:
        with pytest.raises(ValueError):
            _record(attainment_after=Decimal(value))

    def test_ended_before_started_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _record(started_at=_END, ended_at=_START)

    def test_cost_without_currency_is_rejected(self) -> None:
        """有花费就必须有单位 —— 没有单位的数字不代表任何东西。"""
        with pytest.raises(ValueError):
            _record(cost=Decimal("1.5"), currency="")

    def test_blank_evidence_ref_is_rejected(self) -> None:
        """空引用指不到任何东西。"""
        with pytest.raises(ValueError):
            _record(evidence_refs=("artifact://ok", "   "))

    def test_completed_tasks_cannot_exceed_total_tasks(self) -> None:
        with pytest.raises(ValueError):
            CycleCounts(candidates=1, decisions=1, tasks=1, completed_tasks=2)

    def test_record_is_frozen(self) -> None:
        """记录一旦成立就不能改 —— 它是审计凭据，不是工作草稿。"""
        record = _record()

        with pytest.raises(ValueError):
            record.outcome = CycleOutcome.STALLED

    def test_extra_fields_are_rejected(self) -> None:
        """多出来的字段说明调用方以为有某个字段存在 —— 那是误解，不是宽容。"""
        with pytest.raises(ValueError):
            EvolutionCycle.model_validate(
                {
                    **_record().model_dump(),
                    "unexpected": "x",
                }
            )


class TestEntityConversion:
    """领域模型 ↔ 持久化实体 的转换层。

    这一层此前完全没有测试覆盖，而它的失败模式全是"悄悄变差"的：
    字段搬运漏了不报错，只是结果不对；十进制数被当成浮点数存，不报错，
    只是差值算错一点点。
    """

    def test_roundtrip_preserves_everything(self) -> None:
        record = _record(
            cost=Decimal("3.25"),
            currency="USD",
            evidence_refs=("artifact://report-1", "issue:42"),
        )

        restored = EvolutionCycleEntity.from_domain(record).to_domain()

        assert restored == record

    def test_roundtrip_preserves_exact_decimals(self) -> None:
        """十进制往返不能掉精度。"""
        record = _record(
            attainment_before=Decimal("0.1"),
            attainment_after=Decimal("0.3"),
            cost=Decimal("1.10"),
            currency="USD",
        )

        restored = EvolutionCycleEntity.from_domain(record).to_domain()

        assert restored.attainment_before == Decimal("0.1")
        assert restored.attainment_after == Decimal("0.3")
        assert restored.cost == Decimal("1.10")
        assert restored.attainment_delta == Decimal("0.2")

    def test_decimals_are_stored_as_strings_not_floats(self) -> None:
        """JSON 里没有十进制类型 —— 存成浮点数就丢精度了。"""
        entity = EvolutionCycleEntity.from_domain(_record())

        stored = entity.metrics["attainment_before"]
        assert isinstance(stored, str)

    def test_open_cycle_cannot_be_restored(self) -> None:
        """进行中的周期没有结局 —— 还原成完整记录等于凭空造结论。"""
        entity = EvolutionCycleEntity.from_domain(_record())
        entity.ended_at = None
        entity.outcome = None
        entity.status = None

        with pytest.raises(CycleNotClosedError):
            entity.to_domain()

    def test_missing_counts_is_rejected_rather_than_defaulted_to_zero(self) -> None:
        """缺计数时不能还原出一份"什么都没做"的记录。

        全 0 的计数是一个**断言**，而断言必须有依据。
        """
        entity = EvolutionCycleEntity.from_domain(_record())
        metrics = dict(entity.metrics)
        del metrics["counts"]
        entity.metrics = metrics

        with pytest.raises(CycleRecordError):
            entity.to_domain()

    def test_malformed_evidence_refs_are_rejected(self) -> None:
        """字符串当序列拆开，会让记录"看起来有证据"。"""
        entity = EvolutionCycleEntity.from_domain(_record())
        entity.metrics = {**entity.metrics, "evidence_refs": "artifact://report-1"}

        with pytest.raises(CycleRecordError):
            entity.to_domain()

    def test_non_string_evidence_refs_are_rejected(self) -> None:
        entity = EvolutionCycleEntity.from_domain(_record())
        entity.metrics = {**entity.metrics, "evidence_refs": [1, 2]}

        with pytest.raises(CycleRecordError):
            entity.to_domain()


class TestDuration:
    def test_duration_is_the_wall_clock_span(self) -> None:
        record = _record(started_at=_START, ended_at=_START + timedelta(minutes=90))

        assert record.duration_seconds == 5400.0
