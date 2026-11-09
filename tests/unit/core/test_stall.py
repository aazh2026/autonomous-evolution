"""TASK-P1-064 —— 差距停滞的判定。

**这里验的是"连续几轮没改善"这条规则本身。**

**为什么"改善"要有判据。** "没改善"如果靠感觉，就没法数"连续几轮" —— 而
"连续"是这条规则的命根子：断断续续地没改善不算停滞，一轮接一轮地没动才算。
所以判据只有一个来源 —— **严重度**。

**为什么"改善之后计数归零"要被钉住。** 那是"连续"两个字的直接推论；实现
很容易写成"总共没改善过几次"，而那样一个好了又坏、坏了又好的项目会被误判
成停滞。

**为什么熔断可以被解除也要被钉住。** 熔断不是永久的：人被叫来看过一眼、手
动改了一处，那条差距就不该继续被挡着。不然一次误判就把一个差距永久钉死。
"""

from __future__ import annotations

import pytest

from core.evolution.assessment import GapSeverity
from core.evolution.stall import (
    DEFAULT_MAX_UNCHANGED_ROUNDS,
    GapProgress,
    InMemoryGapProgressStore,
    StallDetector,
    advance_progress,
    is_improvement,
    verdict_of,
)
from core.metrics.cycle import CycleOutcome

_GAP = "gap-1"


class TestTheImprovement:
    """什么算"这一轮变好了"。"""

    def test_a_gap_that_was_never_seen_is_an_improvement(self) -> None:
        """**新出现的差距不是"没改善"。** 它还没来得及被处理过，拿它去凑
        "连续没改善"的计数，等于凭空多算一轮。"""
        assert is_improvement(None, GapSeverity.HIGH) is True

    def test_a_gap_that_disappeared_is_an_improvement(self) -> None:
        """**差距消失是这一轮最硬的好消息。**"""
        assert is_improvement(GapSeverity.HIGH, None) is True

    def test_a_lower_severity_is_an_improvement(self) -> None:
        assert is_improvement(GapSeverity.CRITICAL, GapSeverity.HIGH) is True

    def test_the_same_severity_is_not_an_improvement(self) -> None:
        """**一样就是没动。**"""
        assert is_improvement(GapSeverity.MEDIUM, GapSeverity.MEDIUM) is False

    def test_a_higher_severity_is_not_an_improvement(self) -> None:
        """**更严重也不算改善** —— 它是变坏，而"变坏"与"没动"在这条规则里
        一样处理：连续两轮没变好就熔断。"""
        assert is_improvement(GapSeverity.MEDIUM, GapSeverity.HIGH) is False


class TestTheProgress:
    """计数怎么走。"""

    def test_the_first_observation_is_not_stalled(self) -> None:
        """刚见到的差距不算停滞。"""
        progress = advance_progress(GapProgress(gap_id=_GAP), severity=GapSeverity.HIGH)

        assert progress.unchanged_rounds == 0
        assert progress.stalled is False

    def test_one_unchanged_round_is_not_stalled_yet(self) -> None:
        """**一轮没改善还不到熔断线** —— 门槛是 2 轮。"""
        progress = advance_progress(
            GapProgress(gap_id=_GAP, last_severity=GapSeverity.HIGH),
            severity=GapSeverity.HIGH,
        )

        assert progress.unchanged_rounds == 1
        assert progress.stalled is False

    def test_two_unchanged_rounds_stall_it(self) -> None:
        """**连续两轮没改善 → 熔断。**"""
        progress = advance_progress(
            GapProgress(gap_id=_GAP, last_severity=GapSeverity.HIGH, unchanged_rounds=1),
            severity=GapSeverity.HIGH,
        )

        assert progress.unchanged_rounds == 2
        assert progress.stalled is True

    def test_an_improvement_resets_the_counter(self) -> None:
        """**"连续"两个字要求的就是归零。**"""
        progress = advance_progress(
            GapProgress(gap_id=_GAP, last_severity=GapSeverity.HIGH, unchanged_rounds=1),
            severity=GapSeverity.MEDIUM,
        )

        assert progress.unchanged_rounds == 0
        assert progress.stalled is False

    def test_the_severity_is_remembered_for_the_next_round(self) -> None:
        """**下一轮要跟这一轮比** —— 不记下来就永远在跟第一轮比。"""
        progress = advance_progress(
            GapProgress(gap_id=_GAP, last_severity=GapSeverity.HIGH),
            severity=GapSeverity.MEDIUM,
        )

        assert progress.last_severity == GapSeverity.MEDIUM

    def test_a_threshold_below_one_is_rejected(self) -> None:
        """**0 轮门槛等于永不熔断** —— 那不叫配置，那叫把规则关掉。"""
        with pytest.raises(ValueError, match="至少是 1 轮"):
            advance_progress(
                GapProgress(gap_id=_GAP), severity=GapSeverity.HIGH, max_unchanged_rounds=0
            )

    def test_the_default_threshold_is_two(self) -> None:
        """**默认值要说得出理由** —— 一轮没改善可能是运气，两轮就是趋势了。"""
        assert DEFAULT_MAX_UNCHANGED_ROUNDS == 2


class TestTheVerdict:
    """结论的三种形态。"""

    def test_a_stalled_gap_is_marked_stalled(self) -> None:
        """**熔断的标记是 ``stalled``** —— 不是 ``rejected``。停滞的项目能
        推进，只是连续几轮没有可验证的改善；否决是"这条路彻底不通"。"""
        verdict = verdict_of(
            GapProgress(gap_id=_GAP, unchanged_rounds=2, stalled=True),
        )

        assert verdict.marker == CycleOutcome.STALLED
        assert verdict.stalled is True

    def test_a_stalled_gap_says_how_many_rounds(self) -> None:
        """**理由要说得清是哪一种。** "差距停滞了"没法行动。"""
        verdict = verdict_of(
            GapProgress(gap_id=_GAP, unchanged_rounds=2, stalled=True),
        )

        assert "2" in verdict.reason
        assert _GAP in verdict.reason

    def test_an_improved_gap_is_marked_improved(self) -> None:
        """**"变好了"与"没动"要分得开** —— 只说"没动"，就分不出"再等等"与
        "该叫人来看了"。"""
        verdict = verdict_of(GapProgress(gap_id=_GAP, unchanged_rounds=0))

        assert verdict.marker == CycleOutcome.IMPROVED

    def test_an_unchanged_gap_is_marked_no_change(self) -> None:
        """还没到熔断线，但也没改善 —— 这是第三种状态，不能省。"""
        verdict = verdict_of(GapProgress(gap_id=_GAP, unchanged_rounds=1))

        assert verdict.marker == CycleOutcome.NO_CHANGE
        assert verdict.stalled is False


class TestTheDetector:
    """检测器连着记。"""

    async def test_the_first_observation_only_sets_the_baseline(self) -> None:
        """**第一次看到差距只是定基线** —— 它还没来得及被处理过，拿它去凑
        "没改善"的计数，等于凭空多算一轮。"""
        detector = StallDetector(InMemoryGapProgressStore())

        verdict = await detector.observe(_GAP, severity=GapSeverity.HIGH)

        assert verdict.unchanged_rounds == 0
        assert verdict.stalled is False

    async def test_two_unchanged_rounds_stall_the_gap(self) -> None:
        """**T1 ③ —— 同一 gap 连续 2 轮未改善 → 熔断。**

        **门槛是 2 轮，熔断落在第 3 次观察。** 第 1 次定基线，第 2 次判定
        "没改善"（一次尝试失败），第 3 次再判定"没改善"（两次尝试失败）——
        **两次机会都试过了**才停下来叫人。门槛若按"观察次数"算，第二次观察
        就熔断，等于只给了一次机会。
        """
        detector = StallDetector(InMemoryGapProgressStore())

        baseline = await detector.observe(_GAP, severity=GapSeverity.HIGH)
        first = await detector.observe(_GAP, severity=GapSeverity.HIGH)
        second = await detector.observe(_GAP, severity=GapSeverity.HIGH)

        assert baseline.stalled is False
        assert first.stalled is False
        assert first.unchanged_rounds == 1
        assert second.stalled is True
        assert second.marker == CycleOutcome.STALLED

    async def test_the_stalled_gaps_are_listed(self) -> None:
        """**规划时要能把它们挑出来排除掉** —— 熔断不能只停在一句标记上。"""
        detector = StallDetector(InMemoryGapProgressStore())
        await detector.observe(_GAP, severity=GapSeverity.HIGH)
        await detector.observe(_GAP, severity=GapSeverity.HIGH)
        await detector.observe(_GAP, severity=GapSeverity.HIGH)

        assert await detector.stalled_gaps() == (_GAP,)

    async def test_an_improvement_lifts_the_stall(self) -> None:
        """**熔断不是永久的。** 人被叫来看过一眼、手动改了一处 —— 那条差距
        不该继续被挡着。"""
        detector = StallDetector(InMemoryGapProgressStore())
        await detector.observe(_GAP, severity=GapSeverity.HIGH)
        await detector.observe(_GAP, severity=GapSeverity.HIGH)
        await detector.observe(_GAP, severity=GapSeverity.HIGH)

        verdict = await detector.observe(_GAP, severity=GapSeverity.LOW)

        assert verdict.stalled is False
        assert await detector.stalled_gaps() == ()

    async def test_a_gap_that_disappeared_is_not_stalled(self) -> None:
        """**差距不见了是改善** —— 不该继续占着"停滞"的名单。"""
        detector = StallDetector(InMemoryGapProgressStore())
        await detector.observe(_GAP, severity=GapSeverity.HIGH)

        verdict = await detector.observe(_GAP, severity=None)

        assert verdict.marker == CycleOutcome.IMPROVED
        assert verdict.stalled is False

    async def test_two_gaps_are_counted_separately(self) -> None:
        """**熔断是按差距算的，不是按项目算的。** 一条差距停滞了不该把另一条
        一起挡掉。"""
        detector = StallDetector(InMemoryGapProgressStore())
        await detector.observe("gap-1", severity=GapSeverity.HIGH)
        await detector.observe("gap-1", severity=GapSeverity.HIGH)
        await detector.observe("gap-1", severity=GapSeverity.HIGH)
        await detector.observe("gap-2", severity=GapSeverity.HIGH)

        assert await detector.stalled_gaps() == ("gap-1",)

    async def test_the_detector_keeps_the_documented_threshold(self) -> None:
        """门槛是配置项，但**默认值得是那个说得出理由的 2**。"""
        detector = StallDetector(InMemoryGapProgressStore())

        assert detector.max_unchanged_rounds == DEFAULT_MAX_UNCHANGED_ROUNDS
