"""TASK-P1-005 —— 结构化日志。

核心验证点：**日志里不能出现密钥**。这条不满足，其余日志功能都没有价值。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from infrastructure.config.settings import Environment, LogFormat, Settings
from infrastructure.observability.logging import (
    REDACTED,
    RESERVED_CONTEXT_KEYS,
    SENSITIVE_KEYS,
    bind_context,
    bind_task_context,
    clear_context,
    configure_logging,
    current_context,
    get_logger,
    redact,
)


@pytest.fixture(autouse=True)
def _clean_context() -> Iterator[None]:
    """测试之间不共享日志上下文。

    structlog 的上下文是**进程级全局状态** —— 上一个测试绑的 ``task_id``
    会漏进下一个测试，让断言莫名其妙地通过。绑定前清空、结束后再清空。
    """
    clear_context()
    yield
    clear_context()


@pytest.fixture
def json_logs(capsys: pytest.CaptureFixture[str]) -> Any:
    """配置 JSON 日志并返回解析后的输出。"""
    configure_logging(
        Settings(environment=Environment.TEST, log_format=LogFormat.JSON, log_level="INFO")
    )
    return capsys


def _emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out.strip()
    if not out:
        return []
    return [json.loads(line) for line in out.splitlines() if line.strip()]


class TestRedaction:
    """密钥脱敏。"""

    def test_secret_values_are_replaced(self) -> None:
        assert redact("sk-ant-1234567890") == REDACTED
        assert redact("hunter2") == REDACTED

    def test_missing_secret_is_marked_unset(self) -> None:
        assert redact(None) == "unset"

    def test_empty_secret_is_marked_empty(self) -> None:
        assert redact("") == "empty"

    def test_sensitive_keys_cover_common_names(self) -> None:
        for key in ("api_key", "secret_key", "password", "token", "authorization"):
            assert key in SENSITIVE_KEYS


class TestStructuredOutput:
    """结构化输出。"""

    def test_event_is_emitted_as_json(self, json_logs: pytest.CaptureFixture[str]) -> None:
        get_logger("test").info("hello", task_id="TASK-001")
        events = _emitted(json_logs)
        assert len(events) == 1
        assert events[0]["event"] == "hello"
        assert events[0]["task_id"] == "TASK-001"
        assert events[0]["level"] == "info"

    def test_service_name_is_attached(self, json_logs: pytest.CaptureFixture[str]) -> None:
        get_logger("test").info("hello")
        events = _emitted(json_logs)
        assert events[0]["service"] == "ases"

    def test_timestamp_is_attached(self, json_logs: pytest.CaptureFixture[str]) -> None:
        get_logger("test").info("hello")
        events = _emitted(json_logs)
        assert "timestamp" in events[0]

    def test_non_ascii_is_preserved(self, json_logs: pytest.CaptureFixture[str]) -> None:
        get_logger("test").info("演进完成")
        events = _emitted(json_logs)
        assert events[0]["event"] == "演进完成"


class TestSecretNeverLeaks:
    """端到端：密钥绝不出现在日志里。"""

    def test_api_key_is_redacted_in_output(self, json_logs: pytest.CaptureFixture[str]) -> None:
        get_logger("test").info("calling api", api_key="sk-ant-super-secret-value")
        events = _emitted(json_logs)
        assert events[0]["api_key"] == REDACTED
        assert "sk-ant-super-secret-value" not in json.dumps(events, ensure_ascii=False)

    def test_multiple_secret_fields_are_redacted(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        get_logger("test").info(
            "creds",
            access_key="AKIAEXAMPLE",
            secret_key="wJalrEXAMPLEKEY",
            password="p@ssw0rd",
        )
        events = _emitted(json_logs)
        rendered = json.dumps(events, ensure_ascii=False)
        assert "AKIAEXAMPLE" not in rendered
        assert "wJalrEXAMPLEKEY" not in rendered
        assert "p@ssw0rd" not in rendered

    def test_non_sensitive_fields_survive(self, json_logs: pytest.CaptureFixture[str]) -> None:
        get_logger("test").info("ok", task_id="TASK-001", token_count=42)
        events = _emitted(json_logs)
        assert events[0]["task_id"] == "TASK-001"
        assert events[0]["token_count"] == 42


class TestContextBinding:
    """上下文绑定。"""

    def test_bound_context_appears_in_logs(self, json_logs: pytest.CaptureFixture[str]) -> None:
        bind_context(task_id="TASK-042", project_id="my-product")
        get_logger("test").info("working")
        events = _emitted(json_logs)
        assert events[0]["task_id"] == "TASK-042"
        assert events[0]["project_id"] == "my-product"

    def test_context_can_be_cleared(self) -> None:
        bind_context(task_id="TASK-042")
        assert current_context()["task_id"] == "TASK-042"
        clear_context()
        assert current_context() == {}


class TestTaskIdRetrieval:
    """T1 ① —— 任意一条日志都能按 task_id 检索。"""

    def test_every_line_carries_the_bound_task_id(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        bind_task_context(task_id="TASK-042", cycle_id="CYCLE-007", project_id="my-product")
        get_logger("a").info("first")
        get_logger("b").info("second")
        events = _emitted(json_logs)
        assert len(events) == 2
        matched = [event for event in events if event.get("task_id") == "TASK-042"]
        assert len(matched) == 2, "有日志行检索不到 task_id —— 检索维度失效了"

    def test_reserved_keys_are_the_documented_three(self) -> None:
        assert RESERVED_CONTEXT_KEYS == ("task_id", "cycle_id", "project_id")

    def test_unset_reserved_keys_are_not_bound(self) -> None:
        """绑定空值会留下 ``"task_id": null`` —— 与"压根没写"分不开。"""
        bind_task_context(task_id="TASK-042")
        assert "cycle_id" not in current_context()
        assert current_context()["task_id"] == "TASK-042"


class TestConsoleFormat:
    """控制台格式。"""

    def test_console_format_is_human_readable(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(Settings(environment=Environment.TEST, log_format=LogFormat.CONSOLE))
        get_logger("test").info("hello console")
        out = capsys.readouterr().out
        assert "hello console" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out.strip())

    def test_configuration_is_idempotent(self, capsys: pytest.CaptureFixture[str]) -> None:
        settings = Settings(environment=Environment.TEST, log_format=LogFormat.JSON)
        configure_logging(settings)
        configure_logging(settings)
        get_logger("test").info("once")
        events = _emitted(capsys)
        assert len(events) == 1
