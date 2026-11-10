"""观察编排 —— 六路各读各的，拼成项目此刻的样子。

设计依据：docs/design.md §3 第一层（项目状态观察的六类数据来源）；
docs/tasks.md TASK-P3-013（Observe 层六类数据源接入）。

**这一层只做三件事**：跑每一路、把结果收进快照、把跑不成的记成失败。

**"跑不成"不往上抛。** 一路数据源读不成（测试套件起不来、台账格式变到认不
出来、索引建不起来），**那一路就是失败，不是整个流程失败**。理由很实在：
六路里有五路正常的时候，报告"这一路坏了"比让整个流程崩掉有用得多。**但
失败必须留痕** —— 快照里那一行的 ``detail`` 写着它为什么坏，**缺实测值是
失败，不是 0**。

**每一类观察的状态只有一个。** 同一类可能接了好几个来源（比如两个测试套
件），那它们的观察合起来、状态取最乐观的那个（有一个读到了就是读到了）。
**为什么取最乐观**：两个套件里有一个挂了，另一个读到了东西 —— 那"这一类
观察到了没有"的答案是"观察到了"。但另一个套件坏掉这件事不能丢，所以它
的细节会并进这一行的 ``detail`` 里。**"读到了"与"全都坏了"不能混成一句
话。**

**它不判断这些观察是不是机会。** 那是发现层的事（:mod:`orchestration.discover`）。
这一层只说"现在是什么样"。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.evolution.observe import (
    Observation,
    ObservationKind,
    ObservationSnapshot,
    ObservationSource,
    SourceOutcome,
    SourceStatus,
)


@dataclass
class Observer:
    """观察编排器 —— 把六路观察拼成一张快照。

    :param sources: 观察来源。**可以有多路同类**（比如两个测试套件）。
    :param project_id: 这是哪个项目。
    :param clock: 现在的时刻。**不给就用系统时钟。**
    :param statuses: 上一轮留下的状态（**上一次观察的快照**）。**不给就
        不留痕** —— 这一层不负责跨轮的记忆，那要落库。

    **它只读不写。** 除了记状态，它不碰任何东西。
    """

    sources: tuple[ObservationSource, ...]
    project_id: str
    clock: Callable[[], datetime] | None = None
    statuses: tuple[SourceStatus, ...] = field(default=())

    def observe(self) -> ObservationSnapshot:
        """跑一遍六路，拼成快照。"""
        observations: list[Observation] = []
        collected: dict[ObservationKind, list[SourceStatus]] = {}
        for source in self.sources:
            status, found = self._observe_one(source)
            collected.setdefault(source.kind, []).append(status)
            observations.extend(found)
        return ObservationSnapshot(
            project_id=self.project_id,
            observed_at=self._now(),
            observations=tuple(observations),
            statuses=tuple(self._merge(kind, rows) for kind, rows in collected.items()),
        )

    def _observe_one(
        self, source: ObservationSource
    ) -> tuple[SourceStatus, tuple[Observation, ...]]:
        """跑一路 —— 状态与内容一起拿回来。

        **跑不成就是失败，不是空。** 异常在这里被接住 —— 但原因一字不落地
        留在 ``detail`` 里。

        **只跑一遍。** 跑一遍 pytest 要几百毫秒 —— 判"有没有东西"和取内容
        分成两次调用，这一路就要读两遍，那不是省事，是把成本翻倍。
        """
        try:
            observations = source.observe()
        except Exception as error:  # 读不成本身就是结果，要记下来
            return (
                SourceStatus(
                    kind=source.kind,
                    outcome=SourceOutcome.FAILED,
                    detail=f"{type(error).__name__}: {error}",
                ),
                (),
            )
        if observations:
            return SourceStatus(kind=source.kind, outcome=SourceOutcome.OBSERVED), observations
        return SourceStatus(kind=source.kind, outcome=SourceOutcome.EMPTY), ()

    def _merge(self, kind: ObservationKind, rows: list[SourceStatus]) -> SourceStatus:
        """把同一类的几路合成一行。"""
        if any(row.outcome is SourceOutcome.OBSERVED for row in rows):
            outcome = SourceOutcome.OBSERVED
        elif any(row.outcome is SourceOutcome.FAILED for row in rows):
            outcome = SourceOutcome.FAILED
        else:
            outcome = SourceOutcome.EMPTY
        details = [row.detail for row in rows if row.detail]
        return SourceStatus(kind=kind, outcome=outcome, detail="；".join(details))

    def _now(self) -> datetime:
        """现在。"""
        if self.clock is not None:
            return self.clock()
        return datetime.now(UTC)


def build_observer(
    sources: Iterable[ObservationSource],
    *,
    project_id: str,
    clock: Callable[[], datetime] | None = None,
) -> Observer:
    """造一个观察编排器。"""
    return Observer(sources=tuple(sources), project_id=project_id, clock=clock)


__all__ = ["Observer", "build_observer"]
