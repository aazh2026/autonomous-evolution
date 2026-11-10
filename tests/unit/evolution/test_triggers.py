"""TASK-P3-011 —— 触发事件的领域契约。

**测的是"配错了会不会被抓住"，不是"事件长什么样"。** 事件的形状读一遍就
知道；真正会出事的是**一个生产者把代码事件标成定时事件** —— 那时候周期记
录里写的就是"定时触发"，而实际上它是被一次提交触发的，**事后没人能查出真
相**。所以这一层的主张是：**配错是一条错误，不是一个看不出来的事实。**

**断言一律对着具体的数字。** "六类源各有一种事件"写成"六"；"每种源恰好一
种事件"写成"恰好一种"—— 少一种、多一种、配错一种，这里都会红。
"""

from __future__ import annotations

import dataclasses

import pytest

from core.evolution.evolution_loop import TriggerSource
from core.evolution.triggers import (
    SOURCE_EVENT_TYPES,
    TriggerContractError,
    TriggerEvent,
    TriggerEventType,
)

#: 六类触发源 —— **写死六个**。加一类源就得改这里，是有意的。
SIX_SOURCES: tuple[TriggerSource, ...] = (
    TriggerSource.SCHEDULE,
    TriggerSource.CODE,
    TriggerSource.QUALITY,
    TriggerSource.RUNTIME,
    TriggerSource.EXTERNAL,
    TriggerSource.KNOWLEDGE,
)

#: 每类源对应的事件类型 —— 写死这张表，**不看实现**。
EXPECTED_EVENT_TYPES: dict[TriggerSource, TriggerEventType] = {
    TriggerSource.SCHEDULE: TriggerEventType.SCHEDULE_SCAN,
    TriggerSource.CODE: TriggerEventType.CODE_COMMIT,
    TriggerSource.QUALITY: TriggerEventType.QUALITY_FAILURE,
    TriggerSource.RUNTIME: TriggerEventType.RUNTIME_INCIDENT,
    TriggerSource.EXTERNAL: TriggerEventType.EXTERNAL_REPORT,
    TriggerSource.KNOWLEDGE: TriggerEventType.KNOWLEDGE_UPDATE,
}


def _event(source: TriggerSource, *, aggregate_id: str = "a-1") -> TriggerEvent:
    """一份合法的触发事件。"""
    return TriggerEvent(
        source=source,
        event_type=EXPECTED_EVENT_TYPES[source],
        aggregate_id=aggregate_id,
        project_id="P1",
        summary="一件事",
    )


class TestTheTable:
    """对照表本身。"""

    def test_every_source_has_an_entry(self) -> None:
        assert set(SOURCE_EVENT_TYPES) == set(SIX_SOURCES)

    def test_every_source_has_exactly_one_event_type(self) -> None:
        for source, allowed in SOURCE_EVENT_TYPES.items():
            assert len(allowed) == 1, f"{source.value} 允许了 {len(allowed)} 种事件类型"

    def test_the_table_matches_the_intent(self) -> None:
        """**对照表就是设计文档 §4 那张表** —— 一行不多一行不少。"""
        assert {
            source: (event_type,) for source, event_type in EXPECTED_EVENT_TYPES.items()
        } == SOURCE_EVENT_TYPES

    def test_the_six_event_types_are_distinct(self) -> None:
        """**六种事件不能撞名。** 撞了名，去重指纹就分不出事情。"""
        values = [event_type.value for event_type in TriggerEventType]
        assert len(values) == len(set(values))
        assert len(values) == 6

    def test_the_event_type_names_say_what_they_are(self) -> None:
        """**名字带前缀，就看得出是哪一路。**"""
        prefixes = {
            TriggerEventType.SCHEDULE_SCAN: "scan",
            TriggerEventType.CODE_COMMIT: "code",
            TriggerEventType.QUALITY_FAILURE: "quality",
            TriggerEventType.RUNTIME_INCIDENT: "runtime",
            TriggerEventType.EXTERNAL_REPORT: "external",
            TriggerEventType.KNOWLEDGE_UPDATE: "knowledge",
        }
        for event_type, prefix in prefixes.items():
            assert event_type.value.startswith(f"{prefix}."), event_type.value


class TestTheContract:
    """事件与契约相不相符。"""

    def test_a_matched_pair_is_accepted(self) -> None:
        event = _event(TriggerSource.CODE)
        assert event.source is TriggerSource.CODE
        assert event.event_type is TriggerEventType.CODE_COMMIT

    @pytest.mark.parametrize("source", SIX_SOURCES)
    def test_every_source_can_make_its_own_event(self, source: TriggerSource) -> None:
        """**六路都得能产出事件** —— 这是 TASK-P3-011 的验收条件的一半。"""
        assert _event(source).event_type is EXPECTED_EVENT_TYPES[source]

    @pytest.mark.parametrize("source", SIX_SOURCES)
    def test_a_mismatched_pair_is_rejected(self, source: TriggerSource) -> None:
        """**把这一路的事件标成别的一路，必须报错。**"""
        other = next(item for item in SIX_SOURCES if item is not source)
        with pytest.raises(TriggerContractError):
            TriggerEvent(
                source=source,
                event_type=EXPECTED_EVENT_TYPES[other],
                aggregate_id="a-1",
                project_id="P1",
                summary="一件事",
            )

    def test_the_error_says_which_pair_was_wrong(self) -> None:
        """**报错要说人话** —— 事后翻日志的人要知道哪儿配错了。"""
        with pytest.raises(TriggerContractError) as caught:
            TriggerEvent(
                source=TriggerSource.SCHEDULE,
                event_type=TriggerEventType.CODE_COMMIT,
                aggregate_id="a-1",
                project_id="P1",
                summary="一件事",
            )
        message = str(caught.value)
        assert "schedule" in message
        assert "code.commit" in message

    @pytest.mark.parametrize("field", ["aggregate_id", "project_id", "summary"])
    def test_a_blank_field_is_rejected(self, field: str) -> None:
        """**空白的主体标识是最容易犯也最致命的错** —— 它会让所有事件落进
        同一个去重指纹，于是只有第一件被处理，其余全被当成重复挡掉。"""
        values = {"aggregate_id": "a-1", "project_id": "P1", "summary": "一件事"}
        values[field] = "   "
        with pytest.raises(TriggerContractError):
            TriggerEvent(
                source=TriggerSource.CODE,
                event_type=TriggerEventType.CODE_COMMIT,
                aggregate_id=values["aggregate_id"],
                project_id=values["project_id"],
                summary=values["summary"],
            )


class TestTheFingerprint:
    """去重指纹。"""

    def test_the_fingerprint_is_the_event_type_and_the_aggregate(self) -> None:
        event = _event(TriggerSource.CODE, aggregate_id="deadbeef")
        assert event.fingerprint.event_type == "code.commit"
        assert event.fingerprint.aggregate_id == "deadbeef"

    def test_two_different_sources_on_the_same_aggregate_are_not_the_same_finger(
        self,
    ) -> None:
        """**同一个主体上的两种事件不是一件事。**

        主体撞上就撞上了 —— 但**事件类型**不一样，指纹就不一样，所以不会
        互相挡掉。这正是"每类源一个事件类型"的理由。
        """
        code = _event(TriggerSource.CODE, aggregate_id="same")
        quality = _event(TriggerSource.QUALITY, aggregate_id="same")
        assert code.fingerprint != quality.fingerprint

    def test_the_same_event_has_the_same_fingerprint(self) -> None:
        first = _event(TriggerSource.CODE, aggregate_id="deadbeef")
        second = _event(TriggerSource.CODE, aggregate_id="deadbeef")
        assert first.fingerprint == second.fingerprint


class TestTheShape:
    """事件本身是冻结的、字段是齐的。"""

    def test_the_event_is_frozen(self) -> None:
        """**事件一旦造好就不能改** —— 它会被放进队列、放进门控窗口。"""
        event = _event(TriggerSource.CODE)
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.aggregate_id = "changed"  # type: ignore[misc]

    def test_the_fields_are_exactly_these_six(self) -> None:
        names = [item.name for item in dataclasses.fields(TriggerEvent)]
        assert names == [
            "source",
            "event_type",
            "aggregate_id",
            "project_id",
            "summary",
            "causation_id",
        ]

    def test_a_causation_id_is_optional(self) -> None:
        """**没有因果链 id 就是外部触发** —— 定时与外部事件本来就没有。"""
        assert _event(TriggerSource.SCHEDULE).causation_id is None
