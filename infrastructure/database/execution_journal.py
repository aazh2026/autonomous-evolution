"""执行轨迹的数据库落库口（循环 A 的 ``SessionJournal`` 实现）。

设计依据：docs/design.md §3.6（``AgentSession`` / ``ExecutionEvent`` 三表
分离）、§3.5D（审计写入 fail-closed）、docs/tasks.md TASK-P1-060（T1 ④
"每步都写入 ``AgentSession`` 与 ``ExecutionEvent``"）。

**这一层只做翻译。** 领域层的 :class:`core.task.execution_loop.SessionJournal`
是**端口** —— 它只知道"开一个会话""写一条事件"，不知道数据库存在。写库的
实现在这里，规则（留痕 fail-closed）在领域层，写库的**路径**在仓储层
（:mod:`infrastructure.database.repositories`）—— 三处各管一段，谁也不越界。

**写失败必须炸出来。** design.md §3.5D：审计写入失败要 fail-closed。
留痕写不进去还往下跑，等于这次执行没有痕迹 —— 而"没有痕迹"这件事，
恰恰是在出事之后才会被发现的。所以这里**不吞异常**：数据库写不了，
调用方就得停下来。

**会话与事件的关系。** 一次 ``AgentSession`` = **一次 Agent 执行**；事件挂在
它所属的那次执行的会话上。还没发生执行的那几步（就绪、规划、选执行器、
开工作区）没有会话可挂 —— 它们的 ``session_id`` 是空的，而不是挂到某个
不相干的会话上。

**事务边界由调用方决定。** 这里只 ``flush``，不 ``commit``：谁开的事务谁
提交。这样"状态写入与事件写入同事务"才成立（:mod:`infrastructure.database.
engine` 的 ``session_scope``）。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.task.execution_loop import EventRecord, SessionRecord
from infrastructure.database.repositories import (
    AgentSessionRepository,
    ExecutionEventRepository,
)


class JournalWriteError(RuntimeError):
    """轨迹写不进去。

    **单独一个异常类型，是为了让"写不进去"这件事在调用方那里能被一眼
    认出来** —— 而不是混在一堆数据库错误里。
    """


class DatabaseExecutionJournal:
    """把执行轨迹写进数据库。

    **为什么它不是仓储层本身。** 仓储层是"数据写入的唯一受控入口"，而这一层
    是**领域端口的适配器** —— 它把领域层的记录翻译成仓储层的调用。分开的
    理由与 :mod:`infrastructure.database.checkpointer` 一样：编排层依赖的是
    端口，不是数据库。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._sessions = AgentSessionRepository(session)
        self._events = ExecutionEventRepository(session)

    async def open_session(self, record: SessionRecord) -> str:
        """开一个执行会话，返回它的**行 id**。

        **行 id 不是执行器侧的会话标识。** 前者是数据库的主键（事件的
        ``session_id`` 外键指的就是它），后者是执行器自己那边的名字。两者
        混起来，事后就答不出"这条事件属于哪一次执行"了。
        """
        row = await self._sessions.open(
            project_id=record.project_id,
            task_id=_as_uuid(record.task_id, "任务"),
            agent_id=record.agent_id,
            executor=record.executor,
            session_id=record.session_id,
            authorization=record.authorization,
        )
        return str(row.id)

    async def record_event(self, event: EventRecord) -> None:
        """写一条执行事件。

        **写不进去就炸。** 这里不 catch 任何异常 —— 见模块说明里的 fail-closed。
        """
        await self._events.append(
            event_type=event.event_type,
            payload=event.payload,
            task_id=_as_uuid(event.task_id, "任务"),
            project_id=event.project_id or None,
            agent_id=event.agent_id or None,
            session_id=_as_uuid(event.session_ref, "会话") if event.session_ref else None,
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
            sequence=event.sequence,
        )


def _as_uuid(value: str, what: str) -> uuid.UUID:
    """把字符串当 UUID 解析 —— 解析不了就说清楚是哪一样东西解析不了。

    **为什么在这里报错而不是在数据库里报错。** 外键存的是 UUID；传一个
    "p-demo-0001" 过去，数据库会报一个外键违例，而那条信息完全看不出是
    "任务 id 的格式不对"。这里先把它翻译成人能读的话。
    """
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise JournalWriteError(f"{what} id {value!r} 不是合法的 UUID") from error


__all__ = [
    "DatabaseExecutionJournal",
    "JournalWriteError",
]
