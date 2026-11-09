"""差距停滞的判定 —— 连续几轮没改善就熔断。

设计依据：docs/tasks.md TASK-P1-064（熔断规则：同一 gap 连续 N 轮未改善则
停止重规划并标记 ``stalled``；T1 ③ 同一 gap 连续 2 轮未改善 → 熔断，产生
``stalled`` 标记而非继续规划）；R-072。

**"没改善"要有判据，不能靠感觉。** 这里用**严重度**：一轮结束时看到的
差距严重度比上一轮低，就是改善了；一样或者更高，就是没改善。判据只有一个
来源，才谈得上"连续几轮"。

**"连续"两个字是这条规则的命根子。** 断断续续地没改善（好了又坏、坏了又
好）不算停滞 —— 那种项目换个方向就能起来。只有**一轮接一轮地没动**，才说明
这条路走不通了。所以计数在**改善时归零**，而不是"总共没改善过几次"。

**"轮"从基线之后数起。** 第一次看到某个差距只是**定下基线** —— 它还没来得
及被处理过，拿它去凑"没改善"的计数，等于凭空多算一轮。真正的走法是：第 1
轮定基线并排一次修复，第 2 轮判定"没改善"（一次尝试失败），第 3 轮再判定
"没改善"（两次尝试失败）才熔断。**门槛是 2 轮，熔断发生在第 3 次观察** ——
两次机会都试过了才停下来叫人。

**熔断的意思是"停下来让人看一眼"，不是"放弃"。** 所以标记是 ``stalled``
（停滞）而不是 ``rejected``（否决）：停滞的项目**能推进**，只是连续几轮
没有可验证的改善；换方向才能继续，而"换方向"通常要人参与。

**改善之后可以解除熔断。** 熔断不是永久的：人被叫来看过一眼、手动改了
一处、或者别的差距的修复顺带把它解开了 —— 那时候它就不该继续被挡着。
不然一次误判就把一个差距永久钉死。

**这一层不做 IO。** 端口在 :class:`GapProgressStore`，规则在
:func:`advance_progress` 与 :class:`StallDetector`。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from core.evolution.assessment import GapSeverity
from core.metrics.cycle import CycleOutcome

#: 连续几轮没改善就熔断。**默认是 2。** 一轮没改善可能是运气，两轮就是
#: 趋势了；而"轮"本身不便宜（要花预算、要跑执行器），所以门槛不宜高。
DEFAULT_MAX_UNCHANGED_ROUNDS: Final[int] = 2

#: 严重度的次序 —— 数字越大越严重。
#:
#: **次序写死在这里，不靠枚举的声明顺序。** ``StrEnum`` 的声明顺序是实现
#: 细节，改一次声明顺序就悄悄改了一次"什么算改善"。
SEVERITY_RANK: Final[Mapping[GapSeverity, int]] = MappingProxyType(
    {
        GapSeverity.LOW: 0,
        GapSeverity.MEDIUM: 1,
        GapSeverity.HIGH: 2,
        GapSeverity.CRITICAL: 3,
    }
)


@dataclass(frozen=True, slots=True)
class GapProgress:
    """一个差距到目前为止的进展。

    :attr:`last_severity` 为空表示**还没见过这个差距** —— 那它不是"没改善"，
    是"新出现的"。

    :attr:`stalled` 是熔断标记。**它会被一次改善解除** —— 见模块说明。
    """

    gap_id: str
    #: 上一轮看到的严重度（``None`` = 从没见过，或上一轮它已经不见了）。
    last_severity: GapSeverity | None = None
    #: 连续几轮没有改善。
    unchanged_rounds: int = 0
    #: 已经熔断了吗。
    stalled: bool = False


@runtime_checkable
class GapProgressStore(Protocol):
    """差距进展的存储口。"""

    async def get(self, gap_id: str) -> GapProgress:
        """取这个差距的进展。**没见过就返回一条空的** —— 不是 ``None``。"""
        ...

    async def put(self, progress: GapProgress) -> None:
        """写回这个差距的进展。"""
        ...

    async def stalled_gaps(self) -> tuple[str, ...]:
        """已经熔断的那些差距 id。"""
        ...


class InMemoryGapProgressStore:
    """内存实现 —— 只在一个进程内有效。"""

    __slots__ = ("_lock", "_progress")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._progress: dict[str, GapProgress] = {}

    async def get(self, gap_id: str) -> GapProgress:
        async with self._lock:
            return self._progress.get(gap_id, GapProgress(gap_id=gap_id))

    async def put(self, progress: GapProgress) -> None:
        async with self._lock:
            self._progress[progress.gap_id] = progress

    async def stalled_gaps(self) -> tuple[str, ...]:
        async with self._lock:
            return tuple(sorted(gap_id for gap_id, item in self._progress.items() if item.stalled))


@dataclass(frozen=True, slots=True)
class StallVerdict:
    """这一轮之后，这条差距是什么状态。

    :attr:`marker` 是**要记进周期记录的那个结局** —— ``stalled`` 表示熔断，
    ``improved`` 表示这一轮确实变好了，``no_change`` 表示没动但还没到熔断
    的门槛。三者缺一不可：只说"没动"，就分不出"再等等"与"该叫人来看了"。
    """

    #: 熔断了没有。
    stalled: bool
    #: 连续几轮没改善。
    unchanged_rounds: int
    #: 这一轮的结局标记。
    marker: CycleOutcome
    #: 人话理由。
    reason: str


def is_improvement(
    previous: GapSeverity | None,
    current: GapSeverity | None,
) -> bool:
    """这一轮算不算改善。

    :param previous: 上一轮的严重度（``None`` = 从没见过这个差距）。
    :param current: 这一轮的严重度（``None`` = 这个差距已经不见了）。

    **三种情况，三种算法。**

    - 从没见过 → **算改善**。新出现的差距不是"没改善" —— 它还没来得及被
      处理过，拿它去凑"连续没改善"的计数，等于凭空多算一轮。
    - 差距不见了 → **算改善**。差距消失是这一轮最硬的好消息。
    - 都在 → **严重度降了才算改善**。一样或者更严重，就是没动。
    """
    if previous is None:
        return True
    if current is None:
        return True
    return SEVERITY_RANK[current] < SEVERITY_RANK[previous]


def advance_progress(
    current: GapProgress,
    *,
    severity: GapSeverity | None,
    max_unchanged_rounds: int = DEFAULT_MAX_UNCHANGED_ROUNDS,
) -> GapProgress:
    """记一轮观察，算出这条差距现在怎么样。

    :param current: 上一轮之后的状态。
    :param severity: 这一轮的严重度（``None`` = 差距不见了）。
    :param max_unchanged_rounds: 连续几轮没改善就熔断。

    **纯函数** —— 不读时钟、不写库。规则能单独被证伪，才谈得上"这条规则
    是对的"。

    **从没见过这个差距的那一次不算"没改善"** —— 它只是定基线。理由见模块
    说明：门槛是"给几次机会"，机会不该在第一次观察就被用掉。
    """
    if max_unchanged_rounds < 1:
        raise ValueError("熔断阈值至少是 1 轮 —— 0 轮意味着永远不熔断")
    improved = is_improvement(current.last_severity, severity)
    if improved:
        # 改善了就把计数**归零** —— "连续"这两个字要求的就是归零。
        unchanged = 0
        stalled = False
    else:
        unchanged = current.unchanged_rounds + 1
        stalled = current.stalled or unchanged >= max_unchanged_rounds
    return GapProgress(
        gap_id=current.gap_id,
        last_severity=severity,
        unchanged_rounds=unchanged,
        stalled=stalled,
    )


class StallDetector:
    """差距停滞的检测器。

    :param store: 进展的存储口。
    :param max_unchanged_rounds: 连续几轮没改善就熔断。
    :param clock: 现在的时刻（留作记录用，不参与判定）。
    """

    __slots__ = ("_clock", "_max_unchanged_rounds", "_store")

    def __init__(
        self,
        store: GapProgressStore,
        *,
        max_unchanged_rounds: int = DEFAULT_MAX_UNCHANGED_ROUNDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._max_unchanged_rounds = max_unchanged_rounds
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def max_unchanged_rounds(self) -> int:
        """连续几轮没改善就熔断。"""
        return self._max_unchanged_rounds

    async def observe(self, gap_id: str, *, severity: GapSeverity | None) -> StallVerdict:
        """记一轮观察，给出这条差距现在的结论。

        :param gap_id: 差距的 id。
        :param severity: 这一轮的严重度（``None`` = 差距不见了）。
        """
        current = await self._store.get(gap_id)
        progress = advance_progress(
            current,
            severity=severity,
            max_unchanged_rounds=self._max_unchanged_rounds,
        )
        await self._store.put(progress)
        return verdict_of(progress, max_unchanged_rounds=self._max_unchanged_rounds)

    async def stalled_gaps(self) -> tuple[str, ...]:
        """已经熔断的那些差距 id —— **规划时要把它们排除掉**。"""
        return await self._store.stalled_gaps()


def verdict_of(
    progress: GapProgress,
    *,
    max_unchanged_rounds: int = DEFAULT_MAX_UNCHANGED_ROUNDS,
) -> StallVerdict:
    """把进展翻译成一句结论。

    **理由要说得清是哪一种。** "差距停滞了"没法行动；"连续 2 轮没有改善"
    才告诉人该去看哪儿。
    """
    if progress.stalled:
        return StallVerdict(
            stalled=True,
            unchanged_rounds=progress.unchanged_rounds,
            marker=CycleOutcome.STALLED,
            reason=(
                f"差距 {progress.gap_id} 连续 {progress.unchanged_rounds} 轮没有改善 —— "
                f"熔断，停止重规划"
            ),
        )
    if progress.unchanged_rounds == 0:
        return StallVerdict(
            stalled=False,
            unchanged_rounds=0,
            marker=CycleOutcome.IMPROVED,
            reason=f"差距 {progress.gap_id} 这一轮有改善",
        )
    return StallVerdict(
        stalled=False,
        unchanged_rounds=progress.unchanged_rounds,
        marker=CycleOutcome.NO_CHANGE,
        reason=(
            f"差距 {progress.gap_id} 已经 {progress.unchanged_rounds} 轮没有改善"
            f"（熔断门槛 {max_unchanged_rounds} 轮）"
        ),
    )


__all__ = [
    "DEFAULT_MAX_UNCHANGED_ROUNDS",
    "SEVERITY_RANK",
    "GapProgress",
    "GapProgressStore",
    "InMemoryGapProgressStore",
    "StallDetector",
    "StallVerdict",
    "advance_progress",
    "is_improvement",
    "verdict_of",
]
