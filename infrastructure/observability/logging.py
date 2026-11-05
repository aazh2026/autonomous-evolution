"""结构化日志。

设计依据：docs/design.md §10（可观测性）、docs/tasks.md TASK-P1-005。

用 `structlog` 输出结构化日志：JSON 格式便于采集与检索，控制台格式便于本地调试。
两种格式由 :class:`LogFormat` 切换，**不靠环境变量判断**（那会让同一份代码在
不同部署下行为不一致）。

关键约束：**日志里不能出现密钥**。这里统一走 ``structlog`` 的
``processors.format_exc_info`` 与事件字典，密钥字段由调用方用
``redact()`` 包装后再传入，而不是指望调用方记得脱敏。
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any, cast

import structlog
from structlog.typing import Processor

from infrastructure.config.settings import LogFormat, Settings, get_settings

#: 任何出现在日志键名中的敏感词，其值都会被替换为 ``***``。
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_key",
        "secret_key",
        "secret",
        "password",
        "passwd",
        "token",
        "authorization",
        "auth",
        "private_key",
        "credential",
        "credentials",
        "session_token",
    }
)

REDACTED = "***"

#: 保留的上下文键 —— 任务、演进周期、项目。
#:
#: 这三个是日志必须支持的检索维度（"这个任务做过什么""这轮演进有哪些动作"）。
#: 单独列出来，是因为"承诺了哪几个可检索字段"应该一眼看得见，
#: 而不是散落在各处的调用点里。
RESERVED_CONTEXT_KEYS = ("task_id", "cycle_id", "project_id")


def redact(value: object) -> str:
    """把可能含密钥的值替换成固定占位符。

    只保留"是不是有值"的信息 —— 日志要能回答"密钥配了吗"，但不能泄漏密钥本身。
    """
    if value is None:
        return "unset"
    text = str(value)
    return REDACTED if text else "empty"


def _redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """脱敏处理器：按键名匹配敏感词。"""
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = redact(event_dict[key])
    return event_dict


def _add_service_context(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """附加服务名与时间戳。"""
    event_dict.setdefault("service", get_settings().service_name)
    return event_dict


class _StdoutLogger:
    """把日志写到 stdout —— **写入时**才解析 ``sys.stdout``。

    structlog 自带的 ``PrintLogger`` 在构造时就固定了 ``sys.stdout`` 的引用。
    之后 stdout 被替换（进程管理器下的重定向、日志轮转、测试捕获）时，
    日志会写进已失效的旧句柄 —— 表现为 "I/O operation on closed file"，
    或者更糟：日志被写到一个没人看的地方。

    所有日志级别都走同一个 ``msg``，与 ``PrintLogger`` 的行为一致。
    """

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def msg(self, message: str) -> None:
        with self._lock:
            print(message, file=sys.stdout, flush=True)

    def log(self, _level: int, message: str) -> None:
        self.msg(message)

    def debug(self, message: str) -> None:
        self.msg(message)

    def info(self, message: str) -> None:
        self.msg(message)

    def warning(self, message: str) -> None:
        self.msg(message)

    def error(self, message: str) -> None:
        self.msg(message)

    def critical(self, message: str) -> None:
        self.msg(message)

    def fatal(self, message: str) -> None:
        self.msg(message)

    def exception(self, message: str) -> None:
        self.msg(message)


class _StdoutLoggerFactory:
    """logger 工厂。每次调用返回一个写入时解析 stdout 的 logger。"""

    def __call__(self, *_args: object) -> _StdoutLogger:
        return _StdoutLogger()


def configure_logging(settings: Settings | None = None) -> None:
    """配置 structlog 与标准库 logging。

    幂等：重复调用不会叠加处理器。
    """
    cfg = settings or get_settings()

    level = getattr(logging, cfg.log_level, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_service_context,
        _redact_processor,
        structlog.processors.format_exc_info,
    ]

    if cfg.log_format is LogFormat.JSON:
        renderer: Processor = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=_StdoutLoggerFactory(),
        # 不缓存 logger：缓存会让"配置过一次之后再改配置"无效 ——
        # 旧 logger 攥着旧 processors，日志格式改了却还是老样子。
        # 这里每次取 logger 都按当前配置解析，代价可以忽略。
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """取一个绑定了模块名的 logger。"""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


def bind_context(**kwargs: Any) -> None:
    """绑定上下文变量（如 ``task_id``），后续日志自动带上。"""
    structlog.contextvars.bind_contextvars(**kwargs)


def bind_task_context(
    *,
    task_id: str | None = None,
    cycle_id: str | None = None,
    project_id: str | None = None,
) -> None:
    """绑定任务维度的上下文。

    只绑定非 ``None`` 的项 —— 绑定一个空值会在日志里留下 ``"task_id": null``，
    让"这个任务没有 id"和"这个字段压根没写"变得没法区分。
    """
    values = {
        "task_id": task_id,
        "cycle_id": cycle_id,
        "project_id": project_id,
    }
    structlog.contextvars.bind_contextvars(
        **{key: value for key, value in values.items() if value is not None}
    )


def clear_context() -> None:
    """清空上下文变量。测试之间需要隔离时调用。"""
    structlog.contextvars.clear_contextvars()


def current_context() -> Mapping[str, Any]:
    """当前绑定的上下文。"""
    return dict(structlog.contextvars.get_contextvars())


def iter_log_contexts() -> Iterator[tuple[str, Any]]:
    """遍历当前上下文的键值对。"""
    yield from structlog.contextvars.get_contextvars().items()


__all__ = [
    "REDACTED",
    "RESERVED_CONTEXT_KEYS",
    "SENSITIVE_KEYS",
    "bind_context",
    "bind_task_context",
    "clear_context",
    "configure_logging",
    "current_context",
    "get_logger",
    "iter_log_contexts",
    "redact",
]
