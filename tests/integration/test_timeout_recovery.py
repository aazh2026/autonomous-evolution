"""TASK-P1-034 —— 超时处置的真资源验证。

覆盖任务书 T1 ②（超时且会话已损坏 → ``failed``，**且未留下僵尸会话**：断言
容器与进程均已回收）与 T1 ③（重试产生新的幂等键）。

**这一组跑的是真资源：一个真的 docker 容器与一个真的子进程。** 超时处置的
价值恰恰在于"它管的东西是真的"：一个只会说"已回收"的假启动器，能让任何
处置测试都变绿。所以这里断言的是**外部事实** —— 容器在 ``docker ps`` 里查
不到了，进程在操作系统里查不到了 —— 而不是处置方案自己的报告。

**走的全是生产代码。** 中断用真的适配器（它真的会 interrupt、真的会杀进
程），回收用真的取消链（四层齐全），探测用真的适配器。没有任何一层是替身。

**超时不是失败，证据不能被抹掉。** 超时之后调 ``interrupt``，这次执行的结
论仍然是"超时"而不是"已取消" —— 否则"超时 ≠ 失败"这条语义在第一次处置
时就被改写掉了，而处置流程正是靠这条区分才知道该续跑还是该重试。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.task.outcome import ExecutionStatus
from executors.base import ErrorCategory, ExecutionTask, SessionConfig
from executors.claude_code import ClaudeCodeAdapter
from executors.recovery import RecoveryAction, RecoveryOutcome, RecoveryPorts, recover_from_timeout
from harness.sandbox.cancellation import (
    ChainResourceReclaimer,
    LauncherContainerStopper,
    build_cancellation_chain,
)
from harness.sandbox.launcher import SandboxLauncher
from harness.sandbox.policy import build_workspace_sandbox_spec, default_sandbox_policy
from tests.unit.executors.stub_cli import claude_events, read_log, write_stub

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: 打桩 CLI 睡多久。它必须比任务自己的超时长得多 —— 否则它自己就退出了，
#: "这次执行超时了"这句话就没被验到。
_STUB_SLEEP_SECONDS = 300

#: 这次运行的任务超时。给得短一点：等的就是超时本身。
#:
#: **但不能是 1 秒。** 这个超时是**从进程被 spawn 出来那一刻开始算的**，而
#: 子进程从 fork 到真正跑起来（解释器启动、读配置、写下自己的第一行）还要
#: 花掉几十毫秒 —— 机器忙的时候（磁盘被 docker 拉镜像占满、页缓存是冷的）
#: 这段启动时间会明显变长。1 秒的预算把"进程还没起来"和"进程超时了"挤在
#: 了一起：子进程还没写下自己就被掐了，于是"这次执行确实超时了"这个前提
#: 根本立不住 —— 失败时报出来的是"没有产出任何事件"，而真相是"进程压根没
#: 活到能证明自己活着"。
#:
#: 5 秒给了子进程上百倍于正常值的启动余量。代价是这个文件整体多花二十几
#: 秒 —— 它本来就标了 ``slow``，而一个每几轮就假一次的测试比一个慢的测试
#: 贵得多。
_TIMEOUT_SECONDS = 5

#: 这次运行的标识。容器名带它 —— 两次并发运行就不会撞名。
_RUN_TOKEN = str(os.getpid())

#: 容器名。``build_workspace_sandbox_spec`` 按 ``ases-sandbox-<task_id>`` 起
#: 名字 —— 处置要能准确地收掉**这一个**容器，所以名字必须是确定的。
#:
#: **一次运行内确定，跨运行不撞。** 名字不能是全局固定的：两个并发的运行
#: 会撞名，而 docker 会直接拒绝启动第二个容器 —— 那时候失败的**不是被测
#: 的处置流程**，而是测试之间的打架。
_CONTAINER_NAME = f"ases-sandbox-task-timeout-{_RUN_TOKEN}"

#: 处置的时限。取消是**用户等着的**操作，处置也是。
_RECOVERY_BUDGET_SECONDS = 30.0


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

    刻意用 ``sleep``：它会一直在那儿待着，不会自己结束 —— 只有处置能让
    它停下来。

    **起之前先清同名残留。** 上一次运行崩在中间时会留下一个容器，而 docker
    不允许同名容器并存。名字带 PID，所以同名残留只可能是上次跑崩的那一
    个，清掉它不会碰到别的正在跑的测试。
    """
    launcher.stop(_CONTAINER_NAME, grace_seconds=0)
    spec = build_workspace_sandbox_spec(
        workspace_path=workspace,
        task_id=f"task-timeout-{_RUN_TOKEN}",
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


async def _timed_out_session(tmp_path: Path) -> tuple[ClaudeCodeAdapter, str, ExecutionTask, int]:
    """起一个真的执行器会话，让它真的超时。

     **等到超时真的发生才回来。** 后面每个用例都从"这次执行确实超时了"出
     发 —— 否则中断会赶在超时之前把进程掐掉，那测的就不是超时处置，而是取
    消处置。

     返回适配器、会话 id、任务与那个子进程的 PID。
    """
    script = write_stub(
        tmp_path / "cli",
        events=claude_events(),
        sleep_before_exit=_STUB_SLEEP_SECONDS,
    )
    adapter = ClaudeCodeAdapter(binary=str(script))
    session = await adapter.create_session(
        SessionConfig(workspace=str(tmp_path / "ws"), branch="agent/TASK-1")
    )
    task = ExecutionTask(
        session_id=session.session_id,
        task_id="TASK-1",
        title="任务",
        instruction="把 README 的一行改掉",
        timeout_seconds=_TIMEOUT_SECONDS,
    )
    await adapter.execute(task)
    await _wait_until_the_process_is_running(adapter, session.session_id, tmp_path / "cli")
    pid = await _wait_until_the_cli_has_recorded_itself(
        tmp_path / "cli", adapter, session.session_id
    )
    # 掐掉那个进程的是适配器的超时 —— 进程没了，就说明超时真的到过。
    await _wait_until_the_process_is_gone(pid)
    return adapter, session.session_id, task, pid


async def _wait_until_the_process_is_running(
    adapter: ClaudeCodeAdapter, session_id: str, directory: Path
) -> None:
    """等到进程真的跑起来了。

    **用事件流而不是"睡一会儿"。** 适配器是在 ``create_subprocess_exec`` 返
    回之后才把进程记进会话的，事件是那之后才读到的 —— 所以"读到第一条事
    件"就意味着"进程已经起来了"。

    **但"流结束了"不等于"进程起来了"。** 进程若压根没产出任何东西（起不
    来、刚起就被掐了），事件流会直接结束 —— 而一个什么都没等到的等待函
    数，返回时却和"等到了"长得一模一样。于是这里显式地分一次：等到了事
    件就返回，没等到就报错。**空结果必须和"等到了"区分开**，否则调用方会
    拿着一个从没发生过的前提继续往下走。
    """
    events = await adapter.stream_events(session_id)

    async def _first_event() -> None:
        async for _event in events:
            return
        raise AssertionError(await _describe_the_dead_session(adapter, session_id, directory))

    await asyncio.wait_for(_first_event(), timeout=10)


async def _describe_the_dead_session(
    adapter: ClaudeCodeAdapter, session_id: str, directory: Path
) -> str:
    """这次执行没产出任何事件 —— 把现场报出来。

    **"没跑起来"有两种完全不同的坏法**，报出来才能分得清：

    - 打桩 CLI 的日志**不在** —— 进程压根没活到能写下自己的那一行
      （起不来，或刚起就被超时掐了）；
    - 日志**在**（甚至有内容）—— 进程起来了，但它的话没人听见（输出已经
      写进管道，读它的那个协程却被超时取消掉了）。

    两种情况的修法完全不同，所以不能只说"没跑起来"。
    """
    session = adapter._sessions.get(session_id)
    log = directory / "stub_cli.log"
    exists = log.exists()
    size = log.stat().st_size if exists else 0
    status = await adapter.get_status(session_id)
    exit_code = getattr(session, "exit_code", None)
    timed_out = getattr(session, "timed_out", None)
    error = getattr(session, "error", None)
    return (
        f"这次执行没有产出任何事件：打桩 CLI 的日志文件"
        f"{'在' if exists else '不在'}（{size} 字节）—— "
        f"{'进程起来了但输出没人读' if exists else '进程压根没活到写下自己'}；"
        f"会话状态 {status}，退出码 {exit_code}，超时标记 {timed_out}，错误 {error}。"
    )


async def _wait_until_the_cli_has_recorded_itself(
    directory: Path,
    adapter: ClaudeCodeAdapter,
    session_id: str,
    *,
    timeout: float = 30.0,
) -> int:
    """等到打桩 CLI 把自己记下来，返回它的 PID。

    **"看见第一条事件"不等于"日志已经写下来了"。** 事件是适配器转述子进程
    输出之后的结果，日志是子进程**自己**写下的文件 —— 两者之间隔着一次文件
    写入。读完事件就立刻去读日志，是在赌那次写入已经发生；赌输了的代价不
    是"重试一下"，而是一句 ``IndexError``：既不说是哪一步没发生，也不说
    进程当时是什么状态。

    所以这里等的是**外部事实**（子进程自己记下的那一行），等不到就把现
    场报出来：日志文件在不在、多大、会话是什么状态。
    """
    deadline = time.monotonic() + timeout
    while True:
        records = read_log(directory)
        if records:
            return int(records[-1]["pid"])
        if time.monotonic() >= deadline:
            raise AssertionError(
                await _describe_the_missing_record(directory, adapter, session_id, timeout)
            )
        await asyncio.sleep(0.02)


async def _describe_the_missing_record(
    directory: Path,
    adapter: ClaudeCodeAdapter,
    session_id: str,
    waited: float,
) -> str:
    """说清楚"日志里没有记录"这件事当时到底怎么了。

    **报不出现场的失败等于没报。** 只说"读不到日志"，读的人还得自己从头
    复现一遍才知道是哪一步没发生。
    """
    log = directory / "stub_cli.log"
    exists = log.exists()
    size = log.stat().st_size if exists else 0
    status = await adapter.get_status(session_id)
    return (
        f"等了 {waited:.0f} 秒，打桩 CLI 仍没有把自己记下来："
        f"日志文件{'在' if exists else '不在'}（{size} 字节），会话状态 {status}。"
        f"任务的超时间是 {_TIMEOUT_SECONDS} 秒 —— 进程若在起跑阶段就被超时掐"
        f"掉，就来不及留下任何记录。"
    )


async def _wait_until_the_process_is_gone(pid: int, *, timeout: float = 30.0) -> None:
    """等那个进程真的从操作系统里消失。

    ``os.kill(pid, 0)`` 不发信号，只问"这个 PID 还存在吗"。

    **必须让出事件循环。** 掐掉那个进程的是适配器的后台任务，而那个任务要
    在事件循环上跑 —— 用 ``time.sleep`` 阻塞等待的话，循环停摆，超时永远
    不会到期，这里就一直等到超时才报错。报出来的是"进程还在跑"，而真相
    是"没人给它机会死"。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"进程 {pid} 还在跑")


def _assert_the_process_is_gone(pid: int) -> None:
    """从**操作系统**这一侧确认那个进程真的没了。"""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


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


def _reclaimer(adapter: ClaudeCodeAdapter, launcher: SandboxLauncher) -> ChainResourceReclaimer:
    """造一个走生产代码的回收器。

    回收器跑的就是**真的取消链** —— 四层齐全，一层不少。
    """
    chain = build_cancellation_chain(
        interrupter=adapter,
        process_stopper=adapter,
        container_stopper=LauncherContainerStopper(launcher=launcher),
    )
    return ChainResourceReclaimer(chain=chain)


class TestATimeoutThatLeavesARecoverableSession:
    """T1 ① —— 超时且会话可恢复 → ``interrupted_resumable``，可续跑。"""

    async def test_the_outcome_is_interrupted_resumable(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        adapter, _session_id, task, _pid = await _timed_out_session(tmp_path)

        plan = await recover_from_timeout(
            task,
            ports=RecoveryPorts(
                interrupter=adapter,
                probe=adapter,
                reclaimer=_reclaimer(adapter, launcher),
            ),
            container_id=running_container,
        )

        assert plan.outcome is RecoveryOutcome.INTERRUPTED_RESUMABLE
        assert plan.action is RecoveryAction.RESUME
        assert plan.interrupted is True
        assert plan.reclaimed is True

    async def test_the_timeout_is_not_rewritten_as_cancelled(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        """处置之后，这次执行的结论仍然是"超时"。

        **"超时 ≠ 失败"在第一次处置时就被抹掉的话，这条语义就白写了。**
        而处置流程正是靠"超时"与"已取消"这条区分，才知道该续跑还是该重
        试 —— 两者被混成一句话之后，两个动作就分不开了。
        """
        adapter, session_id, task, _pid = await _timed_out_session(tmp_path)

        await recover_from_timeout(
            task,
            ports=RecoveryPorts(
                interrupter=adapter,
                probe=adapter,
                reclaimer=_reclaimer(adapter, launcher),
            ),
            container_id=running_container,
        )

        result = await adapter.collect_result(session_id)
        assert result.status is not ExecutionStatus.CANCELLED
        assert result.status is ExecutionStatus.TIMED_OUT


class TestATimeoutWhoseSessionIsBroken:
    """T1 ② —— 超时且会话已损坏 → ``failed``，且未留下僵尸会话。"""

    async def test_the_outcome_is_failed(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        adapter, _session_id, task, _pid = await _timed_out_session(tmp_path)
        # 会话"损坏"：工作空间没了，工作也就没了 —— 无从接着跑。
        shutil.rmtree(tmp_path / "ws")

        plan = await recover_from_timeout(
            task,
            ports=RecoveryPorts(
                interrupter=adapter,
                probe=adapter,
                reclaimer=_reclaimer(adapter, launcher),
            ),
            container_id=running_container,
            error_category=ErrorCategory.NON_RETRYABLE,
        )

        assert plan.outcome is RecoveryOutcome.FAILED
        assert plan.action is RecoveryAction.FAIL

    async def test_no_zombie_process_and_no_zombie_container_are_left(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        """判失败之后，**容器与进程都已被回收**。

        这正是 T1 ② 的后半句，也是 I-03 要的东西：判失败不能意味着"就那
        样放着"。僵尸容器占着工作空间，而工作空间是别的任务也要用的。
        """
        adapter, _session_id, task, pid = await _timed_out_session(tmp_path)
        shutil.rmtree(tmp_path / "ws")

        await recover_from_timeout(
            task,
            ports=RecoveryPorts(
                interrupter=adapter,
                probe=adapter,
                reclaimer=_reclaimer(adapter, launcher),
            ),
            container_id=running_container,
            error_category=ErrorCategory.NON_RETRYABLE,
        )

        _assert_the_process_is_gone(pid)
        assert launcher.container_running(running_container) is False
        assert not _docker_ps_lists(_CONTAINER_NAME)


class TestTheRetryAfterATimeout:
    """T1 ③ —— 重试产生新的幂等键，不复用旧键。"""

    async def test_the_new_idempotency_key_is_not_the_old_one(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        adapter, _session_id, task, _pid = await _timed_out_session(tmp_path)
        shutil.rmtree(tmp_path / "ws")

        plan = await recover_from_timeout(
            task,
            ports=RecoveryPorts(
                interrupter=adapter,
                probe=adapter,
                reclaimer=_reclaimer(adapter, launcher),
            ),
            container_id=running_container,
            error_category=ErrorCategory.RETRYABLE,
        )

        assert plan.outcome is RecoveryOutcome.INTERRUPTED_RETRYABLE
        assert plan.action is RecoveryAction.RETRY
        assert plan.attempt == task.attempt + 1
        assert plan.idempotency_key is not None
        assert plan.idempotency_key != f"{task.task_id}:{task.attempt}"


class TestTheRecoveryConverges:
    """处置是**用户等着的**操作 —— 它必须在有限时间内收敛。"""

    async def test_it_finishes_within_the_budget(
        self,
        launcher: SandboxLauncher,
        running_container: str,
        tmp_path: Path,
    ) -> None:
        adapter, _session_id, task, _pid = await _timed_out_session(tmp_path)

        started = time.monotonic()
        plan = await recover_from_timeout(
            task,
            ports=RecoveryPorts(
                interrupter=adapter,
                probe=adapter,
                reclaimer=_reclaimer(adapter, launcher),
            ),
            container_id=running_container,
        )
        elapsed = time.monotonic() - started

        assert elapsed < _RECOVERY_BUDGET_SECONDS, f"处置花了 {elapsed:.1f} 秒"
        assert plan.outcome is RecoveryOutcome.INTERRUPTED_RESUMABLE
