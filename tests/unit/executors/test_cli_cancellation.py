"""TASK-P1-033 —— CLI 适配器的进程层：确保那个进程真的不在运行。

**这一组测试守的是"进程层"与"Adapter 层"不是同一件事。**

:meth:`~executors.cli.adapter.CliAdapter.interrupt` 问的是"执行器说自己停
了吗"；:meth:`~executors.cli.adapter.CliAdapter.ensure_process_stopped` 问的
是"那个进程还在吗"。取消传播链把两者分成两层，正是因为执行器可能只停了它
管的**主**进程，而它 spawn 出来的子进程还在跑 —— 那时执行器报的是"已停止"，
而工作空间里还有东西在动。

**这些测试跑的是真的子进程。** 打桩 CLI 会被真的 ``exec``、真的会睡一段时
间、真的能被 SIGKILL 掉。"进程还在不在跑"这件事用假对象测不出来 —— 假对象
的 ``returncode`` 是自己编的。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

import pytest

from core.task.outcome import ExecutionStatus
from executors.base import CancellationState, ExecutionTask, SessionConfig, UnknownSessionError
from executors.claude_code import ClaudeCodeAdapter
from tests.unit.executors.stub_cli import claude_events, read_log, write_stub

#: 等到进程跑起来的上限。超时即失败 —— 等不到说明这一轮验的不是"杀一个
#: 跑着的进程"，而是别的什么东西。
_STARTUP_TIMEOUT_SECONDS = 10.0


def _config(workspace: Path) -> SessionConfig:
    return SessionConfig(workspace=str(workspace), branch="agent/TASK-1")


def _task(session_id: str, *, timeout_seconds: int = 60) -> ExecutionTask:
    return ExecutionTask(
        session_id=session_id,
        task_id="TASK-1",
        title="任务",
        instruction="把 README 的一行改掉",
        timeout_seconds=timeout_seconds,
    )


async def _wait_until_the_process_is_running(adapter: ClaudeCodeAdapter, session_id: str) -> None:
    """等到进程真的跑起来了。

    **为什么用事件流而不是"睡一会儿"。** 适配器是在
    ``create_subprocess_exec`` 返回**之后**才把进程记进会话的，而事件是那
    之后才被读到的 —— 所以"读到第一条事件"就意味着"进程已经起来了"。睡一
    个固定时长则是在赌调度，CI 上慢一点就会变成"断言了一个还没起来的进程"。

    事件流是公开接口：不需要伸手进适配器的私有状态去摸那个进程。
    """
    events = await adapter.stream_events(session_id)

    async def _first_event() -> None:
        async for _event in events:
            return

    await asyncio.wait_for(_first_event(), timeout=_STARTUP_TIMEOUT_SECONDS)


class TestTheProcessIsReallyGone:
    """进程层的核心断言：报"停了"之后，那个进程真的不在了。"""

    async def test_a_running_process_is_killed(self, tmp_path: Path) -> None:
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(),
            sleep_before_exit=300,
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        await _wait_until_the_process_is_running(adapter, session.session_id)

        report = await adapter.ensure_process_stopped(session.session_id)

        assert report.stopped is True
        assert "已被终止" in report.detail

    async def test_the_process_is_dead_after_the_report(self, tmp_path: Path) -> None:
        """报"停了"之后，进程必须**真的**没了。

        这一条是整个进程层的意义所在：报告里的 ``stopped=True`` 必须与事实
        一致，否则取消链就会把一个还在跑的进程当成已经收干净。

        **从操作系统这一侧问，不问适配器。** 问适配器"你那个进程还在吗"没
        有意义 —— 它当然说不在了。
        """
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(),
            sleep_before_exit=300,
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        await _wait_until_the_process_is_running(adapter, session.session_id)
        pid = read_log(tmp_path / "cli")[-1]["pid"]

        await adapter.ensure_process_stopped(session.session_id)

        _assert_the_process_is_gone(pid)


class TestTheProcessLayerEdgeCases:
    async def test_a_session_without_a_process_is_already_stopped(self, tmp_path: Path) -> None:
        """没起过进程 —— 没有进程在跑，后置条件**天然成立**。

        报失败才是撒谎："没有进程"不是"进程没停掉"。
        """
        adapter = ClaudeCodeAdapter(binary=str(tmp_path / "根本没有这个程序"))
        session = await adapter.create_session(_config(tmp_path / "ws"))

        report = await adapter.ensure_process_stopped(session.session_id)

        assert report.stopped is True
        assert "没有起过进程" in report.detail

    async def test_a_process_that_already_exited_is_stopped(self, tmp_path: Path) -> None:
        """进程自己退了 —— 那当然不在运行了。

        这条路径的细节是**退出码**：报出来才知道它是正常退出的还是崩的。
        """
        script = write_stub(tmp_path / "cli", events=claude_events(), exit_code=3)
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        await adapter.collect_result(session.session_id)

        report = await adapter.ensure_process_stopped(session.session_id)

        assert report.stopped is True
        assert "已经退出" in report.detail
        assert "3" in report.detail

    async def test_an_unknown_session_is_rejected(self, tmp_path: Path) -> None:
        """问一个不存在的会话 —— 那是调用方的错，不许悄悄当成"已停止"。

        报"已停止"会让调用方以为那个会话不用管了，而那个会话也许还在跑。
        """
        adapter = ClaudeCodeAdapter(binary=str(tmp_path / "根本没有这个程序"))
        await adapter.create_session(_config(tmp_path / "ws"))

        with pytest.raises(UnknownSessionError):
            await adapter.ensure_process_stopped("根本没有这个会话")

    async def test_a_process_that_survives_the_kill_is_not_reported_as_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """杀不掉时不能报"已停止"。

        **这一条是"报事实"的底线。** 杀掉进程这一步有可能失败 —— 进程卡在
        不可中断的 I/O 里，SIGKILL 递不进去。这时候适配器手上是一个**还活
        着**的进程，而它刚发过"我杀了它"。

        报"已停止"就是撒谎：调用方（取消链的进程层）据此认为这一层完成
        了，于是整条链报"已取消" —— 而那个进程还在跑，继续占着工作空间。

        **怎么造出"杀不掉"。** 真造不出来：SIGKILL 收不到，进程一定死。
        所以这里让"杀"这一步**失效**（把终止动作换成空操作），模拟信号递
        不进去的结果 —— 链看到的现象完全一样：进程还活着，而等待超时了。
        """
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(),
            sleep_before_exit=300,
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        await _wait_until_the_process_is_running(adapter, session.session_id)
        pid = read_log(tmp_path / "cli")[-1]["pid"]

        # 让"杀"失效，并把等待期限缩到 0.2 秒 —— 否则这一条要真的等 10 秒。
        monkeypatch.setattr(adapter, "_kill", _kill_nothing)
        monkeypatch.setattr("executors.cli.adapter._KILL_TIMEOUT_SECONDS", 0.2)

        try:
            report = await adapter.ensure_process_stopped(session.session_id)

            assert report.stopped is False
            assert "没有退出" in report.detail
        finally:
            # 那个进程还活着（正是这一条要证明的）—— 测试收尾时把它收掉。
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


class TestTheCancelledResult:
    """T1 ③ —— 取消之后 ``collect_result`` 给的是"已取消"，不是"已完成"。"""

    async def test_the_result_is_cancelled_not_completed(self, tmp_path: Path) -> None:
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(),
            sleep_before_exit=300,
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        await _wait_until_the_process_is_running(adapter, session.session_id)

        ack = await adapter.interrupt(session.session_id)
        result = await adapter.collect_result(session.session_id)

        assert ack.state is CancellationState.CONFIRMED
        assert result.status is not ExecutionStatus.COMPLETED
        assert result.status is ExecutionStatus.CANCELLED

    async def test_the_self_assessment_does_not_claim_completion(self, tmp_path: Path) -> None:
        """取消掉的执行不许自称做成了。

        ``self_assessment`` 是自我报告，不参与验收判定 —— 但它会出现在报
        表里，说"完成了"就是在说假话。
        """
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(),
            sleep_before_exit=300,
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        await _wait_until_the_process_is_running(adapter, session.session_id)

        await adapter.interrupt(session.session_id)
        result = await adapter.collect_result(session.session_id)

        assert result.self_assessment.completed is False

    async def test_the_status_is_cancelled(self, tmp_path: Path) -> None:
        """取消之后会话状态也必须是"已取消"。

        状态与结果说两件事：状态给轮询的人看，结果给验收的人看。两者必须
        一致 —— 不一致时，轮询的人会以为任务还在跑。
        """
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(),
            sleep_before_exit=300,
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        await _wait_until_the_process_is_running(adapter, session.session_id)

        await adapter.interrupt(session.session_id)

        assert await adapter.get_status(session.session_id) is ExecutionStatus.CANCELLED


def _kill_nothing(_process: asyncio.subprocess.Process) -> None:
    """空操作 —— 模拟"信号递不进去，进程没死"。"""


def _assert_the_process_is_gone(pid: int) -> None:
    """从**操作系统**这一侧确认那个进程真的没了。

    ``os.kill(pid, 0)`` 不发信号，只问"这个 PID 还存在吗"：进程还在跑时
    它成功，进程被**回收**之后它抛 ``ProcessLookupError``。

    **为什么强调"被回收"。** 进程刚死但还没被回收时，它是个僵尸 ——
    ``os.kill`` 仍然成功。而僵尸占着 PID 表项，所以"停干净了"必须包括
    "回收了"。适配器的 ``process.wait()`` 就是干这个的。
    """
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
