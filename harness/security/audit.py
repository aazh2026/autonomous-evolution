"""工具调用审计 —— "做了什么"的唯一记录处。

设计依据：docs/design.md §3.5D（"每一次工具调用均记录主体、任务、资源和
执行结果"）；docs/tasks.md TASK-P1-045、R-062、R-063。

**这个模块只回答"做了什么"。** 准不准是 :mod:`harness.security.authorization`
的事，合不合规是 :mod:`harness.security.gate` 的事，能不能连出去是
:mod:`harness.security.network` 的事。审计**不参与判断** —— 它只把已经
发生的事实原样记下来，让事后的人能回答"那天到底是谁、在哪个任务上、对
哪个资源做了什么、结果如何"。

**四要素缺一不可。** 每一条记录都必须带着主体（``agent_id``）、任务
（``task_id``）、资源（``resource``）、结果（``result``）：

- 少了**主体**，就不知道是谁干的 —— 而"谁干的"是追责与复盘的起点；
- 少了**任务**，就不知道这件事**是不是被授权干过** —— 授权绑定的是任务，
  不是人（见 :mod:`harness.security.authorization`）；
- 少了**资源**，就不知道动了什么 —— "改了点东西"没法复盘，"改了
  ``src/main.py``"才可以；
- 少了**结果**，就不知道成没成 —— 半成品的调用最危险，因为它看上去一切
  正常。

这四条不是"建议填"，而是**构造时就校验**：缺一条就构造不出来。

**写路径只有一条，而且绕不过去。** 业务代码拿到的是 :class:`AuditRecorder`
—— 一个只有"记一次调用"的窄接口：既没有查询，也没有存储句柄。而存储协议
:class:`AuditStore` 里**只有 append**：没有 update，没有 delete，所以审计表
天生只追加。想绕过审计，就得先拿到存储 —— 而业务代码拿不到。

**写失败必须阻断。** 审计写不下来 = 这件事没发生过 = 不能当作发生了。
链上有两道闸：

- **操作之前**的判定留痕（授权判定）写失败 → 判定为**拒绝**，操作不开始
  （见 :meth:`harness.security.authorization.AuthorizationService.authorize`）；
- **操作之后**的调用记录写失败 → 抛 :class:`AuditWriteError`，这次调用
  **不算做成了** —— 工具已经动过手，却没人能证明它做过什么。

注意这两道闸的位置不同，是有意的：判定留痕在**动作之前**，所以它能真正
地"不让他做"；调用记录在**动作之后**，因为结果只有做完才知道，它能保证的
是"做完了却没记下来"这件事不会悄悄过去。

**为什么本体是文件。** 审计要在动作前后落盘才谈得上 fail-closed，而
"数据库写成功"不等于"这条记录不会消失" —— 事务回滚时它跟着没了，
"写下来了"就退化成"大概写下来了"。文件追加 + ``fsync`` 没有这条尾巴：
写成功就是写成功了。数据库那份（将来落库）是为查询与统计准备的投影，
不是留痕的本体。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable
from uuid import uuid4

from harness.security.authorization import (
    AuthorizationDecision,
    DecisionSink,
    capability_for,
)

#: 不适用的占位值。``resource`` / ``operation`` 为它，表示这次记录不针对
#: 具体资源、或没有具体操作名（例如"拆分任务"这种不碰文件的操作）。
NOT_APPLICABLE: Final[str] = "none"


class AuditResult(StrEnum):
    """一次工具调用的结果。

    **只有三种。** "部分成功"、"超时未返回"、"不知道"都不在这里 ——
    拿不准的时候记成 :attr:`FAILURE`：审计表宁可多记一条失败，也不要
    出现一条说不清状态的记录。
    """

    SUCCESS = "success"
    FAILURE = "failure"
    #: 操作压根没开始（被拒绝）。**与"开始了但失败"不是一回事** ——
    #: 前者没有副作用，后者可能有。
    DENIED = "denied"


class AuditDecision(StrEnum):
    """判定 —— 这次操作是被允许了，还是被拒绝了。"""

    ALLOW = "allow"
    DENY = "deny"


class AuditWriteError(RuntimeError):
    """审计写入失败。

    **抛出这个错就意味着"这件事没被记下来"。** 调用方必须按失败处理 ——
    绝不能"记不上就算了，先干活"：那样审计表就变成了一份只记成功、不记
    失败的名册，比没有审计更坏。
    """


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """审计表里的一行 —— 一条不可改的事实。

    **只追加。** 没有"更新结果"这种操作：一条记录写下来是什么样，事后就是
    什么样；要改，就再写一条。

    字段分成三组：

    - **身份**：``agent_id`` / ``task_id`` / ``project_id`` / ``workspace_id``
      —— 回答"谁、在哪个任务上、属于哪个项目、在哪个工作空间里"；
    - **动作**：``resource`` / ``operation`` / ``capability`` —— 回答"动了
      什么"；
    - **结论**：``result`` / ``decision`` / ``reason`` / ``timestamp`` ——
      回答"成没成、凭什么、时间是几点"。
    """

    #: 这条记录的标识。审计表里的主键。
    event_id: str
    #: 主体 —— 干活的 Agent。**不能**用它替代 ``task_id``（R-063）。
    agent_id: str
    #: 任务 —— 这次动作属于哪个任务。授权绑定的是它。
    task_id: str
    #: 项目。
    project_id: str
    #: 工作空间。
    workspace_id: str
    #: 资源 —— 这次动作针对的东西（文件路径、命令、远端对象……）。
    resource: str
    #: 操作 —— 做了什么（``write_file`` / ``merge_to_main``……）。
    operation: str
    #: 结果 —— 成没成。
    result: AuditResult
    #: 判定 —— 允许还是拒绝。
    decision: AuditDecision
    #: 时间戳 —— UTC。
    timestamp: datetime
    #: 理由 —— 为什么是这个结果。判定类记录一定非空。
    reason: str = ""
    #: 涉及的资源级能力（如 ``repo:merge``）。判定类记录会带。
    capability: str | None = None
    #: 执行会话。同一会话里的调用属于同一次 Agent 执行。
    session_id: str | None = None

    def __post_init__(self) -> None:
        """校验 —— **缺一条要素就构造不出来**。

        这里拒绝的是"记一条说不清的记录"。审计表里的每一行都要能被独立
        读懂：谁、哪个任务、动了什么、结果如何。一行缺了主体，读它的人
        只能靠上下文猜 —— 而审计最忌讳的就是"靠猜"。
        """
        for name, value in (
            ("event_id", self.event_id),
            ("agent_id", self.agent_id),
            ("task_id", self.task_id),
            ("project_id", self.project_id),
            ("workspace_id", self.workspace_id),
            ("resource", self.resource),
            ("operation", self.operation),
        ):
            if not value.strip():
                raise ValueError(f"审计记录的 {name} 不能为空")
        object.__setattr__(self, "result", AuditResult(str(self.result)))
        object.__setattr__(self, "decision", AuditDecision(str(self.decision)))
        object.__setattr__(self, "timestamp", _as_utc(self.timestamp))


@dataclass(frozen=True, slots=True)
class AuditQuery:
    """查询条件 —— **留空即不限**。

    除时间范围外，每一项都是精确匹配。没有模糊匹配、没有正则：审计查询
    要能复现，同一个问题今天和下个月必须查出同一个答案。
    """

    task_id: str | None = None
    agent_id: str | None = None
    project_id: str | None = None
    workspace_id: str | None = None
    resource: str | None = None
    operation: str | None = None
    session_id: str | None = None
    #: 闭区间起点（含）。
    since: datetime | None = None
    #: 闭区间终点（含）。
    until: datetime | None = None


@runtime_checkable
class AuditStore(Protocol):
    """审计存储 —— **这个协议里只有追加**。

    **没有 update，没有 delete。** 审计表里的东西不许改、不许删：能改的
    记录等于没有记录，因为事后没人能相信它还是当时写下的那样。存储实现
    要做的只是"把一条记录放进去"，其余的都不归它管。

    这不只是"约定不要写这两个方法"，而是**类型上就没有这两个方法** ——
    拿到 ``AuditStore`` 的代码，无论怎么写都改不了已有的记录。
    """

    def append(self, event: AuditEvent) -> None:
        """追加一条记录。

        **写不成功必须抛异常。** 吞掉异常等于告诉调用方"记下来了"，而
        实际上没有 —— 那正是"审计失败就放行"最隐蔽的一种写法。
        """
        ...

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]:
        """按条件取出记录，**按写入顺序**返回。"""
        ...


@runtime_checkable
class AuditRecorder(Protocol):
    """业务代码拿到的**唯一**东西 —— 一个写入口。

    **它没有查询，也没有存储句柄。** 业务代码要记东西，就调
    :meth:`record_tool_call`；要查历史，走别的接口。存储是审计模块自己的
    事，业务代码碰不到它 —— **碰不到，就绕不过去**。

    这条协议是"审计写入路径不可被业务代码绕过"的技术形态：工具调用链上
    传的是它，不是存储；于是无论业务代码多想要一条"自己的"记录，都得从
    这个入口走 —— 而这个入口会校验四要素、会在写失败时抛错。
    """

    def record_tool_call(
        self,
        *,
        agent_id: str,
        task_id: str,
        project_id: str,
        workspace_id: str,
        resource: str,
        operation: str,
        result: AuditResult,
        decision: AuditDecision = AuditDecision.ALLOW,
        reason: str = "",
        capability: str | None = None,
        session_id: str | None = None,
    ) -> AuditEvent:
        """记下一次工具调用。**写失败就抛 :class:`AuditWriteError`。**"""
        ...


@dataclass(slots=True)
class AuditOutcome:
    """被审计的一次调用的结果 —— 调用方在块内填写。

    :meth:`AuditLog.audited_call` 退出时按它写审计记录。
    """

    #: 成没成。
    result: AuditResult = AuditResult.SUCCESS
    #: 说明。留空时由上下文管理器按异常类型补一句。
    reason: str = ""


class AuditLog(AuditRecorder, DecisionSink):
    """审计的**唯一**写入口。

    **它同时是授权判定的留痕去处**（:class:`~harness.security.authorization.
    DecisionSink` 的实现）—— 授权模块只管"要不要留痕"，"留成什么样"是
    审计模块的事；两边各管一头，谁也不需要知道对方写到哪里去。

    :param store: 记录存到哪里。**这个对象不会外泄** —— 业务代码拿到的
        是 :class:`AuditRecorder`，不是存储。
    :param clock: 取当前时间的方式。测试用它固定时间戳。
    :param id_factory: 生成 ``event_id`` 的方式。测试用它固定标识。
    """

    def __init__(
        self,
        store: AuditStore,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid4()))

    def record_tool_call(
        self,
        *,
        agent_id: str,
        task_id: str,
        project_id: str,
        workspace_id: str,
        resource: str,
        operation: str,
        result: AuditResult,
        decision: AuditDecision = AuditDecision.ALLOW,
        reason: str = "",
        capability: str | None = None,
        session_id: str | None = None,
    ) -> AuditEvent:
        """记下一次工具调用。

        **写失败就抛 :class:`AuditWriteError`** —— 调用方必须把这次调用
        当作失败处理：工具已经动过手，却没人能证明它做过什么，那它就
        不能被当作一次成功的操作继续往下走。
        """
        event = AuditEvent(
            event_id=self._id_factory(),
            agent_id=agent_id,
            task_id=task_id,
            project_id=project_id,
            workspace_id=workspace_id,
            resource=resource,
            operation=operation,
            result=result,
            decision=decision,
            reason=reason,
            capability=capability,
            timestamp=self._clock(),
            session_id=session_id,
        )
        return self._append(event)

    def record(self, decision: AuthorizationDecision) -> None:
        """把一条授权判定记进审计表。

        这是 :class:`~harness.security.authorization.DecisionSink` 的实现。
        **放行与拒绝一样都记** —— 拒绝只留下一句"没做"，而"为什么没做"
        往往正是后来要复盘的东西。
        """
        self._append(self._event_from_decision(decision))

    @contextmanager
    def audited_call(
        self,
        *,
        agent_id: str,
        task_id: str,
        project_id: str,
        workspace_id: str,
        resource: str,
        operation: str,
        decision: AuditDecision = AuditDecision.ALLOW,
        capability: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[AuditOutcome]:
        """把一次工具调用包起来 —— 退出时把结果写进审计表。

        **写失败就抛。** 工具已经动过手，却没人能证明它做过什么 —— 那这次
        调用就不能算作做成了，调用方拿到的是异常，不是成功。

        **这不是操作前的闸。** 它记录的是**已经发生**的事，所以它挡不住
        "不该做的事被做了"。操作前的那道闸是授权判定留痕（写失败即拒绝，
        见 :meth:`harness.security.authorization.AuthorizationService.
        authorize`）；这里的闸保证的是"做过的事不会没有记录"。

        块内抛出异常时，先把这次调用记成失败，再把异常原样抛出去 ——
        **异常不会被审计吞掉**。若连这条失败记录都写不下去，抛的是
        :class:`AuditWriteError`，原始异常挂在它的 ``__cause__`` 上。
        """
        outcome = AuditOutcome()
        try:
            yield outcome
        except BaseException as error:
            outcome.result = AuditResult.FAILURE
            outcome.reason = outcome.reason or f"执行异常：{type(error).__name__}"
            try:
                self.record_tool_call(
                    agent_id=agent_id,
                    task_id=task_id,
                    project_id=project_id,
                    workspace_id=workspace_id,
                    resource=resource,
                    operation=operation,
                    result=outcome.result,
                    decision=decision,
                    reason=outcome.reason,
                    capability=capability,
                    session_id=session_id,
                )
            except AuditWriteError as write_error:
                raise write_error from error
            raise
        self.record_tool_call(
            agent_id=agent_id,
            task_id=task_id,
            project_id=project_id,
            workspace_id=workspace_id,
            resource=resource,
            operation=operation,
            result=outcome.result,
            decision=decision,
            reason=outcome.reason,
            capability=capability,
            session_id=session_id,
        )

    def query(self, query: AuditQuery | None = None) -> tuple[AuditEvent, ...]:
        """查询审计记录 —— **按写入顺序**返回。

        :param query: 查询条件。``None`` 表示全量。
        """
        return self._store.query(query if query is not None else AuditQuery())

    def _event_from_decision(self, decision: AuthorizationDecision) -> AuditEvent:
        """把一条授权判定翻译成一条审计记录。

        **判定的时间用判定自己的时间**（``decided_at``），不是"记下来的
        时间" —— 后者会随着留痕延迟而漂移，同一次判定在不同机器上会给出
        不同的时间，事后对不上。
        """
        operation = decision.operation.value if decision.operation is not None else NOT_APPLICABLE
        resource = decision.resource if decision.resource is not None else NOT_APPLICABLE
        capability = decision.capability
        if capability is None and decision.operation is not None:
            # 请求没显式点名能力时，用操作本身对应的能力 —— 审计要能一眼
            # 看出"这次改代码动的是 repo:write"，而不是让人自己去查另一张表。
            capability = capability_for(decision.operation)
        return AuditEvent(
            event_id=self._id_factory(),
            agent_id=decision.agent_id,
            task_id=decision.task_id,
            project_id=decision.project_id,
            workspace_id=decision.workspace_id,
            resource=resource,
            operation=operation,
            result=AuditResult.SUCCESS if decision.allowed else AuditResult.DENIED,
            decision=AuditDecision.ALLOW if decision.allowed else AuditDecision.DENY,
            reason=decision.reason,
            capability=capability.value if capability is not None else None,
            timestamp=decision.decided_at,
        )

    def _append(self, event: AuditEvent) -> AuditEvent:
        """写入一条记录。**写不成功就抛** —— 这是整个模块的 fail-closed 点。"""
        try:
            self._store.append(event)
        except Exception as error:
            raise AuditWriteError(f"审计记录写入失败（{type(error).__name__}）") from error
        return event


class InMemoryAuditStore:
    """内存实现 —— 测试与单机开发用。

    **只追加**：内部只有一条 list，没有任何修改入口。
    """

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self._events.append(event)

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]:
        return tuple(event for event in self._events if _matches(event, query))


class JsonlAuditStore:
    """文件实现 —— 一行一条记录，**只追加**。

    每行是一个 JSON 对象，字段与 :class:`AuditEvent` 一一对应。文件用
    ``"a"`` 模式打开，写完 ``flush`` + ``fsync``：**写成功就是写成功了**，
    不会因为之后某次事务回滚而消失。

    **读到坏行就报错。** 审计文件被改过（或写坏了）的时候，宁可查不了，
    也不能装作没事 —— 一份会跳过坏行的审计表等于给了篡改者一个后门。

    **多进程。** ``O_APPEND`` 下每一行的写入是原子的，但**行与行之间的
    顺序**没有保证。需要跨进程严格有序时，每个进程写自己的一份文件，
    由上层合并。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """审计文件的位置。"""
        return self._path

    def append(self, event: AuditEvent) -> None:
        line = json.dumps(_to_payload(event), ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]:
        if not self._path.exists():
            return ()
        events: list[AuditEvent] = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            events.append(_from_payload(_parse_line(line, number, self._path)))
        return tuple(event for event in events if _matches(event, query))


def _as_utc(moment: datetime) -> datetime:
    """把 naive 时间当作 UTC。

    **不带时区的时间一律按 UTC 解释**，不按本地时间 —— 换台机器结论就变，
    这在审计里不可接受。审计的时间要能互相比较，也要和授权判定、事件
    轨迹对得上。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _matches(event: AuditEvent, query: AuditQuery) -> bool:
    """一条记录是否命中查询条件。"""
    pairs: tuple[tuple[str | None, str | None], ...] = (
        (query.task_id, event.task_id),
        (query.agent_id, event.agent_id),
        (query.project_id, event.project_id),
        (query.workspace_id, event.workspace_id),
        (query.resource, event.resource),
        (query.operation, event.operation),
        (query.session_id, event.session_id),
    )
    for expected, actual in pairs:
        if expected is not None and expected != actual:
            return False
    if query.since is not None and event.timestamp < _as_utc(query.since):
        return False
    if query.until is None:
        return True
    return event.timestamp <= _as_utc(query.until)


def _to_payload(event: AuditEvent) -> dict[str, str | None]:
    """一条记录 → 可 JSON 化的字典。"""
    return {
        "event_id": event.event_id,
        "agent_id": event.agent_id,
        "task_id": event.task_id,
        "project_id": event.project_id,
        "workspace_id": event.workspace_id,
        "resource": event.resource,
        "operation": event.operation,
        "result": event.result.value,
        "decision": event.decision.value,
        "reason": event.reason,
        "capability": event.capability,
        "timestamp": event.timestamp.isoformat(),
        "session_id": event.session_id,
    }


def _from_payload(payload: Mapping[str, object]) -> AuditEvent:
    """可 JSON 化的字典 → 一条记录。"""
    return AuditEvent(
        event_id=_text(payload, "event_id"),
        agent_id=_text(payload, "agent_id"),
        task_id=_text(payload, "task_id"),
        project_id=_text(payload, "project_id"),
        workspace_id=_text(payload, "workspace_id"),
        resource=_text(payload, "resource"),
        operation=_text(payload, "operation"),
        result=AuditResult(_text(payload, "result")),
        decision=AuditDecision(_text(payload, "decision")),
        reason=_text(payload, "reason", required=False),
        capability=_optional_text(payload, "capability"),
        timestamp=datetime.fromisoformat(_text(payload, "timestamp")),
        session_id=_optional_text(payload, "session_id"),
    )


def _parse_line(line: str, number: int, path: Path) -> Mapping[str, object]:
    """解析一行 JSON。**坏行即错** —— 不跳过。"""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"审计文件第 {number} 行不是合法 JSON：{path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"审计文件第 {number} 行不是 JSON 对象：{path}")
    return {str(key): value for key, value in payload.items()}


def _text(payload: Mapping[str, object], key: str, *, required: bool = True) -> str:
    """取一个非空字符串字段。"""
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value
    if required:
        raise ValueError(f"审计记录缺少字段 {key}")
    return ""


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    """取一个可选的字符串字段。"""
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


__all__ = [
    "NOT_APPLICABLE",
    "AuditDecision",
    "AuditEvent",
    "AuditLog",
    "AuditOutcome",
    "AuditQuery",
    "AuditRecorder",
    "AuditResult",
    "AuditStore",
    "AuditWriteError",
    "InMemoryAuditStore",
    "JsonlAuditStore",
]
