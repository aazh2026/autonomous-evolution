"""「有效演进周期」的判据 —— 长跑的验收口径。

设计依据：docs/tasks.md TASK-P3-018（交付物"10 个有效演进周期的运行记录"；
验收条件"连续 10 轮无人工派发任务；每轮产出可验证证据；达成度呈非负趋势"）、
R-130（首个里程碑：连续 10 个有效演进周期）、R-128（每轮产生可验证的改善）；
docs/design.md §12（可验证的改善）。

**判据必须在开工前固定下来 —— 这不是形式，是防凑数。** docs/tasks.md 那句话
说得很直白："注意『有效演进周期』的判定标准需在开工前固定，否则会出现『跑了
10 轮但全是无效修改』的凑数情况。" 所以这一层是**纯函数**：给定一轮的事实，
它给出这一轮算不算数；给定十轮的事实，它给出这次长跑算不算数。**它不碰数据
库，不跑测试，什么都不做** —— 它只判。

**三条判据，缺一不可。**

| # | 判据 | 为什么是它 |
| --- | --- | --- |
| ① | **一个有证据的候选** | 没有候选就谈不上演进；候选没有证据，"它说的是不是真的"就没法核 |
| ② | **一个被采纳的决策** | 决策是这一轮的判断；"被采纳"意味着它真的派出了活 |
| ③ | **一条可验证的改善记录** | 验收通过 + 合入主分支 + 达成度前后都测得出来 |

**"被采纳"而不是"有一个决策"。** defer 与 reject 也是决策，而且是有价值的
决策 —— 但它们**没有让项目变好**。把 defer 算成"有效演进周期"，那"跑了十轮"
与"十轮里九轮决定什么都不做"就长得一样，而这两件事显然不是一回事。所以判据
②要的是**派出了活**的那一种决策。

**③ 为什么要求"合入了主分支"。** 改了工作区但没合进去的改动**不算数** ——
它没有变成项目的一部分，下一轮读到的还是旧代码。一个停在工作区里的补丁，
在账上看着像"做了"，在项目里什么都没有。

**单轮判定不要求"不许退步"，那是趋势那一层的事。** 把"不得退步"塞进单轮
判定里，等于把退步藏起来 —— 退步是真实结果，它应该出现在趋势里、出现在报
告里，而不是被判定层悄悄吃掉。**单轮说"这一轮有可核的改善记录"，趋势说"整
条曲线没有往下走"** —— 两句都是实话。

**"没人派发"是整跑的性质，但每轮都能查。** 每一轮都记下候选的来源；只要有
一轮是人塞进去的，整跑就不算"自主"。**这不是靠嘴说"我没让人干预"** ——
它是每轮一行可查的事实。

**"算不出来"不是 0。** 达成度趋势在缺一个可比的数时**报算不出来**，而不是
报"持平"或"退步" —— 与指标层其余部分同一套规矩（:mod:`evaluation.common`）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from evaluation.common import MetricState

#: 判据①的名字。
CANDIDATE_WITH_EVIDENCE: Final[str] = "候选有证据"

#: 判据②的名字。
ADOPTED_DECISION: Final[str] = "有被采纳的决策"

#: 判据③的名字。
VERIFIABLE_IMPROVEMENT: Final[str] = "有可验证的改善记录"

#: 候选的来源 —— 系统自己发现的。
ORIGIN_AUTONOMOUS: Final[str] = "autonomous"

#: 候选的来源 —— 人塞进去的。
ORIGIN_HUMAN: Final[str] = "human"

#: 决策状态 —— 被采纳（派出了活）。
DECISION_RECORDED: Final[str] = "record"

#: 验收要求的轮数 —— TASK-P3-018 要的是连续 10 轮。
REQUIRED_ROUNDS: Final[int] = 10

#: 达成度序列比较时的呈现精度 —— 与指标层其余部分一致（4 位小数）。
_PRECISION: Final[Decimal] = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """一轮里的一个候选 —— 判据①看的是它。

    :param candidate_id: 候选 id。
    :param title: 候选标题。
    :param evidence_refs: 证据的引用。**空的话，这条候选就没有证据** ——
        它可以出现在报告里，但撑不起一次演进。
    :param decision: 这条候选的决策结论（``record`` / ``defer`` / ``reject``）。
    """

    candidate_id: str
    title: str
    evidence_refs: tuple[str, ...]
    decision: str


@dataclass(frozen=True, slots=True)
class CycleEvidence:
    """一轮的事实 —— 判据要用的那几样。

    **这里记的都是"查得到"的东西**：候选与它的证据、决策、任务、验收结论、
    合并提交号、前后达成度。**没有"感觉"这一栏** —— 判定层不接受"我觉得这
    轮挺好"。

    :param attainment_before: 这一轮**之前**的达成度。**测不出来就是
        ``None``** —— 不是 0。0 意味着"达成度就是 0"，而测不出来意味着"没
        测"。
    :param attainment_after: 这一轮**之后**的达成度。同样，测不出来是
        ``None``。
    """

    cycle_id: str
    origin: str
    candidates: tuple[CandidateEvidence, ...]
    task_keys: tuple[str, ...]
    verification_passed: bool
    merged: bool
    merge_commit: str
    attainment_before: Decimal | None
    attainment_after: Decimal | None


@dataclass(frozen=True, slots=True)
class CheckResult:
    """一项判据的结论 —— 通过 / 不通过，以及为什么。

    **理由不能空。** 一条没有理由的"不通过"读的人没法复核，而"通过"没有理
    由则说明它没被真的查过。
    """

    name: str
    passed: bool
    reason: str

    def to_json(self) -> dict[str, object]:
        """导出成 JSON 友好的形状。"""
        return {"name": self.name, "passed": self.passed, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class CycleVerdict:
    """一轮的判定 —— 算不算一个"有效演进周期"。

    :param checks: 三条判据的逐条结论。**三条都在** —— 少一条的报告读起来
        像"没查那一条"。
    """

    cycle_id: str
    origin: str
    checks: tuple[CheckResult, ...]
    attainment_before: Decimal | None
    attainment_after: Decimal | None

    @property
    def valid(self) -> bool:
        """这一轮算不算一个有效演进周期 —— 三条判据全过才算。"""
        return bool(self.checks) and all(item.passed for item in self.checks)

    @property
    def autonomous(self) -> bool:
        """这一轮的候选是系统自己发现的吗。"""
        return self.origin == ORIGIN_AUTONOMOUS

    @property
    def improved(self) -> bool:
        """达成度没有退步。**测不出来不算退步** —— 那是"不知道"。"""
        if self.attainment_before is None or self.attainment_after is None:
            return False
        return self.attainment_after >= self.attainment_before

    @property
    def delta(self) -> Decimal | None:
        """达成度差值 —— 后减前。**没有可比的前后值就是 ``None``**。"""
        if self.attainment_before is None or self.attainment_after is None:
            return None
        return self.attainment_after - self.attainment_before

    def to_json(self) -> dict[str, object]:
        """导出成 JSON 友好的形状。"""
        return {
            "cycle_id": self.cycle_id,
            "origin": self.origin,
            "valid": self.valid,
            "autonomous": self.autonomous,
            "improved": self.improved,
            "attainment_before": _text(self.attainment_before),
            "attainment_after": _text(self.attainment_after),
            "delta": _text(self.delta),
            "checks": [item.to_json() for item in self.checks],
        }


@dataclass(frozen=True, slots=True)
class AttainmentTrend:
    """达成度的走向 —— **逐轮比，不是只看首尾**。

    **"非负趋势"的口径在这里写死。** 序列 ``a_1 … a_n`` 满足
    ``a_{i+1} >= a_i`` 对所有 ``i`` 成立，才叫非负趋势。**只看首尾是不够
    的** —— "先崩后涨"与"一路涨上去"在首尾口径下长得一样，而这两件事对
    "系统是不是在变好"的回答完全相反（与 ``docs/architecture/trend-board.md``
    §10 记的那个缺口是同一件事）。

    **序列的头是"第一轮的轮前值"。** 十轮十一个点 —— 头一个点是长跑开始
    之前的那个数。少了它，**第一轮自己把项目做坏了就没人知道**。

    :param state: 三态。``computed`` 是算出来了；``no_samples`` 是一轮都没
        有；``baseline_missing`` 是**有轮次，但有一轮测不出达成度** —— 缺
        一个可比的数，整条序列就比不了。**这不是"持平"，更不是"退步"。**
    """

    state: MetricState
    series: tuple[Decimal, ...]
    non_decreasing: bool
    net_gain: Decimal | None
    regressions: tuple[str, ...] = ()
    reason: str = ""

    def to_json(self) -> dict[str, object]:
        """导出成 JSON 友好的形状。"""
        return {
            "state": self.state.value,
            "series": [_text(value) for value in self.series],
            "non_decreasing": self.non_decreasing,
            "net_gain": _text(self.net_gain),
            "regressions": list(self.regressions),
            "reason": self.reason,
        }


def _empty_trend() -> AttainmentTrend:
    """空趋势 —— 没有轮次时的默认。

    **必须定义在 :class:`LongRunReport` 之前。** ``default_factory`` 在**类建
    立的时候**就要能取到 —— 定义在后面的话，import 这个模块就直接炸。
    """
    return AttainmentTrend(
        state=MetricState.NO_SAMPLES,
        series=(),
        non_decreasing=False,
        net_gain=None,
        reason="一轮都没有 —— 没有可说的",
    )


@dataclass(frozen=True, slots=True)
class LongRunReport:
    """一次长跑的结论 —— **每一条都能回到某一轮**。

    :param rounds: 逐轮的判定。
    :param required_rounds: 验收要求的轮数。
    """

    rounds: tuple[CycleVerdict, ...]
    required_rounds: int = REQUIRED_ROUNDS
    trend: AttainmentTrend = field(default_factory=_empty_trend)

    @property
    def valid_cycles(self) -> int:
        """有几个轮次是有效演进周期。"""
        return sum(1 for item in self.rounds if item.valid)

    @property
    def all_valid(self) -> bool:
        """每一轮都是有效演进周期吗。"""
        return bool(self.rounds) and all(item.valid for item in self.rounds)

    @property
    def all_autonomous(self) -> bool:
        """每一轮的候选都是系统自己发现的吗。"""
        return bool(self.rounds) and all(item.autonomous for item in self.rounds)

    @property
    def enough_rounds(self) -> bool:
        """轮数够吗。"""
        return len(self.rounds) >= self.required_rounds

    @property
    def trend_non_decreasing(self) -> bool:
        """达成度呈非负趋势吗。"""
        return self.trend.state is MetricState.COMPUTED and self.trend.non_decreasing

    @property
    def accepted(self) -> bool:
        """整跑算不算通过验收。

        **四条一起过**：轮数够、每一轮都是有效演进周期、每一轮都是自主发现、
        达成度呈非负趋势。
        """
        return (
            self.enough_rounds
            and self.all_valid
            and self.all_autonomous
            and self.trend_non_decreasing
        )

    @property
    def failures(self) -> tuple[str, ...]:
        """没过的每一条 —— **没过的都要说出来**。"""
        problems: list[str] = []
        if not self.enough_rounds:
            problems.append(f"轮数不够：跑了 {len(self.rounds)} 轮，要 {self.required_rounds} 轮")
        for item in self.rounds:
            if not item.valid:
                failed = "、".join(check.name for check in item.checks if not check.passed)
                problems.append(f"第 {item.cycle_id} 轮不是有效演进周期：{failed}")
            if not item.autonomous:
                problems.append(f"第 {item.cycle_id} 轮的候选不是自主发现的")
        if not self.trend_non_decreasing:
            problems.append(f"达成度没有呈非负趋势：{self.trend.reason}")
        return tuple(problems)

    def to_json(self) -> str:
        """导出成 JSON —— **键排好序，两次导出逐字节相同**。"""
        payload = {
            "required_rounds": self.required_rounds,
            "rounds": [item.to_json() for item in self.rounds],
            "trend": self.trend.to_json(),
            "summary": {
                "valid_cycles": self.valid_cycles,
                "all_valid": self.all_valid,
                "all_autonomous": self.all_autonomous,
                "enough_rounds": self.enough_rounds,
                "trend_non_decreasing": self.trend_non_decreasing,
                "accepted": self.accepted,
            },
            "failures": list(self.failures),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        """导出成 Markdown —— 长跑记录直接抄这一段。"""
        lines: list[str] = []
        lines.append("| 轮次 | 有效演进周期 | 自主发现 | 达成度（前 → 后） | 差值 | 未过的判据 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for index, item in enumerate(self.rounds, start=1):
            failed = "、".join(check.name for check in item.checks if not check.passed)
            lines.append(
                f"| {index} | {'✓' if item.valid else '✗'} "
                f"| {'✓' if item.autonomous else '✗'} "
                f"| {_text(item.attainment_before)} → {_text(item.attainment_after)} "
                f"| {_text(item.delta)} | {failed or '—'} |"
            )
        lines.append("")
        lines.append(f"- 有效演进周期：**{self.valid_cycles} / {len(self.rounds)}**")
        lines.append(f"- 自主发现：{'是' if self.all_autonomous else '否'}")
        lines.append(
            f"- 达成度趋势：**{self.trend.reason or '—'}**（状态 `{self.trend.state.value}`）"
        )
        lines.append(f"- 验收结论：**{'通过' if self.accepted else '不通过'}**")
        if self.failures:
            lines.append("")
            lines.append("没过的：")
            lines.extend(f"- {item}" for item in self.failures)
        return "\n".join(lines)


def judge_cycle(evidence: CycleEvidence) -> CycleVerdict:
    """判一轮 —— 这个循环算不算一个"有效演进周期"。

    **三条判据各给一条结论，缺一不可。** 少一条的报告读起来像"没查那一条"。

    :param evidence: 这一轮的事实。
    """
    return CycleVerdict(
        cycle_id=evidence.cycle_id,
        origin=evidence.origin,
        checks=(
            _judge_candidate(evidence),
            _judge_decision(evidence),
            _judge_improvement(evidence),
        ),
        attainment_before=evidence.attainment_before,
        attainment_after=evidence.attainment_after,
    )


def _judge_candidate(evidence: CycleEvidence) -> CheckResult:
    """判据① —— 有一个**有证据**的候选。

    **"至少一条证据"是硬要求。** 候选可以没有证据 —— 那时候它会被写进"被
    丢掉"的清单里，而不会变成一轮演进。没有证据的候选撑不起一次演进。
    """
    if not evidence.candidates:
        return CheckResult(
            name=CANDIDATE_WITH_EVIDENCE,
            passed=False,
            reason="这一轮没有发现任何候选",
        )
    without = [item.candidate_id for item in evidence.candidates if not item.evidence_refs]
    if without:
        return CheckResult(
            name=CANDIDATE_WITH_EVIDENCE,
            passed=False,
            reason=f"{len(without)} 条候选没有证据：{'、'.join(without)}",
        )
    return CheckResult(
        name=CANDIDATE_WITH_EVIDENCE,
        passed=True,
        reason=f"{len(evidence.candidates)} 条候选，每条都带证据",
    )


def _judge_decision(evidence: CycleEvidence) -> CheckResult:
    """判据② —— 有一个**被采纳**的决策。

    **只有 ``record`` 算数。** defer 与 reject 是合法的决策，但它们没有派
    出活 —— 把它们算成"有效演进周期"，"跑了十轮"与"十轮里九轮决定什么都不
    做"就分不开了。
    """
    adopted = [
        item.candidate_id for item in evidence.candidates if item.decision == DECISION_RECORDED
    ]
    if not adopted:
        return CheckResult(
            name=ADOPTED_DECISION,
            passed=False,
            reason="这一轮没有一条被采纳的决策",
        )
    if not evidence.task_keys:
        return CheckResult(
            name=ADOPTED_DECISION,
            passed=False,
            reason=f"决策 {adopted[0]} 被采纳了，但没有派生出任务",
        )
    return CheckResult(
        name=ADOPTED_DECISION,
        passed=True,
        reason=f"采纳了 {len(adopted)} 条决策，派生出 {len(evidence.task_keys)} 个任务",
    )


def _judge_improvement(evidence: CycleEvidence) -> CheckResult:
    """判据③ —— 有一条**可验证的改善记录**。

    **三样都要在**：独立验证通过、合入主分支（有提交号）、达成度前后都测得
    出来。少一样，这条记录就不可核。
    """
    if not evidence.verification_passed:
        return CheckResult(
            name=VERIFIABLE_IMPROVEMENT,
            passed=False,
            reason="独立验证没有通过",
        )
    if not evidence.merged or not evidence.merge_commit:
        return CheckResult(
            name=VERIFIABLE_IMPROVEMENT,
            passed=False,
            reason="改动没有合入主分支 —— 工作区里的补丁不算数",
        )
    if evidence.attainment_before is None or evidence.attainment_after is None:
        return CheckResult(
            name=VERIFIABLE_IMPROVEMENT,
            passed=False,
            reason="达成度的前后值没有都测出来 —— 差值算不出来",
        )
    return CheckResult(
        name=VERIFIABLE_IMPROVEMENT,
        passed=True,
        reason=(
            f"验收通过、合入 {evidence.merge_commit[:8]}、"
            f"达成度 {_text(evidence.attainment_before)} → {_text(evidence.attainment_after)}"
        ),
    )


def build_report(
    verdicts: Sequence[CycleVerdict],
    *,
    required_rounds: int = REQUIRED_ROUNDS,
) -> LongRunReport:
    """把逐轮的判定收成一次长跑的结论。

    :param verdicts: 逐轮的判定。
    :param required_rounds: 验收要求的轮数。
    """
    rounds = tuple(verdicts)
    return LongRunReport(
        rounds=rounds,
        required_rounds=required_rounds,
        trend=build_trend(rounds),
    )


def build_trend(rounds: Sequence[CycleVerdict]) -> AttainmentTrend:
    """算达成度的走向 —— **逐轮比**。

    **缺一个可比的数就报"算不出来"。** 拿一段序列的头尾去比，缺的中间那
    几轮就被悄悄跳过了 —— 而那正是"回退藏在中间"的情形。

    **序列从第一轮的"轮前"开始。** 不是从第一轮的"轮后"开始 —— 那样的话
    **第一轮自己的退步就看不见了**：序列里没有它的轮前值，跟谁比都比不出来。
    所以序列是 ``[第一轮轮前, 第一轮轮后, 第二轮轮后, …]``，十轮十一个点。

    **净收益因此是"整跑走了多远"。** 十轮之前那个基线也在序列里，所以净收益
    算的是**从长跑开始到长跑结束** —— 而不是"第一轮之后到第十轮之后"。

    :param rounds: 逐轮的判定。
    """
    if not rounds:
        return _empty_trend()
    missing = [item.cycle_id for item in rounds if item.attainment_after is None]
    if missing:
        return AttainmentTrend(
            state=MetricState.BASELINE_MISSING,
            series=(),
            non_decreasing=False,
            net_gain=None,
            reason=(f"{len(missing)} 轮测不出达成度（{'、'.join(missing)}）—— 整条序列比不了"),
        )
    baseline = rounds[0].attainment_before
    if baseline is None:
        return AttainmentTrend(
            state=MetricState.BASELINE_MISSING,
            series=(),
            non_decreasing=False,
            net_gain=None,
            reason=f"{rounds[0].cycle_id} 轮测不出轮前达成度 —— 序列缺头，比不了",
        )
    after_values: list[Decimal] = []
    for item in rounds:
        value = item.attainment_after
        if value is None:
            continue
        after_values.append(value)
    series = (baseline, *after_values)
    regressions = _regressions(rounds, series)
    non_decreasing = not regressions
    net_gain = series[-1] - series[0]
    return AttainmentTrend(
        state=MetricState.COMPUTED,
        series=series,
        non_decreasing=non_decreasing,
        net_gain=net_gain,
        regressions=regressions,
        reason=(
            "逐轮不下降"
            if non_decreasing
            else f"{len(regressions)} 处退步：{'、'.join(regressions)}"
        ),
    )


def _regressions(
    rounds: Sequence[CycleVerdict],
    series: Sequence[Decimal],
) -> tuple[str, ...]:
    """序列里哪几处往下走了 —— **每一处都要说得清是哪两轮之间**。

    **头一处是"第一轮自己"。** 序列的头是那一轮的轮前值，尾是它的轮后值 ——
    这一处退步就是**第一轮把项目做坏了**，与后面的轮次无关。
    """
    found: list[str] = []
    for index in range(len(series) - 1):
        if series[index + 1] >= series[index]:
            continue
        if index == 0:
            found.append(f"{rounds[0].cycle_id} 轮内（轮前 → 轮后）")
        else:
            found.append(f"{rounds[index - 1].cycle_id} → {rounds[index].cycle_id}")
    return tuple(found)


def _text(value: Decimal | None) -> str:
    """把达成度渲染成一句话里的样子。

    **测不出来渲染成 ``—``，不是 0。** 0 意味着"达成度就是 0"，而 ``—``
    意味着"没测" —— 这两件事在报表上长得一样，在含义上完全相反。
    """
    if value is None:
        return "—"
    return str(value.quantize(_PRECISION))


__all__ = [
    "ADOPTED_DECISION",
    "CANDIDATE_WITH_EVIDENCE",
    "DECISION_RECORDED",
    "ORIGIN_AUTONOMOUS",
    "ORIGIN_HUMAN",
    "REQUIRED_ROUNDS",
    "VERIFIABLE_IMPROVEMENT",
    "AttainmentTrend",
    "CandidateEvidence",
    "CheckResult",
    "CycleEvidence",
    "CycleVerdict",
    "LongRunReport",
    "build_report",
    "build_trend",
    "judge_cycle",
]
