"""CLI 执行器的共同骨架 —— 进程、事件流、退出码、结果。

**为什么有这一层。** Claude Code 与 Codex 都是"起一个进程、读它的 NDJSON
输出、看它怎么退出"的 CLI。进程管理、事件排队、结果装配这些活儿在两个
适配器里**完全一样**；不一样的只有三件事 —— 怎么拼命令行、一行输出怎么变成
事件、结果事件怎么读出结论。所以共同部分放在这里，两个适配器各写一张
**协议表**。

这正是 design.md R-043 要的："不要强制所有执行器实现同一种底层协议，而是
强制它们实现同一种系统级任务契约。" 底层协议（NDJSON 的事件词汇表）各写
各的；系统级契约（七个方法 + 统一结果）由
:class:`~executors.base.CodingAgentAdapter` 统一。

**会话 ID 由我们给，不由 CLI 给。** CLI 自己会生成一个会话 ID，但那时候我们
已经需要它了 —— 会话是在执行之前就建好的。所以这里自己生成 UUID，用
``--session-id`` 交给 CLI，并**核对** CLI 报回来的那个是不是同一个
（:class:`~executors.cli.errors.SessionMismatchError`）。核对不是多余的：
CLI 忽略了这个参数、或者将来改了行为，我们就会把别的会话的事件记到这个
任务头上 —— 那看起来是成功的，比直接失败坏得多。

**取消必须确认。** :meth:`interrupt` 只有在**进程真的死了**之后才回
``CONFIRMED``。取消传播是四层链（R-096 / I-02），每层确认收到才推进下一层
—— 没有确认就没有推进。

**结果在收集之前不定案。** 进程退出了不等于这次执行有结论：结论在
:meth:`collect_result` 时才根据退出码与结果事件算出来。这条规矩让"取消
请求在结论之前到达"有确定的含义 —— 按取消收场，而不是看谁先到。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from core.task.cancellation import StopReport
from core.task.outcome import ExecutionStatus
from executors.base import (
    AgentSession,
    CancellationAck,
    CancellationState,
    Capability,
    CodingAgentAdapter,
    ErrorCategory,
    ExecutionArtifacts,
    ExecutionEvent,
    ExecutionHandle,
    ExecutionMetrics,
    ExecutionResult,
    ExecutionTask,
    ExecutorError,
    SelfAssessment,
    SessionConfig,
    UnknownSessionError,
    build_idempotency_key,
)
from executors.cli.artifacts import (
    GitWorkspaceInspector,
    WorkspaceInspector,
    fallback_branch,
)
from executors.cli.errors import (
    CliInvocationError,
    CliNotFoundError,
    SessionMismatchError,
)
from executors.cli.events import RawEvent, parse_event_line
from executors.cli.exit_codes import ExitOutcome, classify_exit
from executors.recovery import SessionProbeResult

#: 一条原始事件映射出的统一事件碎片：``(kind, payload)``。
EventFragment = tuple[str, Mapping[str, object]]

#: 读子进程 stdout 单行的上限。一条事件可能很大（结果事件带完整用量），
#: 默认的 64KB 不够。
_STREAM_LIMIT: Final[int] = 4 * 1024 * 1024

#: 杀掉进程后等它真正退出的上限。SIGKILL 之后一般立刻就死了；这个上限是
#: 兜底 —— 万一没死，我们不能无限期挂着。
_KILL_TIMEOUT_SECONDS: Final[float] = 10.0

#: 取凭证的接口：给一个**引用**，拿回一个**短时令牌**。
CredentialProvider = Callable[[str], str]

#: 造子进程环境的接口。沙箱那一侧会把它换成"按白名单重造的环境"。
EnvProvider = Callable[[SessionConfig], Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """一次 CLI 运行的结果摘要 —— 从事件流里读出来的事实。

    :attr:`is_error` 是**三态**，这个区分是
    :func:`~executors.cli.exit_codes.classify_exit` 的前提：

    - ``None`` —— **没有结论**（连结果事件都没有）；
    - ``False`` —— 有结论，且不是失败；
    - ``True`` —— 有结论，且是失败。

    "没结论"与"结论是成功"走的是不同的分支：前者不能当成成功 —— 把"没出
    错"当"做成了"，那正是"进程正常退出但什么都没产出"这种假成功的来源。
    """

    is_error: bool | None = None
    api_error_status: int | None = None
    cost: Decimal | None = None
    tool_calls: int = 0
    duration_ms: int | None = None
    text: str | None = None


@dataclass
class _CliSession:
    """一个 CLI 会话的内部状态。"""

    session_id: str
    executor: str
    workspace: str
    created_at: datetime
    config: SessionConfig
    task: ExecutionTask | None = None
    status: ExecutionStatus = ExecutionStatus.PENDING
    events: list[ExecutionEvent] = field(default_factory=list)
    raws: list[RawEvent] = field(default_factory=list)
    result: ExecutionResult | None = None
    error: ExecutorError | None = None
    baseline: str | None = None
    cancel_requested: bool = False
    timed_out: bool = False
    exit_code: int | None = None
    reported_session_id: str | None = None
    process: asyncio.subprocess.Process | None = None
    worker: asyncio.Task[None] | None = None
    started_at: datetime | None = None
    finished: bool = False
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class CliAdapter(CodingAgentAdapter, ABC):
    """CLI 执行器的骨架。

    子类要填的三件事就是"协议"本身：怎么拼命令行、一行输出怎么变成事件、
    结果事件怎么读出结论。其余（进程、排队、退出码、结果装配）都在这里。
    """

    #: 凭证放进子进程环境时用的变量名。子类覆盖它。
    credential_env_name: str = ""

    #: 币种 —— 成本必须带币种才能和预算比（R-039 要求三处成本同源同币种）。
    currency: str = "USD"

    #: CLI 报会话 ID 时用的字段名。
    session_id_field: str = "session_id"

    def __init__(
        self,
        *,
        name: str,
        binary: str,
        credential_provider: CredentialProvider | None = None,
        env_provider: EnvProvider | None = None,
        inspector: WorkspaceInspector | None = None,
    ) -> None:
        self._name = name
        self._binary = binary
        self._credential_provider = credential_provider
        self._env_provider = env_provider
        self._inspector: WorkspaceInspector = inspector or GitWorkspaceInspector()
        self._sessions: dict[str, _CliSession] = {}

    # ------------------------------------------------------------------
    # 协议：子类填的三件事
    # ------------------------------------------------------------------

    @abstractmethod
    def build_command(
        self,
        config: SessionConfig,
        task: ExecutionTask,
        session_id: str,
    ) -> tuple[str, ...]:
        """这次执行的命令行。"""

    @abstractmethod
    def map_event(self, raw: RawEvent) -> Sequence[EventFragment]:
        """一行原始事件 → 若干条统一事件。

        返回空序列表示"这一行没有值得记的事件"。返回 ``("unhandled", ...)``
        表示"不认识，但原样记下来" —— 信息不丢，将来核对协议时用得上。
        """

    @abstractmethod
    def summarize(self, raws: Sequence[RawEvent]) -> RunSummary:
        """把整段事件流读成一份摘要。"""

    # ------------------------------------------------------------------
    # 契约
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def binary(self) -> str:
        """可执行文件名。"""
        return self._binary

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.STREAM_EVENTS,
                Capability.INTERRUPT,
                Capability.PARALLEL_SESSIONS,
                Capability.STRUCTURED_OUTPUT,
            }
        )

    async def create_session(self, config: SessionConfig) -> AgentSession:
        workspace = Path(config.workspace)
        # 目录必须存在 —— CLI 需要一个能 chdir 进去的地方。目录不存在时进程
        # 根本起不来，那时我们连一行事件都看不到，报错会指向"进程起不来"
        # 而不是"工作空间没了"，白跑一趟。
        workspace.mkdir(parents=True, exist_ok=True)
        session_id = str(uuid.uuid4())
        created_at = datetime.now(UTC)
        self._sessions[session_id] = _CliSession(
            session_id=session_id,
            executor=self.name,
            workspace=str(workspace),
            created_at=created_at,
            config=config,
        )
        return AgentSession(
            session_id=session_id,
            executor=self.name,
            workspace=str(workspace),
            created_at=created_at,
        )

    async def execute(self, task: ExecutionTask) -> ExecutionHandle:
        session = self._session(task.session_id)
        if session.task is not None:
            raise ExecutorError(
                "这个会话已经有任务在跑",
                category=ErrorCategory.NON_RETRYABLE,
                executor=self.name,
                task_id=task.task_id,
                session_id=session.session_id,
            )
        session.task = task
        session.status = ExecutionStatus.RUNNING
        session.started_at = datetime.now(UTC)
        session.baseline = self._inspector.baseline(Path(session.workspace))
        # 留住 worker 的引用：没人引着的 asyncio 任务可能被回收掉，
        # 那样这次执行就凭空消失了，而调用方只会看到"永远等不到结果"。
        session.worker = asyncio.create_task(self._run(session, task))
        return ExecutionHandle(
            session_id=session.session_id,
            task_id=task.task_id,
            idempotency_key=self.idempotency_key(task),
            attempt=task.attempt,
            accepted_at=datetime.now(UTC),
        )

    async def get_status(self, session_id: str) -> ExecutionStatus:
        return self._session(session_id).status

    async def stream_events(self, session_id: str) -> AsyncIterator[ExecutionEvent]:
        # 能力检查在前，会话查找在后 —— 顺序与另外两个实现一致。
        self.require_capability(Capability.STREAM_EVENTS)
        session = self._session(session_id)
        return self._event_stream(session)

    async def interrupt(self, session_id: str) -> CancellationAck:
        session = self._session(session_id)
        if session.result is not None:
            # 结果已经定案 —— 这次执行有结论了，取消无从谈起。
            return CancellationAck(
                session_id=session_id,
                state=CancellationState.CONFIRMED,
                detail="会话已结束，结果已定案",
            )
        session.cancel_requested = True
        process = session.process
        if process is not None and process.returncode is None:
            self._kill(process)
        try:
            await asyncio.wait_for(session.done.wait(), timeout=_KILL_TIMEOUT_SECONDS)
        except TimeoutError:
            return CancellationAck(
                session_id=session_id,
                state=CancellationState.REFUSED,
                detail=f"进程在 {_KILL_TIMEOUT_SECONDS:.0f} 秒内没有退出，无法确认已停止",
            )
        if session.timed_out:
            # 这次执行是**超时**结束的，不是被取消的 —— 两个不同的状态
            # （R-097）。中断不能把它改写成"已取消"：那是抹掉证据，超时与
            # 失败就再也分不开了。超时处置（TASK-P1-034）正是靠这条区分，才
            # 知道该续跑还是该重试。
            session.status = ExecutionStatus.TIMED_OUT
            session.result = self._build_result(
                session, status=ExecutionStatus.TIMED_OUT, summary=None
            )
            return CancellationAck(
                session_id=session_id,
                state=CancellationState.CONFIRMED,
                detail="这次执行已经超时结束",
            )
        session.status = ExecutionStatus.CANCELLED
        session.result = self._build_result(session, status=ExecutionStatus.CANCELLED, summary=None)
        detail = "执行进程已终止"
        if session.error is not None:
            detail = f"{detail}（期间记录到错误：{session.error}）"
        return CancellationAck(
            session_id=session_id,
            state=CancellationState.CONFIRMED,
            detail=detail,
        )

    async def collect_result(self, session_id: str) -> ExecutionResult:
        session = self._session(session_id)
        settled = self._settled_result(session)
        if settled is not None:
            return settled
        if session.task is None:
            raise ExecutorError(
                "会话尚未执行，没有可收集的结果",
                category=ErrorCategory.NON_RETRYABLE,
                executor=self.name,
                session_id=session_id,
            )
        await session.done.wait()
        settled = self._settled_result(session)
        if settled is not None:
            return settled
        if session.error is not None:
            raise session.error
        summary = self.summarize(session.raws)
        outcome = self._verdict(session, summary)
        session.status = outcome.status
        session.result = self._build_result(session, status=outcome.status, summary=summary)
        return session.result

    @staticmethod
    def _settled_result(session: _CliSession) -> ExecutionResult | None:
        """取已经定案的结果，没有就返回 ``None``。

        **绕一层函数是为了让类型检查器忘掉上一轮的收窄。** 结果完全可能
        在 ``await`` 期间被别的协程定案（中断那条路径就会），而收窄不会
        随 ``await`` 失效 —— 不绕这一层，第二道检查会被判成"不可达"。
        """
        return session.result

    @staticmethod
    def _cancel_requested(session: _CliSession) -> bool:
        """读一次取消标记。

        **和 :meth:`_settled_result` 同一个理由。** 取消请求是在别的协程里
        置上的，而类型检查器不会因为中间隔了一次 ``await`` 就忘掉上一次
        判断的结果 —— 直接读字段，第二次检查会被判成"不可达"。那条窄
        窗口恰恰是这里要堵的，所以这个绕法不是为了让检查器闭嘴，是为了让
        **检查点真的存在**。
        """
        return session.cancel_requested

    async def terminate(self, session_id: str) -> None:
        session = self._session(session_id)
        session.cancel_requested = True
        process = session.process
        if process is not None and process.returncode is None:
            self._kill(process)
        # 等后台任务真的收工：不等的话，测试结束时它还挂着，事件循环关掉
        # 会留下一句"Task was destroyed but it is pending"。
        worker = session.worker
        if worker is not None and not worker.done():
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(worker, timeout=_KILL_TIMEOUT_SECONDS)
        if not session.status.is_terminal:
            session.status = ExecutionStatus.CANCELLED
        if session.result is None and session.task is not None:
            session.result = self._build_result(
                session, status=ExecutionStatus.CANCELLED, summary=None
            )

    async def ensure_process_stopped(self, session_id: str) -> StopReport:
        """确保这个会话背后的进程真的不在运行。

        **这一层与 :meth:`interrupt` 不是同一件事。** :meth:`interrupt` 问的
        是"执行器说自己停了吗"，这一层问的是"那个进程还在吗" —— 取消传播
        链（``harness.sandbox.cancellation``）把两者分开，因为执行器可能只
        停了它管的**主**进程，而它 spawn 出来的子进程还在跑。

        这里是**唯一**知道那个进程是谁的地方，所以只有执行器能回答这个
        问题。
        """
        session = self._session(session_id)
        process = session.process
        if process is None:
            # 没起过进程 —— 没有进程在跑，后置条件天然成立。
            return StopReport(stopped=True, detail="这个会话没有起过进程")
        if process.returncode is not None:
            return StopReport(
                stopped=True,
                detail=f"进程已经退出（退出码 {process.returncode}）",
            )
        self._kill(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=_KILL_TIMEOUT_SECONDS)
        except TimeoutError:
            return StopReport(
                stopped=False,
                detail=f"进程在 {_KILL_TIMEOUT_SECONDS:.0f} 秒内没有退出",
            )
        return StopReport(stopped=True, detail="进程已被终止")

    async def probe_session(self, session_id: str) -> SessionProbeResult:
        """这个会话还能不能接着跑。

        **问的是"工作还在不在"，不是"CLI 的对话上下文能不能恢复"。** 后者
        是 CLI 的内部实现（Claude Code 把它写在会话记录里），我们不猜它的
        格式 —— 猜错了就会得出一个假的"能续跑"，而假的"能续跑"会让编排层
        去续一个其实已经没了的会话。

        所以这里只查两件**我们真的知道**的事：这个会话我们还认不认，以及它
        的工作空间还在不在。两件都在，就说"可以接着跑"。

        **认不认得会话，与工作空间在不在，是两件事。** 会话记录没了（执行
        器重启过、租约被回收）说明我们无从接着跑；工作空间没了说明工作已
        经丢了 —— 那种情况下接着跑没有意义，重做一次才是对的。这两种情形
        都报"续不上"，但理由不同：事后对账时要能分清是哪一种。
        """
        try:
            session = self._session(session_id)
        except UnknownSessionError:
            return SessionProbeResult(resumable=False, reason="执行器不认这个会话了")
        if not Path(session.workspace).exists():
            return SessionProbeResult(resumable=False, reason="工作空间没了 —— 工作已经丢了")
        return SessionProbeResult(resumable=True, reason="会话还在，工作空间也在")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _session(self, session_id: str) -> _CliSession:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise UnknownSessionError(self.name, session_id) from None

    async def _run(self, session: _CliSession, task: ExecutionTask) -> None:
        try:
            if self._cancel_requested(session):
                # 取消在进程起来之前就到了 —— 根本不用起它。起了再杀是
                # 浪费，而且会给"这次执行到底跑了什么"留下一条含糊的记录。
                session.status = ExecutionStatus.CANCELLED
                return
            command = self.build_command(session.config, task, session.session_id)
            env = self._build_env(session.config)
            workspace = Path(session.workspace)
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(workspace),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_STREAM_LIMIT,
                )
            except FileNotFoundError as exc:
                raise CliNotFoundError(command[0], executor=self.name) from exc
            except OSError as exc:
                raise CliInvocationError(
                    f"CLI 进程起不来：{exc}",
                    executor=self.name,
                    task_id=task.task_id,
                    session_id=session.session_id,
                ) from exc
            session.process = process
            if self._cancel_requested(session):
                # 取消在进程起来之前就到了 —— 刚起就杀。
                #
                # 这一步堵的是一个窄窗口：:meth:`interrupt` 若正好落在"进程
                # 已经 spawn、但还没记进 ``session.process``"之间，它看到的
                # 是"没有进程可杀"，于是这个进程就被放跑了。
                self._kill(process)
            try:
                await asyncio.wait_for(
                    self._supervise(session, process),
                    timeout=task.timeout_seconds,
                )
            except TimeoutError:
                session.timed_out = True
                self._kill(process)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=_KILL_TIMEOUT_SECONDS)
        except ExecutorError as exc:
            session.error = exc
            if not session.cancel_requested:
                session.status = ExecutionStatus.FAILED
        except Exception as exc:
            session.error = ExecutorError(
                f"CLI 执行异常：{exc}",
                category=ErrorCategory.RETRYABLE,
                executor=self.name,
                task_id=task.task_id,
                session_id=session.session_id,
            )
            if not session.cancel_requested:
                session.status = ExecutionStatus.FAILED
        finally:
            session.finished = True
            session.wakeup.set()
            session.done.set()

    async def _supervise(self, session: _CliSession, process: asyncio.subprocess.Process) -> None:
        """读 stdout 直到 EOF，然后等进程退出。

        stderr 单独读 —— 两个管道都得有人读，否则写满缓冲区的一方会把进程
        卡死，而卡死的进程看起来就像"任务很慢"。
        """
        stderr_task = asyncio.create_task(self._drain_stderr(process))
        try:
            await self._read_stdout(session, process)
        except BaseException:
            # 读流出错（比如输出坏了）—— 进程不能留着继续跑。
            self._kill(process)
            raise
        finally:
            await asyncio.gather(stderr_task, return_exceptions=True)
        session.exit_code = await process.wait()

    async def _read_stdout(self, session: _CliSession, process: asyncio.subprocess.Process) -> None:
        stdout = process.stdout
        if stdout is None:
            return
        while True:
            chunk = await stdout.readline()
            if not chunk:
                return
            line = chunk.decode("utf-8", errors="replace")
            event = parse_event_line(line, executor=self.name)
            if event is None:
                continue
            self._record(session, event)

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        stderr = process.stderr
        if stderr is None:
            return
        while True:
            chunk = await stderr.readline()
            if not chunk:
                return

    def _record(self, session: _CliSession, raw: RawEvent) -> None:
        self._assert_session_matches(session, raw)
        session.raws.append(raw)
        for kind, payload in self.map_event(raw):
            event = ExecutionEvent(
                session_id=session.session_id,
                task_id=session.task.task_id if session.task is not None else "",
                seq=len(session.events) + 1,
                kind=kind,
                occurred_at=datetime.now(UTC),
                payload=payload,
            )
            session.events.append(event)
        session.wakeup.set()

    def _assert_session_matches(self, session: _CliSession, raw: RawEvent) -> None:
        """核对 CLI 报出来的会话 ID。

        报出来就必须对得上：对不上就说明这条事件流不是这个会话的，硬读下去
        会把别的会话的产出记到这个任务头上。
        """
        reported = raw.get(self.session_id_field)
        if reported is None:
            return
        if not isinstance(reported, str):
            raise SessionMismatchError(
                session.session_id,
                None,
                executor=self.name,
                task_id=session.task.task_id if session.task is not None else None,
            )
        session.reported_session_id = reported
        if reported != session.session_id:
            raise SessionMismatchError(
                session.session_id,
                reported,
                executor=self.name,
                task_id=session.task.task_id if session.task is not None else None,
            )

    async def _event_stream(self, session: _CliSession) -> AsyncIterator[ExecutionEvent]:
        cursor = 0
        while True:
            while cursor < len(session.events):
                yield session.events[cursor]
                cursor += 1
            if session.finished:
                if session.error is not None:
                    raise session.error
                return
            session.wakeup.clear()
            # 清空之后再看一眼 —— 清空与写入之间可能刚好错过一次唤醒。
            if cursor < len(session.events) or session.finished:
                continue
            await session.wakeup.wait()

    def _verdict(self, session: _CliSession, summary: RunSummary) -> ExitOutcome:
        if session.cancel_requested:
            return ExitOutcome(
                ExecutionStatus.CANCELLED,
                ErrorCategory.RETRYABLE,
                "执行已被取消",
            )
        exit_code = session.exit_code if session.exit_code is not None else -1
        return classify_exit(
            exit_code,
            is_error=summary.is_error,
            api_error_status=summary.api_error_status,
            timed_out=session.timed_out,
        )

    def _build_result(
        self,
        session: _CliSession,
        *,
        status: ExecutionStatus,
        summary: RunSummary | None,
    ) -> ExecutionResult:
        task = session.task
        if task is None:
            raise ExecutorError(
                "会话尚未执行，没有可收集的结果",
                category=ErrorCategory.NON_RETRYABLE,
                executor=self.name,
                session_id=session.session_id,
            )
        artifacts = self._resolve_artifacts(session, task.task_id)
        metrics = self._build_metrics(session, summary)
        return ExecutionResult(
            task_id=task.task_id,
            executor=self.name,
            status=status,
            artifacts=artifacts,
            execution=metrics,
            # CLI 协议没有结构化的自我报告字段。这里用"这次执行是否成功"
            # 作保守近似：**不成功就绝不声称完成**。
            self_assessment=SelfAssessment(completed=status is ExecutionStatus.COMPLETED),
            idempotency_key=build_idempotency_key(task.task_id, task.attempt),
        )

    def _resolve_artifacts(self, session: _CliSession, task_id: str) -> ExecutionArtifacts:
        state = self._inspector.snapshot(Path(session.workspace), session.baseline)
        branch = state.branch or fallback_branch(task_id)
        return ExecutionArtifacts(
            branch=branch,
            commit=state.commit,
            changed_files=state.changed_files,
        )

    def _build_metrics(self, session: _CliSession, summary: RunSummary | None) -> ExecutionMetrics:
        duration = self._duration_seconds(session, summary)
        cost = summary.cost if summary is not None and summary.cost is not None else Decimal(0)
        tool_calls = summary.tool_calls if summary is not None else 0
        return ExecutionMetrics(
            duration=duration,
            tool_calls=tool_calls,
            cost=cost,
            currency=self.currency,
        )

    def _duration_seconds(self, session: _CliSession, summary: RunSummary | None) -> Decimal:
        if summary is not None and summary.duration_ms is not None:
            return Decimal(summary.duration_ms) / Decimal(1000)
        if session.started_at is not None:
            # 毫秒取整再换算：``total_seconds()`` 给的是浮点数，直接
            # ``Decimal(float)`` 会把 0.12 变成 0.12000000000000000266…，
            # 而时长是要被累加、被比较的数。
            elapsed_ms = int((datetime.now(UTC) - session.started_at).total_seconds() * 1000)
            return Decimal(elapsed_ms) / Decimal(1000)
        return Decimal(0)

    def _build_env(self, config: SessionConfig) -> dict[str, str]:
        if self._env_provider is not None:
            env = dict(self._env_provider(config))
        else:
            # 沙箱那一侧会注入 env_provider；没有注入时继承宿主环境 ——
            # 这也是为什么"沙箱隔离必须先于执行器接入"（docs/tasks.md §2
            # 的 EP 顺序约束）。
            env = dict(os.environ)
        env.update(config.env)
        credential_ref = config.credential_ref
        if credential_ref is None:
            return env
        if self._credential_provider is None:
            raise ExecutorError(
                "配置里带了凭证引用，却没有取用它的办法",
                category=ErrorCategory.NON_RETRYABLE,
                executor=self.name,
            )
        if not self.credential_env_name:
            raise ExecutorError(
                "配置里带了凭证引用，但执行器没有声明凭证放进哪个环境变量",
                category=ErrorCategory.NON_RETRYABLE,
                executor=self.name,
            )
        token = self._credential_provider(credential_ref)
        if not token:
            raise ExecutorError(
                f"凭证引用 {credential_ref!r} 取不到令牌",
                category=ErrorCategory.NEEDS_HUMAN,
                executor=self.name,
            )
        # 凭证只走环境变量：它不进 argv、不进 Prompt —— argv 会出现在 ps 的
        # 输出里，而 Prompt 会被回传给模型。
        env[self.credential_env_name] = token
        return env

    @staticmethod
    def _kill(process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


__all__ = [
    "CliAdapter",
    "CredentialProvider",
    "EnvProvider",
    "EventFragment",
    "RunSummary",
]
