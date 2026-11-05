"""TASK-P1-071 —— 自主发现层指标。

验证（T1）：有效问题发现率、重复发现率各有**已知输入的期望值断言**。

两条规矩贯穿本文件：

1. **分母只算已标注的。** 未标注的候选既不能算对也不能算错 —— 把它们塞进
   分母，指标就随标注进度漂移：一个刚标了 3 个候选的系统，"有效率"是 1/3，
   与它实际的水准毫无关系。
2. **算不出来不是 0。** 没有标注时报"基线未建立"，不报 0 —— 报 0 会让看板
   显示"有效率 0%"，读的人会以为系统一个真问题都找不到。
"""

from __future__ import annotations

from decimal import Decimal

from evaluation.common import MetricState, Verdict
from evaluation.evolution_evaluation.discovery import (
    Discovery,
    duplicate_discovery_rate,
    valid_discovery_rate,
)


def _discovery(
    candidate_id: str,
    *,
    label: Verdict | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> Discovery:
    return Discovery(
        candidate_id=candidate_id,
        cycle_id="cycle-0001",
        title=f"候选 {candidate_id}",
        evidence_refs=evidence_refs,
        label=label,
    )


class TestValidDiscoveryRate:
    def test_a_known_set_of_labels_gives_a_known_rate(self) -> None:
        discoveries = [
            _discovery("C-1", label=Verdict.VALID),
            _discovery("C-2", label=Verdict.VALID),
            _discovery("C-3", label=Verdict.INVALID),
            _discovery("C-4", label=Verdict.VALID),
        ]

        result = valid_discovery_rate(discoveries)

        assert result.metric_id == "valid_discovery_rate"
        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal(3)
        assert result.denominator == Decimal(4)
        assert result.value == Decimal("0.75")
        assert result.is_computed is True

    def test_unlabelled_candidates_stay_out_of_the_denominator(self) -> None:
        """未标注的既不能算对也不能算错 —— 它们不进分母。"""
        discoveries = [
            _discovery("C-1", label=Verdict.VALID),
            _discovery("C-2", label=Verdict.INVALID),
            _discovery("C-3"),
            _discovery("C-4"),
            _discovery("C-5"),
        ]

        result = valid_discovery_rate(discoveries)

        assert result.numerator == Decimal(1)
        assert result.denominator == Decimal(2)
        assert result.detail["total_candidates"] == 5
        assert result.detail["labelled_candidates"] == 2

    def test_without_any_label_the_rate_is_uncomputable_not_zero(self) -> None:
        discoveries = [
            _discovery("C-1"),
            _discovery("C-2"),
        ]

        result = valid_discovery_rate(discoveries)

        assert result.state is MetricState.BASELINE_MISSING
        assert result.value is None
        assert result.is_computed is False
        assert result.detail["labelled_candidates"] == 0

    def test_with_no_candidates_at_all_there_are_no_samples(self) -> None:
        """连候选都没有，与"有候选但没标注"是两件不同的事。"""
        result = valid_discovery_rate([])

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None
        assert result.detail["total_candidates"] == 0

    def test_a_rate_of_zero_is_a_computed_result(self) -> None:
        """全标为无效 —— 这是"试过了，确实是零"，与"没标"必须区分。"""
        discoveries = [
            _discovery("C-1", label=Verdict.INVALID),
            _discovery("C-2", label=Verdict.INVALID),
        ]

        result = valid_discovery_rate(discoveries)

        assert result.state is MetricState.COMPUTED
        assert result.value == Decimal(0)
        assert result.is_computed is True


class TestDuplicateDiscoveryRate:
    def test_a_known_set_of_evidence_refs_gives_a_known_rate(self) -> None:
        """候选 C-3 与 C-1 共享证据，C-4 与 C-2 共享证据。"""
        discoveries = [
            _discovery("C-1", evidence_refs=("artifact://a",)),
            _discovery("C-2", evidence_refs=("artifact://b",)),
            _discovery("C-3", evidence_refs=("artifact://a",)),
            _discovery("C-4", evidence_refs=("artifact://b", "artifact://c")),
        ]

        result = duplicate_discovery_rate(discoveries)

        assert result.metric_id == "duplicate_discovery_rate"
        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal(2)
        assert result.denominator == Decimal(4)
        assert result.value == Decimal("0.5")

    def test_a_candidate_is_not_a_duplicate_of_itself(self) -> None:
        """引用同一份证据的两个候选是重复；一条候选引用多次不是。"""
        discoveries = [_discovery("C-1", evidence_refs=("artifact://a", "artifact://b"))]

        result = duplicate_discovery_rate(discoveries)

        assert result.numerator == Decimal(0)
        assert result.value == Decimal(0)

    def test_distinct_evidence_means_no_duplicates(self) -> None:
        discoveries = [
            _discovery("C-1", evidence_refs=("artifact://a",)),
            _discovery("C-2", evidence_refs=("artifact://b",)),
            _discovery("C-3", evidence_refs=("artifact://c",)),
        ]

        result = duplicate_discovery_rate(discoveries)

        assert result.value == Decimal(0)
        assert result.state is MetricState.COMPUTED

    def test_with_no_candidates_there_are_no_samples(self) -> None:
        result = duplicate_discovery_rate([])

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None
