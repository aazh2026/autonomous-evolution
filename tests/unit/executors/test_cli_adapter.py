"""CLI 适配器的端到端行为 —— 真的起进程，真的读 NDJSON。

**这些测试跑的是真的子进程。** 打桩 CLI 是一个真的可执行文件：它真的被
``exec``、真的往 stdout 写 NDJSON、真的按配置的退出码退出、真的能被 SIGKILL。
这一点很重要 —— 进程边界上的那些坑（stdout 没人读、stderr 写满缓冲区把
进程卡死、SIGKILL 之后还要等它真的退出）假对象一个都测不出来。

但它**不调真实 Agent**：不联网、不花钱、不碰工作空间里的任何文件。

对应 docs/tasks.md TASK-P1-031 的验证 T1：

- ① 合法 NDJSON 事件流被解析为统一事件序列 → :class:`TestTheEventStream`
- ② 非零退出码映射为正确状态 → :class:`TestTheExitCode`
- ③ 畸形输出归类为"不可重试"错误 → :class:`TestBrokenOutput`
- ④ 打桩 CLI 跑通 ``TASK-P1-030`` 的全套契约测试 → 由
  ``tests/unit/executors/conftest.py`` 把 CLI 适配器接进契约套件
"""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from core.task.outcome import ExecutionStatus
from executors.base import (
    ErrorCategory,
    ExecutionTask,
    ExecutorError,
    SessionConfig,
)
from executors.claude_code import ClaudeCodeAdapter
from executors.claude_code.command import build_command
from executors.cli.errors import CliNotFoundError, MalformedOutputError, SessionMismatchError
from executors.codex import CodexAdapter
from executors.gemini_cli import GeminiCliAdapter
from executors.opencode import OpenCodeAdapter
from tests.unit.executors.conftest import ADAPTERS
from tests.unit.executors.stub_cli import (
    claude_events,
    codex_events,
    gemini_cli_events,
    opencode_events,
    read_log,
    write_stub,
)


def _config(workspace: Path, *, credential_ref: str | None = None) -> SessionConfig:
    return SessionConfig(
        workspace=str(workspace),
        branch="agent/TASK-1",
        credential_ref=credential_ref,
    )


def _task(session_id: str, *, task_id: str = "TASK-1", timeout_seconds: int = 30) -> ExecutionTask:
    return ExecutionTask(
        session_id=session_id,
        task_id=task_id,
        title="任务",
        instruction="把 README 的一行改掉",
        timeout_seconds=timeout_seconds,
    )


class TestTheEventStream:
    """T1 ① —— 合法 NDJSON 事件流被解析为统一事件序列。"""

    async def test_the_events_arrive_in_order(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=claude_events())
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        events = [event async for event in await adapter.stream_events(session.session_id)]

        assert [event.kind for event in events] == [
            "session.started",
            "message.assistant",
            "tool.started",
            "result",
        ]

    async def test_the_sequence_numbers_are_increasing(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=claude_events())
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        events = [event async for event in await adapter.stream_events(session.session_id)]

        assert [event.seq for event in events] == sorted(event.seq for event in events)

    async def test_the_events_belong_to_the_session_and_the_task(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=claude_events())
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id, task_id="TASK-7"))

        events = [event async for event in await adapter.stream_events(session.session_id)]

        assert all(event.session_id == session.session_id for event in events)
        assert all(event.task_id == "TASK-7" for event in events)

    async def test_the_cost_and_the_tool_calls_are_reported(self, tmp_path: Path) -> None:
        """成本与工具次数从**结果事件**里读 —— 那是 CLI 报的事实。"""
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(cost_usd=0.25, tool_calls=3, duration_ms=1755),
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        result = await adapter.collect_result(session.session_id)

        assert result.execution.cost == Decimal("0.25")
        assert result.execution.tool_calls == 3
        assert result.execution.duration == Decimal("1.755")
        assert result.execution.currency == "USD"


class TestTheExitCode:
    """T1 ② —— 非零退出码映射为正确状态。"""

    async def test_a_successful_run_is_completed(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=claude_events(), exit_code=0)
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        result = await adapter.collect_result(session.session_id)

        assert result.status is ExecutionStatus.COMPLETED

    async def test_a_non_zero_exit_code_is_failed(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=[], exit_code=1)
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        result = await adapter.collect_result(session.session_id)

        assert result.status is ExecutionStatus.FAILED

    async def test_a_usage_error_is_not_retryable(self, tmp_path: Path) -> None:
        """退出码 2 = 参数写错了 —— 同样的参数只会再错一次。"""
        script = write_stub(tmp_path / "cli", events=[], exit_code=2)
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        result = await adapter.collect_result(session.session_id)

        assert result.status is ExecutionStatus.FAILED

    async def test_a_success_reported_with_a_bad_exit_code_is_failed(self, tmp_path: Path) -> None:
        """CLI 说"我成功了"，但进程没能正常退出 —— 判失败。

        拿"成功"当结论会掩盖"退出阶段炸了"这件事，而退出阶段炸了的工作
        空间未必干净。
        """
        script = write_stub(tmp_path / "cli", events=claude_events(), exit_code=1)
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        result = await adapter.collect_result(session.session_id)

        assert result.status is ExecutionStatus.FAILED

    async def test_a_timeout_is_its_own_status(self, tmp_path: Path) -> None:
        """超时不等于失败 —— 并进 FAILED 会直接判死，那正是僵尸会话的来源。"""
        script = write_stub(tmp_path / "cli", events=claude_events(), sleep_before_exit=30)
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id, timeout_seconds=1))

        result = await adapter.collect_result(session.session_id)

        assert result.status is ExecutionStatus.TIMED_OUT


class TestBrokenOutput:
    """T1 ③ —— 畸形输出归类为"不可重试"错误。"""

    async def test_output_that_is_not_json_raises(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=[], raw_lines=("{这不是 JSON}",))
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        with pytest.raises(MalformedOutputError) as excinfo:
            await adapter.collect_result(session.session_id)

        assert excinfo.value.category is ErrorCategory.NON_RETRYABLE

    async def test_output_that_is_not_an_object_raises(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=["只是一个字符串"], raw_lines=())
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        with pytest.raises(MalformedOutputError):
            await adapter.collect_result(session.session_id)

    async def test_a_missing_binary_is_not_retryable(self, tmp_path: Path) -> None:
        """可执行文件不在是**部署问题** —— 重试一万次也装不上它。"""
        adapter = ClaudeCodeAdapter(binary=str(tmp_path / "根本没有这个程序"))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        with pytest.raises(CliNotFoundError) as excinfo:
            await adapter.collect_result(session.session_id)

        assert excinfo.value.category is ErrorCategory.NON_RETRYABLE


class TestTheSessionId:
    """会话 ID 由我们给、由 CLI 回显、由我们核对。"""

    async def test_it_is_handed_to_the_cli(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=claude_events())
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        await adapter.collect_result(session.session_id)

        record = read_log(tmp_path / "cli")[-1]

        assert record["session_id"] == session.session_id
        assert "--session-id" in record["argv"]

    async def test_a_mismatched_session_id_is_rejected(self, tmp_path: Path) -> None:
        """CLI 悄悄换掉会话 ID 时必须炸出来。

        不核对的话，别的会话的事件会被记到这个任务头上 —— 而那看起来是
        成功的，比直接失败坏得多。
        """
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(),
            reported_session_id="00000000-0000-4000-8000-000000000000",
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        with pytest.raises(SessionMismatchError) as excinfo:
            await adapter.collect_result(session.session_id)

        assert excinfo.value.category is ErrorCategory.NON_RETRYABLE


class TestTheCredential:
    async def test_it_goes_into_the_environment_not_the_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """长期密钥不进 argv —— argv 会出现在 ``ps`` 的输出里。"""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(),
            recorded_env=("ANTHROPIC_API_KEY",),
        )
        adapter = ClaudeCodeAdapter(
            binary=str(script),
            credential_provider=lambda _ref: "sk-测试令牌",
        )
        session = await adapter.create_session(
            _config(tmp_path / "ws", credential_ref="secret://anthropic/default")
        )
        await adapter.execute(_task(session.session_id))
        await adapter.collect_result(session.session_id)

        record = read_log(tmp_path / "cli")[-1]

        assert record["env"]["ANTHROPIC_API_KEY"] == "sk-测试令牌"
        assert "sk-测试令牌" not in " ".join(record["argv"])

    async def test_no_credential_means_no_credential_in_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(),
            recorded_env=("ANTHROPIC_API_KEY",),
        )
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))
        await adapter.collect_result(session.session_id)

        record = read_log(tmp_path / "cli")[-1]

        assert record["env"]["ANTHROPIC_API_KEY"] is None

    async def test_a_credential_reference_without_a_provider_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """配置里带凭证引用却没有取用它的办法 —— 不许悄悄跳过。"""
        script = write_stub(tmp_path / "cli", events=claude_events())
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(
            _config(tmp_path / "ws", credential_ref="secret://anthropic/default")
        )
        await adapter.execute(_task(session.session_id))

        with pytest.raises(ExecutorError) as excinfo:
            await adapter.collect_result(session.session_id)

        assert excinfo.value.category is ErrorCategory.NON_RETRYABLE


class TestTheArtifacts:
    """产出物只从**事实**里来 —— 工作空间里的 git 状态。"""

    async def test_the_branch_falls_back_to_the_convention(self, tmp_path: Path) -> None:
        """工作空间不是 git 仓库时，按约定报 ``agent/<task_id>``。

        没有提交就是没有提交 —— commit 仍然是 ``None``，不拿别的东西顶替。
        """
        script = write_stub(tmp_path / "cli", events=claude_events())
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id, task_id="TASK-9"))

        result = await adapter.collect_result(session.session_id)

        assert result.artifacts.branch == "agent/TASK-9"
        assert result.artifacts.commit is None
        assert result.artifacts.changed_files == 0

    async def test_the_commit_comes_from_the_workspace(self, tmp_path: Path) -> None:
        """工作空间是 git 仓库时，报出来的必须是**仓库现在什么样**。"""
        workspace = tmp_path / "ws"
        workspace.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.name", "测试"], cwd=workspace, check=True)
        (workspace / "README.md").write_text("一行\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "初始"], cwd=workspace, check=True)

        script = write_stub(tmp_path / "cli", events=claude_events())
        adapter = ClaudeCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(workspace))
        await adapter.execute(_task(session.session_id))

        result = await adapter.collect_result(session.session_id)

        assert result.artifacts.commit is not None
        assert result.artifacts.branch is not None


class TestTheCommandLine:
    def test_the_verbose_flag_is_always_there(self) -> None:
        """``--print`` 配 stream-json **必须**带 ``--verbose``。

        少了它，CLI 直接以退出码 2 退出 —— 它不是"多打印点东西"，它是这条
        输出模式的开关。
        """
        argv = build_command(
            binary="claude",
            config=SessionConfig(workspace="/tmp", model="test-model"),
            task=ExecutionTask(
                session_id="s",
                task_id="TASK-1",
                title="任务",
                instruction="改一行",
                timeout_seconds=60,
            ),
            session_id="11111111-2222-4333-8444-555555555555",
        )

        assert "--verbose" in argv
        assert "--print" in argv
        assert "stream-json" in argv
        assert "11111111-2222-4333-8444-555555555555" in argv

    def test_the_prompt_is_the_last_argument(self) -> None:
        """提示词单独一个参数 —— 拼进别的串会引入引号与转义的歧义。"""
        argv = build_command(
            binary="claude",
            config=SessionConfig(workspace="/tmp"),
            task=ExecutionTask(
                session_id="s",
                task_id="TASK-1",
                title="任务",
                instruction="把 README 的一行改掉",
                timeout_seconds=60,
            ),
            session_id="s-1",
        )

        assert argv[-1] == "任务\n\n把 README 的一行改掉"


class TestTheCodexSkeleton:
    """骨架要**满足契约**，但不能对没验证过的协议下结论。"""

    async def test_it_reports_no_conclusion_without_a_result_event(self, tmp_path: Path) -> None:
        """协议表还没填 ⇒ 没有证据 ⇒ 没有结论。

        把"没结论"当成成功，那正是"进程正常退出但什么都没产出"这种假成功
        的来源。
        """
        script = write_stub(tmp_path / "cli", events=claude_events())
        adapter = CodexAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        result = await adapter.collect_result(session.session_id)

        assert result.status is ExecutionStatus.FAILED

    async def test_its_events_are_all_recorded_verbatim(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=claude_events())
        adapter = CodexAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        events = [event async for event in await adapter.stream_events(session.session_id)]

        assert events
        assert all(event.kind == "unhandled" for event in events)


class TestCodexAdapterExecution:
    """TASK-P2-010 —— 验证 Codex 真实事件流能跑通 COMPLETED 结论。"""

    async def test_codex_events_stream_and_complete(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=codex_events(tool_calls=2, cost_usd=0.03))
        adapter = CodexAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        events = [event async for event in await adapter.stream_events(session.session_id)]
        assert [event.kind for event in events] == [
            "session.started",
            "message.thinking",
            "message.assistant",
            "tool.started",
            "tool.completed",
            "tool.started",
            "tool.completed",
            "result",
        ]

        result = await adapter.collect_result(session.session_id)
        assert result.status is ExecutionStatus.COMPLETED
        assert result.execution.tool_calls == 2
        assert result.execution.cost == Decimal("0.03")


class TestGeminiCliAdapterExecution:
    """TASK-P2-010b —— 验证 Gemini CLI 真实事件流能跑通 COMPLETED 结论。"""

    async def test_gemini_cli_events_stream_and_complete(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=gemini_cli_events(tool_calls=1, cost_usd=0.02))
        adapter = GeminiCliAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        events = [event async for event in await adapter.stream_events(session.session_id)]
        assert [event.kind for event in events] == [
            "session.started",
            "message.thinking",
            "message.assistant",
            "tool.started",
            "tool.completed",
            "result",
        ]

        result = await adapter.collect_result(session.session_id)
        assert result.status is ExecutionStatus.COMPLETED
        assert result.execution.tool_calls == 1
        assert result.execution.cost == Decimal("0.02")


class TestOpenCodeAdapterExecution:
    """TASK-P2-010c —— 验证 OpenCode 真实事件流能跑通 COMPLETED 结论。"""

    async def test_opencode_events_stream_and_complete(self, tmp_path: Path) -> None:
        script = write_stub(tmp_path / "cli", events=opencode_events(tool_calls=1, cost_usd=0.01))
        adapter = OpenCodeAdapter(binary=str(script))
        session = await adapter.create_session(_config(tmp_path / "ws"))
        await adapter.execute(_task(session.session_id))

        events = [event async for event in await adapter.stream_events(session.session_id)]
        assert [event.kind for event in events] == [
            "session.started",
            "message.thinking",
            "message.assistant",
            "tool.started",
            "tool.completed",
            "result",
        ]

        result = await adapter.collect_result(session.session_id)
        assert result.status is ExecutionStatus.COMPLETED
        assert result.execution.tool_calls == 1
        assert result.execution.cost == Decimal("0.01")


class TestTheContractSuiteCoversTheCliAdapters:
    """T1 ④ —— 打桩 CLI 要跑通 ``TASK-P1-030`` 的全套契约测试。

    契约测试的价值在于"多个实现跑同一份断言"。CLI 适配器没进那套参数化，
    "可替换"就只是说说 —— 所以这里把接线本身钉住。
    """

    def test_both_cli_adapters_are_in_the_contract_parameterization(self) -> None:
        assert "claude-code" in ADAPTERS
        assert "codex" in ADAPTERS
        assert "gemini-cli" in ADAPTERS
        assert "opencode" in ADAPTERS

    def test_the_stub_records_what_the_cli_was_given(self, tmp_path: Path) -> None:
        """打桩 CLI 自己也要能用 —— 它记下 argv，才谈得上断言 argv。"""
        script = write_stub(tmp_path / "cli", events=claude_events())
        completed = subprocess.run(
            [str(script), "--session-id", "abc"],
            capture_output=True,
            text=True,
            check=True,
        )

        assert json.loads(completed.stdout.splitlines()[0])["session_id"] == "abc"
