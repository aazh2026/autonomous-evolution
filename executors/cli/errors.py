"""CLI 执行器的错误 —— 每一类都必须能回答"该不该重试"。

**为什么这里自己定义一套错误，而不是直接用** :class:`~executors.base.ExecutorError`。
基类只要求"带分类"，但 CLI 这一层有三类特有的失败，它们的**重试语义完全
不同**，混在一起会让编排层只能瞎猜：

1. :class:`MalformedOutputError` —— **不可重试**。输出坏了，同一份输入重跑
   一次还是那份坏输出；重试不是修复，是再付一次钱。
2. :class:`CliNotFoundError` —— **不可重试**。可执行文件不在，这是部署问题，
   不是运行时抖动；重试一万次也装不上它。
3. :class:`CliInvocationError` —— **可重试**。文件在、权限对，但这一刻起不来
   （fork 失败、内存不够、进程表满了）。这类故障换个时刻重来通常就好了。
4. :class:`SessionMismatchError` —— **不可重试**。CLI 报出来的会话不是我们要的
   那个，说明我们对协议的理解和它对不上；继续读下去只会把别的事件记到这
   个任务头上。

**分类为什么必须落在异常上而不是返回值上。** 编排层拿到的是"这次执行怎么了"
—— 异常带着分类，它才知道该重试、该判失败，还是该挂起等人。把分类藏在
返回值里，调用方就有可能忘了看 —— 而忘了看的后果是"一律重试"或"一律不重试"，
两个都是错的。
"""

from __future__ import annotations

from executors.base import ErrorCategory, ExecutorError


class CliError(ExecutorError):
    """CLI 执行器的错误基类。"""


class MalformedOutputError(CliError):
    """CLI 的输出不是合法的 NDJSON 事件流。

    **不可重试**：同一份输入重跑一次，得到的还是同样一份坏输出。

    :attr:`line` 保留原始的那一行 —— 没有它，排查时就得回去翻日志，而日志
    里可能已经被截断过了。
    """

    def __init__(
        self,
        message: str,
        *,
        executor: str,
        task_id: str | None = None,
        session_id: str | None = None,
        line: str | None = None,
    ) -> None:
        super().__init__(
            message,
            category=ErrorCategory.NON_RETRYABLE,
            executor=executor,
            task_id=task_id,
            session_id=session_id,
        )
        self.line = line


class CliNotFoundError(CliError):
    """CLI 可执行文件不在（或不可执行）。

    **不可重试**：这是部署问题，不是运行时抖动。
    """

    def __init__(self, binary: str, *, executor: str) -> None:
        super().__init__(
            f"找不到 CLI 可执行文件 {binary!r}",
            category=ErrorCategory.NON_RETRYABLE,
            executor=executor,
        )
        self.binary = binary


class CliInvocationError(CliError):
    """CLI 进程起不来 —— 文件在，但这一刻没能启动。

    **可重试**：fork 失败、内存不够、进程表满了 —— 这类故障换个时刻重来
    通常就好了，而"文件不在"是另一回事（那是 :class:`CliNotFoundError`）。
    """

    def __init__(
        self,
        message: str,
        *,
        executor: str,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            category=ErrorCategory.RETRYABLE,
            executor=executor,
            task_id=task_id,
            session_id=session_id,
        )


class SessionMismatchError(CliError):
    """CLI 报出来的会话不是我们要的那个。

    **不可重试**：我们对协议的理解和 CLI 对不上。硬读下去只会把别的会话的
    事件记到这个任务头上 —— 那比直接失败坏得多，因为它看起来是成功的。
    """

    def __init__(
        self,
        expected: str,
        actual: str | None,
        *,
        executor: str,
        task_id: str | None = None,
    ) -> None:
        if actual is None:
            detail = f"CLI 没有报出任何会话 ID，无法确认事件流属于会话 {expected!r}"
        else:
            detail = f"CLI 报出的会话 {actual!r} 与请求的 {expected!r} 不是同一个"
        super().__init__(
            detail,
            category=ErrorCategory.NON_RETRYABLE,
            executor=executor,
            task_id=task_id,
            session_id=expected,
        )
        self.expected = expected
        self.actual = actual


__all__ = [
    "CliError",
    "CliInvocationError",
    "CliNotFoundError",
    "MalformedOutputError",
    "SessionMismatchError",
]
