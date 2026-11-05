"""TASK-P1-005 ② —— Langfuse 不可达时主流程不中断，且降级可见。

这两件事是一体的：不可达时**不抛异常**（主流程继续），
并且**留下可见标记**（否则"这段时间没有观测数据"和"这段时间没发生事"分不开）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from infrastructure.config.settings import Environment, LogFormat, Settings
from infrastructure.observability.langfuse import (
    LangfuseClient,
    LangfuseState,
    Transport,
)
from infrastructure.observability.logging import clear_context, configure_logging


@pytest.fixture
def json_logs(capsys: pytest.CaptureFixture[str]) -> pytest.CaptureFixture[str]:
    configure_logging(
        Settings(environment=Environment.TEST, log_format=LogFormat.JSON, log_level="INFO")
    )
    clear_context()
    return capsys


def _emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out.strip()
    if not out:
        return []
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _failing_transport(exc: Exception) -> Transport:
    async def _send(_name: str, _body: Mapping[str, Any]) -> None:
        raise exc

    return _send


def _recording_transport(sink: list[tuple[str, Mapping[str, Any]]]) -> Transport:
    async def _send(name: str, body: Mapping[str, Any]) -> None:
        sink.append((name, body))

    return _send


def _enabled_settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        langfuse_enabled=True,
        langfuse_host="http://langfuse.invalid",
        langfuse_public_key=SecretStr("pk-test"),
        langfuse_secret_key=SecretStr("sk-test"),
    )


def _state_of(client: LangfuseClient) -> LangfuseState:
    """取客户端状态。

    绕一层函数是为了躲开 mypy 的属性收窄：``client.state is X`` 断言两次时，
    mypy 会把第一次断言收窄出的类型带进第二次断言，于是第二次被判定为
    "不可能到达"。函数调用返回的值不参与收窄。
    """
    return client.state


class TestDisabledClientIsNoop:
    """没启用 Langfuse 时，所有调用都是 no-op。"""

    async def test_record_does_nothing(self) -> None:
        sink: list[tuple[str, Mapping[str, Any]]] = []
        client = LangfuseClient(
            Settings(environment=Environment.TEST), transport=_recording_transport(sink)
        )
        assert await client.record_trace("hello") is False
        assert sink == [], "未启用时不该发出任何请求"

    async def test_state_is_disabled(self) -> None:
        client = LangfuseClient(Settings(environment=Environment.TEST))
        assert _state_of(client) is LangfuseState.DISABLED
        assert client.degraded is False

    async def test_flush_is_a_noop(self) -> None:
        client = LangfuseClient(Settings(environment=Environment.TEST))
        assert await client.flush() is False


class TestUnreachableLangfuseDoesNotBreakTheCaller:
    """T1 ② —— 主流程不中断。"""

    async def test_transport_failure_does_not_raise(self) -> None:
        client = LangfuseClient(
            _enabled_settings(),
            transport=_failing_transport(ConnectionError("collector down")),
        )
        assert await client.record_trace("hello") is False

    async def test_client_marks_itself_degraded(self) -> None:
        client = LangfuseClient(
            _enabled_settings(),
            transport=_failing_transport(ConnectionError("collector down")),
        )
        await client.record_trace("hello")
        assert _state_of(client) is LangfuseState.DEGRADED
        assert client.degraded is True
        assert "ConnectionError" in (client.degraded_reason or "")

    async def test_degradation_is_visible_in_the_logs(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        client = LangfuseClient(
            _enabled_settings(),
            transport=_failing_transport(ConnectionError("collector down")),
        )
        await client.record_trace("hello")
        events = _emitted(json_logs)
        degraded_events = [event for event in events if event.get("degraded") is True]
        assert degraded_events, "降级必须在日志里留下可见标记"
        assert degraded_events[0]["langfuse_state"] == "degraded"

    async def test_missing_credentials_degrade_instead_of_raising(self) -> None:
        """启用了 Langfuse 却没配凭证 —— 降级，不是崩溃。"""
        settings = Settings(environment=Environment.TEST, langfuse_enabled=True)
        client = LangfuseClient(settings)
        assert await client.record_trace("hello") is False
        assert client.degraded is True

    async def test_only_one_warning_per_degradation(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        """持续故障时反复刷屏会把真正的日志淹掉。"""
        client = LangfuseClient(
            _enabled_settings(),
            transport=_failing_transport(ConnectionError("down")),
        )
        for _ in range(5):
            await client.record_trace("hello")
        events = _emitted(json_logs)
        warnings = [event for event in events if event.get("degraded") is True]
        assert len(warnings) == 1, f"降级告警只该记一次，实际记了 {len(warnings)} 次"


class TestRecovery:
    """一次成功即视为恢复。"""

    async def test_successful_call_keeps_client_active(self) -> None:
        sink: list[tuple[str, Mapping[str, Any]]] = []
        client = LangfuseClient(_enabled_settings(), transport=_recording_transport(sink))
        assert await client.record_trace("hello", trace_id="T-1") is True
        assert _state_of(client) is LangfuseState.ACTIVE
        assert sink and sink[0][1]["traceId"] == "T-1"

    async def test_client_recovers_after_a_failure(self) -> None:
        calls = {"n": 0}

        async def _flaky(_name: str, _body: Mapping[str, Any]) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("down")

        client = LangfuseClient(_enabled_settings(), transport=_flaky)
        await client.record_trace("hello")
        assert _state_of(client) is LangfuseState.DEGRADED
        await client.record_trace("hello")
        assert _state_of(client) is LangfuseState.ACTIVE

    async def test_recovery_is_logged(self, json_logs: pytest.CaptureFixture[str]) -> None:
        calls = {"n": 0}

        async def _flaky(_name: str, _body: Mapping[str, Any]) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("down")

        client = LangfuseClient(_enabled_settings(), transport=_flaky)
        await client.record_trace("hello")
        await client.record_trace("hello")
        events = _emitted(json_logs)
        assert any(event.get("event") == "langfuse_recovered" for event in events)
