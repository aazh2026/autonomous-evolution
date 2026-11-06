"""TASK-P1-033 —— 取消传播链的真资源验证。

覆盖任务书的 T1 ①（取消一个正在执行的沙箱任务，30s 内进程退出且容器停
止）与 T1 ③（取消后 ``collect_result`` 返回明确的 ``cancelled`` 状态而非
``completed``）。

**这一组跑的是真资源：一个真的 docker 容器与一个真的子进程。**

链的价值恰恰在于"它管的东西是真的"：一个只会说"已停止"的假启动器，能让任
何取消链测试都变绿。所以这里断言的是**外部事实** —— 容器在 ``docker ps``
里查不到了，进程在操作系统里查不到了 —— 而不是链自己的报告。

**时间预算是硬断言。** 取消是**用户等着的**操作：30 秒内没停干净，这次取消
就失败了 —— 哪怕它最后停了。链上每层都有期限，正是为了守住这个预算。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.task.cancellation import CancellationTarget, LayerName, LayerState
from core.task.outcome import ExecutionStatus
from executors.base import ExecutionTask, SessionConfig
from executors.claude_code import ClaudeCodeAdapter
from harness.sandbox.cancellation import (
    CancellationChain,
    LauncherContainerStopper,
    build_cancellation_chain,
)
from harness.sandbox.launcher import SandboxLauncher
from harness.sandbox.policy import build_workspace_sandbox_spec, default_sandbox_policy
from tests.unit.executors.stub_cli import claude_events, read_log, write_stub

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: 任务书 T1 ① 给的预算：取消必须在 30 秒内收干净。
CANCELLATION_BUDGET_SECONDS = 30.0

#: 打桩 CLI 睡多久。它必须比取消预算长得多 —— 否则它自己就退出了，
#: "取消掉一个正在跑的任务"这句话就没被验到。
_STUB_SLEEP_SECONDS = 300

#: 这次运行的标识。容器名带它 —— 两次并发运行就不会撞名。
_RUN_TOKEN = str(os.getpid())

#: 容器名。``build_workspace_sandbox_spec`` 按 ``ases-sandbox-<task_id>`` 起
#: 名字 —— 取消要能准确地停掉**这一个**容器，所以名字必须是确定的。
#:
#: **一次运行内确定，跨运行不撞。** 它不能是全局固定的：两个并发的运行
#: 会撞名，而 docker 会直接拒绝启动第二个容器（"Conflict. The container
#: name ... is already in use"）—— 那时候失败的**不是被测的取消链**，而是
#: 测试之间的打架：报出来的是"启动沙箱容器失败"，与取消毫无关系。
#:
#: 用 PID 做后缀解决这件事。名字唯一之后，起容器之前就可以安全地清掉同名
#: 残留 —— 名字一样就说明是同一个 PID，那个残留只可能来自上一次跑崩的
#: 运行，不会抢走另一个正在跑的测试的容器。
_CONTAINER_NAME = f"ases-sandbox-task-cancel-{_RUN_TOKEN}"


@pytest.fixture
def launcher() -> SandboxLauncher:
    return SandboxLauncher(timeout_seconds=120)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = (tmp_path / "workspace").resolve()
    path.mkdir()
    return path


@pytest.fixture
def running_container(
    launcher: SandboxLauncher, sandbox_image: str, workspace: Path
) -> Iterator[str]:
    """起一个真的容器，跑一个长活。

    刻意用 ``sleep``：它会一直在那儿待着，不会自己结束 —— 只有取消能让
    它停下来。

    **收尾也在这里做。** 有一条测试故意让容器停不下来；不收尾的话，它会
    带着名字占着后面的测试。

    **起之前先清同名残留。** 上一次运行崩在中间时会留下一个容器，而
    docker 不允许同名容器并存 —— 起容器就会失败，报的还是"启动沙箱容器
    失败"，跟取消没关系。名字带 PID，所以同名残留只可能是上次跑崩的那
    一个，清掉它不会碰到别的正在跑的测试。
    """
    launcher.stop(_CONTAINER_NAME, grace_seconds=0)
    spec = build_workspace_sandbox_spec(
        workspace_path=workspace,
        task_id=f"task-cancel-{_RUN_TOKEN}",
        image=sandbox_image,
        policy=default_sandbox_policy(),
        command=("sleep", str(_STUB_SLEEP_SECONDS)),
    )
    container_id = launcher.start(spec)
    assert launcher.container_running(container_id) is True
    try:
        yield container_id
    finally:
        launcher.stop(container_id, grace_seconds=1)


async def _running_session(tmp_path: Path) -> tuple[ClaudeCodeAdapter, str]:
    """起一个真的执行器会话，让它的进程真的在跑。"""
    script = write_stub(
        tmp_path / "cli",
        events=claude_events(),
        sleep_before_exit=_STUB_SLEEP_SECONDS,
    )
    adapter = ClaudeCodeAdapter(binary=str(script))
    session = await adapter.create_session(
        SessionConfig(workspace=str(tmp_path / "ws"), branch="agent/TASK-1")
    )
    await adapter.execute(
        ExecutionTask(
            session_id=session.session_id,
            task_id="TASK-1",
            title="任务",
            instruction="把 README 的一行改掉",
            timeout_seconds=600,
        )
    )
    await _wait_until_the_process_is_running(adapter, session.session_id)
    return adapter, session.session_id


async def _wait_until_the_process_is_running(adapter: ClaudeCodeAdapter, session_id: str) -> None:
    """等到进程真的跑起来了。

    **用事件流而不是"睡一会儿"。** 适配器是在 ``create_subprocess_exec``
    返回之后才把进程记进会话的，事件是那之后才读到的 —— 所以"读到第一条
    事件"就意味着"进程已经起来了"。睡固定时长则是在赌调度。
    """
    events = await adapter.stream_events(session_id)

    async def _first_event() -> None:
        async for _event in events:
            return

    await asyncio.wait_for(_first_event(), timeout=10)


def _docker_ps_lists(container_name: str) -> bool:
    """这个容器还在 ``docker ps`` 的名单里吗。

    **独立于启动器问一遍。** 只问启动器的话，验的是"启动器与启动器自己的
    报告一致" —— 那正是最需要防的那种自证。
    """
    completed = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return container_name in completed.stdout


def _assert_the_process_is_gone(pid: int) -> None:
    """从**操作系统**这一侧确认那个进程真的没了。

    ``os.kill(pid, 0)`` 不发信号，只问"这个 PID 还存在吗"。进程被**回收**
    之后它抛 ``ProcessLookupError``；还在跑时它成功。
    """
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _chain(
    adapter: ClaudeCodeAdapter,
    launcher: SandboxLauncher,
) -> CancellationChain:
    """造一条走生产代码的取消链。

    执行器那两层用的是**真的适配器**（它真的会 interrupt、真的会杀进程）；
    容器那层用的是**真的启动器**。没有任何一层是替身。
    """
    return build_cancellation_chain(
        interrupter=adapter,
        process_stopper=adapter,
        container_stopper=LauncherContainerStopper(launcher=launcher),
    )


class TestCancellingARunningSandboxTask:
    """T1 ① —— 取消一个正在执行的沙箱任务。"""

    async def test_the_process_and_the_container_are_both_stopped(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        adapter, session_id = await _running_session(tmp_path)
        pid = read_log(tmp_path / "cli")[-1]["pid"]
        chain = _chain(adapter, launcher)
        target = CancellationTarget(
            task_id="TASK-1",
            session_id=session_id,
            container_id=running_container,
        )

        started = time.monotonic()
        report = await chain.cancel(target)
        elapsed = time.monotonic() - started

        assert elapsed < CANCELLATION_BUDGET_SECONDS, f"取消花了 {elapsed:.1f} 秒"
        assert report.cancelled is True, [outcome.detail for outcome in report.outcomes]
        _assert_the_process_is_gone(pid)
        assert launcher.container_running(running_container) is False
        assert not _docker_ps_lists(_CONTAINER_NAME)

    async def test_every_layer_gave_a_conclusion(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        """四层**各有一条**结论 —— 事后对账时不该缺一条。

        缺一条不是"少了个信息"，是"有一类资源没人管"。
        """
        adapter, session_id = await _running_session(tmp_path)
        chain = _chain(adapter, launcher)

        report = await chain.cancel(
            CancellationTarget(
                task_id="TASK-1",
                session_id=session_id,
                container_id=running_container,
            )
        )

        assert [outcome.layer for outcome in report.outcomes] == [
            LayerName.TASK,
            LayerName.ADAPTER,
            LayerName.PROCESS,
            LayerName.SANDBOX,
        ]
        assert all(outcome.state is LayerState.CONFIRMED for outcome in report.outcomes)

    async def test_the_result_is_cancelled_not_completed(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        """T1 ③ —— 取消之后 ``collect_result`` 给的是"已取消"。"""
        adapter, session_id = await _running_session(tmp_path)
        chain = _chain(adapter, launcher)

        await chain.cancel(
            CancellationTarget(
                task_id="TASK-1",
                session_id=session_id,
                container_id=running_container,
            )
        )
        result = await adapter.collect_result(session_id)

        assert result.status is not ExecutionStatus.COMPLETED, (
            f"取消没有生效：状态={result.status} 详情={result.execution}"
        )
        assert result.status is ExecutionStatus.CANCELLED


class TestTheContainerStateIsReadFromDocker:
    """容器"停掉了但还没删掉"这种状态，也必须被读成"不在运行"。"""

    def test_a_stopped_but_not_removed_container_is_not_running(self, sandbox_image: str) -> None:
        """``exited`` 不等于 ``running``。

        **刻意绕过生产代码起这个容器。** :class:`SandboxLauncher` 总是带
        ``--rm``，所以它起出来的容器退出即消失 —— "停了但还在"这个状态它
        造不出来。而这个状态在真实世界很常见（``docker stop`` 成功、
        ``docker rm`` 失败）。

        把它算成"还在跑"，取消链就会对着一具尸体报"停不掉" —— 用户会看到
        一个永远取消不掉的取消。

        **名字带 PID**：与 :data:`_CONTAINER_NAME` 同理 —— 两次并发的运行
        不能撞名，否则 docker 拒绝启动第二个，报出来的失败与被测的东西
        毫无关系。
        """
        name = f"ases-sandbox-exited-probe-{_RUN_TOKEN}"
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, check=False)
        created = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--network",
                "none",
                "--name",
                name,
                sandbox_image,
                "sleep",
                "60",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        container_id = created.stdout.strip()
        try:
            subprocess.run(
                ["docker", "stop", "-t", "1", name], capture_output=True, text=True, check=True
            )

            launcher = SandboxLauncher(timeout_seconds=60)

            assert launcher.container_running(container_id) is False
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, text=True, check=False
            )


class TestALayerThatCannotDoItsJob:
    """某层停不下来时，链必须**如实**说，而且必须收得住。"""

    async def test_the_chain_converges_and_does_not_claim_success(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        """docker 问不话时，链报"没取消"，而且**很快就报**。

        两件事都要：报"已取消"是撒谎；报不出来是挂死 —— 而挂死的取消比
        撒谎更糟，因为用户会一直等在那里。
        """
        adapter, session_id = await _running_session(tmp_path)
        broken_launcher = SandboxLauncher(docker_binary="根本没有这个docker")
        chain = build_cancellation_chain(
            interrupter=adapter,
            process_stopper=adapter,
            container_stopper=LauncherContainerStopper(launcher=broken_launcher),
        )

        started = time.monotonic()
        report = await chain.cancel(
            CancellationTarget(
                task_id="TASK-1",
                session_id=session_id,
                container_id=running_container,
            )
        )
        elapsed = time.monotonic() - started

        assert elapsed < CANCELLATION_BUDGET_SECONDS
        assert report.cancelled is False
        assert [outcome.layer for outcome in report.unfinished] == [LayerName.SANDBOX]
        # 前面几层**照样收了** —— 一层失败不挡住后面的动作。
        assert report.outcomes[2].state is LayerState.CONFIRMED

        # 真容器还在 —— 链说没停掉，它就确实还在。这两句话必须对上。
        assert launcher.container_running(running_container) is True
        assert _docker_ps_lists(_CONTAINER_NAME)
