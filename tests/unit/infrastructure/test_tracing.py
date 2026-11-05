"""TASK-P1-005 —— OTel 追踪骨架。

原则和 Langfuse 一样：**追踪不可用不得阻塞主流程**。
没启用 OTel 时 tracer 是 no-op；启用失败时降级并留下标记，而不是抛异常。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

from infrastructure.config.settings import Environment, LogFormat, Settings
from infrastructure.observability import tracing
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


class TestDisabledTracing:
    """没开 OTel 时，tracer 是 no-op 但依然可用。"""

    def test_configure_returns_none_when_disabled(self) -> None:
        settings = Settings(environment=Environment.TEST, otel_enabled=False)
        assert tracing.configure_tracing(settings) is None
        assert tracing.is_configured() is False

    def test_get_tracer_still_works(self) -> None:
        """调用方代码不该关心 provider 装没装 —— 两种模式下签名一致。"""
        tracer = tracing.get_tracer("test")
        with tracer.start_as_current_span("noop"):
            pass

    def test_shutdown_without_provider_is_safe(self) -> None:
        tracing.shutdown_tracing()


class TestTracingDegradation:
    """exporter 构造失败时降级，不抛异常。"""

    def test_exporter_failure_degrades(
        self, monkeypatch: pytest.MonkeyPatch, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> Any:
            raise RuntimeError("collector 不可达")

        monkeypatch.setattr(tracing, "OTLPSpanExporter", _boom)
        settings = Settings(environment=Environment.TEST, otel_enabled=True)

        assert tracing.configure_tracing(settings) is None
        assert tracing.is_configured() is False

        events = _emitted(json_logs)
        degraded = [event for event in events if event.get("degraded") is True]
        assert degraded, "降级必须在日志里留下可见标记"
        assert degraded[0]["event"] == "otel_unavailable"

    def test_repeated_configuration_is_stable(self) -> None:
        """重复调用不该反复建 provider。"""
        settings = Settings(environment=Environment.TEST, otel_enabled=False)
        assert tracing.configure_tracing(settings) is None
        assert tracing.configure_tracing(settings) is None


class TestSpanAttributes:
    """诊断辅助。"""

    def test_no_active_span_gives_empty_attributes(self) -> None:
        assert tracing.current_span_attributes() == {}

    def test_attributes_are_available_inside_a_span(self) -> None:
        """没装全局 provider 时 OTel 给的是无效 span —— 这里用一个局部 provider。

        局部 provider 的 span 一样会被挂到当前 context 上，
        所以 ``get_current_span()`` 拿得到它。
        """
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("work"):
            attributes = tracing.current_span_attributes()
        assert "trace_id" in attributes
        assert "span_id" in attributes
