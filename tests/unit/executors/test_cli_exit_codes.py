"""退出码 → 统一状态与错误分类。

**这张表是跨执行器可比的前提。** design.md R-043 要求各执行器实现同一种
系统级任务契约 —— 契约要落到数据上，就得有这样一张**写下来的**对照表，
而不是每个适配器各自揣摩一套。

**判错的两种后果都很贵。** 把"不可重试"判成"可重试" → 自动重试一次授权
被拒的任务，凭据与预算都白烧一遍；把"可重试"判成"不可重试" → 一次网络
抖动报废掉一整次执行。

**"没有证据就不重试"是这里的默认。** CLI 说"我失败了"但没给 API 状态码时，
一律判不可重试 —— 权限被拒、预算超了、工具报错都不带状态码，而它们重试
一次还是同样的结果。多一次人工介入比多烧一次预算便宜。
"""

from __future__ import annotations

from core.task.outcome import ExecutionStatus
from executors.base import ErrorCategory
from executors.cli.exit_codes import (
    EXIT_GENERAL_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    classify_exit,
)


class TestASuccessfulRun:
    def test_exit_zero_with_a_successful_result_is_completed(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=False)

        assert outcome.status is ExecutionStatus.COMPLETED
        assert outcome.category is None

    def test_the_reason_says_what_happened(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=False)

        assert outcome.reason.strip()


class TestARunWithoutAConclusion:
    """退出码 0 但**没有结果事件** —— 判失败。

    判成功是拿"没出错"当"做成了"。CLI 说"我成功了"却没产出结果事件，我们
    没有任何证据说明这次执行做完了什么 —— 那就不能报成功。
    """

    def test_it_is_failed(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=None)

        assert outcome.status is ExecutionStatus.FAILED

    def test_it_is_not_retryable(self) -> None:
        """同样的输入重跑一次，还是没有结果事件 —— 重试不是修复。"""
        outcome = classify_exit(EXIT_SUCCESS, is_error=None)

        assert outcome.category is ErrorCategory.NON_RETRYABLE

    def test_the_reason_points_at_the_missing_evidence(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=None)

        assert "结果事件" in outcome.reason


class TestAReportedFailure:
    def test_it_is_failed(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=True)

        assert outcome.status is ExecutionStatus.FAILED

    def test_without_an_api_status_it_is_not_retryable(self) -> None:
        """没有证据支持重试，就不重试。"""
        outcome = classify_exit(EXIT_SUCCESS, is_error=True)

        assert outcome.category is ErrorCategory.NON_RETRYABLE

    def test_a_server_error_is_retryable(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=True, api_error_status=500)

        assert outcome.category is ErrorCategory.RETRYABLE

    def test_a_rate_limit_is_retryable(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=True, api_error_status=429)

        assert outcome.category is ErrorCategory.RETRYABLE

    def test_a_bad_request_is_not_retryable(self) -> None:
        """请求本身不对，重试还是同样的错。"""
        outcome = classify_exit(EXIT_SUCCESS, is_error=True, api_error_status=400)

        assert outcome.category is ErrorCategory.NON_RETRYABLE

    def test_the_status_code_is_in_the_reason(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=True, api_error_status=503)

        assert "503" in outcome.reason


class TestANonZeroExitCode:
    def test_a_general_error_is_failed_and_retryable(self) -> None:
        """没给出任何诊断就死了 —— 这通常是环境问题，换个时刻重来就好了。"""
        outcome = classify_exit(EXIT_GENERAL_ERROR, is_error=None)

        assert outcome.status is ExecutionStatus.FAILED
        assert outcome.category is ErrorCategory.RETRYABLE

    def test_a_usage_error_is_not_retryable(self) -> None:
        """参数写错了，同样的参数只会再错一次。"""
        outcome = classify_exit(EXIT_USAGE_ERROR, is_error=None)

        assert outcome.status is ExecutionStatus.FAILED
        assert outcome.category is ErrorCategory.NON_RETRYABLE

    def test_an_unknown_exit_code_is_retryable(self) -> None:
        outcome = classify_exit(42, is_error=None)

        assert outcome.category is ErrorCategory.RETRYABLE

    def test_a_successful_result_with_a_bad_exit_code_is_still_failed(self) -> None:
        """CLI 说"我成功了"，但进程没能正常退出。

        判失败：拿"成功"当结论，会掩盖"退出阶段炸了"这件事 —— 而退出阶段
        炸了的工作空间未必干净。
        """
        outcome = classify_exit(EXIT_GENERAL_ERROR, is_error=False)

        assert outcome.status is ExecutionStatus.FAILED
        assert outcome.category is ErrorCategory.RETRYABLE


class TestATimeout:
    """超时**不等于**失败（R-097 / I-03）。"""

    def test_it_is_its_own_status(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=False, timed_out=True)

        assert outcome.status is ExecutionStatus.TIMED_OUT

    def test_it_is_retryable(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=False, timed_out=True)

        assert outcome.category is ErrorCategory.RETRYABLE

    def test_it_wins_over_a_reported_failure(self) -> None:
        """超时优先于"CLI 说它失败了" —— 先要回答的是"会话还能不能恢复"。"""
        outcome = classify_exit(EXIT_GENERAL_ERROR, is_error=True, timed_out=True)

        assert outcome.status is ExecutionStatus.TIMED_OUT

    def test_the_reason_says_a_timeout_is_not_a_failure(self) -> None:
        outcome = classify_exit(EXIT_SUCCESS, is_error=False, timed_out=True)

        assert "不" in outcome.reason
