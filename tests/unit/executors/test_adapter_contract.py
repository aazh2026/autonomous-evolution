"""Coding Agent Adapter 的契约测试。

**这套测试是"执行器可替换"的唯一客观证据**（docs/tasks.md TASK-P1-030 验证
T1 ②）。design.md §3.4 说"统一执行器的核心接口**应该是**这七个方法" ——
"应该是"不是"就是"。要让这句话落地，得有一样东西能证明某个执行器**确实是**
按这份契约实现的，而不只是"看起来像"。

所以这里有两条规矩：

1. **两个结构不同的实现跑同一段断言。** :class:`InlineAdapter`（同步跑完就
   返回）与 :class:`SessionAdapter`（后台推进、有事件流、有中断确认）差得很
   远，但都跑本文件里的同一套测试。全绿，才是"可替换"的证据。
2. **能力声明必须兑现。** 一个适配器没声明的能力要**明确拒绝**，而不是
   安静地返回空结果 —— 安静地返回空结果，调用方会以为"事件流是空的，
   说明这次没有事件"，而真相是"这个执行器根本没有事件流"。

**契约测试挑的是行为，不是实现方式。** 所以同一个问题在两个实现上的分支
不同（一个支持中断、一个不支持），断言要写成"两条分支各自对"，
而不是"只测支持的那条"。
"""

from __future__ import annotations

import pytest

from core.task.outcome import ExecutionStatus
from executors.base import (
    AgentSession,
    CancellationState,
    Capability,
    CodingAgentAdapter,
    ErrorCategory,
    ExecutionHandle,
    ExecutionResult,
    ExecutionTask,
    ExecutorError,
    SessionConfig,
    UnknownSessionError,
    UnsupportedCapabilityError,
)
from tests.unit.executors.doubles import InlineAdapter, SessionAdapter


def _config(workspace: str = "/tmp/ases-ws") -> SessionConfig:
    return SessionConfig(workspace=workspace, branch="agent/TASK-1", model="test-model")


def _task(session: AgentSession, task_id: str = "TASK-1", attempt: int = 1) -> ExecutionTask:
    return ExecutionTask(
        session_id=session.session_id,
        task_id=task_id,
        title=f"任务 {task_id}",
        instruction="把 README 的一行改掉",
        timeout_seconds=60,
        attempt=attempt,
    )


class TestTheAbstractBaseCannotBeInstantiated:
    """T1 ① —— 抽象基类无法直接实例化。

    不封死这一条，抽象基类就退化成一个"大家记得实现"的约定 —— 而约定是会被
    绕过的。少实现一个方法，就该在**实例化那一刻**炸出来。
    """

    def test_the_base_class_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            CodingAgentAdapter()  # type: ignore[abstract]

    def test_every_contract_member_is_abstract(self) -> None:
        """七方法加 ``name`` 与 ``capabilities``，九个成员一个都不能是具体实现。

        只要有一个是具体实现，它就退化成了"可选的约定" —— 而约定是会被绕过的。
        """
        assert CodingAgentAdapter.__abstractmethods__ == frozenset(
            {
                "capabilities",
                "collect_result",
                "create_session",
                "execute",
                "get_status",
                "interrupt",
                "name",
                "stream_events",
                "terminate",
            }
        )


class TestTheAdapterIsWellFormed:
    def test_the_name_is_a_non_empty_string(self, adapter: CodingAgentAdapter) -> None:
        assert adapter.name.strip()

    def test_the_capabilities_are_declared_capabilities(self, adapter: CodingAgentAdapter) -> None:
        assert adapter.capabilities <= frozenset(Capability)

    def test_the_capabilities_are_immutable(self, adapter: CodingAgentAdapter) -> None:
        """能力声明被改了 = 契约被改了 —— 而契约不该在运行时变。"""
        assert isinstance(adapter.capabilities, frozenset)

    def test_supports_agrees_with_the_declared_set(self, adapter: CodingAgentAdapter) -> None:
        for capability in Capability:
            assert adapter.supports(capability) is (capability in adapter.capabilities)


class TestCreatingASession:
    async def test_the_session_reports_the_workspace_and_the_executor(
        self, adapter: CodingAgentAdapter
    ) -> None:
        session = await adapter.create_session(_config("/tmp/ases-ws-a"))

        assert session.workspace == "/tmp/ases-ws-a"
        assert session.executor == adapter.name

    async def test_the_session_id_is_not_empty(self, adapter: CodingAgentAdapter) -> None:
        session = await adapter.create_session(_config())

        assert session.session_id.strip()

    async def test_two_sessions_get_different_ids(self, adapter: CodingAgentAdapter) -> None:
        """两个会话共用一个 id，后面的事件与结果就分不清是谁的了。"""
        first = await adapter.create_session(_config())
        second = await adapter.create_session(_config())

        assert first.session_id != second.session_id


class TestExecutingATask:
    async def test_the_handle_names_the_task_and_the_session(
        self, adapter: CodingAgentAdapter
    ) -> None:
        session = await adapter.create_session(_config())

        handle = await adapter.execute(_task(session))

        assert isinstance(handle, ExecutionHandle)
        assert handle.session_id == session.session_id
        assert handle.task_id == "TASK-1"
        assert handle.attempt == 1

        # 会话用完必须终止。**这不是收尾的礼貌，是契约的一部分** —— CLI
        # 执行器背后是一个真的子进程，不终止它就等于让一个进程接着跑下去。
        # 留着不终止的会话在测试里表现为"事件循环关掉时留下一句
        # Task was destroyed but it is pending"，在生产里表现为进程泄漏。
        await adapter.terminate(session.session_id)

    async def test_the_result_names_the_task_and_the_executor(
        self, adapter: CodingAgentAdapter
    ) -> None:
        session = await adapter.create_session(_config())
        await adapter.execute(_task(session))

        result = await adapter.collect_result(session.session_id)

        assert result.task_id == "TASK-1"
        assert result.executor == adapter.name

    async def test_the_result_is_in_a_terminal_state(self, adapter: CodingAgentAdapter) -> None:
        """结果是在终态上报的 —— "还在跑"不是结果。"""
        session = await adapter.create_session(_config())
        await adapter.execute(_task(session))

        result = await adapter.collect_result(session.session_id)

        assert result.status.is_terminal

    async def test_the_result_survives_a_json_round_trip(self, adapter: CodingAgentAdapter) -> None:
        """结果要能落库、能传出去 —— 走一趟 JSON 之后必须一模一样。"""
        session = await adapter.create_session(_config())
        await adapter.execute(_task(session))
        result = await adapter.collect_result(session.session_id)

        again = ExecutionResult.model_validate_json(result.model_dump_json())

        assert again == result


class TestTheIdempotencyKey:
    async def test_the_key_is_derived_from_the_task_and_the_attempt(
        self, adapter: CodingAgentAdapter
    ) -> None:
        session = await adapter.create_session(_config())
        task = _task(session)

        assert adapter.idempotency_key(task) == f"{task.task_id}:{task.attempt}"

    async def test_the_key_on_the_result_matches_the_one_on_the_handle(
        self, adapter: CodingAgentAdapter
    ) -> None:
        """结果自带键，核对"同一幂等键返回同一结果"才有依据（I-01）。"""
        session = await adapter.create_session(_config())
        task = _task(session)

        handle = await adapter.execute(task)
        result = await adapter.collect_result(session.session_id)

        assert handle.idempotency_key == adapter.idempotency_key(task)
        assert result.idempotency_key == handle.idempotency_key

    async def test_a_retry_gets_a_different_key(self, adapter: CodingAgentAdapter) -> None:
        """重试必须换键 —— 否则重放去重会把新的一次尝试当成旧的扔掉。"""
        session = await adapter.create_session(_config())

        first = adapter.idempotency_key(_task(session, attempt=1))
        second = adapter.idempotency_key(_task(session, attempt=2))

        assert first != second

    def test_two_different_implementations_derive_the_same_key(self) -> None:
        """键由基类派生，所以换了执行器，重放去重仍然认得同一件事。

        这是"可替换"在**数据**层面的一环：编排层不能因为换了执行器就得换
        去重口径。
        """
        task = ExecutionTask(
            session_id="session-1",
            task_id="TASK-1",
            title="任务",
            instruction="改一行",
            timeout_seconds=60,
        )

        assert InlineAdapter().idempotency_key(task) == SessionAdapter().idempotency_key(task)


class TestUnknownSessions:
    """不认识的会话要报错，不能编一个状态出来。"""

    async def test_an_unknown_session_raises(self, adapter: CodingAgentAdapter) -> None:
        with pytest.raises(UnknownSessionError):
            await adapter.get_status("no-such-session")

    async def test_the_error_carries_a_category_and_the_identity(
        self, adapter: CodingAgentAdapter
    ) -> None:
        """错误必须带分类 —— 否则编排层不知道该重试还是该判失败。"""
        with pytest.raises(UnknownSessionError) as excinfo:
            await adapter.get_status("no-such-session")

        assert excinfo.value.category is ErrorCategory.NON_RETRYABLE
        assert excinfo.value.executor == adapter.name
        assert excinfo.value.session_id == "no-such-session"


class TestTheErrorsCarryTheirCategory:
    """错误分类是**必填**的，不是"可选的补充信息"。

    没有分类，编排层只剩两种选择：一律重试（撞上不可重试的错就一直撞）或
    一律不重试（网络抖动就报废一次执行）。第三类"需人工"就是为这两种都不对
    的场合准备的 —— 授权、预算、口径这类事系统不该自己拍板。
    """

    def test_the_category_is_required(self) -> None:
        with pytest.raises(TypeError):
            ExecutorError("出错了", executor="inline")  # type: ignore[call-arg]

    def test_an_unsupported_capability_is_not_retryable(self) -> None:
        """没声明的能力不会因为重试而冒出来 —— 重试一次只是再错一次。"""
        error = UnsupportedCapabilityError("inline", Capability.STREAM_EVENTS)

        assert error.category is ErrorCategory.NON_RETRYABLE
        assert error.capability is Capability.STREAM_EVENTS


class TestCancellation:
    async def test_interrupting_says_whether_the_session_stopped(
        self, adapter: CodingAgentAdapter
    ) -> None:
        """契约的两条分支：声明了中断就得真的停；没声明就得明确拒绝。"""
        session = await adapter.create_session(_config())
        await adapter.execute(_task(session))

        ack = await adapter.interrupt(session.session_id)

        if adapter.supports(Capability.INTERRUPT):
            assert ack.state is CancellationState.CONFIRMED
            status = await adapter.get_status(session.session_id)
            assert status is not ExecutionStatus.RUNNING
            result = await adapter.collect_result(session.session_id)
            assert result.status is ExecutionStatus.CANCELLED
        else:
            assert ack.state is CancellationState.REFUSED
            assert ack.detail.strip()
            status = await adapter.get_status(session.session_id)
            assert status.is_terminal

    async def test_the_ack_names_the_session(self, adapter: CodingAgentAdapter) -> None:
        session = await adapter.create_session(_config())
        await adapter.execute(_task(session))

        ack = await adapter.interrupt(session.session_id)

        assert ack.session_id == session.session_id


class TestEventStreaming:
    async def test_streaming_yields_the_events_of_the_session_in_order(
        self, adapter: CodingAgentAdapter
    ) -> None:
        session = await adapter.create_session(_config())
        await adapter.execute(_task(session))

        if adapter.supports(Capability.STREAM_EVENTS):
            events = [event async for event in await adapter.stream_events(session.session_id)]

            assert events, "声明了事件流能力却一条事件都没有"
            assert [event.seq for event in events] == sorted(event.seq for event in events)
            assert all(event.session_id == session.session_id for event in events)
            assert all(event.task_id == "TASK-1" for event in events)
        else:
            with pytest.raises(UnsupportedCapabilityError):
                await adapter.stream_events(session.session_id)

    async def test_streaming_an_unknown_session_raises(self, adapter: CodingAgentAdapter) -> None:
        """能力检查在前，会话查找在后 —— 两个实现都得是这个顺序。

        顺序有讲究：能力是**适配器**的属性，与参数无关。一个 incapable 的
        适配器对**任何**会话都给不了事件流，所以先说这一点，调用方才知道
        该换适配器还是换会话。
        """
        if adapter.supports(Capability.STREAM_EVENTS):
            with pytest.raises(UnknownSessionError):
                await adapter.stream_events("no-such-session")
        else:
            with pytest.raises(UnsupportedCapabilityError):
                await adapter.stream_events("no-such-session")


class TestTermination:
    async def test_terminating_twice_is_not_an_error(self, adapter: CodingAgentAdapter) -> None:
        """终止要幂等 —— 清理代码不该因为"已经清过了"而炸。"""
        session = await adapter.create_session(_config())
        await adapter.execute(_task(session))

        await adapter.terminate(session.session_id)
        await adapter.terminate(session.session_id)

        assert (await adapter.get_status(session.session_id)).is_terminal

    async def test_a_terminated_session_is_not_running_any_more(
        self, adapter: CodingAgentAdapter
    ) -> None:
        session = await adapter.create_session(_config())
        await adapter.execute(_task(session))

        await adapter.terminate(session.session_id)

        assert (await adapter.get_status(session.session_id)) is not ExecutionStatus.RUNNING


class TestParallelSessions:
    async def test_two_sessions_do_not_interfere(self, adapter: CodingAgentAdapter) -> None:
        if not adapter.supports(Capability.PARALLEL_SESSIONS):
            pytest.skip("该执行器没有声明并行会话能力")

        first = await adapter.create_session(_config("/tmp/ases-ws-a"))
        second = await adapter.create_session(_config("/tmp/ases-ws-b"))
        await adapter.execute(_task(first, "TASK-A"))
        await adapter.execute(_task(second, "TASK-B"))

        first_result = await adapter.collect_result(first.session_id)
        second_result = await adapter.collect_result(second.session_id)

        assert first_result.task_id == "TASK-A"
        assert second_result.task_id == "TASK-B"
        assert first_result.artifacts.branch != second_result.artifacts.branch
