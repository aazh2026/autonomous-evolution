"""checkpoint 持久化与恢复的集成测试（TASK-P1-050）。

设计依据：docs/tasks.md TASK-P1-050（"图状态可持久化（checkpoint）"、
"中途异常后从 checkpoint 恢复不重复执行已完成的节点"）、docs/design.md §5。

**这一组要过真的 PostgreSQL。** 因为这里验的不是"序列化对不对"（那是单元
测试的事），而是"**状态落进了项目自己的库里**"—— 换句话说：崩溃之后进程
重启，状态还在。

**最重要的一条是"不重复执行已完成的节点"。** 它不是性能优化，是**正确性**：
像"调模型"这样的节点，重跑一遍不是白干，是**又花钱又可能给出另一个结论** ——
而决策链上一个节点换个结论，下游拿到的就是另一份计划。

**为什么计数而不是看最终状态。** 重跑过的节点，产出的状态可能与没重跑时
一模一样（打桩节点尤其如此）。所以盯的是**调用次数**：每个阶段各跑了几次。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.entities.checkpoint import GraphCheckpoint, GraphCheckpointWrite
from core.evolution.assessment import ProjectAssessment
from core.planning.roadmap import EvolutionRoadmap
from core.planning.stages import PipelineState
from infrastructure.database.checkpointer import SqlAlchemyCheckpointSaver
from orchestration.langgraph.decision_graph import (
    build_decision_graph,
    run_decision_graph,
)
from orchestration.langgraph.serialization import state_serializer
from tests.support.decision_stages import RecordingHandlers, project_context

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def factory(
    engine: AsyncEngine, session: AsyncSession
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """一个会话工厂。

    ``session`` 夹具在这里有两个作用：每个测试前把全部表清空（**副作用**），
    以及**验一验表确实是干净的**（前置条件）—— 后者让"清空"这件事不只
    靠信任：万一某个夹具的依赖顺序变了，这里会当场红。
    """
    rows = await session.execute(text("SELECT count(*) FROM graph_checkpoint"))
    assert rows.scalar_one() == 0, "checkpoint 表不是干净的 —— 前置条件不满足"
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def saver(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[SqlAlchemyCheckpointSaver]:
    """一个把 checkpoint 写进项目自己数据库的 saver。"""
    yield SqlAlchemyCheckpointSaver(factory, serde=state_serializer())


def _state() -> PipelineState:
    return PipelineState(cycle_id="cycle-0007", context=project_context())


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _checkpoint(checkpoint_id: str) -> Checkpoint:
    """一个手写的 checkpoint。

    **为什么不用跑出来的。** 存取那一层的契约（排他、父链、幂等、顺序）要能
    **单独**验 —— 跑一整张图去凑出这些情形，就得靠"恰好"跑到某个形态，测试
    于是既慢又不准。这里直接摆出想要的样子，要验什么就是什么。
    """
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }


def _config_at(thread_id: str, checkpoint_id: str) -> RunnableConfig:
    """指着某个具体 checkpoint 的配置。"""
    return {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}


class TestTheRunResumes:
    """T1 ③：中途异常后从 checkpoint 恢复，不重复执行已完成的节点。"""

    async def test_a_failed_run_resumes_where_it_stopped(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """**这是本文件的核心一条。**

        第一次跑到"决策"阶段崩掉；第二次接着跑，**前面三个阶段一次都没有
        重跑**。
        """
        thread = f"thread-{uuid.uuid4().hex}"

        first = RecordingHandlers()
        first.fail_on = "decide"
        crashing = build_decision_graph(first.handlers, checkpointer=saver)

        with pytest.raises(RuntimeError, match="decide"):
            await run_decision_graph(crashing, _state(), config=_config(thread))

        assert first.calls == {
            "understand": 1,
            "diagnose": 1,
            "explore": 1,
            "decide": 1,
        }

        second = RecordingHandlers()
        resumed = build_decision_graph(second.handlers, checkpointer=saver)
        final = await run_decision_graph(resumed, None, config=_config(thread))

        # 跑完的阶段一次都没重跑。
        assert second.calls == {"decide": 1, "plan": 1}
        assert final.roadmap is not None
        assert final.completed_stages == (
            "understand",
            "diagnose",
            "explore",
            "decide",
            "plan",
        )

    async def test_the_resumed_run_produces_the_same_roadmap(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """**恢复出来的结果与一次跑完的结果一样。**

        这条盯的是"恢复"不是"重来" —— 如果恢复时把状态弄丢了、或者把某个
        阶段重跑了一遍（打桩节点产出一样时看不出来），最终路线图就会不一样。
        """
        thread = f"thread-{uuid.uuid4().hex}"

        first = RecordingHandlers()
        first.fail_on = "decide"
        crashing = build_decision_graph(first.handlers, checkpointer=saver)
        with pytest.raises(RuntimeError):
            await run_decision_graph(crashing, _state(), config=_config(thread))

        second = RecordingHandlers()
        resumed = build_decision_graph(second.handlers, checkpointer=saver)
        after_resume = await run_decision_graph(resumed, None, config=_config(thread))

        clean_handlers = RecordingHandlers()
        clean = build_decision_graph(clean_handlers.handlers, checkpointer=saver)
        in_one_go = await run_decision_graph(
            clean, _state(), config=_config(f"thread-{uuid.uuid4().hex}")
        )

        assert after_resume.roadmap == in_one_go.roadmap

    async def test_a_failure_in_the_first_stage_resumes_from_the_start(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """第一个阶段就崩了，恢复时从头跑 —— 没有"跑了一半"的状态可继承。"""
        thread = f"thread-{uuid.uuid4().hex}"

        first = RecordingHandlers()
        first.fail_on = "understand"
        crashing = build_decision_graph(first.handlers, checkpointer=saver)
        with pytest.raises(RuntimeError):
            await run_decision_graph(crashing, _state(), config=_config(thread))

        second = RecordingHandlers()
        resumed = build_decision_graph(second.handlers, checkpointer=saver)
        final = await run_decision_graph(resumed, None, config=_config(thread))

        assert second.calls["understand"] == 1
        assert final.roadmap is not None

    async def test_a_thread_that_never_ran_has_no_checkpoint(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """没跑过的线程，库里不该有它的 checkpoint —— 恢复时也不该凭空造出状态。"""
        found = [item async for item in saver.alist(_config(f"thread-{uuid.uuid4().hex}"))]

        assert found == []


class TestTheCheckpointsLandInTheDatabase:
    """状态真的落进了项目自己的库。"""

    async def test_the_run_leaves_checkpoint_rows(
        self, saver: SqlAlchemyCheckpointSaver, engine: AsyncEngine
    ) -> None:
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        await run_decision_graph(graph, _state(), config=_config(thread))

        async with engine.begin() as conn:
            rows = await conn.execute(
                select(func.count())
                .select_from(GraphCheckpoint)
                .where(GraphCheckpoint.thread_id == thread)
            )
            assert rows.scalar_one() > 0

    async def test_the_checkpoint_holds_the_state(
        self,
        saver: SqlAlchemyCheckpointSaver,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """checkpoint 里存的是**真的状态** —— 不是空壳，不是占位符。"""
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        final = await run_decision_graph(graph, _state(), config=_config(thread))

        async with factory() as session:
            latest = await session.scalar(
                select(GraphCheckpoint)
                .where(GraphCheckpoint.thread_id == thread)
                .order_by(GraphCheckpoint.checkpoint_id.desc())
            )

        assert latest is not None
        restored = saver.serde.loads_typed((latest.serde_type, latest.checkpoint))
        values = restored["channel_values"]
        assert values["cycle_id"] == "cycle-0007"
        # 通道里存的是**列表** —— msgpack 不区分元组与数组，而领域状态里的
        # ``completed_stages`` 是元组。两边对得上（元素一致），容器类型不同。
        assert list(values["completed_stages"]) == list(final.completed_stages)

    async def test_the_writes_are_stored_too(
        self, saver: SqlAlchemyCheckpointSaver, engine: AsyncEngine
    ) -> None:
        """节点写下的中间结果也在库里。

        **它们不是可有可无的。** 一个 super-step 里的多个任务会往同一个通道
        写，reducer 按顺序把它们合起来 —— 存的顺序与活的顺序不一致，重放
        出来的状态就与当初跑出来的不一样，而那看起来完全正常。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        await run_decision_graph(graph, _state(), config=_config(thread))

        async with engine.begin() as conn:
            rows = await conn.execute(
                select(func.count())
                .select_from(GraphCheckpointWrite)
                .where(GraphCheckpointWrite.thread_id == thread)
            )
            assert rows.scalar_one() > 0

    async def test_the_state_read_back_is_typed(self, saver: SqlAlchemyCheckpointSaver) -> None:
        """从库里读回来的状态，**类型还在**。

        **类型走样的 checkpoint 比没有 checkpoint 更糟** —— 它看起来好好的，
        于是人以为可以接着跑。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        await run_decision_graph(graph, _state(), config=_config(thread))

        found = await saver.aget_tuple(_config(thread))
        assert found is not None
        values = found.checkpoint["channel_values"]
        # 读回来的是**模型**，不是字典 —— 类型走样的 checkpoint 比没有更糟。
        assert isinstance(values["assessment"], ProjectAssessment)
        assert values["assessment"].goal_gap[0].id == "GAP-1"

    async def test_the_checkpoint_is_the_only_place_the_state_lives(
        self, saver: SqlAlchemyCheckpointSaver, engine: AsyncEngine
    ) -> None:
        """状态不落在任何业务表里。

        **checkpoint 记的是编排器自己的运行状态，不是业务事实。** 它跟项目
        一起生灭，所以它在项目自己的库里、用项目自己的会话与迁移 —— 但它
        不是"项目进展"，业务表里不该有它的影子。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        await run_decision_graph(graph, _state(), config=_config(thread))

        async with engine.begin() as conn:
            rows = await conn.execute(
                select(func.count())
                .select_from(GraphCheckpoint)
                .where(GraphCheckpoint.thread_id == thread)
            )
            assert rows.scalar_one() > 0


class TestTheHistory:
    """checkpoint 的历史。"""

    async def test_the_history_is_newest_first(self, saver: SqlAlchemyCheckpointSaver) -> None:
        """**顺序是倒的，而且这很重要。** LangGraph 靠这个顺序回放时间线；
        从旧到新列，"最近一次"会显示成最早那次。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        await run_decision_graph(graph, _state(), config=_config(thread))

        ids = [item.checkpoint["id"] async for item in saver.alist(_config(thread))]

        assert len(ids) >= 2
        assert ids == sorted(ids, reverse=True)

    async def test_the_history_can_be_filtered_by_source(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """按 ``source`` 过滤是支持的 —— LangGraph 的"回放某一步之后"靠它。"""
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        await run_decision_graph(graph, _state(), config=_config(thread))

        updates = [item async for item in saver.alist(_config(thread), filter={"source": "update"})]
        everything = [item async for item in saver.alist(_config(thread))]

        assert len(updates) <= len(everything)
        for item in updates:
            assert item.metadata.get("source") == "update"

    async def test_an_unknown_filter_key_is_ignored(self, saver: SqlAlchemyCheckpointSaver) -> None:
        """不认识的过滤键**忽略**，而不是当成"匹配不上"。

        后者会把列表变成空的，而调用方以为自己筛出了一个空结果。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        await run_decision_graph(graph, _state(), config=_config(thread))

        everything = [item async for item in saver.alist(_config(thread))]
        filtered = [item async for item in saver.alist(_config(thread), filter={"不认识的键": "x"})]

        assert len(filtered) == len(everything)


class TestTheThreadCanBeDeleted:
    """删掉一个线程的全部 checkpoint。"""

    async def test_the_delete_removes_the_checkpoints(
        self, saver: SqlAlchemyCheckpointSaver, engine: AsyncEngine
    ) -> None:
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        await run_decision_graph(graph, _state(), config=_config(thread))
        await saver.adelete_thread(thread)

        async with engine.begin() as conn:
            rows = await conn.execute(
                select(func.count())
                .select_from(GraphCheckpoint)
                .where(GraphCheckpoint.thread_id == thread)
            )
            assert rows.scalar_one() == 0

    async def test_the_delete_removes_the_writes_too(
        self, saver: SqlAlchemyCheckpointSaver, engine: AsyncEngine
    ) -> None:
        """写也一起删 —— 留着它们就是留着一半的现场。"""
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        await run_decision_graph(graph, _state(), config=_config(thread))
        await saver.adelete_thread(thread)

        async with engine.begin() as conn:
            rows = await conn.execute(
                select(func.count())
                .select_from(GraphCheckpointWrite)
                .where(GraphCheckpointWrite.thread_id == thread)
            )
            assert rows.scalar_one() == 0


class TestTheStoreItself:
    """**存的那一头也要有契约。**

    上面那些用例盯的是"整张图跑起来对不对"；这一组盯的是**存取这一个动作**
    本身 —— 少了 thread id 会怎样、``before`` 是不是排他的、父链是不是连着
    的、同一个写写两遍会怎样。

    **为什么单独开一组。** 图跑通只能证明"这条路上没出事"，证明不了"这条路上
    的**每一个岔口**都守着规矩"。而存档这一层恰恰全是岔口：排序、排他、父链、
    幂等 —— 每一处错了都不会让图跑不起来，只会让**恢复读出另一份状态**，而
    那看起来完全正常。
    """

    async def test_a_missing_thread_id_is_refused_on_read(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """**没有 thread id 就不知道这份状态属于哪次编排。**

        静默造一个会把不同次运行的 checkpoint 混在一起 —— 恢复时读到别人的
        状态，而那份状态看起来完全合法。
        """
        with pytest.raises(ValueError, match="thread_id"):
            await saver.aget_tuple({"configurable": {}})

    async def test_a_missing_thread_id_is_refused_on_write(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """写的那一头同样要挡 —— 读挡了、写没挡，等于没挡。"""
        with pytest.raises(ValueError, match="thread_id"):
            await saver.aput(
                {"configurable": {}},
                _checkpoint("cp-0001"),
                {"source": "update", "step": 0},
                {},
            )

    async def test_before_is_exclusive(self, saver: SqlAlchemyCheckpointSaver) -> None:
        """``before`` 是**排他**的上界 —— "在这个 checkpoint **之前**的那批"。

        含端点的话，"回到第 N 步"会回到第 N 步**自己**，于是永远回不去。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        for index in (1, 2, 3):
            await saver.aput(
                _config(thread),
                _checkpoint(f"cp-000{index}"),
                {"source": "update", "step": index},
                {},
            )

        found = [
            item.checkpoint["id"]
            async for item in saver.alist(_config(thread), before=_config_at(thread, "cp-0002"))
        ]

        assert found == ["cp-0001"]

    async def test_the_latest_checkpoint_is_the_one_returned(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """不带 ``checkpoint_id`` 来取，取到的必须是**最新的**那一个。

        **这一条是"接着跑"的地基。** 取错了就意味着从上一个状态接着跑 —— 上一
        个状态已经跑完的阶段被重跑一遍，而重跑一个调模型的节点既花钱又可能给出
        另一个结论。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        for index in (1, 2, 3):
            await saver.aput(
                _config(thread),
                _checkpoint(f"cp-000{index}"),
                {"source": "update", "step": index},
                {},
            )

        found = await saver.aget_tuple(_config(thread))

        assert found is not None
        assert found.checkpoint["id"] == "cp-0003"

    async def test_the_parent_chain_links_the_checkpoints(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """**父链是连着的**，而且能一路走到根。

        恢复时要沿这条链往回走；链断了，"回到第 N 步"就变成"回到某个孤立的
        checkpoint"，而那一步的上下文已经不在了。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        await saver.aput(
            _config(thread), _checkpoint("cp-0001"), {"source": "update", "step": 1}, {}
        )
        await saver.aput(
            _config_at(thread, "cp-0001"),
            _checkpoint("cp-0002"),
            {"source": "update", "step": 2},
            {},
        )

        found = await saver.aget_tuple(_config_at(thread, "cp-0002"))

        assert found is not None
        assert found.parent_config is not None
        assert found.parent_config["configurable"]["checkpoint_id"] == "cp-0001"

        root = await saver.aget_tuple(_config_at(thread, "cp-0001"))
        assert root is not None
        assert root.parent_config is None, "根 checkpoint 不该有父"

    async def test_the_metadata_records_the_step_and_the_source(
        self, saver: SqlAlchemyCheckpointSaver, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """**元数据要真的记下来。**

        它记的是"这一步是怎么来的" —— 来源、第几步。少了它，回放时间线时只剩
        一串无名的 checkpoint，而"这一步是从哪来的"就再也说不清了。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        await saver.aput(
            _config(thread),
            _checkpoint("cp-0001"),
            {"source": "update", "step": 7},
            {},
        )

        async with factory() as session:
            row = await session.scalar(
                select(GraphCheckpoint).where(GraphCheckpoint.thread_id == thread)
            )

        assert row is not None
        assert row.source == "update"
        assert row.step == 7
        assert row.metadata_["source"] == "update"
        assert row.metadata_["step"] == 7

    async def test_the_writes_come_back_in_idx_order(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        """**写是按 ``idx`` 排的，不是按写进去的先后。**

        同一个 super-step 里的多个任务往同一个通道写，reducer 按顺序把它们合
        起来。顺序错了，重放出来的状态与当初跑出来的不一样 —— 而那看起来完全
        正常。

        这里特意用一个特殊通道（``__error__``）打乱"写进去的先后"与"idx"：
        它被写在**第二个**，而它的 idx 是负数，所以按 idx 排它必须排**第一**。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        await saver.aput(
            _config(thread), _checkpoint("cp-0001"), {"source": "update", "step": 1}, {}
        )
        config = _config_at(thread, "cp-0001")
        await saver.aput_writes(
            config,
            [("plain_channel", "先写的"), ("__error__", "后写的")],
            task_id="task-0001",
        )

        found = await saver.aget_tuple(config)

        assert found is not None
        assert found.pending_writes is not None
        channels = [channel for _, channel, _ in found.pending_writes]
        assert channels == ["__error__", "plain_channel"]

    async def test_writing_the_same_write_twice_changes_nothing(
        self, saver: SqlAlchemyCheckpointSaver, engine: AsyncEngine
    ) -> None:
        """**重复写要幂等 —— 而且是"第一份赢"。**

        节点失败后重跑会把同样的写再写一遍，那是"重试"，不是"重复"。所以已有的
        那份不覆盖（LangGraph 自带的 postgres saver 也是这么做的）。

        这里第二次写的**值不一样**，正是为了区分两件事：真幂等（重写一遍什么
        都没变）与"用新值盖掉旧值"。后者会让一次失败之后的重跑**悄悄改掉**已
        经记下的结果。
        """
        thread = f"thread-{uuid.uuid4().hex}"
        await saver.aput(
            _config(thread), _checkpoint("cp-0001"), {"source": "update", "step": 1}, {}
        )
        config = _config_at(thread, "cp-0001")

        await saver.aput_writes(config, [("plain_channel", "第一次")], task_id="task-0001")
        await saver.aput_writes(config, [("plain_channel", "第二次")], task_id="task-0001")

        async with engine.begin() as conn:
            rows = await conn.execute(
                select(func.count())
                .select_from(GraphCheckpointWrite)
                .where(GraphCheckpointWrite.thread_id == thread)
            )
            assert rows.scalar_one() == 1, "重复写多出了一行"

        found = await saver.aget_tuple(config)
        assert found is not None
        assert found.pending_writes is not None
        values = [value for _, _, value in found.pending_writes]
        assert values == ["第一次"], "已有的写被覆盖了"


class TestTheRoadmapSurvives:
    """**最终产物也要能从 checkpoint 里原样读回来。**"""

    async def test_the_roadmap_is_readable_afterwards(
        self, saver: SqlAlchemyCheckpointSaver
    ) -> None:
        thread = f"thread-{uuid.uuid4().hex}"
        handlers = RecordingHandlers()
        graph = build_decision_graph(handlers.handlers, checkpointer=saver)

        final = await run_decision_graph(graph, _state(), config=_config(thread))

        found = await saver.aget_tuple(_config(thread))
        assert found is not None
        values = found.checkpoint["channel_values"]
        assert isinstance(values["roadmap"], EvolutionRoadmap)
        assert final.roadmap is not None
        assert values["roadmap"].cycle_id == final.roadmap.cycle_id
