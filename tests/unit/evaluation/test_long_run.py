"""TASK-P3-018 —— 「有效演进周期」判据的契约。

设计依据：docs/tasks.md TASK-P3-018（"『有效演进周期』的判定标准需在开工前
固定，否则会出现『跑了 10 轮但全是无效修改』的凑数情况"）。

**这一层是纯函数。** 给它一组已知事实，它每次给出同样的判定 —— 所以这里每
一条断言都在证伪**判据的口径**，而不是证伪"数据库连得上"。

**防凑数的四组用例。**

- **候选没有证据** → 那一轮不算数（判据①）。
- **决策是 defer / reject** → 那一轮不算数（判据②）："决定了不做"是有价值
  的决策，但它没有让项目变好。
- **改了但没合入主分支** → 那一轮不算数（判据③）：工作区里的补丁在项目里
  什么都不是。
- **达成度缺一个可比的数** → 报"算不出来"，**不报"持平"**（趋势层）。

**趋势层按轮比，不看首尾。** "先崩后涨"与"一路涨上去"在首尾口径下长得一
样，而这两件事对"系统是不是在变好"的回答完全相反。

**序列从第一轮的轮前值起头。** 否则**第一轮自己把项目做坏了都没人知道** ——
序列里没有它的轮前值，跟谁比都比不出来。所以序列是十轮十一个点，净收益算的
是"整跑走了多远"。
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from evaluation.common import MetricState
from evaluation.long_run import (
    ADOPTED_DECISION,
    CANDIDATE_WITH_EVIDENCE,
    DECISION_RECORDED,
    ORIGIN_AUTONOMOUS,
    ORIGIN_HUMAN,
    REQUIRED_ROUNDS,
    VERIFIABLE_IMPROVEMENT,
    AttainmentTrend,
    CandidateEvidence,
    CheckResult,
    CycleEvidence,
    CycleVerdict,
    LongRunReport,
    build_report,
    build_trend,
    judge_cycle,
)


def _candidate(*, decision: str = DECISION_RECORDED, evidence: bool = True) -> CandidateEvidence:
    """造一条候选证据 —— 默认是一条"有证据且被采纳"的候选。"""
    return CandidateEvidence(
        candidate_id="CAND-1",
        title="某条候选",
        evidence_refs=("src/tinylib/cli.py",) if evidence else (),
        decision=decision,
    )


#: 一轮"什么都对"的事实 —— 各条用例只改自己关心的那几处。
_GOOD = CycleEvidence(
    cycle_id="cycle-1",
    origin=ORIGIN_AUTONOMOUS,
    candidates=(_candidate(),),
    task_keys=("T-LR-1",),
    verification_passed=True,
    merged=True,
    merge_commit="abc123456789",
    attainment_before=Decimal("0.50"),
    attainment_after=Decimal("0.60"),
)


def _evidence(**changes: Any) -> CycleEvidence:
    """在一轮"什么都对"的事实上改几处。

    **改动写不动的就是默认值** —— 于是每条用例只写自己那一条判据，不受别的
    判据干扰。
    """
    return replace(_GOOD, **changes)


def _check(verdict: CycleVerdict, name: str) -> CheckResult:
    """从判定里取出某一条判据的结论。"""
    for item in verdict.checks:
        if item.name == name:
            return item
    raise AssertionError(f"判定里没有 {name} 这一条")


class TestSingleCycle:
    """单轮判定 —— 三条判据各管一件事。"""

    def test_a_complete_cycle_is_valid(self) -> None:
        verdict = judge_cycle(_evidence())
        assert verdict.valid is True
        assert verdict.autonomous is True
        assert verdict.improved is True
        assert verdict.delta == Decimal("0.10")

    def test_three_checks_are_always_reported(self) -> None:
        """少一条的报告读起来像"没查那一条"。"""
        verdict = judge_cycle(_evidence())
        assert [item.name for item in verdict.checks] == [
            CANDIDATE_WITH_EVIDENCE,
            ADOPTED_DECISION,
            VERIFIABLE_IMPROVEMENT,
        ]

    def test_a_candidate_without_evidence_fails_the_cycle(self) -> None:
        verdict = judge_cycle(_evidence(candidates=(_candidate(evidence=False),)))
        assert verdict.valid is False
        assert _check(verdict, CANDIDATE_WITH_EVIDENCE).passed is False

    def test_no_candidate_fails_the_cycle(self) -> None:
        verdict = judge_cycle(_evidence(candidates=()))
        assert verdict.valid is False
        assert "没有发现任何候选" in _check(verdict, CANDIDATE_WITH_EVIDENCE).reason

    def test_a_deferred_decision_does_not_count(self) -> None:
        """**"决定了不做"不等于"推进了一轮"。**"""
        verdict = judge_cycle(_evidence(candidates=(_candidate(decision="defer"),)))
        assert verdict.valid is False
        assert _check(verdict, ADOPTED_DECISION).passed is False

    def test_a_rejected_decision_does_not_count(self) -> None:
        verdict = judge_cycle(_evidence(candidates=(_candidate(decision="reject"),)))
        assert verdict.valid is False
        assert _check(verdict, ADOPTED_DECISION).passed is False

    def test_a_decision_without_a_task_does_not_count(self) -> None:
        verdict = judge_cycle(_evidence(task_keys=()))
        assert verdict.valid is False
        assert _check(verdict, ADOPTED_DECISION).passed is False

    def test_a_cycle_that_did_not_merge_does_not_count(self) -> None:
        """改了工作区但没合进去 —— 项目里什么都没变。"""
        verdict = judge_cycle(_evidence(merged=False, merge_commit=""))
        assert verdict.valid is False
        assert _check(verdict, VERIFIABLE_IMPROVEMENT).passed is False

    def test_a_cycle_that_failed_verification_does_not_count(self) -> None:
        verdict = judge_cycle(_evidence(verification_passed=False))
        assert verdict.valid is False
        assert _check(verdict, VERIFIABLE_IMPROVEMENT).passed is False

    def test_a_cycle_without_measured_attainment_does_not_count(self) -> None:
        verdict = judge_cycle(_evidence(attainment_after=None))
        assert verdict.valid is False
        assert _check(verdict, VERIFIABLE_IMPROVEMENT).passed is False

    def test_a_human_dispatched_candidate_is_not_autonomous(self) -> None:
        verdict = judge_cycle(_evidence(origin=ORIGIN_HUMAN))
        assert verdict.autonomous is False
        assert verdict.valid is True, "有效性与人派不派发是两件事"

    def test_an_unmeasurable_attainment_is_not_an_improvement(self) -> None:
        """**"测不出来"不是"持平"。**"""
        verdict = judge_cycle(_evidence(attainment_before=None))
        assert verdict.improved is False
        assert verdict.delta is None


class TestTrend:
    """趋势 —— 逐轮比，不看首尾。"""

    def test_a_rising_series_is_non_decreasing(self) -> None:
        rounds = [_verdict("c1", "0.50", "0.60"), _verdict("c2", "0.60", "0.70")]
        trend = build_trend(rounds)
        assert trend.state is MetricState.COMPUTED
        assert trend.non_decreasing is True
        assert trend.series == (Decimal("0.50"), Decimal("0.60"), Decimal("0.70"))
        assert trend.net_gain == Decimal("0.20")

    def test_a_flat_series_is_non_decreasing(self) -> None:
        rounds = [_verdict("c1", "0.50", "0.60"), _verdict("c2", "0.60", "0.60")]
        trend = build_trend(rounds)
        assert trend.non_decreasing is True

    def test_a_regression_in_the_middle_is_caught(self) -> None:
        """ "先崩后涨"在首尾口径下与"一路涨上去"长得一样。"""
        rounds = [
            _verdict("c1", "0.50", "0.70"),
            _verdict("c2", "0.70", "0.60"),
            _verdict("c3", "0.60", "0.90"),
        ]
        trend = build_trend(rounds)
        assert trend.non_decreasing is False
        assert trend.regressions == ("c1 → c2",)
        assert trend.net_gain == Decimal("0.40")

    def test_a_regression_in_the_first_round_is_caught(self) -> None:
        """**第一轮自己把项目做坏了，也要看得见。**

        序列若从第一轮的"轮后"起头，这一处退步就没有可比的对象 —— 它会被
        悄悄吞掉，而"第一轮就把项目改坏了"是最该被抓住的情形。
        """
        rounds = [
            _verdict("c1", "0.70", "0.50"),
            _verdict("c2", "0.50", "0.60"),
        ]
        trend = build_trend(rounds)
        assert trend.non_decreasing is False
        assert trend.regressions == ("c1 轮内（轮前 → 轮后）",)
        assert trend.net_gain == Decimal("-0.10")

    def test_a_missing_first_round_baseline_is_not_computable(self) -> None:
        """序列缺头 → 比不了 → **报算不出来**。"""
        rounds = [_verdict("c1", "0.50", "0.60"), _verdict("c2", "0.60", "0.70")]
        broken = CycleVerdict(
            cycle_id=rounds[0].cycle_id,
            origin=rounds[0].origin,
            checks=rounds[0].checks,
            attainment_before=None,
            attainment_after=rounds[0].attainment_after,
        )
        trend = build_trend([broken, rounds[1]])
        assert trend.state is MetricState.BASELINE_MISSING
        assert trend.series == ()

    def test_a_missing_attainment_is_reported_as_not_computable(self) -> None:
        """缺一个可比的数 → **报算不出来**，不报"持平"。"""
        rounds = [_verdict("c1", "0.50", "0.60"), _verdict("c2", "0.60", None)]
        trend = build_trend(rounds)
        assert trend.state is MetricState.BASELINE_MISSING
        assert trend.non_decreasing is False
        assert trend.series == ()

    def test_no_rounds_is_no_samples(self) -> None:
        trend = build_trend([])
        assert trend.state is MetricState.NO_SAMPLES
        assert trend.non_decreasing is False


class TestReport:
    """整跑的结论 —— 轮数够不够、每轮算不算数、趋势对不对。"""

    def test_ten_valid_autonomous_rounds_are_accepted(self) -> None:
        rounds = [
            _verdict(
                f"c{index}",
                "0.50",
                str(Decimal("0.50") + Decimal(index) * Decimal("0.01")),
            )
            for index in range(REQUIRED_ROUNDS)
        ]
        report = build_report(rounds)
        assert report.enough_rounds is True
        assert report.all_valid is True
        assert report.all_autonomous is True
        assert report.trend_non_decreasing is True
        assert report.accepted is True
        assert report.failures == ()

    def test_nine_rounds_are_not_enough(self) -> None:
        rounds = [_verdict(f"c{index}", "0.50", "0.60") for index in range(REQUIRED_ROUNDS - 1)]
        report = build_report(rounds)
        assert report.enough_rounds is False
        assert report.accepted is False
        assert any("轮数不够" in item for item in report.failures)

    def test_one_bad_round_fails_the_whole_run(self) -> None:
        rounds = [_verdict(f"c{index}", "0.50", "0.60") for index in range(REQUIRED_ROUNDS)]
        rounds[4] = _verdict("c4", "0.50", "0.60", valid=False)
        report = build_report(rounds)
        assert report.accepted is False
        assert report.valid_cycles == REQUIRED_ROUNDS - 1
        assert any("c4" in item for item in report.failures)

    def test_a_human_round_fails_the_whole_run(self) -> None:
        rounds = [_verdict(f"c{index}", "0.50", "0.60") for index in range(REQUIRED_ROUNDS)]
        rounds[0] = _verdict("c0", "0.50", "0.60", origin=ORIGIN_HUMAN)
        report = build_report(rounds)
        assert report.accepted is False
        assert any("不是自主发现" in item for item in report.failures)

    def test_json_export_is_stable(self) -> None:
        """两次导出逐字节相同 —— 记录要能进版本库。"""
        rounds = [_verdict("c1", "0.50", "0.60")]
        report = build_report(rounds)
        assert report.to_json() == report.to_json()
        payload = json.loads(report.to_json())
        assert payload["summary"]["valid_cycles"] == 1
        assert payload["required_rounds"] == REQUIRED_ROUNDS

    def test_markdown_export_lists_every_round(self) -> None:
        rounds = [_verdict(f"c{index}", "0.50", "0.60") for index in range(3)]
        report = build_report(rounds)
        text = report.to_markdown()
        assert "c1" not in text, "Markdown 用轮序号，不抄 cycle id"
        assert text.count("| ✓ | ✓ |") == 3
        assert "通过" in text


class TestEmptyReport:
    """空报告 —— 一轮都没有的时候不许说"通过"。"""

    def test_empty_report_is_not_accepted(self) -> None:
        report = LongRunReport(rounds=())
        assert report.accepted is False
        assert report.enough_rounds is False
        assert report.all_valid is False
        assert report.all_autonomous is False
        assert report.trend.state is MetricState.NO_SAMPLES

    def test_empty_report_reports_the_missing_rounds(self) -> None:
        report = LongRunReport(rounds=())
        assert any("轮数不够" in item for item in report.failures)


def _verdict(
    cycle_id: str,
    before: str,
    after: str | None,
    *,
    valid: bool = True,
    origin: str = ORIGIN_AUTONOMOUS,
) -> CycleVerdict:
    """造一轮的判定 —— 默认是一轮"有效、自主、达成度不降"的判定。"""
    passed = CheckResult(name="占位", passed=True, reason="占位")
    checks = (
        passed,
        passed,
        passed,
    )
    verdict = CycleVerdict(
        cycle_id=cycle_id,
        origin=origin,
        checks=checks,
        attainment_before=Decimal(before),
        attainment_after=Decimal(after) if after is not None else None,
    )
    if valid:
        return verdict
    return CycleVerdict(
        cycle_id=cycle_id,
        origin=origin,
        checks=(CheckResult(name="候选有证据", passed=False, reason="没证据"),),
        attainment_before=Decimal(before),
        attainment_after=Decimal(after) if after is not None else None,
    )


def test_attainment_trend_default_is_empty() -> None:
    """默认的趋势是"没有样本"，不是"持平"。"""
    trend = AttainmentTrend(
        state=MetricState.NO_SAMPLES,
        series=(),
        non_decreasing=False,
        net_gain=None,
    )
    assert trend.regressions == ()
    assert trend.reason == ""


@pytest.mark.parametrize(
    "name",
    [CANDIDATE_WITH_EVIDENCE, ADOPTED_DECISION, VERIFIABLE_IMPROVEMENT],
)
def test_check_names_are_stable(name: str) -> None:
    """判据的名字是记录里的字段 —— 改了名字，老记录就查不到了。"""
    assert name in ("候选有证据", "有被采纳的决策", "有可验证的改善记录")
