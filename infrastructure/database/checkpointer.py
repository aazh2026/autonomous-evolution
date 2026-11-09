"""LangGraph checkpoint 的数据库实现。

设计依据：docs/tasks.md TASK-P1-050（"图状态可持久化（checkpoint）"、
"中途异常后从 checkpoint 恢复不重复执行已完成的节点"）。

**为什么自己写，而不是用 ``langgraph-checkpoint-postgres``。** 那个包要另带一
个驱动（psycopg）与另一套迁移路径 —— 于是同一个项目里有两个数据库连接、两套
表、两套迁移顺序，而"决策图跑到哪一步了"这件事只跟编排有关。checkpoint 记的是
**编排器自己的运行状态**，它跟项目一起生灭，所以它就该在项目自己的库里，用项
目自己的会话与迁移。

**只实现异步那一半。** 这个项目从头到尾是异步的（见
``infrastructure/database/engine.py``：演进循环里慢的是等模型、等测试，不是
烧 CPU）。同步方法留着基类的默认实现 —— 调到就会 ``NotImplementedError``，
而不是悄悄用一条同步连接去堵住事件循环。

**checkpoint 本体存成 BYTEA，序列化类型标签单独存一列。** 理由见
``core/entities/checkpoint.py`` 的模块文档：checkpoint 里装的是 Pydantic 模型、
``Decimal``、元组，存成 JSONB 就得在存之前把它们降级成 JSON，读回来再猜原来是
什么类型 —— **猜错类型的那份 checkpoint 比没有 checkpoint 更糟**。

**排序靠 checkpoint_id，不靠时间戳。** LangGraph 的 checkpoint id 是 uuid6，
高位是毫秒级时间戳，所以它的**字符串序就是时间序**。于是"取最新一个
checkpoint"是一条干净的 ``ORDER BY checkpoint_id DESC LIMIT 1``，不用在两个时
间戳相同时猜。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.entities.checkpoint import GraphCheckpoint, GraphCheckpointWrite

#: checkpoint 的来源。必须与 LangGraph 的 ``CheckpointMetadata["source"]`` 一致。
_SOURCES = ("input", "loop", "update", "fork")

_Source = Literal["input", "loop", "update", "fork"]


class SqlAlchemyCheckpointSaver(BaseCheckpointSaver[str]):
    """把决策图的 checkpoint 存进项目自己的 PostgreSQL。

    实现 :class:`~langgraph.checkpoint.base.BaseCheckpointSaver` 的异步面：
    ``aget_tuple`` / ``alist`` / ``aput`` / ``aput_writes`` /
    ``adelete_thread``。

    **每次操作自己开一个会话。** LangGraph 的接口不传会话进来，而 checkpoint
    必须**当场落盘** —— 中途崩溃时，"上一个 checkpoint 还在库里"是恢复能成立的
    唯一前提。跟着外层事务走的话，外层一 rollback，刚写下的 checkpoint 就跟着
    没了。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self._session_factory = session_factory

    # ------------------------------------------------------------------ 读

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """取一个 checkpoint。

        ``config`` 里带了 ``checkpoint_id`` 就取那一个；没带就取**最新**一个。
        """
        thread_id = _thread_id(config)
        checkpoint_ns = _checkpoint_ns(config)
        checkpoint_id = _checkpoint_id(config)

        async with self._session_factory() as session:
            row = await self._fetch(session, thread_id, checkpoint_ns, checkpoint_id)
            if row is None:
                return None
            writes = await self._fetch_writes(session, thread_id, checkpoint_ns, row.checkpoint_id)

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": row.checkpoint_id,
                }
            },
            checkpoint=cast(Checkpoint, self.serde.loads_typed((row.serde_type, row.checkpoint))),
            metadata=_to_metadata(row.metadata_),
            parent_config=_parent_config(thread_id, checkpoint_ns, row.parent_checkpoint_id),
            pending_writes=writes,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, object] | None = None,  # noqa: A002 - 基类就叫这个名字
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """列出 checkpoint，**从新到旧**。

        **顺序是倒的，而且这很重要。** LangGraph 的 ``get_state_history`` 靠这个
        顺序回放时间线；从旧到新列，界面上"最近一次"会显示成最早那次。

        ``before`` 是**排他**的上界 —— "在这个 checkpoint 之前的那批"。

        ``filter`` 目前只按元数据里的 ``source`` 过滤。**不认识的键一律忽略**，
        而不是当成"匹配不上" —— 后者会把列表变成空的，而调用方以为自己筛出
        了一个空结果。
        """
        thread_id = _thread_id(config) if config is not None else None
        checkpoint_ns = _checkpoint_ns(config) if config is not None else ""
        before_id = _checkpoint_id(before) if before is not None else None
        wanted_source = _wanted_source(filter)

        statement = select(GraphCheckpoint)
        if thread_id is not None:
            statement = statement.where(GraphCheckpoint.thread_id == thread_id)
        if checkpoint_ns:
            statement = statement.where(GraphCheckpoint.checkpoint_ns == checkpoint_ns)
        if before_id is not None:
            statement = statement.where(GraphCheckpoint.checkpoint_id < before_id)
        statement = statement.order_by(GraphCheckpoint.checkpoint_id.desc())
        if limit is not None:
            statement = statement.limit(limit)

        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
            tuples: list[CheckpointTuple] = []
            for row in rows:
                if wanted_source is not None and row.source != wanted_source:
                    continue
                writes = await self._fetch_writes(
                    session, row.thread_id, row.checkpoint_ns, row.checkpoint_id
                )
                tuples.append(
                    CheckpointTuple(
                        config={
                            "configurable": {
                                "thread_id": row.thread_id,
                                "checkpoint_ns": row.checkpoint_ns,
                                "checkpoint_id": row.checkpoint_id,
                            }
                        },
                        checkpoint=cast(
                            Checkpoint, self.serde.loads_typed((row.serde_type, row.checkpoint))
                        ),
                        metadata=_to_metadata(row.metadata_),
                        parent_config=_parent_config(
                            row.thread_id, row.checkpoint_ns, row.parent_checkpoint_id
                        ),
                        pending_writes=writes,
                    )
                )

        for item in tuples:
            yield item

    # ------------------------------------------------------------------ 写

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """存下一个 checkpoint。

        **整个 checkpoint 一起存，不拆成"每个通道一个 blob"。** 通道级的版本
        追踪是给增量通道（``DeltaChannel``）用的；本项目的状态是普通 Pydantic
        模型，整体存整体取，类型不会在拆装之间走形。

        ``new_versions`` 因此用不上 —— 但它不能改名字：基类把它叫这个，
        改成别的，``saver.aput(..., new_versions=...)`` 这种调用就会在类型检查
        上失败。
        """
        _ = new_versions
        thread_id = _thread_id(config)
        checkpoint_ns = _checkpoint_ns(config)
        parent_checkpoint_id = _checkpoint_id(config)
        serde_type, blob = self.serde.dumps_typed(checkpoint)
        payload = _jsonable(get_checkpoint_metadata(config, metadata))

        values: dict[str, object] = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint["id"],
            "parent_checkpoint_id": parent_checkpoint_id,
            "step": _as_int(payload.get("step"), default=-1),
            "source": _as_str(payload.get("source"), default="update"),
            "checkpoint": blob,
            "serde_type": serde_type,
            # 键是**ORM 属性名**（``metadata_``），不是列名（``metadata``）。
            #
            # **属性名为什么带下划线。** ``metadata`` 是 SQLAlchemy 声明式基类
            # 自己占用的名字（``Base.metadata`` —— 那是元数据注册表），所以这一
            # 列的属性名只能叫 ``metadata_``，列名仍是 ``metadata``。
            #
            # **为什么写属性名而不是列名。** 实测这两种拼法编出来的 INSERT 一模
            # 一样（``values()`` 认两者），所以这不是"只能这么写"，而是"统一这么
            # 写"：下面 ``on_conflict_do_update`` 里用的是 ``GraphCheckpoint
            # .metadata_``，两处保持一个写法，改起来不用两头对。
            "metadata_": payload,
        }

        async with self._session_factory() as session, session.begin():
            await session.execute(
                pg_insert(GraphCheckpoint)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[
                        GraphCheckpoint.thread_id,
                        GraphCheckpoint.checkpoint_ns,
                        GraphCheckpoint.checkpoint_id,
                    ],
                    set_={
                        GraphCheckpoint.parent_checkpoint_id: parent_checkpoint_id,
                        GraphCheckpoint.step: values["step"],
                        GraphCheckpoint.source: values["source"],
                        GraphCheckpoint.checkpoint: blob,
                        GraphCheckpoint.serde_type: serde_type,
                        GraphCheckpoint.metadata_: payload,
                    },
                )
            )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, object]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """存下一个节点写下的中间结果。

        **重复写要幂等。** 同一个节点失败后重跑会把同样的写再写一遍 —— 那是
        "重试"，不是"重复"。所以用 ``ON CONFLICT DO NOTHING``：已有的不覆盖，
        新的补上。
        """
        checkpoint_id = _checkpoint_id(config)
        if checkpoint_id is None:
            return
        thread_id = _thread_id(config)
        checkpoint_ns = _checkpoint_ns(config)

        rows: list[dict[str, object]] = []
        for position, (channel, value) in enumerate(writes):
            serde_type, blob = self.serde.dumps_typed(value)
            rows.append(
                {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                    "task_id": task_id,
                    "task_path": task_path,
                    "idx": WRITES_IDX_MAP.get(channel, position),
                    "channel": channel,
                    "value": blob,
                    "serde_type": serde_type,
                }
            )
        if not rows:
            return

        async with self._session_factory() as session, session.begin():
            await session.execute(
                pg_insert(GraphCheckpointWrite)
                .values(rows)
                .on_conflict_do_nothing(
                    index_elements=[
                        GraphCheckpointWrite.thread_id,
                        GraphCheckpointWrite.checkpoint_ns,
                        GraphCheckpointWrite.checkpoint_id,
                        GraphCheckpointWrite.task_id,
                        GraphCheckpointWrite.idx,
                    ]
                )
            )

    async def adelete_thread(self, thread_id: str) -> None:
        """删掉一个线程的全部 checkpoint 与写。"""
        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(GraphCheckpointWrite).where(GraphCheckpointWrite.thread_id == thread_id)
            )
            await session.execute(
                delete(GraphCheckpoint).where(GraphCheckpoint.thread_id == thread_id)
            )

    # ------------------------------------------------------------------ 内部

    async def _fetch(
        self,
        session: AsyncSession,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
    ) -> GraphCheckpoint | None:
        statement = select(GraphCheckpoint).where(
            GraphCheckpoint.thread_id == thread_id,
            GraphCheckpoint.checkpoint_ns == checkpoint_ns,
        )
        if checkpoint_id is not None:
            statement = statement.where(GraphCheckpoint.checkpoint_id == checkpoint_id)
        else:
            statement = statement.order_by(GraphCheckpoint.checkpoint_id.desc()).limit(1)
        return await session.scalar(statement)

    async def _fetch_writes(
        self,
        session: AsyncSession,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> list[tuple[str, str, object]]:
        """读出这个 checkpoint 的写，**按 ``writes_sort_key`` 排**。

        **顺序不是细节。** 一个 super-step 里的多个任务会往同一个通道写，
        reducer 按顺序把它们合起来。存的顺序与活的顺序不一致，重放出来的
        状态就与当初跑出来的不一样 —— 而那看起来完全正常。
        """
        statement = (
            select(GraphCheckpointWrite)
            .where(
                GraphCheckpointWrite.thread_id == thread_id,
                GraphCheckpointWrite.checkpoint_ns == checkpoint_ns,
                GraphCheckpointWrite.checkpoint_id == checkpoint_id,
            )
            .order_by(
                GraphCheckpointWrite.task_path,
                GraphCheckpointWrite.task_id,
                GraphCheckpointWrite.idx,
            )
        )
        rows = (await session.scalars(statement)).all()
        return [
            (row.task_id, row.channel, self.serde.loads_typed((row.serde_type, row.value)))
            for row in rows
        ]


# ---------------------------------------------------------------------- 辅助


def _thread_id(config: RunnableConfig) -> str:
    """取 thread id。

    **缺了就是错。** 没有 thread id 就不知道这次编排的状态存在哪 ——
    静默造一个会把不同次运行的 checkpoint 混在一起，恢复时读到别人的状态。
    """
    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not thread_id:
        raise ValueError("checkpoint 必须带 thread_id")
    return str(thread_id)


def _checkpoint_ns(config: RunnableConfig) -> str:
    configurable = config.get("configurable") or {}
    return str(configurable.get("checkpoint_ns", "") or "")


def _checkpoint_id(config: RunnableConfig) -> str | None:
    configurable = config.get("configurable") or {}
    checkpoint_id = configurable.get("checkpoint_id")
    if checkpoint_id is None:
        return None
    return str(checkpoint_id)


def _parent_config(
    thread_id: str, checkpoint_ns: str, parent_checkpoint_id: str | None
) -> RunnableConfig | None:
    if parent_checkpoint_id is None:
        return None
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": parent_checkpoint_id,
        }
    }


def _wanted_source(criteria: dict[str, object] | None) -> str | None:
    """从过滤条件里取 ``source``。取不出就是不过滤。"""
    if not criteria:
        return None
    source = criteria.get("source")
    if isinstance(source, str) and source in _SOURCES:
        return source
    return None


def _jsonable(value: object) -> dict[str, object]:
    """把元数据整成 JSON 能存的形状。

    **不认识的类型转成字符串，而不是丢掉。** 元数据是"这一步是怎么来的"的
    记录；丢字段比字段变成一行文本更糟 —— 丢了就再也找不回来了。
    """
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _jsonable_scalar(item) for key, item in value.items()}


def _jsonable_scalar(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable_scalar(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_scalar(item) for item in value]
    return str(value)


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _as_str(value: object, *, default: str) -> str:
    if isinstance(value, str):
        return value
    return default


def _to_metadata(payload: Mapping[str, object]) -> CheckpointMetadata:
    """数据库里的 JSON → LangGraph 的元数据类型。

    **只带 LangGraph 认识的那四个键。** ``CheckpointMetadata`` 是
    ``total=False`` 的 TypedDict，而普通 ``dict`` 不是它的子类型 —— 不显式挑出
    来，类型检查就过不了。库里存的 JSON 仍然是**完整**的（那一列什么都会留），
    这里只是"从库里读出来、交给 LangGraph 的那一份"。

    **不带别的键不等于丢信息。** 完整元数据在 ``graph_checkpoint.metadata``
    那一列里；要查它就查那一列。
    """
    metadata = CheckpointMetadata()
    source = payload.get("source")
    if isinstance(source, str) and source in _SOURCES:
        metadata["source"] = cast(_Source, source)
    step = payload.get("step")
    if isinstance(step, int) and not isinstance(step, bool):
        metadata["step"] = step
    parents = payload.get("parents")
    if isinstance(parents, Mapping):
        metadata["parents"] = {str(key): str(item) for key, item in parents.items()}
    run_id = payload.get("run_id")
    if isinstance(run_id, str):
        metadata["run_id"] = run_id
    return metadata


__all__ = ["SqlAlchemyCheckpointSaver"]
