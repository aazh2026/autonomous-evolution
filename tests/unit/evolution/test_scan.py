"""TASK-P3-012 —— 定时扫描的领域契约。

四条对应四组用例：

1. **事实必须有名字和值** —— 空白的事实名等于没有事实。
2. **信号的身份与时间无关、与计数无关** —— 同一个问题跨轮次可比。
3. **"没扫"与"扫了没发现"是两件事** —— 空报告有三种，不能混。
4. **范围过滤只认前缀** —— 限了前缀就只报范围内的。

外加一条**方向性断言**：信号涉及到的模块必须包含主模块 —— 否则
"这条信号说的是谁"就有两个答案了。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.evolution.assessment import GapSeverity
from core.evolution.scan import (
    DEFAULT_COUPLING_HUB_THRESHOLD,
    DEFAULT_SCAN_KINDS,
    ScanFact,
    ScanKind,
    ScanPolicy,
    ScanReport,
    ScanScope,
    ScanSignal,
)


def _fact(name: str = "fan_in", value: str = "7") -> ScanFact:
    return ScanFact(name=name, value=value)


def _signal(
    *,
    kind: ScanKind = ScanKind.COUPLING_HUB,
    module: str = "pkg.hub",
    modules: tuple[str, ...] = (),
    severity: GapSeverity = GapSeverity.MEDIUM,
    facts: tuple[ScanFact, ...] | None = None,
) -> ScanSignal:
    return ScanSignal(
        kind=kind,
        module=module,
        modules=modules,
        severity=severity,
        summary="一个说明",
        facts=(_fact(),) if facts is None else facts,
    )


class TestTheFacts:
    """事实必须有名有姓。"""

    def test_a_blank_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScanFact(name="  ", value="7")

    def test_a_blank_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScanFact(name="fan_in", value=" ")

    def test_the_value_is_a_string_because_the_kinds_differ(self) -> None:
        """数字与名单放进同一个容器 —— 所以值是字符串。"""
        fact = ScanFact(name="dependents", value="a, b, c")
        assert fact.value == "a, b, c"


class TestTheSignal:
    """信号的身份与形状。"""

    def test_a_signal_without_facts_is_rejected(self) -> None:
        """没有事实的信号只是一句说法 —— 那不成立。"""
        with pytest.raises(ValidationError):
            _signal(facts=())

    def test_the_involved_modules_must_contain_the_main_one(self) -> None:
        with pytest.raises(ValidationError):
            _signal(module="pkg.hub", modules=("pkg.other",))

    def test_the_fingerprint_does_not_depend_on_the_numbers(self) -> None:
        """身份只由种类与模块决定 —— 变了的是事实，不是身份。"""
        before = _signal(facts=(_fact("fan_in", "7"),))
        after = _signal(facts=(_fact("fan_in", "9"),))
        assert before.fingerprint == after.fingerprint

    def test_the_fingerprint_separates_different_kinds(self) -> None:
        cycle = _signal(kind=ScanKind.DEPENDENCY_CYCLE)
        hub = _signal(kind=ScanKind.COUPLING_HUB)
        assert cycle.fingerprint != hub.fingerprint

    def test_the_fact_can_be_looked_up_by_name(self) -> None:
        signal = _signal(facts=(_fact("fan_in", "7"), _fact("impact", "9")))
        assert signal.fact("impact") == "9"
        assert signal.fact("nope") is None


class TestTheScope:
    """范围就是前缀。"""

    def test_an_empty_prefix_list_covers_everything(self) -> None:
        scope = ScanScope(project_id="P1")
        assert scope.covers("anything.at.all")

    def test_a_prefix_filters(self) -> None:
        scope = ScanScope(project_id="P1", module_prefixes=("pkg.",))
        assert scope.covers("pkg.hub")
        assert not scope.covers("other.hub")

    def test_the_project_id_cannot_be_blank(self) -> None:
        with pytest.raises(ValidationError):
            ScanScope(project_id="  ")


class TestThePolicy:
    """策略是配置，结论是事实。"""

    def test_the_default_scans_every_kind(self) -> None:
        policy = ScanPolicy()
        assert policy.kinds == DEFAULT_SCAN_KINDS
        assert all(policy.scans(kind) for kind in ScanKind)

    def test_a_kind_can_be_switched_off(self) -> None:
        policy = ScanPolicy(kinds=(ScanKind.DEPENDENCY_CYCLE,))
        assert policy.scans(ScanKind.DEPENDENCY_CYCLE)
        assert not policy.scans(ScanKind.COUPLING_HUB)

    def test_the_threshold_is_a_positive_number(self) -> None:
        with pytest.raises(ValidationError):
            ScanPolicy(coupling_hub_threshold=0)

    def test_the_default_threshold_is_the_documented_one(self) -> None:
        assert ScanPolicy().coupling_hub_threshold == DEFAULT_COUPLING_HUB_THRESHOLD


class TestTheReport:
    """空报告有三种，不能混。"""

    def _report(self, *, examined: int, signals: tuple[ScanSignal, ...] = ()) -> ScanReport:
        return ScanReport(
            scan_id="scan-1",
            scope=ScanScope(project_id="P1"),
            policy=ScanPolicy(),
            signals=signals,
            examined_modules=examined,
        )

    def test_the_signals_can_be_filtered_by_kind(self) -> None:
        report = self._report(
            examined=3,
            signals=(_signal(kind=ScanKind.COUPLING_HUB), _signal(kind=ScanKind.DEPENDENCY_CYCLE)),
        )
        assert len(report.of_kind(ScanKind.COUPLING_HUB)) == 1
        assert len(report.of_kind(ScanKind.UNTESTED_MODULE)) == 0

    def test_the_signals_can_be_filtered_by_module(self) -> None:
        report = self._report(
            examined=3,
            signals=(
                _signal(module="pkg.hub", modules=("pkg.hub", "pkg.other")),
                _signal(module="pkg.elsewhere", modules=("pkg.elsewhere",)),
            ),
        )
        assert len(report.for_module("pkg.other")) == 1
        assert len(report.for_module("pkg.hub")) == 1
        assert report.for_module("pkg.nothing") == ()

    def test_the_number_of_examined_modules_separates_the_three_cases(self) -> None:
        """没数据可扫（0）与扫了没发现（>0）是两件事。"""
        assert self._report(examined=0).examined_modules == 0
        assert self._report(examined=12).examined_modules == 12

    def test_the_fingerprints_are_the_set_of_identities(self) -> None:
        report = self._report(examined=2, signals=(_signal(), _signal(module="pkg.other")))
        assert report.fingerprints == frozenset(
            {"coupling_hub:pkg.hub", "coupling_hub:pkg.other"},
        )

    def test_a_negative_module_count_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._report(examined=-1)
