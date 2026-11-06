"""退出码 → 统一状态。

**为什么要有这张表。** CLI 用退出码说话，而 ASES 用
:class:`~core.task.outcome.ExecutionStatus` 说话。两者之间必须有一张**写下来
的**对照表 —— 否则每个适配器各自揣摩一套，跨执行器的结果就不可比
（design.md R-043："不要强制所有执行器实现同一种底层协议，而是强制它们实现
同一种系统级任务契约"）。这张表就是那份契约的具体化。

**分类的依据只有两个，按信息量排序。**

1. **有 API 状态码** —— 按状态码分：5xx / 429 / 408 是服务端的瞬时故障，
   重试有意义；4xx 是请求本身不对，重试还是同样的错。
2. **没有 API 状态码** —— 按退出码分。用法错误（退出码 2）是参数写错了，
   重试还是同样的错；其他非零退出码意味着"进程没给出任何诊断就死了"，这
   往往是环境问题（fork 失败、磁盘满），**重试有意义**。

**CLI 说"我失败了"但没给状态码时，一律不可重试。** 这一条最容易被写错。
像"权限被拒""预算超了""工具报错"这类失败都不带 API 状态码，而它们重试
一次还是同样的结果 —— 自动重试只会把预算再烧一遍。**没有证据支持重试时，
就不重试**：多一次人工介入比多烧一次预算便宜。

**超时单列成一个状态。** 超时不等于失败（R-097 / I-03）：会话可能还活着，
需要先探测能不能恢复。把它并进 ``FAILED``，编排层会直接判死 —— 那正是
僵尸会话的来源。

**进程正常退出但没有结果事件，判失败。** CLI 说"我成功了"（退出码 0），
却没有产出结果事件 —— 我们没有任何证据说明这次执行做完了什么。判成功
是拿"没出错"当"做成了"，那是两回事。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from core.task.outcome import ExecutionStatus
from executors.base import ErrorCategory

#: 一切正常。
EXIT_SUCCESS: Final[int] = 0
#: 一般性错误 —— 进程没能给出更多诊断。
EXIT_GENERAL_ERROR: Final[int] = 1
#: 用法错误 —— 参数或配置不对，同样的参数只会再错一次。
EXIT_USAGE_ERROR: Final[int] = 2

#: 服务端的瞬时故障 —— 重试有意义。
RETRYABLE_API_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: 退出码 → 错误分类。用法错误不可重试；其余的"没诊断就死了"可重试。
EXIT_CODE_CATEGORY: Final[MappingProxyType[int, ErrorCategory]] = MappingProxyType(
    {
        EXIT_USAGE_ERROR: ErrorCategory.NON_RETRYABLE,
        EXIT_GENERAL_ERROR: ErrorCategory.RETRYABLE,
    }
)


@dataclass(frozen=True, slots=True)
class ExitOutcome:
    """一次退出的定性。

    ``category`` 为 ``None`` 表示没有错误 —— 用一个独立的布尔字段会更啰嗦，
    而"没有错误"和"有错误但不知道该不该重试"在这里是同一件事：没有。
    """

    status: ExecutionStatus
    category: ErrorCategory | None
    reason: str


def category_for_api_status(api_error_status: int | None) -> ErrorCategory:
    """API 状态码 → 错误分类。

    没有状态码时按**不可重试**处理：没有证据支持重试，就不重试。
    """
    if api_error_status is None:
        return ErrorCategory.NON_RETRYABLE
    if api_error_status in RETRYABLE_API_STATUS:
        return ErrorCategory.RETRYABLE
    return ErrorCategory.NON_RETRYABLE


def category_for_exit_code(exit_code: int) -> ErrorCategory:
    """退出码 → 错误分类。

    用法错误（2）不可重试；其余非零退出码可重试 —— 进程没给出诊断就死了，
    这通常是环境问题，换个时刻重来就好了。
    """
    return EXIT_CODE_CATEGORY.get(exit_code, ErrorCategory.RETRYABLE)


def classify_exit(
    exit_code: int,
    *,
    is_error: bool | None = None,
    api_error_status: int | None = None,
    timed_out: bool = False,
) -> ExitOutcome:
    """退出码 + 结果事件 → 统一状态与错误分类。

    :param is_error: CLI 自己报的"这次失败了"（结果事件里的 ``is_error``）。
        ``None`` 表示**没有结果事件** —— 那本身就是一种失败。
    :param api_error_status: 结果事件里的 API 状态码，有则据以分类。
    :param timed_out: 是不是我们自己的超时把它掐掉的。
    """
    if timed_out:
        return ExitOutcome(
            ExecutionStatus.TIMED_OUT,
            ErrorCategory.RETRYABLE,
            "执行超时 —— 超时不等于失败，会话可能还能恢复",
        )

    if is_error is True:
        category = category_for_api_status(api_error_status)
        if api_error_status is None:
            detail = "CLI 报告失败但没有给出 API 状态码"
        else:
            detail = f"CLI 报告失败，API 状态码 {api_error_status}"
        return ExitOutcome(ExecutionStatus.FAILED, category, detail)

    if exit_code == EXIT_SUCCESS:
        if is_error is None:
            return ExitOutcome(
                ExecutionStatus.FAILED,
                ErrorCategory.NON_RETRYABLE,
                "进程正常退出，但没有产出结果事件 —— 无从判断这次执行做完了什么",
            )
        return ExitOutcome(ExecutionStatus.COMPLETED, None, "正常完成")

    if is_error is False:
        # CLI 说"我成功了"，但进程没能正常退出。判失败：拿"成功"当结论，
        # 会掩盖"退出阶段炸了"这件事 —— 而退出阶段炸了的工作空间未必干净。
        return ExitOutcome(
            ExecutionStatus.FAILED,
            category_for_exit_code(exit_code),
            f"CLI 报告成功，但进程以退出码 {exit_code} 结束",
        )

    return ExitOutcome(
        ExecutionStatus.FAILED,
        category_for_exit_code(exit_code),
        f"进程以退出码 {exit_code} 结束，且没有产出结果事件",
    )


__all__ = [
    "EXIT_CODE_CATEGORY",
    "EXIT_GENERAL_ERROR",
    "EXIT_SUCCESS",
    "EXIT_USAGE_ERROR",
    "RETRYABLE_API_STATUS",
    "ExitOutcome",
    "category_for_api_status",
    "category_for_exit_code",
    "classify_exit",
]
