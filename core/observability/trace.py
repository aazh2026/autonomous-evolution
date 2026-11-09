"""执行轨迹的领域口径 —— **端口在领域层，实现在基础设施层**。

设计依据：docs/tasks.md TASK-P1-073（T1 ① 一次任务执行后可按 ``task_id``
检索到完整轨迹；T1 ② 轨迹中的工具调用数与 ``ExecutionEvent`` 记录数一致）、
R-062（每次工具调用记录主体 / 任务 / 资源 / 结果）、docs/design.md §8
（OpenTelemetry + Langfuse 必选）。

**为什么轨迹要有领域层。** 轨迹不是"日志的另一种写法" —— 它要回答 R-062 点名
的四样：**主体**（谁做的）、**任务**（替哪个任务做的）、**资源**（动了什么）、
**结果**（成了没成）。而"什么算一条轨迹""一次工具调用算不算一条""数工具
调用该数哪些事件"是**口径**问题，不是 OTel 的问题 —— 口径写在基础设施层，
换个观测后端就得再抄一遍，而抄的那一遍迟早抄歪。所以口径在这里，
OTel 与 Langfuse 只是它的两种写法。

**三个标识，缺一不可 —— 但不是每条都三个都有。** ``task_id`` 说"这是哪个
任务的活"，``cycle_id`` 说"这活属于哪一轮演进"，``agent_session_id`` 说
"是谁干的"。**每类条目必须带哪些，由 :data:`REQUIRED_IDS` 定死**：Agent 会话
与工具调用必须带主体与任务，验证运行必须带任务，决策节点必须带周期 ——
决策节点没有任务（它还没造出任务），任务也没有周期（它不属于任何一轮演进）。

**为什么不能让标识"缺着也行"。** 缺了 task_id 的工具调用，事后没人能说清
它替哪个任务做的；缺了 session 的会话，没人说得清是谁开的。**缺的那个标识
不会有人发现 —— 报表照出，而报表上那一栏本来就是空的。**

**记不下来的与记下来的要分得开。** :class:`TraceObserver` 的返回值是"这次
真的记下来了吗" —— 观测系统连不上时它返回 ``False``、**不抛异常**
（观测不可用不得阻塞主流程），但"没记下来"这件事要能被问出来，否则缺的那
段轨迹与"这段时间没发生事"分不开。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable


class TraceError(ValueError):
    """轨迹口径的错 —— **在写下去之前就报**。"""


class TraceKind(StrEnum):
    """轨迹条目的类别。"""

    #: 一次 Agent 会话 —— **主体是谁**。
    AGENT_SESSION = "agent.session"
    #: 一次工具调用 —— R-062 的四样：主体 / 任务 / 资源 / 结果。
    TOOL_CALL = "tool.call"
    #: 一次验证运行。
    VERIFICATION_RUN = "verification.run"
    #: 一个决策节点 —— **记了 defer/reject 也算**（"不行动"同样要留痕）。
    DECISION_NODE = "decision.node"
    #: 一次执行里的其它事件（文字、结果等）。
    EXECUTION_EVENT = "execution.event"


#: 表示"一次工具调用**开始**"的事件类别 —— **数工具调用就数这些**。
#:
#: 只数开始、不数结束：结束事件是同一次调用的结果。数两次就翻倍了。
TOOL_CALL_KINDS: Final[frozenset[str]] = frozenset({"tool.started"})

#: 表示"一次工具调用**结束**"的事件类别 —— **结果从这些事件里取**。
TOOL_RESULT_KINDS: Final[frozenset[str]] = frozenset({"tool.completed"})

#: 每一类条目**必须**带的标识。
#:
#: **这是"每个 Agent 会话、验证运行、决策节点都带 task_id/cycle_id/
#: agent_session_id"那句话的执行处** —— 不是靠约定，是靠这张表：缺了就报错，
#: 而报错发生在写下去之前。
REQUIRED_IDS: Final[Mapping[TraceKind, tuple[str, ...]]] = MappingProxyType(
    {
        TraceKind.AGENT_SESSION: ("task_id", "agent_session_id"),
        TraceKind.TOOL_CALL: ("task_id", "agent_session_id"),
        TraceKind.VERIFICATION_RUN: ("task_id",),
        TraceKind.DECISION_NODE: ("cycle_id",),
    }
)


@dataclass(frozen=True, slots=True)
class TraceContext:
    """一次轨迹的身份 —— 三个标识。

    **三个都不是必填，但至少要有一个。** 一条谁都不属于的轨迹没有意义 ——
    记下来也检索不到。
    """

    #: 任务的标识。
    task_id: str | None = None
    #: 演进周期的标识。
    cycle_id: str | None = None
    #: Agent 会话的标识（执行器那边那个）。
    agent_session_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "cycle_id", "agent_session_id"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise TraceError(f"{name} 不能是空白")
        if not self.attributes():
            raise TraceError("轨迹至少要带一个标识：task_id / cycle_id / agent_session_id")

    def attributes(self) -> dict[str, str]:
        """三个标识 —— **只放有的那些**。

        **不放空串。** 属性表里有一个空串，看起来像"这个标识是空的"，而
        "这个标识没有"与"这个标识是空的"是两回事。
        """
        result: dict[str, str] = {}
        if self.task_id is not None:
            result["task_id"] = self.task_id
        if self.cycle_id is not None:
            result["cycle_id"] = self.cycle_id
        if self.agent_session_id is not None:
            result["agent_session_id"] = self.agent_session_id
        return result

    def missing(self, names: tuple[str, ...]) -> tuple[str, ...]:
        """``names`` 里**没带**的那些。"""
        present = self.attributes()
        return tuple(name for name in names if name not in present)


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """轨迹里的一条 —— **最小单位**。"""

    #: 这一条是什么。
    kind: TraceKind
    #: 这一条叫什么（人看的名字）。
    name: str
    #: 这一条属于谁。
    context: TraceContext
    #: 这一条的细节。
    attributes: Mapping[str, object] = field(default_factory=dict)
    #: 会话内的顺序号。
    sequence: int = 0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise TraceError("轨迹条目的名字不能是空白")
        missing = self.context.missing(REQUIRED_IDS.get(self.kind, ()))
        if missing:
            raise TraceError(
                f"{self.kind.value} 必须带 {'、'.join(missing)} —— "
                "缺了它，事后没人能说清这条是谁做的"
            )
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class ToolCallTally:
    """工具调用的两笔账 —— **对不上就是轨迹漏了东西**。

    **两笔账来自两条独立的路。** ``recorded`` 是事件流里数出来的，
    ``reported`` 是执行器自己报上来的。拿同一条路的两个数互相比，比出来的
    永远是 0 —— 那是自证，不是核对。

    **``reported`` 可以没有。** 适配器不一定报这个数；报不出来就说没有，
    不拿 0 冒充（"报了 0 次"与"没报"是两件事）。
    """

    #: 事件流里数出来的。
    recorded: int = 0
    #: 执行器报上来的。
    reported: int | None = None

    @property
    def agrees(self) -> bool:
        """两笔账对得上吗。

        **没报就不算对不上。** "没报"不是"报了 0" —— 后者会跟"真的一次都
        没调"分不开。
        """
        return self.reported is None or self.reported == self.recorded


@dataclass(frozen=True, slots=True)
class TaskTrace:
    """一个任务的完整轨迹。"""

    #: 这条轨迹属于谁。
    context: TraceContext
    #: 轨迹里的每一条。
    records: tuple[TraceRecord, ...] = ()
    #: 工具调用的两笔账。
    tool_calls: ToolCallTally = ToolCallTally()

    @property
    def task_id(self) -> str | None:
        """按 ``task_id`` 检索的键 —— **决策节点没有任务**。"""
        return self.context.task_id

    @property
    def cycle_id(self) -> str | None:
        return self.context.cycle_id

    def by_kind(self, kind: TraceKind) -> tuple[TraceRecord, ...]:
        """某一类的条目。"""
        return tuple(item for item in self.records if item.kind is kind)


@runtime_checkable
class TraceableEvent(Protocol):
    """一条可入轨迹的事件。

    **结构化地符合 :class:`executors.base.ExecutionEvent`** —— 领域层不 import
    执行器层，而是**说出自己要什么**：一个顺序号、一个类别、一个负载。
    执行器那边照着给就行，换个执行器也不影响这里。
    """

    @property
    def seq(self) -> int:
        """会话内的顺序号。"""
        ...

    @property
    def kind(self) -> str:
        """事件类别。"""
        ...

    @property
    def payload(self) -> Mapping[str, object]:
        """事件负载。"""
        ...


@runtime_checkable
class TraceObserver(Protocol):
    """把一条轨迹记进观测系统。

    **端口在领域层，实现在基础设施层** —— 与 :class:`core.metrics.cost.CostObserver`
    同一套做法：领域层要能回答"这次执行的轨迹记下来了吗"，但它不该知道
    OTel 或 Langfuse 的存在。

    **返回值是"这次真的记下来了吗"。** 观测系统连不上时它返回 ``False`` ——
    **观测不可用不得阻塞主流程**，所以这里不抛异常；但"没记下来"这件事要能
    被问出来，否则缺的那段轨迹与"这段时间没发生事"分不开。
    """

    async def observe(self, trace: TaskTrace) -> bool:
        """记下一条轨迹。返回是否真的记下来了。"""
        ...


class TraceStore:
    """按 ``task_id`` 检索轨迹 —— **阶段一的检索入口**。

    **为什么先要一个进程内的。** 真正上线时轨迹在 OTel collector 与
    Langfuse 里，检索是查它们。可那要有一个跑着的 collector 才谈得上
    "能按 task_id 检索" —— 而阶段一没有。**先有一个进程内的索引，检索这条
    要求才是真的可验的**；等 collector 接上了，这个索引换成查 collector，
    口径不变。

    **不并发写。** 一次执行的轨迹是在一次调用里攒完的，没有两个协程同时
    往同一个 ``task_id`` 上写。要真并发了，得换成带锁的版本 —— 而现在加锁
    只是给"没发生的事"写保险。
    """

    def __init__(self) -> None:
        self._traces: list[TaskTrace] = []
        self._by_task: dict[str, TaskTrace] = {}

    def add(self, trace: TaskTrace) -> None:
        """收下一条轨迹。"""
        self._traces.append(trace)
        if trace.task_id is not None:
            self._by_task[trace.task_id] = trace

    def by_task(self, task_id: str) -> TaskTrace | None:
        """按 ``task_id`` 取轨迹 —— **取不到是 ``None``，不是空轨迹**。

        空轨迹与"没有轨迹"在报表上长得一模一样，而它们说的是两件事：
        前者是"跑了但什么都没记"，后者是"没跑"。
        """
        return self._by_task.get(task_id)

    def by_cycle(self, cycle_id: str) -> tuple[TaskTrace, ...]:
        """按 ``cycle_id`` 取轨迹 —— 一轮演进里的全部。"""
        return tuple(item for item in self._traces if item.cycle_id == cycle_id)

    def __len__(self) -> int:
        return len(self._traces)


__all__ = [
    "REQUIRED_IDS",
    "TOOL_CALL_KINDS",
    "TOOL_RESULT_KINDS",
    "TaskTrace",
    "ToolCallTally",
    "TraceContext",
    "TraceError",
    "TraceKind",
    "TraceObserver",
    "TraceRecord",
    "TraceStore",
    "TraceableEvent",
]
