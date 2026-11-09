"""TASK-P1-083 —— 试点标注集与误报率的分母。

验证（T1）：

- ① 标注集可被程序读取，每条含 ``issue_ref`` / ``is_valid_finding`` /
  ``category`` / ``annotated_by`` / ``annotated_at``；
- ② 条数恰为 20 且 ``is_valid_finding`` 为布尔；
- ③ 误报率指标的分母取自该标注集，且标注集缺失时指标报"基线未建立"而非
  返回 0。

**为什么解析要这么严。** 一个读坏了的标注集，如果被当成"没有标注"，误报率
会报"基线未建立"；如果被当成"空的标注集"，误报率会报 **0** —— 而 0 是最
容易被当成好消息的那个数。所以多一个键、少一个键、``is_valid_finding`` 写成
``1`` 而不是 ``true``、时间戳不带时区，一律报错。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluation.common import MetricState, Verdict
from evaluation.evolution_evaluation.decision import (
    DecisionRecord,
    apply_labels,
    false_positive_rate_from_labels,
)
from evaluation.labels.pilot import (
    EXPECTED_LABEL_COUNT,
    PILOT_LABELS_PATH,
    JsonlLabelSetStore,
    LabelSet,
    LabelSetError,
    LabelSetMissingError,
    PilotLabel,
    pilot_label_store,
)

#: 一条**合法**的标注 —— 改字段时拿它当底本。
_VALID_RECORD: dict[str, object] = {
    "annotated_at": "2026-11-09T09:12:00+00:00",
    "annotated_by": "aazh2026",
    "category": "runtime_error",
    "is_valid_finding": True,
    "issue_ref": "PILOT-001",
}


def _record(**overrides: object) -> dict[str, object]:
    """在合法底本上改字段。"""
    return {**_VALID_RECORD, **overrides}


def _write(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    """写一份 JSONL 标注集。"""
    path = tmp_path / "labels.jsonl"
    path.write_text(
        "".join(f"{json.dumps(record, ensure_ascii=False)}\n" for record in records),
        encoding="utf-8",
    )
    return path


class TestTheLabelSetIsReadable:
    """T1 ① —— 标注集可被程序读取。"""

    def test_the_pilot_labels_can_be_read(self) -> None:
        label_set = pilot_label_store().load()

        assert len(label_set) == EXPECTED_LABEL_COUNT
        assert all(isinstance(label, PilotLabel) for label in label_set.labels)

    def test_every_label_carries_the_five_required_fields(self) -> None:
        """五个字段缺一不可 —— 缺了"谁标的"就没法复核。"""
        label_set = pilot_label_store().load()

        for label in label_set.labels:
            assert label.issue_ref.strip()
            assert isinstance(label.is_valid_finding, bool)
            assert label.category.strip()
            assert label.annotated_by.strip()
            assert label.annotated_at.tzinfo is not None

    def test_the_issue_refs_are_unique(self) -> None:
        """同一个问题标两次，第二次盖掉第一次的话，标注就变成"最后写的人赢"。"""
        label_set = pilot_label_store().load()

        assert len(set(label_set.refs)) == len(label_set)

    def test_the_timestamps_are_utc(self) -> None:
        """时间戳统一归到 UTC —— 同一个时刻在不同机器上要是一个时刻。"""
        label_set = pilot_label_store().load()

        assert all(label.annotated_at.tzinfo is UTC for label in label_set.labels)

    def test_the_label_set_can_be_written_back_out_unchanged(self) -> None:
        """读出来再写出去必须逐字节相同 —— 否则标注集没法被版本管理。"""
        label_set = pilot_label_store().load()

        written = label_set.to_jsonl()

        assert LabelSet.from_jsonl(written).to_jsonl() == written


class TestTheStrictParsing:
    """读不懂的标注集必须报错 —— 不能被当成"没有标注"。"""

    def test_a_missing_field_is_refused(self) -> None:
        record = _record()
        del record["category"]

        with pytest.raises(LabelSetError, match="缺少字段"):
            LabelSet.from_jsonl(json.dumps(record, ensure_ascii=False))

    def test_an_unknown_field_is_refused(self) -> None:
        """多出来的键说明写的人以为它有意义 —— 那就不能装作没看见。"""
        with pytest.raises(LabelSetError, match="多了字段"):
            LabelSet.from_jsonl(json.dumps(_record(note="随手写的"), ensure_ascii=False))

    def test_a_numeric_finding_flag_is_refused(self) -> None:
        """``1`` 不是 ``true`` —— 布尔与整数的混用会让"没标"和"标成否"分不清。"""
        with pytest.raises(LabelSetError, match="必须是布尔值"):
            LabelSet.from_jsonl(json.dumps(_record(is_valid_finding=1), ensure_ascii=False))

    def test_a_string_finding_flag_is_refused(self) -> None:
        with pytest.raises(LabelSetError, match="必须是布尔值"):
            LabelSet.from_jsonl(json.dumps(_record(is_valid_finding="true"), ensure_ascii=False))

    def test_a_naive_timestamp_is_refused(self) -> None:
        """不带时区的时间戳没有意义 —— 它指的是哪个时刻？"""
        with pytest.raises(LabelSetError, match="必须带时区"):
            LabelSet.from_jsonl(
                json.dumps(_record(annotated_at="2026-11-09T09:12:00"), ensure_ascii=False)
            )

    def test_an_unparsable_timestamp_is_refused(self) -> None:
        with pytest.raises(LabelSetError, match="不是 ISO 时间戳"):
            LabelSet.from_jsonl(
                json.dumps(_record(annotated_at="2026/11/09 09:12"), ensure_ascii=False)
            )

    def test_a_blank_issue_ref_is_refused(self) -> None:
        with pytest.raises(LabelSetError, match="issue_ref"):
            LabelSet.from_jsonl(json.dumps(_record(issue_ref="  "), ensure_ascii=False))

    def test_a_duplicated_issue_ref_is_refused(self) -> None:
        text = "\n".join(json.dumps(_record(), ensure_ascii=False) for _ in range(2))

        with pytest.raises(LabelSetError, match="重复"):
            LabelSet.from_jsonl(text)

    def test_a_line_that_is_not_json_is_refused(self) -> None:
        with pytest.raises(LabelSetError):
            LabelSet.from_jsonl("这不是 JSON")

    def test_a_line_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(LabelSetError, match="不是一个 JSON 对象"):
            LabelSet.from_jsonl("[1, 2, 3]")


class TestTheLabelCount:
    """T1 ② —— 条数恰为 20，且 ``is_valid_finding`` 为布尔。"""

    def test_there_are_exactly_twenty_labels(self) -> None:
        assert EXPECTED_LABEL_COUNT == 20
        assert len(pilot_label_store().load()) == 20

    def test_every_finding_flag_is_a_boolean(self) -> None:
        label_set = pilot_label_store().load()

        assert all(isinstance(label.is_valid_finding, bool) for label in label_set.labels)

    def test_the_split_is_twelve_valid_and_eight_invalid(self) -> None:
        """分母里有 12 条有效发现与 8 条无效发现。

        **八条无效发现不是凑数** —— 全是真问题的清单会让误报率永远是 0。
        """
        label_set = pilot_label_store().load()

        assert label_set.valid_count == 12
        assert label_set.invalid_count == 8

    def test_the_store_refuses_a_label_set_with_the_wrong_count(self, tmp_path: Path) -> None:
        """条数不对，分母就不成立 —— 必须报错，不能凑合着用。"""
        path = _write(tmp_path, [_record()])

        store = JsonlLabelSetStore(path, expected_count=EXPECTED_LABEL_COUNT)

        with pytest.raises(LabelSetError, match="条数必须是 20"):
            store.load()

    def test_a_missing_file_is_baseline_missing(self, tmp_path: Path) -> None:
        """文件不在是**基线未建立**，与"文件坏了"分开 —— 处置方式不同。"""
        store = JsonlLabelSetStore(tmp_path / "nope.jsonl")

        with pytest.raises(LabelSetMissingError):
            store.load()

    def test_the_shipped_file_is_the_one_the_task_book_names(self) -> None:
        """交付物路径是任务书写死的 —— 换个地方就找不着了。"""
        assert PILOT_LABELS_PATH.name == "pilot_labels.jsonl"
        assert PILOT_LABELS_PATH.parent.name == "labels"
        assert PILOT_LABELS_PATH.exists()


class TestTheFalsePositiveRateDenominator:
    """T1 ③ —— 分母取自标注集；标注集缺失时报"基线未建立"。"""

    def test_the_denominator_comes_from_the_label_set(self) -> None:
        """三条 record 决策，两条在标注集里 —— 分母就是 2。"""
        decisions = [
            DecisionRecord(decision_id="D-1", candidate_id="PILOT-001", decision="record"),
            DecisionRecord(decision_id="D-2", candidate_id="PILOT-013", decision="record"),
        ]

        result = false_positive_rate_from_labels(decisions, pilot_label_store())

        assert result.state is MetricState.COMPUTED
        assert result.denominator == 2
        assert result.numerator == 1
        assert result.value is not None
        assert result.detail["label_set_size"] == EXPECTED_LABEL_COUNT

    def test_a_candidate_outside_the_label_set_is_not_in_the_denominator(self) -> None:
        """没被审过的不能说它错，也不能说它对。"""
        decisions = [
            DecisionRecord(decision_id="D-1", candidate_id="PILOT-001", decision="record"),
            DecisionRecord(decision_id="D-2", candidate_id="PILOT-999", decision="record"),
        ]

        result = false_positive_rate_from_labels(decisions, pilot_label_store())

        assert result.denominator == 1
        assert result.numerator == 0

    def test_the_caller_cannot_inject_its_own_labels(self) -> None:
        """**分母不能由被评价的一方决定。**

        调用方自己塞进决策记录里的标注一律被清掉 —— 否则"误报率 0%"可以靠
        自己给候选贴上"有效"标签做出来。
        """
        decisions = [
            DecisionRecord(
                decision_id="D-1",
                candidate_id="PILOT-013",
                decision="record",
                candidate_label=Verdict.VALID,
            ),
        ]

        labelled = apply_labels(decisions, pilot_label_store().load())

        assert labelled[0].candidate_label is Verdict.INVALID

        result = false_positive_rate_from_labels(decisions, pilot_label_store())
        assert result.numerator == 1

    def test_deferred_and_rejected_decisions_stay_out_of_the_denominator(self) -> None:
        """只有"被派了任务"的无效候选才构成浪费。"""
        decisions = [
            DecisionRecord(decision_id="D-1", candidate_id="PILOT-013", decision="record"),
            DecisionRecord(decision_id="D-2", candidate_id="PILOT-015", decision="defer"),
            DecisionRecord(decision_id="D-3", candidate_id="PILOT-017", decision="reject"),
        ]

        result = false_positive_rate_from_labels(decisions, pilot_label_store())

        assert result.denominator == 1
        assert result.numerator == 1

    def test_a_missing_label_set_reports_baseline_missing_not_zero(self) -> None:
        """**"还没标"与"全是无效"是相反的两件事。**

        报 0 会让看板显示"误报率 0%"，读的人会以为系统很准。
        """
        decisions = [
            DecisionRecord(decision_id="D-1", candidate_id="PILOT-001", decision="record"),
        ]
        store = JsonlLabelSetStore(Path("/nonexistent/pilot_labels.jsonl"))

        result = false_positive_rate_from_labels(decisions, store)

        assert result.state is MetricState.BASELINE_MISSING
        assert result.value is None
        assert result.numerator is None or result.numerator == 0
        assert "标注基线" in str(result.detail["reason"])

    def test_a_broken_label_set_also_reports_baseline_missing(self, tmp_path: Path) -> None:
        """文件坏了也是"基线未建立" —— 但理由要说明白是**文件坏了**。"""
        path = tmp_path / "labels.jsonl"
        path.write_text("{不是 JSON}\n", encoding="utf-8")
        decisions = [
            DecisionRecord(decision_id="D-1", candidate_id="PILOT-001", decision="record"),
        ]

        result = false_positive_rate_from_labels(decisions, JsonlLabelSetStore(path))

        assert result.state is MetricState.BASELINE_MISSING
        assert result.value is None
        assert "不可用" in str(result.detail["reason"])

    def test_no_decisions_at_all_is_no_samples(self) -> None:
        """一个决策都没有是"系统还没产出东西"，不是"缺标注"。"""
        result = false_positive_rate_from_labels([], pilot_label_store())

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None

    def test_the_timestamp_of_a_label_is_readable(self) -> None:
        """标注时间要能被读成时刻 —— 那是"谁在什么时候标的"的载体。"""
        label = pilot_label_store().load().label_of("PILOT-001")

        assert label is not None
        assert label.annotated_at == datetime(2026, 11, 9, 9, 12, tzinfo=UTC)
