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
    append_log,
    claude_tokens,
    codex_is_error,
    codex_tokens,
    cost_from_payloads,
    is_quota_exhausted,
    is_transient_failure,
    opencode_cost,
    opencode_tokens,
    price_table,
    read_log,
    record_from_dict,
    redact,
    redact_output,
    token_steps,
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

    def test_the_last_usage_block_wins(self) -> None:
        """**``result`` 报的是整场会话的累计用量** —— 混进别的 ``usage`` 块时，
        取最后一份，别把两份加起来。
        """
        text = "\n".join(
            [
                json.dumps({"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 1}}),
                json.dumps(
                    {
                        "type": "result",
                        "usage": {
                            "input_tokens": 20799,
                            "cache_read_input_tokens": 128,
                            "cache_creation_input_tokens": 64,
                            "output_tokens": 3,
                        },
                    }
                ),
            ]
        )

        usage = claude_tokens(text)

        assert usage is not None
        assert usage.input == 20799
        assert usage.output == 3


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

    def test_the_last_turn_wins(self) -> None:
        """**取最后一个 ``turn.completed``** —— 一次 ``codex exec`` 是一轮，
        但输出里若混进多轮，最后一轮的数已经含了前面几轮。
        """
        text = "\n".join(
            [
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 500,
                            "cached_input_tokens": 100,
                            "output_tokens": 10,
                        },
                    }
                ),
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

    def test_the_last_step_is_the_cumulative_one(self) -> None:
        """**``step_finish`` 里有两种语义 —— 必须分开处理。**

        抓了一次完整的事件流逐字段对过：

        - ``input`` / ``output`` / ``reasoning`` 是**每一步的增量**（第一步
          ``input 35009``，第二步就掉到 ``141``；``output`` 也会从 75 掉回 45）
          —— **要加起来**；
        - ``cache.read`` 是**累计值**（1956 → 36964 → 37104 → 37224 → 37315，
          一路只增不减）—— **取最后一个就对了**。

        下面的事件流是实测抓的（把 src/calc.py 里的加法改成减法）。
        """
        text = "\n".join(
            [
                self._step(35009, 30, 78, 1956),
                self._step(141, 45, 0, 36964),
                self._step(121, 75, 0, 37104),
                self._step(92, 45, 0, 37224),
                self._step(121, 19, 0, 37315),
            ]
        )

        usage = opencode_tokens(text)

        assert usage is not None
        # 增量相加：35009+141+121+92+121
        assert usage.input == 35484
        # 输出 30+45+75+45+19，推理 78 并进来
        assert usage.output == 214 + 78
        # 缓存读是累计值 —— 取最后一个
        assert usage.cache_read == 37315
        assert usage.total == 35484 + 214 + 78 + 37315

    def _step(self, inp: int, out: int, reasoning: int, cache_read: int) -> str:
        return json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "type": "step-finish",
                    "tokens": {
                        "total": inp + out + reasoning + cache_read,
                        "input": inp,
                        "output": out,
                        "reasoning": reasoning,
                        "cache": {"write": 0, "read": cache_read},
                    },
                    "cost": 0,
                },
            }
        )

    def test_the_reasoning_tokens_are_counted_as_output(self) -> None:
        """**推理 token 按输出价计费** —— 不并进输出，就等于白送它一截便宜。

        Claude Code 与 Codex 的输出计数里都已经含了推理；OpenCode 单列一格，
        不并进去，三家就没法比。
        """
        text = self._step(100, 50, 150, 900)

        usage = opencode_tokens(text)

        assert usage is not None
        assert usage.output == 200

    def test_a_cost_of_zero_is_not_a_free_run(self) -> None:
        """**0 不是免费，是没标价。** 记成 0，看板上就会冒出一条"这个执行器便宜
        到离谱"的假差异 —— 那是记账口径的差别。
        """
        text = json.dumps({"type": "step_finish", "part": {"cost": 0}})

        assert opencode_cost(text) is None

    def test_a_priced_run_keeps_its_cost(self) -> None:
        text = json.dumps({"type": "step_finish", "part": {"cost": 0.01105878}})

        assert opencode_cost(text) == Decimal("0.01105878")

    def test_the_last_step_carries_the_cost(self) -> None:
        """**成本也是累计的** —— 和缓存读一个道理。"""
        text = "\n".join(
            [
                json.dumps({"type": "step_finish", "part": {"cost": 0.001}}),
                json.dumps({"type": "step_finish", "part": {"cost": 0.01105878}}),
            ]
        )

        assert opencode_cost(text) == Decimal("0.01105878")

    def test_every_step_is_kept_for_a_later_recalculation(self) -> None:
        """**逐步明细要留在日志里** —— 原始输出是截断的，事后翻不回来。

        口径改了要能重算，而重算不能靠"再跑一遍几十次真任务"。
        """
        text = "\n".join([self._step(35009, 30, 78, 1956), self._step(141, 45, 0, 36964)])

        steps = token_steps(text)

        assert len(steps) == 2
        assert steps[0]["input"] == 35009
        assert steps[0]["cache_read"] == 1956
        assert steps[1]["input"] == 141
        assert steps[1]["cache_read"] == 36964


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


class TestTheQuotaIsNotTheRateLimit:
    """**额度用完了 ≠ 忙不过来** —— 两者的处理方式不一样。"""

    def _record(self) -> RunRecord:
        return RunRecord(
            task_id="pilot-004",
            executor="opencode",
            attempt=2,
            outcome=RunOutcome.NOT_RUN,
            detail="执行器报错：已达到 Token Plan 用量上限",
            output=(
                '{"type":"error","error":{"name":"APIError","data":'
                '{"message":"已达到 Token Plan 用量上限","statusCode":429,'
                '"isRetryable":true}}}'
            ),
        )

    def test_the_quota_is_recognised(self) -> None:
        assert is_quota_exhausted(self._record()) is True

    def test_the_quota_is_not_retried(self) -> None:
        """**429 看着像限流，但额度不会因为重试而多出来。**

        实测撞过一次：按限流去重试，三次重试烧掉三倍的调用，换来的还是同一句
        "额度用完了"。
        """
        assert is_transient_failure(self._record()) is False

    def test_a_busy_backend_is_not_a_quota_problem(self) -> None:
        record = RunRecord(
            task_id="pilot-004",
            executor="opencode",
            attempt=2,
            outcome=RunOutcome.NOT_RUN,
            detail="执行器报错：high demand",
        )

        assert is_quota_exhausted(record) is False

    def test_a_capability_failure_is_not_a_quota_problem(self) -> None:
        record = RunRecord(
            task_id="pilot-004",
            executor="opencode",
            attempt=2,
            outcome=RunOutcome.FAILED,
            detail="复现用例没过",
        )

        assert is_quota_exhausted(record) is False


class TestTheLogKeepsOneRowPerRun:
    """**同一个 ``(执行器, 任务, 次数)`` 只留一行。**

    **"没跑成"的旧记录会被顶掉** —— 它是空缺不是结果，环境修好了就该重跑。
    **跑过了的不会被顶** —— 那是一次量好的数据。
    """

    def _entry(self, outcome: str) -> dict[str, object]:
        return {
            "task_id": "pilot-001",
            "executor": "opencode",
            "attempt": 1,
            "outcome": outcome,
        }

    def test_a_stale_not_run_row_is_replaced(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        append_log(log, self._entry("not_run"))
        append_log(log, self._entry("succeeded"))

        entries = read_log(log)

        assert len(entries) == 1
        assert entries[0]["outcome"] == "succeeded"

    def test_a_finished_row_is_never_overwritten(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        append_log(log, self._entry("failed"))
        append_log(log, self._entry("succeeded"))

        entries = read_log(log)

        assert len(entries) == 1
        assert entries[0]["outcome"] == "failed"

    def test_different_runs_are_kept_apart(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        append_log(log, self._entry("not_run"))
        append_log(log, {**self._entry("not_run"), "attempt": 2})
        append_log(log, {**self._entry("not_run"), "task_id": "pilot-002"})

        assert len(read_log(log)) == 3
