"""TASK-P1-064 —— 事件去重窗口与因果链的领域契约。

**这里验的是两条规则本身，不依赖数据库。**

1. **去重窗口** —— 同一种事件在同一个主体上，窗口内只算一次。
2. **因果链** —— 链上有演进循环，这条事件就是回环。

**为什么"窗口"要能被证伪。** 没有窗口，系统要么被重复触发淹死（一次持续
集成失败点着五十轮演进），要么被去重误伤（隔一天的同一个故障被当成重复）。
中间那条线只能靠"窗口有多长"这个数来定，所以它得能被单独测。

**为什么"占窗口"要单独验一次原子性。** 分成"查上次是什么时候"加"写这次"
两步的话，两个并发的同种事件会各自看到"没人占着"然后各自占上 —— 两轮演进
被点着，而**没有任何一个会报错**。所以这条用例盯的是"五个并发里恰好一个
算数"。

**为什么回环判定要盯"链的深处"。** 回环的形状是"演进→执行→回写"三环：
判定要是只看最近那一环（回写），就只会挡住直白的回环，而挡不住"回写之后
又执行又回写"那种绕回来的。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from core.events.dedup import (
    DEFAULT_DEDUP_WINDOW,
    CausationLink,
    CausationTracker,
    ChainKind,
    EventDedupWindow,
    EventFingerprint,
    InMemoryCausationChainStore,
    InMemoryDedupWindowStore,
)

#: 固定时刻。**所有涉及时间的断言都用它** —— 真等十五分钟不现实，而"把钟
#: 拨过去"正是窗口到期这件事本身。
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class _FakeClock:
    """能往前拨的钟。"""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _fingerprint(
    event_type: str = "ci.failed",
    aggregate_id: str = "pipeline-1",
) -> EventFingerprint:
    return EventFingerprint(event_type=event_type, aggregate_id=aggregate_id)


class TestTheDedupWindow:
    """同一种事件在同一个主体上，窗口内只算一次。"""

    async def test_the_first_event_counts(self) -> None:
        """第一次当然算数 —— 否则什么事件都不会触发任何事。"""
        window = EventDedupWindow(InMemoryDedupWindowStore(), clock=_FakeClock())

        decision = await window.admit(_fingerprint())

        assert decision.duplicate is False

    async def test_the_same_event_within_the_window_is_a_duplicate(self) -> None:
        """**同一种事件、同一个主体、窗口内 —— 只算一次。**"""
        window = EventDedupWindow(InMemoryDedupWindowStore(), clock=_FakeClock())
        await window.admit(_fingerprint())

        decision = await window.admit(_fingerprint())

        assert decision.duplicate is True

    async def test_the_same_event_after_the_window_counts_again(self) -> None:
        """**窗口过了就是新信息。** 同一个故障隔一天再报一次，可能是环境变了
        —— 把它当重复处理掉，就永远等不到那一次真的变化。"""
        clock = _FakeClock()
        window = EventDedupWindow(InMemoryDedupWindowStore(), clock=clock)
        await window.admit(_fingerprint())

        clock.advance(DEFAULT_DEDUP_WINDOW + timedelta(seconds=1))
        decision = await window.admit(_fingerprint())

        assert decision.duplicate is False

    async def test_a_different_aggregate_is_not_a_duplicate(self) -> None:
        """**去重粒度是 (种类, 主体)。** 另一条流水线挂了就是另一件事 ——
        把它挡住，下一条流水线的问题就没人管了。"""
        window = EventDedupWindow(InMemoryDedupWindowStore(), clock=_FakeClock())
        await window.admit(_fingerprint(aggregate_id="pipeline-1"))

        decision = await window.admit(_fingerprint(aggregate_id="pipeline-2"))

        assert decision.duplicate is False

    async def test_a_different_event_type_is_not_a_duplicate(self) -> None:
        """同一个主体上的另一种事件 —— 也是另一件事。"""
        window = EventDedupWindow(InMemoryDedupWindowStore(), clock=_FakeClock())
        await window.admit(_fingerprint(event_type="ci.failed"))

        decision = await window.admit(_fingerprint(event_type="ci.passed"))

        assert decision.duplicate is False

    async def test_the_duplicate_says_why(self) -> None:
        """**理由缺了，日志里就只剩一句"没算数"** —— 而那看起来像故障。"""
        window = EventDedupWindow(InMemoryDedupWindowStore(), clock=_FakeClock())
        await window.admit(_fingerprint())

        decision = await window.admit(_fingerprint())

        assert decision.reason.strip() != ""
        assert "窗口" in decision.reason

    async def test_the_decision_carries_the_fingerprint(self) -> None:
        """结论要说得清是**哪一条**被挡了 —— 不然排查时不知道该看哪儿。"""
        window = EventDedupWindow(InMemoryDedupWindowStore(), clock=_FakeClock())

        decision = await window.admit(_fingerprint())

        assert decision.fingerprint == _fingerprint()

    async def test_only_one_of_five_concurrent_admissions_counts(self) -> None:
        """**占窗口必须是一个原子动作。** 分成"查上次"加"写这次"两步的话，
        五个并发的同种事件会各自看到"没人占着"然后各自占上 —— 五轮演进被
        点着，而**没有任何一个会报错**。"""
        window = EventDedupWindow(InMemoryDedupWindowStore(), clock=_FakeClock())

        decisions = await asyncio.gather(*(window.admit(_fingerprint()) for _ in range(5)))

        assert sum(1 for item in decisions if not item.duplicate) == 1

    def test_the_window_length_is_the_documented_one(self) -> None:
        """**默认值要说得出理由** —— 一个"15"看着像随手写的。"""
        assert timedelta(minutes=15) == DEFAULT_DEDUP_WINDOW

    def test_a_blank_event_type_is_rejected(self) -> None:
        """空的种类会让"是不是同一种"永远成立 —— 那就等于什么都不去重。"""
        with pytest.raises(ValueError, match="种类"):
            EventFingerprint(event_type="  ", aggregate_id="pipeline-1")

    def test_a_blank_aggregate_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="主体"):
            EventFingerprint(event_type="ci.failed", aggregate_id="")

    def test_the_fingerprint_reads_as_a_pair(self) -> None:
        """指纹要能一眼看出是"哪件事"。"""
        assert str(_fingerprint()) == "ci.failed@pipeline-1"


class TestTheCausationChain:
    """因果链与回环判定。"""

    async def test_a_chain_without_an_evolution_link_is_not_an_echo(self) -> None:
        """**外部触发不是回环。** 人点的、定时器触发的、外部系统推来的 ——
        它们都该开新一轮。"""
        tracker = CausationTracker(InMemoryCausationChainStore())
        await tracker.start(causation_id="E1", kind=ChainKind.ROOT)

        assert await tracker.is_echo("E1") is False

    async def test_no_chain_at_all_is_not_an_echo(self) -> None:
        """没有链就是外面来的 —— 挡它就等于系统再也不会自己动。"""
        tracker = CausationTracker(InMemoryCausationChainStore())

        assert await tracker.is_echo(None) is False

    async def test_a_chain_with_an_evolution_link_is_an_echo(self) -> None:
        """**链上有演进循环 → 这是我们自己触发的后果。**"""
        tracker = CausationTracker(InMemoryCausationChainStore())
        await tracker.start(causation_id="C1", kind=ChainKind.EVOLUTION)

        assert await tracker.is_echo("C1") is True

    async def test_the_whole_chain_is_walked(self) -> None:
        """**判定要看链的深处。** 回环的形状是"演进→执行→回写"三环：只看最近
        那一环（回写），就只会挡住直白的回环，而挡不住绕回来的那种。"""
        tracker = CausationTracker(InMemoryCausationChainStore())
        await tracker.start(causation_id="C1", kind=ChainKind.EVOLUTION)
        await tracker.derive(causation_id="X1", parent_id="C1", kind=ChainKind.EXECUTION)
        await tracker.derive(causation_id="W1", parent_id="X1", kind=ChainKind.WRITEBACK)

        assert await tracker.is_echo("W1") is True

    async def test_a_broken_chain_still_looks_at_what_is_there(self) -> None:
        """**链断了就断在断的地方** —— 但已经记下的那一环仍然算数。进程重启
        之后内存里的链没了，可那一环的 ``kind`` 还在事件上。"""
        tracker = CausationTracker(InMemoryCausationChainStore())
        await tracker.derive(causation_id="W1", parent_id="X1", kind=ChainKind.WRITEBACK)

        assert await tracker.is_echo("W1") is False
        assert len(await tracker.chain("W1")) == 1

    async def test_a_chain_that_loops_does_not_hang(self) -> None:
        """**链里可能有环。** 不兜底的话这个 while 会永远转下去 —— 那不是
        "判错"，是整个编排进程卡死。"""
        tracker = CausationTracker(InMemoryCausationChainStore())
        await tracker.derive(causation_id="A1", parent_id="B1", kind=ChainKind.EXECUTION)
        await tracker.derive(causation_id="B1", parent_id="A1", kind=ChainKind.EXECUTION)

        links = await tracker.chain("A1")

        assert len(links) == 2

    async def test_the_chain_is_returned_from_the_link_upwards(self) -> None:
        """**顺序是"从这一环到链头"** —— 读的人要能顺着看下来。"""
        tracker = CausationTracker(InMemoryCausationChainStore())
        await tracker.start(causation_id="C1", kind=ChainKind.ROOT)
        await tracker.derive(causation_id="X1", parent_id="C1", kind=ChainKind.EXECUTION)

        links = await tracker.chain("X1")

        assert [link.causation_id for link in links] == ["X1", "C1"]

    def test_a_blank_causation_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="因果链 id"):
            CausationLink(causation_id="", parent_id=None, kind=ChainKind.ROOT)

    def test_an_empty_parent_id_is_rejected(self) -> None:
        """**空串与没有父链是两回事。** 空串会让"链头"变得说不清 —— 而说不清
        的链，回环判定就时好时坏。"""
        with pytest.raises(ValueError, match="父链 id"):
            CausationLink(causation_id="C1", parent_id="", kind=ChainKind.ROOT)
