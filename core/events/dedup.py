"""事件去重窗口与因果链的领域契约。

设计依据：docs/tasks.md TASK-P1-064（T1 ① 同一事件重复投递只被消费一次；
T1 ② 演进循环触发的执行，其结果回写不再触发新一轮演进循环）；I-11（防止
"执行结果回写 → 再次触发演进循环"的无限回环）；R-072。

**去重窗口与 outbox 的幂等是两件事。** TASK-P1-025 的幂等按 ``event_id``
去重 —— 同**一条**事件投递两次只处理一次。这里按
``(event_type, aggregate_id)`` 去重 —— 同**一种**事件在同一个主体上短时间
里连着来，只算一次。前者防的是"重复投递"，后者防的是"重复触发"。

**为什么后者也必须有。** 持续集成失败一次是一条事件，失败五十次就是五十条
事件 —— 而它们说的是同一件事。若每条都触发一轮演进，一次偶发的连续失败会
把演进循环点着五十次：预算烧光、任务堆成山，而项目一点没变好。

**"窗口"是关键。** 同一件事隔一天再报一次，**那是新信息** —— 上一次已经
处理过了，这次可能是环境变了。窗口把"同一次故障的重复报警"与"新的一次
故障"分开；没有窗口，系统要么被重复触发淹死，要么被去重误伤。

**因果链记的是"这件事是谁引起的"。** 回环的形状是固定的：演进循环发起执行
→ 执行结果回写 → 回写再触发演进循环 → …… 每一轮回环都从"回写"开始，而
回写带着它上游那条链。**只要链上有演进循环，这一轮就不该再开新循环。**

**为什么不能靠"标记已处理"来挡。** 那样挡不住**别的**形状的回环 —— 比如
A 事件的回写触发 B 事件的演进，B 的执行结果又触发 A 的演进。因果链记的是
"这条事件的源头在哪"，比"我处理过什么"能挡的范围大。

**因果链 id 必须一路带着。** 演进 → 执行 → 回写，每一步都要把自己那环的 id
传下去；少传一步，链就断了，而断了的链看起来跟"外部触发"一模一样 ——
于是回环又回来了。

**这一层不做 IO。** 端口在 :class:`DedupWindowStore` 与
:class:`CausationChainStore`，规则在 :class:`EventDedupWindow` 与
:class:`CausationTracker`；写库的实现放在 ``infrastructure/``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

#: 去重窗口的默认长度。**说得出理由的数字。** 十五分钟：短于一次持续集成
#: 重试的间隔（那样一次故障只会被算一次），长于一轮演进从触发到出结论的
#: 时间（那样同一件事不会在两轮之间被算两次）。
DEFAULT_DEDUP_WINDOW: Final[timedelta] = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class EventFingerprint:
    """事件的语义指纹 —— (种类, 主体)。

    **它不是事件的唯一标识。** 唯一标识是 ``event_id``（TASK-P1-025 用它做
    幂等）。这个指纹是"说的是同一件事吗"的判断依据 —— 同一主体上的同一种
    事件，在窗口内只算一次。
    """

    event_type: str
    aggregate_id: str

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("事件的种类不能是空白")
        if not self.aggregate_id.strip():
            raise ValueError("事件的主体不能是空白")

    def __str__(self) -> str:
        return f"{self.event_type}@{self.aggregate_id}"


@runtime_checkable
class DedupWindowStore(Protocol):
    """去重窗口的存储口 —— **占窗口是一个原子动作**。

    **为什么不能分成"查上次是什么时候"加"写这次"两步。** 两步之间只要有
    让出控制权的缝隙（``await``），两个并发的同种事件就会各自看到"没人占
    着"然后各自占上 —— 两轮演进被点着，而**没有任何一个会报错**。所以这个
    端口只有一个方法，把检查与占用合成一步。
    """

    async def claim(
        self,
        fingerprint: EventFingerprint,
        *,
        at: datetime,
        window: timedelta,
    ) -> bool:
        """原子地占这个窗口。

        :param at: 这一刻。
        :param window: 窗口有多长。
        :return: 抢到了（这是窗口内的第一次）返回 ``True``；窗口里已经有过
            一次返回 ``False``。
        """
        ...


class InMemoryDedupWindowStore:
    """内存实现 —— 只在一个进程内有效。

    **跨进程不成立这件事写在代码里，不靠记忆。** 多个编排器进程各有一份
    窗口表，就各去重各的；阶段一一个项目由一个编排器进程负责，所以这一版
    够用。真要跨进程，端口不变，实现换成数据库上的唯一约束。

    **锁不是为了并发，是为了把"这是原子的"这件事写进代码。** 检查与占用
    之间不能有 ``await`` —— 而这一点靠人记着，迟早会在某次重构里被破坏。
    """

    __slots__ = ("_last_seen", "_lock")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_seen: dict[EventFingerprint, datetime] = {}

    async def claim(
        self,
        fingerprint: EventFingerprint,
        *,
        at: datetime,
        window: timedelta,
    ) -> bool:
        async with self._lock:
            previous = self._last_seen.get(fingerprint)
            if previous is not None and at - previous < window:
                return False
            self._last_seen[fingerprint] = at
            return True


@dataclass(frozen=True, slots=True)
class DedupDecision:
    """一条事件要不要算数。

    :attr:`reason` 是人话 —— "为什么没算数"是排查时第一个要看的东西。
    """

    #: 是不是窗口内的重复。
    duplicate: bool
    #: 为什么。
    reason: str
    #: 这条事件的指纹。
    fingerprint: EventFingerprint


class EventDedupWindow:
    """事件去重窗口 —— 同一种事件在同一个主体上，窗口内只算一次。

    :param store: 窗口的存储口。
    :param window: 窗口有多长。
    :param clock: 现在的时刻。**默认取系统时钟**；测试里换成能拨的假钟 ——
        真等十五分钟不现实，而"把钟拨过去"正是窗口到期这件事本身。
    """

    __slots__ = ("_clock", "_store", "_window")

    def __init__(
        self,
        store: DedupWindowStore,
        *,
        window: timedelta = DEFAULT_DEDUP_WINDOW,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._window = window
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def window(self) -> timedelta:
        """窗口有多长。"""
        return self._window

    async def admit(self, fingerprint: EventFingerprint) -> DedupDecision:
        """这条事件要不要算数 —— **并占住窗口**。

        **占窗口与判断是一件事。** 分开的话，两个并发事件会各自看到"窗口里
        没人"然后各自算数 —— 于是同一件事开了两轮演进。
        """
        now = self._clock()
        claimed = await self._store.claim(fingerprint, at=now, window=self._window)
        if claimed:
            return DedupDecision(
                duplicate=False,
                reason="窗口内的第一次 —— 算数",
                fingerprint=fingerprint,
            )
        return DedupDecision(
            duplicate=True,
            reason=f"去重窗口（{self._window}）内已经有同一种事件了 —— 不算数",
            fingerprint=fingerprint,
        )


class ChainKind(StrEnum):
    """因果链上一环的性质。

    **四 kinds，缺一不可。** ``root`` 是链头 —— 事情从这儿开始；``evolution``
    是演进循环发起的；``execution`` 是执行发起的；``writeback`` 是执行结果
    回写。**``evolution`` 是回环判定的关键** —— 链上有它，就说明这条事件是
    系统自己触发的后果。
    """

    #: 链头 —— 事情从这儿开始（人、定时器、外部系统）。
    ROOT = "root"
    #: 演进循环发起的一环。
    EVOLUTION = "evolution"
    #: 执行发起的一环。
    EXECUTION = "execution"
    #: 执行结果回写的一环。
    WRITEBACK = "writeback"


@dataclass(frozen=True, slots=True)
class CausationLink:
    """因果链上的一环。

    :attr:`parent_id` 为空表示这是链头。**它不能为空字符串** —— 那与"链头"
    分不清，而分不清的链会让回环判定时好时坏。
    """

    causation_id: str
    parent_id: str | None
    kind: ChainKind

    def __post_init__(self) -> None:
        if not self.causation_id.strip():
            raise ValueError("因果链 id 不能是空白")
        if self.parent_id is not None and not self.parent_id.strip():
            raise ValueError("父链 id 不能是空字符串 —— 空串与没有父链是两回事")


@runtime_checkable
class CausationChainStore(Protocol):
    """因果链的存储口。"""

    async def put(self, link: CausationLink) -> None:
        """记下一环。"""
        ...

    async def get(self, causation_id: str) -> CausationLink | None:
        """取一环。**没有就是没有** —— 返回 ``None``，不猜。"""
        ...


class InMemoryCausationChainStore:
    """内存实现 —— 只在一个进程内有效（同 :class:`InMemoryDedupWindowStore`）。"""

    __slots__ = ("_links",)

    def __init__(self) -> None:
        self._links: dict[str, CausationLink] = {}

    async def put(self, link: CausationLink) -> None:
        self._links[link.causation_id] = link

    async def get(self, causation_id: str) -> CausationLink | None:
        return self._links.get(causation_id)


class CausationTracker:
    """因果链的记录与查询。

    :param store: 链的存储口。
    """

    __slots__ = ("_store",)

    def __init__(self, store: CausationChainStore) -> None:
        self._store = store

    async def start(
        self,
        *,
        causation_id: str,
        kind: ChainKind = ChainKind.ROOT,
    ) -> CausationLink:
        """开一条链 —— 没有父链。

        **链头默认是 ``root``。** 那意味着"这件事是从外面来的" —— 人、
        定时器、外部系统。链头是 ``evolution`` 的意思是"这件事是我们自己
        开的循环引起的"，那就已经是回环了。
        """
        link = CausationLink(causation_id=causation_id, parent_id=None, kind=kind)
        await self._store.put(link)
        return link

    async def derive(
        self,
        *,
        causation_id: str,
        parent_id: str,
        kind: ChainKind,
    ) -> CausationLink:
        """在父链下面挂一环。

        **父链不用存在。** 链可能被截断（比如进程重启后内存里的链没了）——
        那时这一环就成了新的链头，而它仍然带着自己的 ``kind``，回环判定不会
        因为链断了就放过它。
        """
        link = CausationLink(causation_id=causation_id, parent_id=parent_id, kind=kind)
        await self._store.put(link)
        return link

    async def chain(self, causation_id: str) -> tuple[CausationLink, ...]:
        """从这一环一路往上到链头。

        **链断了就断在断的地方。** 找不到父链时停在那儿 —— 猜一个父链只会
        让判定基于不存在的事实。

        **链里可能有环。** 兜底断在第一次重复出现的那一环 —— 不然这个
        while 会永远转下去。
        """
        links: list[CausationLink] = []
        seen: set[str] = set()
        current: str | None = causation_id
        while current is not None:
            if current in seen:
                break
            seen.add(current)
            link = await self._store.get(current)
            if link is None:
                break
            links.append(link)
            current = link.parent_id
        return tuple(links)

    async def is_echo(self, causation_id: str | None) -> bool:
        """这条事件是不是我们自己触发的后果 —— **回环**。

        :param causation_id: 事件带的因果链 id。没有链（``None``）就是外部
            触发的，不是回环。

        **判据是"链上有演进循环"，不是"链头是演进循环"。** 一次由定时器
        触发的演进也会在链上留下一环 ``evolution`` —— 它上游还有定时器那
        一环，但**这一轮的事件确实是这次演进的后果**，回写它就不该再开
        新一轮。只看链头的话，每次演进都是"外部触发"，回环就挡不住。
        """
        if causation_id is None:
            return False
        return any(link.kind is ChainKind.EVOLUTION for link in await self.chain(causation_id))


__all__ = [
    "DEFAULT_DEDUP_WINDOW",
    "CausationChainStore",
    "CausationLink",
    "CausationTracker",
    "ChainKind",
    "DedupDecision",
    "DedupWindowStore",
    "EventDedupWindow",
    "EventFingerprint",
    "InMemoryCausationChainStore",
    "InMemoryDedupWindowStore",
]
