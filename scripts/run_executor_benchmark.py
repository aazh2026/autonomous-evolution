"""执行器横向评测 —— 驱动脚本：真跑一遍，产出对比看板。

设计依据：docs/tasks.md TASK-P2-018（"在标准基准任务集上运行 Claude Code、
Codex、Gemini CLI、OpenCode 各 ≥3 次，按统一口径产出成功率、成本、耗时对比
看板，**验证统计显著性差异**"）；I-17（多执行器结果可比性口径需前置设计）。

**口径在 ``evaluation/executors/benchmark.py`` 里，这个脚本只负责"把口径跑起
来"。** 换执行器、换任务集、换机器，都不动口径 —— 口径一改，前面量出来的数
就废了。

**先探针，再矩阵。** 一个执行器连"回答一个字 ok"都做不到（后端连不上、认证
过期、参数不认），就别急着跑 27 次真任务 —— 那是把同一个环境问题重复 27 遍，
烧钱还烧时间。探针失败 → 该执行器的全部运行记为"没跑成"，理由写清楚。**探针
本身也是证据**，一并存进报告。

**同一个模型。** 四个执行器跑的是同一个模型、连的是同一个后端 —— 比的是执行
器，就不能同时换模型。Codex 与 OpenCode 的 provider 配置由本脚本现写（**只写
环境变量的名字，不写凭证**）。

**跑得断，跑得续。** 每跑完一次就往 JSONL 里追加一行。中途断了、机器睡了、
对话换了 —— 接着跑就行，不用从头再来。**看板也能只从原始记录重新算**
（``--render-only``）—— 口径改了要重算，不该重跑那几十次真任务。

**报告里不许有凭证。** CLI 的 stderr 里有可能带出 token，而报告是要进版本库
的。所以写盘前一律脱敏。

**用法**::

    python -m scripts.run_executor_benchmark
    python -m scripts.run_executor_benchmark --attempts 3 --timeout 300
    python -m scripts.run_executor_benchmark --executors claude-code opencode
    python -m scripts.run_executor_benchmark --render-only --log docs/evaluation/runs.jsonl
    python -m scripts.run_executor_benchmark --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from evaluation.executors.benchmark import (
    BenchmarkBoard,
    BenchmarkHarness,
    BenchmarkTask,
    CliRunner,
    CliSpec,
    ExecutionObservation,
    PriceTable,
    RunOutcome,
    RunRecord,
    TokenUsage,
    build_board,
    evaluate_executor,
    pilot_benchmark_tasks,
)

#: 试点仓库 —— 标准任务集的来源。
DEFAULT_REPO = Path("samples/pilot_repo")

#: 原始记录（JSONL）。**每跑完一次就追加一行**，所以跑得断也跑得续。
DEFAULT_LOG = Path("docs/evaluation/executor-benchmark-runs.jsonl")

#: 探针提示 —— 与基准任务无关，只问它能不能干活。
PROBE_PROMPT = "只回答一个字：ok。不要读文件，不要执行任何命令。"

#: 探针的超时。**探针不该等太久** —— 它只证明"能不能干活"，证明不了就报
#: "没跑成"，剩下 27 次真任务不必再烧。
PROBE_TIMEOUT: int = 90

#: 重试之间的等待（秒）。**逐次加倍** —— 后端刚缓过来，挤上去只会再被拒。
RETRY_BACKOFF_SECONDS: int = 15

#: 认得出"后端暂时不行"的说法 —— **只有这些才值得重试**。
#:
#: **认的是后端自己的话**，不是执行器的话。"我忙不过来"是环境；"我做完了"是
#: 能力。
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "high demand",
    "rate limit",
    "rate_limit",
    "too many requests",
    "overloaded",
    "temporarily unavailable",
    "service unavailable",
    "服务繁忙",
    "限流",
    "稍后重试",
)

#: 原始输出存盘时留多少字 —— 够查因，又不至于把仓库塞满。
OUTPUT_LIMIT: int = 4000

#: 后端报"你太频繁了"的状态码。
_STATUS_TOO_MANY_REQUESTS: int = 429

#: 后端报"我这边出问题了"的状态码起点。
_STATUS_SERVER_ERROR: int = 500

#: OpenCode 用的模型。**它的默认模型是本机一个没起的 MLX 服务**（连接就
#: 404），所以显式指一个能通的 —— 换机器时这个值要跟着改。
DEFAULT_OPENCODE_MODEL = "google/antigravity-claude-sonnet-4-6"

#: 评测统一使用的模型 —— **四个执行器跑的是同一个模型、连的是同一个后端**。
#:
#: **为什么必须固定模型。** 比的是执行器，就不能同时换模型 —— 换了模型，
#: "Claude Code 成功率更高"这句话就说不清了：是它执行得好，还是它背后的模型
#: 更强？那就成了一句谁也复核不了的话。
DEFAULT_MODEL = "MiniMax-M3"

#: 后端地址 —— OpenAI 兼容的那一套。
DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"

#: 凭证从哪个环境变量读 —— **只读环境变量，绝不把 token 写进文件**。
DEFAULT_API_KEY_ENV = "ANTHROPIC_AUTH_TOKEN"

#: 统一价目表 —— 每百万 token 的价格。
#:
#: **出处：models.dev 的 ``minimax/MiniMax-M3`` 条目**（厂商自己的表，2026-11-10
#: 取的值）。价目表是**数据不是口径** —— 折算的公式在
#: :mod:`evaluation.executors.benchmark` 里，数字在这儿，换模型就换这张表。
PRICE_INPUT = Decimal("0.30")
PRICE_OUTPUT = Decimal("1.20")
PRICE_CACHE_READ = Decimal("0.06")
#: 厂商价目表只列了缓存**读**价。缓存**写**按输入价计 —— 宁可算高一点，也不
#: 凭空造出一笔便宜。
PRICE_CACHE_WRITE = PRICE_INPUT

#: 凭证形状 —— 写盘前一律脱敏（对齐 tests/unit/infrastructure/
#: test_no_hardcoded_secrets.py 里的那套形状规则）。
_CREDENTIAL_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
)


def redact(text: str, *, limit: int = 200) -> str:
    """脱敏并截断 —— 报告要进版本库，里面不该有凭证。

    **宁可少写，也不要写进去。** 报告的价值是"别人能复核这些数"，不是"把
    终端输出全存一份"。
    """
    cleaned = text
    for shape in _CREDENTIAL_SHAPES:
        cleaned = shape.sub("***", cleaned)
    cleaned = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def redact_output(text: str, *, limit: int = OUTPUT_LIMIT) -> str:
    """脱敏原始输出 —— **但保留换行**。

    **原始输出是给人查因的。** 压成一行就看不出"哪一行报错了"；所以这里只
    脱凭证，不动排版。截断保留尾部 —— 报错通常在最后。
    """
    cleaned = text
    for shape in _CREDENTIAL_SHAPES:
        cleaned = shape.sub("***", cleaned)
    cleaned = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", cleaned)
    if len(cleaned) <= limit:
        return cleaned
    return "…" + cleaned[-limit:]


def is_transient_failure(record: RunRecord) -> bool:
    """这次失败是不是**环境暂时不行**。

    **只认后端自己的话** —— 限流、繁忙、暂时不可用，外加后端自己报出来的
    429/5xx 状态码。

    **超时不算。** 超时是执行器自己跑得慢，那是能力问题 —— 重试只会把它盖成
    "它行"，而这次评测要量的恰恰是能力。

    **"模型没做对"也不算。** 那是 FAILED，不是 NOT_RUN，根本不进这个判断。
    """
    if record.outcome is not RunOutcome.NOT_RUN:
        return False
    haystack = f"{record.detail}\n{record.output}".lower()
    if any(marker in haystack for marker in _TRANSIENT_MARKERS):
        return True
    status = _api_error_status(record.output)
    if status is None:
        return False
    return status == _STATUS_TOO_MANY_REQUESTS or status >= _STATUS_SERVER_ERROR


def _api_error_status(output: str) -> int | None:
    """从原始输出里读出后端报的状态码。"""
    for payload in json_payloads(output):
        status = payload.get("api_error_status")
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    return None


def json_payloads(text: str) -> list[dict[str, object]]:
    """从输出里抽出所有 JSON 对象。

    **四个 CLI 的输出格式不一样** —— claude 是一整个 JSON，codex 是 JSONL，
    gemini 是一整个 JSON，opencode 是 JSONL 事件流。这里不挑格式，一行一行
    试着解，解出来是对象就算。
    """
    payloads: list[dict[str, object]] = []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads


def cost_from_payloads(text: str) -> Decimal | None:
    """从输出里读成本 —— **读不到就是读不到**。

    成本字段在这几个 CLI 里的叫法是同族的（``total_cost_usd`` / ``cost_usd``
    / ``cost``），位置可能在顶层也可能在 ``part`` / ``usage`` 里。
    """
    for payload in json_payloads(text):
        for container in (payload, payload.get("part"), payload.get("usage")):
            if not isinstance(container, dict):
                continue
            for key in ("total_cost_usd", "cost_usd", "cost"):
                value = container.get(key)
                if value is None:
                    continue
                try:
                    return Decimal(str(value))
                except InvalidOperation:
                    continue
    return None


def opencode_cost(text: str) -> Decimal | None:
    """OpenCode 的成本 —— 但 antigravity 这条路**没有价目表**。

    **0 不是免费，是没标价。** OpenCode 对不在它价目表里的模型一律报 0。
    把这个 0 当成本记上去，看板上就会冒出一条"OpenCode 便宜到离谱"的假差异
    —— 那是记账口径的差别，不是能力差别。所以这里记 ``None``（未计价），
    成本那一格写"无样本"。
    """
    value = cost_from_payloads(text)
    if value is not None and value == 0:
        return None
    return value


def claude_is_error(text: str) -> bool:
    """Claude Code 的失败信号。

    **"用完了轮次"不算环境坏** —— 那说明它跑了、只是没跑完，是能力问题
    （FAILED，交给验收判）；只有 API 层出错（认证、配额、网络）才算"没跑
    成"（NOT_RUN）。
    """
    for payload in json_payloads(text):
        if payload.get("api_error_status") is not None:
            return True
        if payload.get("is_error") is True and payload.get("subtype") != "error_max_turns":
            return True
    return False


def json_error_event(text: str) -> bool:
    """输出里有没有"环境级失败"的信号。

    **只认环境级失败** —— 认证不通、后端连不上、配额用尽、参数不认。
    **不认"模型没做对"** —— 那属于能力问题，该记 FAILED。
    """
    for payload in json_payloads(text):
        if payload.get("type") == "error":
            return True
        error = payload.get("error")
        if isinstance(error, dict) and error:
            return True
        if isinstance(error, str) and error:
            return True
    return False


def codex_is_error(text: str) -> bool:
    """Codex 的失败信号。

    **Codex 把错误包在 ``item.completed`` 里** —— 外层的 ``type`` 是
    ``item.completed``，真正的错误类型在 ``item.type`` 里。

    **"Model metadata not found" 不算失败。** Codex 遇到没登记过的模型会打一
    条兜底提示（用默认的上下文长度），随后照常干活 —— 那是**提示**，不是错误。
    把它当成失败，这个执行器每一次都会被记成"没跑成"。
    """
    for payload in json_payloads(text):
        if payload.get("type") == "error":
            return True
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "error":
            message = str(item.get("message", ""))
            if "Model metadata" in message:
                continue
            return True
        error = payload.get("error")
        if isinstance(error, dict) and error:
            return True
        if isinstance(error, str) and error:
            return True
    return False


def claude_tokens(text: str) -> TokenUsage | None:
    """Claude Code 的 token 用量。

    **``input_tokens`` 不含缓存命中** —— 缓存读、缓存写是另外两个字段。
    """
    for payload in json_payloads(text):
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        return TokenUsage(
            input=_as_int(usage.get("input_tokens")) or 0,
            output=_as_int(usage.get("output_tokens")) or 0,
            cache_read=_as_int(usage.get("cache_read_input_tokens")) or 0,
            cache_write=_as_int(usage.get("cache_creation_input_tokens")) or 0,
        )
    return None


def codex_tokens(text: str) -> TokenUsage | None:
    """Codex 的 token 用量。

    **Codex 的 ``input_tokens`` 含缓存命中** —— 所以要把命中的那部分减出来，
    否则缓存会被算两遍钱。
    """
    for payload in json_payloads(text):
        if payload.get("type") != "turn.completed":
            continue
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        total_input = _as_int(usage.get("input_tokens")) or 0
        cached = _as_int(usage.get("cached_input_tokens")) or 0
        return TokenUsage(
            input=max(total_input - cached, 0),
            output=_as_int(usage.get("output_tokens")) or 0,
            cache_read=cached,
            cache_write=0,
        )
    return None


def opencode_tokens(text: str) -> TokenUsage | None:
    """OpenCode 的 token 用量。

    **token 数挂在 ``step_finish`` 事件的 ``part.tokens`` 里** —— 输入、输出、
    推理、缓存读写各占一格，其中输入**不含**缓存读。
    """
    for payload in json_payloads(text):
        part = payload.get("part")
        if not isinstance(part, dict):
            continue
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            continue
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        return TokenUsage(
            input=_as_int(tokens.get("input")) or 0,
            output=_as_int(tokens.get("output")) or 0,
            cache_read=_as_int(cache.get("read")) or 0,
            cache_write=_as_int(cache.get("write")) or 0,
        )
    return None


def claude_argv(_workspace: str, prompt: str, model: str) -> tuple[str, ...]:
    """Claude Code 的命令行。

    **权限放开是有意的。** 评测要在无人值守下跑完，而复现命令要执行
    ``python3 -m pytest`` —— 不放权限它连复现都跑不了。工作区是临时铺出来
    的仓库副本，炸也炸不坏原仓库。

    **工具白名单用逗号分隔** —— 空格分隔会把 ``Bash(python3 -m pytest:*)``
    这样的条目从中间切开。

    **模型写死在命令行里** —— 别让"这台机器的默认模型"悄悄决定评测跑的是
    什么模型。
    """
    allowed = ",".join(
        (
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            "Bash(python3 -m pytest:*)",
            "Bash(python3:*)",
            "Bash(ls:*)",
            "Bash(cat:*)",
            "Bash(pwd:*)",
            "Bash(find:*)",
            "Bash(grep:*)",
        )
    )
    return (
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        model,
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        allowed,
    )


def codex_argv(workspace: str, prompt: str) -> tuple[str, ...]:
    """Codex 的命令行。

    ``--skip-git-repo-check`` —— 试点仓库没有 ``.git``，不跳过它 Codex 会
    直接拒绝动手。``--dangerously-bypass-approvals-and-sandbox`` —— 无人值守
    跑，审批没人点。

    **模型不在这儿指定** —— 在 ``config.toml`` 里（``model`` 字段），因为模型
    跟 provider 是一份配置。
    """
    return (
        "codex",
        "exec",
        prompt,
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "-C",
        workspace,
    )


def gemini_argv(_workspace: str, prompt: str) -> tuple[str, ...]:
    """Gemini CLI 的命令行。"""
    return ("gemini", "--approval-mode", "yolo", "-o", "json", prompt)


def opencode_argv(_workspace: str, prompt: str, model: str) -> tuple[str, ...]:
    """OpenCode 的命令行。"""
    return ("opencode", "run", "--format", "json", "--auto", "-m", model, prompt)


@dataclass(frozen=True, slots=True)
class ProviderSetup:
    """各执行器怎么找到那个模型。

    **Codex 与 OpenCode 各要一份自己的配置格式** —— 一个是 TOML，一个是
    JSON；而且它们找配置的地方也不一样（一个看 ``CODEX_HOME``，一个看
    ``OPENCODE_CONFIG``）。
    """

    #: 模型名。
    model: str
    #: 后端地址（OpenAI 兼容的那一套）。
    base_url: str
    #: 凭证在哪个环境变量里。
    api_key_env: str
    #: Codex 的配置目录。
    codex_home: Path
    #: OpenCode 的配置文件。
    opencode_config: Path


#: provider 的名字 —— OpenCode 里用 ``provider/model`` 的写法。
PROVIDER_ID = "bench"


def write_provider_setup(
    root: Path,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key_env: str = DEFAULT_API_KEY_ENV,
) -> ProviderSetup:
    """把 provider 配置写出来。

    **配置里没有凭证** —— 只有环境变量的名字。凭证由子进程自己从环境里读。
    """
    codex_home = root / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        f'model = "{model}"\n'
        f'model_provider = "{PROVIDER_ID}"\n'
        "\n"
        f"[model_providers.{PROVIDER_ID}]\n"
        f'name = "{PROVIDER_ID}"\n'
        f'base_url = "{base_url}"\n'
        f'env_key = "{api_key_env}"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n",
        encoding="utf-8",
    )
    opencode_config = root / "opencode.json"
    opencode_config.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    PROVIDER_ID: {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "评测后端",
                        "options": {
                            "baseURL": base_url,
                            "apiKey": "{env:" + api_key_env + "}",
                        },
                        "models": {model: {"name": model}},
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return ProviderSetup(
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        codex_home=codex_home,
        opencode_config=opencode_config,
    )


def executor_specs(setup: ProviderSetup) -> dict[str, CliSpec]:
    """四个执行器的调用口径。"""
    model = setup.model
    return {
        "claude-code": CliSpec(
            executor="claude-code",
            binary="claude",
            argv=lambda workspace, prompt: claude_argv(workspace, prompt, model),
            cost_of=cost_from_payloads,
            tokens_of=claude_tokens,
            currency="USD",
            env={"ANTHROPIC_MODEL": model},
            is_error=claude_is_error,
        ),
        "codex": CliSpec(
            executor="codex",
            binary="codex",
            argv=codex_argv,
            cost_of=cost_from_payloads,
            tokens_of=codex_tokens,
            currency="USD",
            env={"CODEX_HOME": str(setup.codex_home)},
            is_error=codex_is_error,
        ),
        "gemini-cli": CliSpec(
            executor="gemini-cli",
            binary="gemini",
            argv=gemini_argv,
            cost_of=cost_from_payloads,
            currency="USD",
            is_error=json_error_event,
        ),
        "opencode": CliSpec(
            executor="opencode",
            binary="opencode",
            argv=lambda workspace, prompt: opencode_argv(
                workspace, prompt, f"{PROVIDER_ID}/{model}"
            ),
            cost_of=opencode_cost,
            tokens_of=opencode_tokens,
            currency="USD",
            env={"OPENCODE_CONFIG": str(setup.opencode_config)},
            is_error=json_error_event,
        ),
    }


async def probe(spec: CliSpec, timeout: int = PROBE_TIMEOUT) -> ExecutionObservation:
    """探针 —— 只问它能不能干活。

    **探针失败就不要再跑矩阵。** 那不是"它这次没做好"，是"它在这儿根本用不
    起来" —— 记成"没跑成"，理由写清楚，比跑 27 次然后 27 次都记成"没跑成"
    省事，也省钱。
    """
    runner = CliRunner(spec)
    with tempfile.TemporaryDirectory(prefix="executor-probe-") as workspace:
        task = BenchmarkTask(
            task_id="probe",
            issue_id="PROBE",
            summary="探针",
            target_test="probe::probe",
            title="探针",
            instruction=PROBE_PROMPT,
            repo=Path(workspace),
            timeout_seconds=timeout,
        )
        return await runner.run(task, Path(workspace))


def read_log(path: Path) -> list[dict[str, object]]:
    """读原始记录 —— 用来续跑。"""
    if not path.exists():
        return []
    entries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            entries.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return entries


def append_log(path: Path, entry: Mapping[str, object]) -> None:
    """追加一条原始记录。

    **带上时间戳** —— 报告要能看出"这些数是什么时候量的"。一周前的数和今天
    的数不是一回事。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = {**entry, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stamped, ensure_ascii=False) + "\n")


def _as_bool(value: object) -> bool | None:
    """从原始记录里取一个布尔值 —— 类型不对就当没有。"""
    return value if isinstance(value, bool) else None


def _as_int(value: object) -> int | None:
    """从原始记录里取一个整数 —— 类型不对就当没有。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def record_from_dict(entry: Mapping[str, object]) -> RunRecord:
    """把原始记录还原成 :class:`RunRecord`。"""
    return RunRecord(
        task_id=str(entry["task_id"]),
        executor=str(entry["executor"]),
        attempt=int(str(entry["attempt"])),
        outcome=RunOutcome(str(entry["outcome"])),
        target_passed=_as_bool(entry.get("target_passed")),
        regressions=_as_int(entry.get("regressions")),
        suite_ran=bool(entry.get("suite_ran", False)),
        duration_seconds=(
            Decimal(str(entry["duration_seconds"])) if entry.get("duration_seconds") else None
        ),
        cost=Decimal(str(entry["cost"])) if entry.get("cost") is not None else None,
        currency=str(entry["currency"]) if entry.get("currency") else None,
        tokens=_tokens_from(entry.get("tokens")),
        touched_reserved=bool(entry.get("touched_reserved", False)),
        detail=str(entry.get("detail", "")),
        output=str(entry.get("output", "")),
        retries=_as_int(entry.get("retries")) or 0,
    )


def _tokens_from(value: object) -> TokenUsage | None:
    """从原始记录里还原 token 用量 —— 字段不对就当没有。"""
    if not isinstance(value, dict):
        return None
    return TokenUsage(
        input=_as_int(value.get("input")) or 0,
        output=_as_int(value.get("output")) or 0,
        cache_read=_as_int(value.get("cache_read")) or 0,
        cache_write=_as_int(value.get("cache_write")) or 0,
    )


def price_table() -> PriceTable:
    """统一价目表 —— 成本折算的依据。"""
    return PriceTable(
        input=PRICE_INPUT,
        output=PRICE_OUTPUT,
        cache_read=PRICE_CACHE_READ,
        cache_write=PRICE_CACHE_WRITE,
        currency="USD",
        source="models.dev 的 minimax/MiniMax-M3 条目（2026-11-10 取值）",
    )


@dataclass(frozen=True, slots=True)
class RunPlan:
    """一轮评测跑什么。

    **参数打包成对象** —— 跑到一半才发现某个参数传错了，那种错在几十次真
    跑之后才显形，代价太大。
    """

    #: 试点仓库。
    repo: Path
    #: 工作区的根。
    root: Path
    #: 原始记录。
    log: Path
    #: 每个任务跑几次。
    attempts: int
    #: 单次运行的超时。
    timeout: int
    #: 环境级故障的重试上限。
    retries: int = 0


async def run_executor(
    spec: CliSpec,
    tasks: Sequence[BenchmarkTask],
    plan: RunPlan,
    done: set[tuple[str, str, int]],
) -> list[RunRecord]:
    """跑一个执行器的全部任务 × 全部次数。"""
    harness = BenchmarkHarness(
        CliRunner(spec),
        repo=plan.repo,
        root=plan.root / spec.executor,
        timeout_seconds=plan.timeout,
    )
    records: list[RunRecord] = []
    for task in tasks:
        for attempt in range(1, plan.attempts + 1):
            key = (spec.executor, task.task_id, attempt)
            if key in done:
                records.append(record_from_dict(_find_log(plan.log, key)))
                continue
            record = await _run_with_retries(harness, task, attempt, retries=plan.retries)
            records.append(record)
            append_log(plan.log, _entry_of(record))
            done.add(key)
            print(
                f"  [{spec.executor}] {task.task_id} 第{attempt}次：{record.outcome.value}"
                f"（{redact(record.detail, limit=80)}）",
                flush=True,
            )
    return records


async def _run_with_retries(
    harness: BenchmarkHarness,
    task: BenchmarkTask,
    attempt: int,
    *,
    retries: int,
) -> RunRecord:
    """跑一次；**只有环境级故障**才重试。

    **重试的是后端的限流，不是执行器的能力。** 后端说"我忙不过来"，那是环境
    抖了一下；后端说"我做完了"，那是能力。混在一起重试，能力差就会被环境抖
    掩盖。

    **超时不在重试之列。** 超时是执行器自己跑得慢 —— 重试只会把"它慢"盖成
    "它行"，那正好把这次评测要量的东西盖掉。

    重试次数记在记录里 —— 某个执行器次次都要重试才跑得通，那本身就是结论。
    """
    record = await harness.run(task, attempt)
    count = 0
    while count < retries and is_transient_failure(record):
        count += 1
        await asyncio.sleep(RETRY_BACKOFF_SECONDS * count)
        record = await harness.run(task, attempt)
    return replace(record, retries=count)


def _entry_of(record: RunRecord) -> dict[str, object]:
    """记录落盘前脱敏 —— 原始输出也要脱。"""
    entry = record.to_dict()
    entry["output"] = redact_output(record.output)
    return entry


def _find_log(log: Path, key: tuple[str, str, int]) -> dict[str, object]:
    """从原始记录里找一条。"""
    for entry in read_log(log):
        if (
            entry.get("executor") == key[0]
            and entry.get("task_id") == key[1]
            and entry.get("attempt") == key[2]
        ):
            return entry
    raise KeyError(f"原始记录里没有 {key}")


def not_run_records(
    executor: str,
    tasks: Sequence[BenchmarkTask],
    *,
    attempts: int,
    reason: str,
    output: str = "",
) -> list[RunRecord]:
    """给"没跑成"的执行器铺一份记录。

    **一次都不跑，也要留下痕迹。** 报表上少了一个执行器，看板上就是一行空
    —— 而"它为什么没跑"才是选型时最要紧的信息。

    **探针的原始输出也带上** —— 只留一句结论，事后还是猜不出是认证过期还是
    参数不认。
    """
    return [
        RunRecord(
            task_id=task.task_id,
            executor=executor,
            attempt=attempt,
            outcome=RunOutcome.NOT_RUN,
            detail=reason,
            output=output,
        )
        for task in tasks
        for attempt in range(1, attempts + 1)
    ]


async def main_async(args: argparse.Namespace) -> int:
    """跑一遍，出看板。"""
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"仓库不存在：{repo}", file=sys.stderr)
        return 2
    log = Path(args.log)
    setup = write_provider_setup(
        Path(tempfile.mkdtemp(prefix="executor-provider-")),
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
    )
    specs = executor_specs(setup)
    selected = [specs[name] for name in args.executors] if args.executors else list(specs.values())

    if args.render_only:
        return _render_from_log(log)

    tasks = pilot_benchmark_tasks(repo, timeout_seconds=args.timeout)

    print(f"任务集：{len(tasks)} 条 × {args.attempts} 次 × {len(selected)} 个执行器")
    print(f"仓库：{repo}")
    print(f"模型：{setup.model}（{setup.base_url}）")
    if args.dry_run:
        for spec in selected:
            print(f"  {spec.executor}: {' '.join(spec.argv('<工作区>', '<指令>'))}")
        return 0

    done = {
        (str(entry.get("executor")), str(entry.get("task_id")), int(str(entry.get("attempt", 0))))
        for entry in read_log(log)
    }
    plan = RunPlan(
        repo=repo,
        root=Path(tempfile.mkdtemp(prefix="executor-benchmark-")),
        log=log,
        attempts=args.attempts,
        timeout=args.timeout,
        retries=args.retries,
    )
    all_records: list[RunRecord] = []

    for spec in selected:
        print(f"\n=== {spec.executor} ===")
        availability = await probe(spec, args.probe_timeout)
        if not availability.executed or availability.timed_out:
            # **探针超时就等于"它压根没跑起来"** —— 不管它有没有吐出过一个字。
            # 让它接着跑 27 次真任务，27 次都会卡在同一个地方：那是把同一个环境
            # 问题重复 27 遍，还烧 27 遍的钱。
            reason = f"探针就没跑成：{redact(availability.detail)}"
            print(f"  {reason}")
            records = not_run_records(
                spec.executor,
                tasks,
                attempts=args.attempts,
                reason=reason,
                output=availability.output,
            )
            for record in records:
                append_log(log, _entry_of(record))
            all_records.extend(records)
            continue
        print(f"  探针通过（{redact(availability.detail, limit=60)}）")
        records = await run_executor(spec, tasks, plan, done)
        all_records.extend(records)

    board = _build(all_records, [spec.executor for spec in selected])
    print("\n" + board.render())
    print(f"\n原始记录：{log}")
    _write_board(board, args.board)
    return 0


def _render_from_log(log: Path) -> int:
    """**只画板，不重跑** —— 看板必须能从原始记录重新生成。

    **看板是结论，原始记录才是事实。** 结论要能被重新算一遍 —— 所以这里给一
    条"从 JSONL 重新出看板"的路子：改了口径可以重算，不该重跑那几十次真任务。
    """
    entries = read_log(log)
    if not entries:
        print(f"原始记录是空的：{log}", file=sys.stderr)
        return 2
    records = [record_from_dict(entry) for entry in entries]
    names = sorted({record.executor for record in records})
    board = _build(records, names)
    print(board.render())
    print(f"\n原始记录：{log}（{len(records)} 条）")
    return 0


def _build(records: Sequence[RunRecord], names: Sequence[str]) -> BenchmarkBoard:
    """成绩单 + 看板。"""
    reports = []
    for name in names:
        rows = [record for record in records if record.executor == name]
        if rows:
            reports.append(evaluate_executor(rows, prices=price_table()))
    return build_board(reports)


def _write_board(board: BenchmarkBoard, path: str) -> None:
    """把看板写盘。"""
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(board.render() + "\n", encoding="utf-8")
    print(f"看板已写入：{target}")


def build_parser() -> argparse.ArgumentParser:
    """命令行。"""
    parser = argparse.ArgumentParser(description="执行器横向评测")
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="试点仓库")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="原始记录（JSONL）")
    parser.add_argument("--board", default="", help="把看板写到这个 Markdown 文件")
    parser.add_argument("--attempts", type=int, default=3, help="每个任务跑几次")
    parser.add_argument("--timeout", type=int, default=300, help="单次运行的超时（秒）")
    parser.add_argument("--probe-timeout", type=int, default=PROBE_TIMEOUT, help="探针超时")
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="环境级故障（后端限流）的重试上限 —— 超时与能力问题不重试",
    )
    parser.add_argument(
        "--executors",
        nargs="*",
        choices=["claude-code", "codex", "gemini-cli", "opencode"],
        help="只跑这几个执行器",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="评测统一使用的模型")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="后端地址")
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV, help="凭证在哪个环境变量里")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="只从原始记录重新出看板，不重跑",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印命令行，不真跑")
    return parser


def main() -> int:
    """入口。"""
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
