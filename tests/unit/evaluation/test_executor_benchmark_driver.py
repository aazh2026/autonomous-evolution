"""TASK-P2-018 —— 评测驱动脚本的读数口径。

验证（T1）：**从各 CLI 的输出里读数**这件事本身是对的。

**为什么读数也要单测。** 四个 CLI 的输出格式各不相同 —— Claude Code 是一个
JSON，Codex 是 JSONL，OpenCode 是事件流；而 token 的记法连**含不含缓存命中**
都不一样。读错一个字段，成本那一列就整体偏掉，而偏掉的成本比没有成本更危险：
它看上去像个结论。

**这些数都是手算得出来的** —— 所以期望值直接写成算式，不用"跑一遍看看"。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from evaluation.executors.benchmark import RunOutcome, RunRecord, TokenUsage
from scripts.run_executor_benchmark import (
    claude_tokens,
    codex_is_error,
    codex_tokens,
    cost_from_payloads,
    is_transient_failure,
    opencode_cost,
    opencode_tokens,
    price_table,
    record_from_dict,
    redact,
    redact_output,
    write_provider_setup,
)


class TestTheClaudeOutput:
    """Claude Code —— 一个 JSON，token 在 ``usage`` 里。"""

    def test_the_token_usage_is_read(self) -> None:
        text = json.dumps(
            {
                "type": "result",
                "total_cost_usd": 0.104134,
                "usage": {
                    "input_tokens": 20799,
                    "cache_read_input_tokens": 128,
                    "cache_creation_input_tokens": 64,
                    "output_tokens": 3,
                },
            }
        )

        usage = claude_tokens(text)

        assert usage is not None
        assert usage.input == 20799
        assert usage.output == 3
        assert usage.cache_read == 128
        assert usage.cache_write == 64
        assert usage.total == 20994

    def test_the_reported_cost_is_read(self) -> None:
        text = json.dumps({"type": "result", "total_cost_usd": 0.104134})

        assert cost_from_payloads(text) == Decimal("0.104134")

    def test_output_without_a_usage_block_has_no_tokens(self) -> None:
        assert claude_tokens('{"type":"result","result":"ok"}') is None


class TestTheCodexOutput:
    """Codex —— JSONL，token 在 ``turn.completed`` 里。"""

    def test_the_cached_tokens_are_taken_out_of_the_input(self) -> None:
        """**Codex 的 ``input_tokens`` 含缓存命中。** 不减出来，缓存会被算两遍
        钱 —— 同一个 token 既按输入价又按缓存价。
        """
        text = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 9416,
                            "cached_input_tokens": 128,
                            "output_tokens": 3,
                        },
                    }
                ),
            ]
        )

        usage = codex_tokens(text)

        assert usage is not None
        assert usage.input == 9288
        assert usage.cache_read == 128
        assert usage.output == 3

    def test_a_model_metadata_notice_is_not_a_failure(self) -> None:
        """**Codex 遇到没登记过的模型会打一条兜底提示** —— 那是提示，不是错误。
        把它当成失败，这个执行器每一次都会被记成"没跑成"。
        """
        text = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "error",
                    "message": "Model metadata for `MiniMax-M3` not found. "
                    "Defaulting to fallback metadata.",
                },
            }
        )

        assert codex_is_error(text) is False

    def test_a_real_failure_is_a_failure(self) -> None:
        text = json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_1", "type": "error", "message": "stream disconnected"},
            }
        )

        assert codex_is_error(text) is True


class TestTheOpencodeOutput:
    """OpenCode —— 事件流，token 挂在 ``step_finish`` 的 ``part.tokens`` 上。"""

    def test_the_token_usage_is_read(self) -> None:
        text = "\n".join(
            [
                json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {
                            "type": "step-finish",
                            "tokens": {
                                "total": 36956,
                                "input": 36825,
                                "output": 3,
                                "reasoning": 0,
                                "cache": {"write": 0, "read": 128},
                            },
                            "cost": 0.01105878,
                        },
                    }
                ),
            ]
        )

        usage = opencode_tokens(text)

        assert usage is not None
        assert usage.input == 36825
        assert usage.output == 3
        assert usage.cache_read == 128
        assert usage.total == 36956

    def test_a_cost_of_zero_is_not_a_free_run(self) -> None:
        """**0 不是免费，是没标价。** 记成 0，看板上就会冒出一条"这个执行器便宜
        到离谱"的假差异 —— 那是记账口径的差别。
        """
        text = json.dumps({"type": "step_finish", "part": {"cost": 0}})

        assert opencode_cost(text) is None

    def test_a_priced_run_keeps_its_cost(self) -> None:
        text = json.dumps({"type": "step_finish", "part": {"cost": 0.01105878}})

        assert opencode_cost(text) == Decimal("0.01105878")


class TestTheProviderSetup:
    """provider 配置 —— **只写环境变量的名字，不写凭证**。"""

    def test_the_codex_config_points_at_the_backend(self, tmp_path: Path) -> None:
        setup = write_provider_setup(tmp_path, model="MiniMax-M3")

        source = (setup.codex_home / "config.toml").read_text(encoding="utf-8")
        assert 'model = "MiniMax-M3"' in source
        assert "api.minimaxi.com" in source
        assert "ANTHROPIC_AUTH_TOKEN" in source

    def test_the_opencode_config_points_at_the_backend(self, tmp_path: Path) -> None:
        setup = write_provider_setup(tmp_path, model="MiniMax-M3")

        config = json.loads(setup.opencode_config.read_text(encoding="utf-8"))
        provider = config["provider"]["bench"]
        assert provider["options"]["baseURL"] == "https://api.minimaxi.com/v1"
        assert "MiniMax-M3" in provider["models"]

    def test_no_credential_is_written_to_disk(self, tmp_path: Path) -> None:
        """**配置文件要进版本库的邻居** —— 里面绝不能有 token。"""
        setup = write_provider_setup(tmp_path, model="MiniMax-M3")

        written = (setup.codex_home / "config.toml").read_text(encoding="utf-8")
        written += setup.opencode_config.read_text(encoding="utf-8")
        assert "sk-" not in written


class TestTheRecordsSurviveTheRoundTrip:
    """原始记录 → 记录对象 —— 看板要能从 JSONL 重新算出来。"""

    def test_the_tokens_survive(self) -> None:
        entry = {
            "task_id": "pilot-001",
            "executor": "codex",
            "attempt": 1,
            "outcome": "succeeded",
            "duration_seconds": "61.257",
            "cost": "0.42",
            "currency": "USD",
            "tokens": {"input": 100, "output": 20, "cache_read": 5, "cache_write": 1},
        }

        record = record_from_dict(entry)

        assert record.tokens is not None
        assert record.tokens.input == 100
        assert record.tokens.cache_read == 5
        assert record.tokens.total == 126

    def test_a_record_without_tokens_has_none(self) -> None:
        entry = {
            "task_id": "pilot-001",
            "executor": "gemini-cli",
            "attempt": 1,
            "outcome": "not_run",
        }

        assert record_from_dict(entry).tokens is None

    def test_the_raw_output_and_the_retries_survive(self) -> None:
        """**原始输出要留着** —— 只有一句结论，事后分不清是限流还是能力差。"""
        entry = {
            "task_id": "pilot-001",
            "executor": "codex",
            "attempt": 1,
            "outcome": "not_run",
            "output": "We're currently experiencing high demand",
            "retries": 2,
        }

        record = record_from_dict(entry)

        assert record.output == "We're currently experiencing high demand"
        assert record.retries == 2


class TestThePriceTable:
    """价目表 —— 成本折算的依据。"""

    def test_the_cost_of_a_run_is_folded_from_its_tokens(self) -> None:
        """**手算：36825 输入 + 3 输出 + 128 缓存读。**

        36825×0.30/1e6 + 3×1.20/1e6 + 128×0.06/1e6
        = 0.0110475 + 0.0000036 + 0.00000768 = 0.01105878
        """
        usage = TokenUsage(input=36825, output=3, cache_read=128)

        cost = price_table().cost_of(usage)

        assert cost == Decimal("0.01105878")


class TestTheRedaction:
    """写盘之前一律脱敏 —— 报告要进版本库。"""

    def test_a_token_is_removed(self) -> None:
        text = "authorization: Bearer sk-cp-abcdefghijklmnopqrstuvwxyz012345"

        cleaned = redact(text)

        assert "sk-cp-abcdefghijklmnopqrstuvwxyz012345" not in cleaned
        assert "***" in cleaned

    def test_a_jwt_is_removed(self) -> None:
        text = "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"

        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redact(text)


class TestTheRawOutputIsRedacted:
    """原始输出也要脱敏 —— 但**不能压成一行**。"""

    def test_the_line_structure_survives(self) -> None:
        """**原始输出是给人查因的。** 压成一行就看不出哪一行报错了。"""
        text = '{"type":"error"}\n{"type":"turn.completed"}\n'

        cleaned = redact_output(text)

        assert cleaned.count("\n") == 2

    def test_a_credential_is_removed(self) -> None:
        text = "sk-cp-abcdefghijklmnopqrstuvwxyz012345"

        assert "sk-cp-abcdefghijklmnopqrstuvwxyz012345" not in redact_output(text)

    def test_the_tail_is_kept(self) -> None:
        """**报错通常在最后** —— 截断要留尾巴，不是留开头。"""
        text = "前面很长" * 2000 + "这是最后一句"

        cleaned = redact_output(text, limit=200)

        assert cleaned.endswith("这是最后一句")


class TestTheTransientFailures:
    """**只认后端自己的话** —— 环境抖了才重试，能力差不重试。"""

    def _record(self, outcome: str, detail: str, output: str = "") -> RunRecord:
        return RunRecord(
            task_id="pilot-001",
            executor="codex",
            attempt=1,
            outcome=RunOutcome(outcome),
            detail=detail,
            output=output,
        )

    def test_a_busy_backend_is_transient(self) -> None:
        record = self._record(
            "not_run",
            "执行器报错：{'message': 'We're currently experiencing high demand'}",
        )

        assert is_transient_failure(record) is True

    def test_a_server_error_status_is_transient(self) -> None:
        record = self._record(
            "not_run",
            "执行器报错：（没有说清楚）",
            '{"type":"result","api_error_status":500}',
        )

        assert is_transient_failure(record) is True

    def test_a_timeout_is_not_transient(self) -> None:
        """**超时是执行器自己跑得慢** —— 重试只会把"它慢"盖成"它行"。"""
        record = self._record("not_run", "超过 300 秒仍未结束，已终止")

        assert is_transient_failure(record) is False

    def test_a_capability_failure_is_not_transient(self) -> None:
        """**"模型没做对"是能力问题** —— 那是 FAILED，根本不是环境问题。"""
        record = self._record("failed", "复现用例没过")

        assert is_transient_failure(record) is False

    def test_a_plain_environment_failure_is_not_transient(self) -> None:
        """**认不出原因就不重试** —— 宁可少重试，也不要把能力差重试成通过。"""
        record = self._record("not_run", "执行器报错：认证过期")

        assert is_transient_failure(record) is False
