"""执行器横向评测 —— 统一口径、对比看板、统计显著性。

设计依据：docs/tasks.md TASK-P2-018（"在标准基准任务集上运行 Claude Code、
Codex、Gemini CLI、OpenCode 各 ≥3 次，按统一口径产出成功率、成本、耗时对比看
板，**验证统计显著性差异**"）；I-17（多执行器结果可比性口径需前置设计，否则
阶段二验收无法判定）；R-039（成本必须同源同币种）。

**没有统一口径的横向评测等于没做。** 四个执行器跑同样的任务，但"跑完"对它们
各自的含义不一样、"成功"对它们各自的门槛也不一样 —— 那么看板上的高低之分
量的就不是执行器，而是口径。所以这一层把口径写死成代码：

1. **同一批任务** —— 标准任务集就是试点仓库的九条已知缺陷（PILOT-001~009），
   每条都有机器可验的复现用例；
2. **同一段指令** —— 每个执行器拿到**字面完全相同**的指令与工作区；
3. **同一套验收** —— 验收由**我们**跑，不由执行器自证（design.md §3.4："自
   评只是自我报告，不是验收结果"）；
4. **同一套指标与检验** —— 可用率 / 成功率 / 回归率 / 成本 / 耗时 / token 用
   量，指标口径一致，显著性用精确检验 + 多重比较校正；
5. **同一张价目表** —— 成本**不是各 CLI 自报的那个数**，而是用同一张价目表把
   各自的 token 用量折算出来的。**各家工具的计价表不一样**：同一个模型、同一
   次调用，Claude Code 按 $5/M 输入记，OpenCode 按 $0.30/M 记 —— 差 16 倍。
   拿自报成本互相比，比的是记账口径，不是执行器。

**三态，不是两态。** 一次运行有三种结局：

===============  ==========================================================
``SUCCEEDED``   验收通过
``FAILED``      验收没过 —— 执行器跑了，但没做到
``NOT_RUN``     **没跑成** —— 执行器不可用、起不来、或者我们验不成
===============  ==========================================================

**"没跑成"不进成功的分母，但必须单独报出来。** 把没装的执行器算成"成功率
0"，那是在说谎；把它从报表里删掉，那是在藏事。所以还有一项**可用率**：跑了
几次 / 该跑几次。**没有可用率的成功率是会骗人的。**

**验收在干净副本里跑。** 执行器拿到的工作区里有 ``tests/`` 与 ``known_issues/``
—— 它当然会去动它们（"把复现用例搬进回归套件"是个好习惯）。但**验收不在那
份工作区里跑**：验收跑在另一份从原仓库重新铺出来的副本上，测试文件一律取自
原仓库，只把执行器改过的 ``src/`` 覆盖上去。**改验收文件改不动结论** —— 与
其事后查作弊，不如让作弊没有收益。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree

from evaluation.common import MetricResult, MetricState, rate
from evaluation.executors.statistics import (
    ALPHA,
    StatisticsOptions,
    TestResult,
    compare_success_rates,
    holm_adjust,
    permutation_test,
)
from executors.base import CodingAgentAdapter, ExecutionTask, SessionConfig

#: 可用率 —— 跑了多少次 / 该跑多少次。
METRIC_AVAILABILITY = "executor_availability"
#: 成功率 —— 在真正跑起来的那些运行里，验收通过的比例。
METRIC_SUCCESS_RATE = "executor_success_rate"
#: 回归率 —— 引入了回归的运行占"验过套件的那些运行"的比例。
METRIC_REGRESSION_RATE = "executor_regression_rate"
#: 成本中位数 —— **统一价目表折算出来的**，不是各 CLI 自报的。
METRIC_COST = "executor_cost_median"
#: 工具自报成本中位数 —— **只记录，不参与比较**（各家计价表不一样，比不了）。
METRIC_REPORTED_COST = "executor_cost_reported_median"
#: token 用量中位数 —— 成本折算的原料，也是最耐久的那一列。
METRIC_TOKENS = "executor_tokens_median"
#: 耗时中位数。
METRIC_DURATION = "executor_duration_median"

#: 复现用例文件（相对仓库根）。
KNOWN_ISSUES_FILE = "known_issues/test_known_issues.py"

#: 仓库自有的回归套件（相对仓库根）。
SUITE_DIR = "tests"

#: 验收时不采信执行器改动的目录 —— 一律取原仓库的那一份。
_RESERVED_DIRS = ("tests", "known_issues")
#: 验收时不采信执行器改动的文件。
_RESERVED_FILES = ("conftest.py",)
#: 铺工作区时跳过的杂项。
_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".coverage")

#: pytest 的退出码：全部通过。
_EXIT_OK = 0
#: pytest 的退出码：有测试失败。
_EXIT_FAILED = 1
#: pytest 的退出码：一个用例都没收集到。
_EXIT_NO_TESTS = 5

#: 缺陷标记的匹配 —— ``@pytest.mark.xfail(strict=True, reason="PILOT-001：……")``。
_XFAIL_PATTERN = re.compile(
    r"@pytest\.mark\.xfail\("
    r"strict=True,\s*"
    r'reason="(?P<issue>[A-Z]+-\d+)[：:]?\s*(?P<reason>[^"]*)"\s*'
    r"\)\s*\n\s*def\s+(?P<name>test_[A-Za-z0-9_]+)"
)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一次运行的 token 用量。

    **四个字段是刻意分开的** —— 因为"输入"在不同工具里含不含缓存命中不是一样
    的：Claude Code 报的 ``input_tokens`` 不含缓存命中，Codex 报的
    ``input_tokens`` **含**。把它们塞进一个"输入"字段里，就会有人在折算成本时
    把缓存命中算两遍。**这里统一成"没命中缓存的输入"**，各家自己负责换算。

    **为什么要有这个类型。** 成本必须是"同一张价目表 × 各家的 token"折出来的
    —— 各家 CLI 自报的成本是按各自的计价表算的，同一个模型同一个调用能差十几
    倍，拿自报成本互相比，比的是记账口径。
    """

    #: 没命中缓存的输入 token。
    input: int = 0
    #: 输出 token。
    output: int = 0
    #: 命中缓存读到的 token。
    cache_read: int = 0
    #: 写进缓存的 token。
    cache_write: int = 0

    @property
    def total(self) -> int:
        """总量 —— 输入 + 输出 + 缓存读写。"""
        return self.input + self.output + self.cache_read + self.cache_write

    def to_dict(self) -> dict[str, int]:
        """转成可 JSON 化的字典 —— 存盘用。"""
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }


@dataclass(frozen=True, slots=True)
class PriceTable:
    """每百万 token 的价格 —— 成本折算的**唯一**依据。

    **价格是数据，不是口径。** 所以价目表由调用方给（换模型就换表），而折算的
    公式住在这儿 —— 谁来折、按什么折，口径不变。

    **为什么缓存写入按输入价计。** 厂商价目表通常只列缓存**读**价，不列**写**
    价。写价按输入价计（偏保守）比按 0 计（偏乐观）安全：宁可把成本算高一点，
    也不要凭空造出一笔便宜。
    """

    #: 每百万输入 token 的价格。
    input: Decimal
    #: 每百万输出 token 的价格。
    output: Decimal
    #: 每百万缓存读 token 的价格。
    cache_read: Decimal
    #: 每百万缓存写 token 的价格。
    cache_write: Decimal
    #: 币种。
    currency: str = "USD"
    #: 价目表的出处与生效日期 —— 报表上的数要能被人核对。
    source: str = ""

    def cost_of(self, usage: TokenUsage) -> Decimal:
        """把一次运行的 token 用量折成钱。"""
        total = (
            Decimal(usage.input) * self.input
            + Decimal(usage.output) * self.output
            + Decimal(usage.cache_read) * self.cache_read
            + Decimal(usage.cache_write) * self.cache_write
        )
        return total / Decimal(1_000_000)


class RunOutcome(StrEnum):
    """一次运行的三种结局。

    **三态而不是两态**，是因为"执行器没装"与"执行器没做到"要分开报 ——
    前者是环境问题（换一个就好），后者是能力问题（要重新选型）。
    """

    #: 验收通过。
    SUCCEEDED = "succeeded"
    #: 验收没过。
    FAILED = "failed"
    #: 没跑成。
    NOT_RUN = "not_run"


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    """一个基准任务 —— 一次可比较的执行。

    **任务集就是试点仓库的九条已知缺陷。** 挑它们的理由有三个：每条都有机器
    可验的复现用例（不是靠人打分）、每条都改的是真代码（不是改文档）、而且数
    量刚好够做统计（再多就要花更多的钱，更少就测不出差异）。
    """

    #: 任务编号。
    task_id: str
    #: 缺陷编号（PILOT-001）。
    issue_id: str
    #: 缺陷的一句话现象。
    summary: str
    #: 复现用例的 pytest 节点 id。
    target_test: str
    #: 给执行器的标题。
    title: str
    #: 给执行器的指令 —— **所有执行器拿到的是同一段**。
    instruction: str
    #: 源仓库。
    repo: Path
    #: 单次运行的超时（秒）。
    timeout_seconds: int = 600

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id 不能为空")
        if not self.issue_id.strip():
            raise ValueError("issue_id 不能为空")
        if self.timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds 必须为正，得到 {self.timeout_seconds}")


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    """一次执行的**执行器侧**观测。

    **这里不含验收结论。** 验收是另一回事，由 :mod:`evaluation.executors.benchmark`
    的工作区自己跑出来 —— 执行器说自己做完了不算数（design.md §3.4）。
    """

    #: 执行器是否真的动过手。
    executed: bool
    #: 耗时（秒）。
    duration_seconds: Decimal | None = None
    #: 成本 —— **执行器自报的**。折算出来的成本在评测那层算。
    cost: Decimal | None = None
    #: 币种。
    currency: str | None = None
    #: token 用量 —— 成本折算的原料。
    tokens: TokenUsage | None = None
    #: 补充说明 —— 失败原因、退出码之类。
    detail: str = ""
    #: 是不是超时结束的。
    timed_out: bool = False
    #: 执行的原始输出 —— **留着，是为了事后能查"到底发生了什么"**。
    #:
    #: 只有一句结论没有原文，出了事就只能猜。上一轮全量跑挂掉的时候，正是
    #: 因为原文没留，才分不清"执行器不行"与"后端在限流"—— 那 54 条记录白跑了。
    output: str = ""


@dataclass(frozen=True, slots=True)
class RunRecord:
    """一次运行的完整记录 —— 看板的原始输入。

    **三态在这里定案。** 执行器的观测与我们的验收结论合成一条记录，谁也不能
    单独改写它。
    """

    #: 任务编号。
    task_id: str
    #: 执行器名字。
    executor: str
    #: 第几次尝试（从 1 起）。
    attempt: int
    #: 结局。
    outcome: RunOutcome
    #: 复现用例是否通过 —— ``None`` 表示"没验成"。
    target_passed: bool | None = None
    #: 回归套件跑出的失败数 —— ``None`` 表示"没验成"。
    regressions: int | None = None
    #: 回归套件是否真的跑了。
    suite_ran: bool = False
    #: 耗时（秒）。
    duration_seconds: Decimal | None = None
    #: 成本 —— **执行器自报的**。
    cost: Decimal | None = None
    #: 币种。
    currency: str | None = None
    #: token 用量 —— 成本折算的原料。
    tokens: TokenUsage | None = None
    #: 执行器改动了验收文件（改不动结论，但要记下来）。
    touched_reserved: bool = False
    #: 执行器**动了源仓库** —— 评测把被测对象改了。
    #:
    #: **这不是小事。** 有个执行器不认工作目录，把改动落在了真正的试点仓库
    #: 上 —— 于是下一次运行的工作区副本是从"已经被改过"的仓库拷的，那次失败
    #: 是假的，后面几次的成功也是假的。**测出来的数会自己骗自己。**
    source_touched: bool = False
    #: 补充说明。
    detail: str = ""
    #: 执行的原始输出 —— 存盘要脱敏。
    output: str = ""
    #: 因为**环境级**故障重试了几次。
    #:
    #: **重试只针对环境问题**（后端限流、暂时不可用），不针对"模型没做对" ——
    #: 后者重试多少遍都一样，那是能力问题。重试次数要摆在明面上：某个执行器
    #: 次次都要重试才跑得通，那本身就是结论。
    retries: int = 0

    @property
    def executed(self) -> bool:
        """这次运行是不是真的跑了。"""
        return self.outcome is not RunOutcome.NOT_RUN

    def to_dict(self) -> dict[str, object]:
        """转成可 JSON 化的字典 —— 存盘、画图都用它。"""
        return {
            "task_id": self.task_id,
            "executor": self.executor,
            "attempt": self.attempt,
            "outcome": self.outcome.value,
            "target_passed": self.target_passed,
            "regressions": self.regressions,
            "suite_ran": self.suite_ran,
            "duration_seconds": str(self.duration_seconds) if self.duration_seconds else None,
            "cost": str(self.cost) if self.cost is not None else None,
            "currency": self.currency,
            "tokens": self.tokens.to_dict() if self.tokens is not None else None,
            "touched_reserved": self.touched_reserved,
            "source_touched": self.source_touched,
            "detail": self.detail,
            "output": self.output,
            "retries": self.retries,
        }


class ExecutorRunner(Protocol):
    """评测口径与执行器之间**唯一**的接缝。

    **为什么要有这条缝。** 评测要能被离线测 —— 用一个假执行器就能把"口径本身
    对不对"这件事钉住，不必真花钱。而真跑的时候，换的是这条缝后面的实现，不是
    口径。
    """

    @property
    def executor(self) -> str:
        """执行器名字。"""

    async def run(self, task: BenchmarkTask, workspace: Path) -> ExecutionObservation:
        """在 ``workspace`` 里干这个活，返回执行器侧的观测。"""


@dataclass(frozen=True, slots=True)
class CliSpec:
    """一个 CLI 执行器的调用口径。

    **命令行怎么拼、成本从输出的哪个字段读，是"这个执行器是什么"的一部分** ——
    所以它们跟评测口径放在一起，而不是散在调用方的脚本里。
    """

    #: 执行器名字。
    executor: str
    #: 可执行文件名。
    binary: str
    #: 拼命令行：``(工作区, 提示词) -> argv``。
    argv: Callable[[str, str], tuple[str, ...]]
    #: 从 stdout 里读成本。读不到就返回 ``None`` —— **"没读到成本"与"成本是
    #: 0"是两件事**。
    cost_of: Callable[[str], Decimal | None]
    #: 从 stdout 里读 token 用量。读不到就返回 ``None``。
    tokens_of: Callable[[str], TokenUsage | None] | None = None
    #: 计价币种。
    currency: str = "USD"
    #: 额外的环境变量 —— **指向"这个执行器怎么找到那个模型"**。
    #:
    #: 例：Codex 要一个 ``CODEX_HOME`` 指向自带的 provider 配置，OpenCode 要一
    #: 个 ``OPENCODE_CONFIG`` 指向自带的 provider 配置。**这些是环境事实，不是
    #: 口径** —— 写在调用口径里，是因为"这个执行器怎么跑起来"本来就是"这个执行
    #: 器是什么"的一部分。
    env: Mapping[str, str] | None = None
    #: 跑之前先往工作区里放东西 —— **只放环境配置，不放任何与任务有关的东西**。
    #:
    #: 放进去的东西要是能帮执行器通过验收，那评测就废了。所以这个口子只用来放
    #: provider 配置这类"不碰任务"的东西。
    prepare: Callable[[Path], None] | None = None
    #: 判断"这次输出是不是说明它**根本没干活**"。

    #: **只认环境级失败** —— 认证不通、后端连不上、配额用尽、参数不认。
    #: **不认"模型没做对"** —— 那属于 FAILED，是能力问题。环境问题算成能力
    #: 问题，是横向评测最常见的一处造假。
    is_error: Callable[[str], bool] | None = None


class CliRunner:
    """直接起 CLI 进程的跑者。

    **为什么需要它。** 仓库里的执行器适配器是一个一个长出来的；评测要能对比的
    是**产品本身**（Claude Code、Codex、Gemini CLI、OpenCode），而不是适配器长
    出来没有。所以这条路径绕开适配器，直接按各自的命令行规范调 CLI —— **每个
    执行器的适配器还没就位时，评测照样能做**。

    **耗时由我们测，不由它自报。** 四个 CLI 各报各的（有的报 ``duration_ms``，
    有的报 token 数，有的什么都不报），自报的口径根本没法比。所以耗时一律取
    **我们自己量的墙钟时间** —— 口径统一，与工具无关。**成本只能由它自报**
    （只有它知道花了多少钱），读不到就是读不到；**自报的成本只记录，不比较**
    （各家计价表不一样）—— 真正的成本在评测层按统一价目表折算。

    **stdin 必须接 DEVNULL。** 有一半的 CLI 会"顺手读一下 stdin"：Claude Code
    会等 3 秒，Codex 会先打一行"Reading additional input from stdin..."。不接
    DEVNULL，一次运行就白等 3 秒 —— 27 次就是一分半的纯等待。
    """

    def __init__(self, spec: CliSpec) -> None:
        self._spec = spec

    @property
    def executor(self) -> str:
        return self._spec.executor

    async def run(self, task: BenchmarkTask, workspace: Path) -> ExecutionObservation:
        """跑一次 CLI。"""
        argv = self._spec.argv(str(workspace), task.instruction)
        if self._spec.prepare is not None:
            self._spec.prepare(workspace)
        began = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workspace),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(),
            )
        except (FileNotFoundError, PermissionError, NotADirectoryError) as error:
            return ExecutionObservation(
                executed=False,
                detail=f"起不来 {self._spec.binary}：{error}",
            )
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        drain = asyncio.ensure_future(_drain(process, stdout_chunks, stderr_chunks))
        _, pending = await asyncio.wait({drain}, timeout=task.timeout_seconds)
        timed_out = bool(pending)
        if timed_out:
            process.kill()
            await process.wait()
        await drain
        duration = _seconds(time.monotonic() - began)
        text = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        error_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        if timed_out:
            detail = f"超过 {task.timeout_seconds} 秒仍未结束，已终止"
            if not text.strip() and not error_text.strip():
                # **一个字都没说就卡住了 —— 它压根没起来**，那是环境问题，不是
                # 它能力差。
                return ExecutionObservation(
                    executed=False,
                    duration_seconds=duration,
                    detail=f"{detail}（一个字都没说 —— 它压根没起来）",
                    timed_out=True,
                    output=error_text,
                )
            return ExecutionObservation(
                executed=True,
                duration_seconds=duration,
                detail=detail,
                timed_out=True,
                output=text,
            )
        if self._spec.is_error is not None and self._spec.is_error(text):
            return ExecutionObservation(
                executed=False,
                duration_seconds=duration,
                detail=f"执行器报错：{_first_error(text, error_text)}",
                output=text or error_text,
            )
        cost = self._spec.cost_of(text)
        tokens = self._spec.tokens_of(text) if self._spec.tokens_of is not None else None
        return ExecutionObservation(
            executed=True,
            duration_seconds=duration,
            cost=cost,
            currency=self._spec.currency if cost is not None else None,
            tokens=tokens,
            detail=_cli_detail(process.returncode, error_text),
            output=text,
        )

    def _environment(self) -> Mapping[str, str] | None:
        """子进程的环境变量。

        **在父进程环境上叠加，而不是替换。** 凭证（``ANTHROPIC_AUTH_TOKEN`` 之
        类的）得留着 —— 父进程有的，子进程就得有。
        """
        if not self._spec.env:
            return None
        return {**os.environ, **self._spec.env}


async def _drain(
    process: asyncio.subprocess.Process,
    stdout_chunks: list[bytes],
    stderr_chunks: list[bytes],
) -> None:
    """把两个管道读到 EOF。

    **不能直接用 ``communicate()``。** 超时之后 communicate 的结果就丢了 ——
    而"它临死前说了什么"恰恰是判断"它跑过没有"的关键证据。
    """
    await asyncio.gather(
        _read_stream(process.stdout, stdout_chunks),
        _read_stream(process.stderr, stderr_chunks),
    )


async def _read_stream(stream: asyncio.StreamReader | None, sink: list[bytes]) -> None:
    """把一个流读到 EOF，块攒进 ``sink``。"""
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        sink.append(chunk)


def _seconds(value: float) -> Decimal:
    """秒数落成 :class:`~decimal.Decimal` —— 报表上的数要能被人核对。"""
    return Decimal(str(round(value, 3)))


def _first_error(stdout: str, stderr: str) -> str:
    """从输出里摘一句人话 —— 报错的原因要看得见。"""
    for text in (stdout, stderr):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                error = payload.get("error")
                if isinstance(error, dict):
                    data = error.get("data")
                    message = data.get("message") if isinstance(data, dict) else None
                    return str(message or error.get("name") or error)[:200]
                if isinstance(error, str):
                    return error[:200]
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    return tail[:200] or "（没有说清楚）"


def _cli_detail(returncode: int | None, stderr: str) -> str:
    """把退出码与 stderr 压成一句人话。"""
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    if returncode == 0:
        return tail
    return f"退出码 {returncode}" + (f"：{tail}" if tail else "")


class AdapterRunner:
    """按统一执行器契约跑的跑者。

    **这条路走的是仓库自己的适配器**（``executors/base.py`` 的七个方法）。它证
    明的是"ASES 换个执行器不用改代码"；:class:`CliRunner` 证的是"这四个 CLI
    本身差多少"。**两件事都要有** —— 前者是设计承诺，后者是实测数据。
    """

    def __init__(self, adapter: CodingAgentAdapter, *, model: str | None = None) -> None:
        self._adapter = adapter
        self._model = model

    @property
    def executor(self) -> str:
        return self._adapter.name

    async def run(self, task: BenchmarkTask, workspace: Path) -> ExecutionObservation:
        """建会话、提交、等结果。"""
        config = SessionConfig(workspace=str(workspace), model=self._model)
        session = await self._adapter.create_session(config)
        execution = ExecutionTask(
            session_id=session.session_id,
            task_id=task.task_id,
            title=task.title,
            instruction=task.instruction,
            timeout_seconds=task.timeout_seconds,
        )
        await self._adapter.execute(execution)
        result = await self._adapter.collect_result(session.session_id)
        await self._adapter.terminate(session.session_id)
        return ExecutionObservation(
            executed=True,
            duration_seconds=result.execution.duration,
            cost=result.execution.cost,
            currency=result.execution.currency,
            detail=f"状态 {result.status.value}",
        )


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    """铺好的工作区 —— 路径与验收基线。"""

    #: 执行器干活的地方。
    path: Path
    #: 验收副本的路径。
    verify_path: Path
    #: 原仓库里 ``tests/`` 下每个文件的摘要。
    suite_digest: Mapping[str, str]
    #: 原仓库里复现用例文件的摘要。
    known_issues_digest: str


class BenchmarkHarness:
    """评测的执行装置 —— 铺工作区、跑执行器、验验收、出记录。

    **次序是死的。** 铺工作区 → 执行器动手 → 铺验收副本 → 验复现用例 → 验回
    归套件 → 定案。**验收永远在执行器干完活之后**，而且**永远在干净副本上**。
    """

    def __init__(
        self,
        runner: ExecutorRunner,
        *,
        repo: Path,
        root: Path,
        timeout_seconds: int = 600,
    ) -> None:
        self._runner = runner
        self._repo = repo
        self._root = root
        self._timeout = timeout_seconds

    @property
    def executor(self) -> str:
        return self._runner.executor

    async def run(self, task: BenchmarkTask, attempt: int = 1) -> RunRecord:
        """跑一次，出一条记录。

        **装置的超时盖过任务的超时。** 任务集里的超时是"这类任务该给多久"，
        而装置的超时是"这轮评测该给多久" —— 后者是上限，前者只是默认值。
        """
        scoped = replace(task, timeout_seconds=self._timeout)
        before = _snapshot_dir(self._repo)
        prepared = self._prepare(scoped, attempt)
        observation = await self._observe(scoped, prepared.path)
        record = self._judge(scoped, attempt, prepared, observation)
        return replace(record, source_touched=_snapshot_dir(self._repo) != before)

    def _prepare(self, task: BenchmarkTask, attempt: int) -> PreparedWorkspace:
        """把工作区铺好。

        **铺两份。** 一份给执行器动手（``workspace``），一份留作验收（``verify``）
        —— 验收那一份从头到尾只由我们铺，执行器碰不到。
        """
        workspace = self._root / f"{task.task_id}-r{attempt}"
        if workspace.exists():
            shutil.rmtree(workspace)
        shutil.copytree(self._repo, workspace, ignore=_IGNORED)
        test_name = task.target_test.rsplit("::", 1)[-1]
        stripped = _strip_xfail(
            (workspace / KNOWN_ISSUES_FILE).read_text(encoding="utf-8"),
            test_name,
        )
        (workspace / KNOWN_ISSUES_FILE).write_text(stripped, encoding="utf-8")
        verify = self._root / f"{task.task_id}-r{attempt}-verify"
        if verify.exists():
            shutil.rmtree(verify)
        shutil.copytree(self._repo, verify, ignore=_IGNORED)
        (verify / KNOWN_ISSUES_FILE).write_text(stripped, encoding="utf-8")
        return PreparedWorkspace(
            path=workspace,
            verify_path=verify,
            suite_digest=_digest_dir(self._repo / SUITE_DIR),
            known_issues_digest=_digest_file(workspace / KNOWN_ISSUES_FILE),
        )

    async def _observe(self, task: BenchmarkTask, workspace: Path) -> ExecutionObservation:
        """让执行器动手 —— 它起不来就是"没跑成"。"""
        try:
            return await self._runner.run(task, workspace)
        except Exception as error:
            return ExecutionObservation(executed=False, detail=f"执行器抛错：{error}")

    def _judge(
        self,
        task: BenchmarkTask,
        attempt: int,
        prepared: PreparedWorkspace,
        observation: ExecutionObservation,
    ) -> RunRecord:
        """验验收、定案。"""
        touched = self._touched_reserved(prepared)
        self._overlay(prepared.path, prepared.verify_path)
        target_passed = self._target_result(prepared.verify_path, task)
        regressions, suite_ran = self._suite_result(
            prepared.verify_path, f"{task.task_id}-r{attempt}"
        )
        outcome = _decide(observation, target_passed, regressions)
        return RunRecord(
            task_id=task.task_id,
            executor=self.executor,
            attempt=attempt,
            outcome=outcome,
            target_passed=target_passed,
            regressions=regressions,
            suite_ran=suite_ran,
            duration_seconds=observation.duration_seconds,
            cost=observation.cost,
            currency=observation.currency,
            tokens=observation.tokens,
            touched_reserved=touched,
            detail=_detail_of(observation, target_passed, regressions, suite_ran),
            output=observation.output,
        )

    def _overlay(self, workspace: Path, verify: Path) -> None:
        """把执行器改过的东西盖到验收副本上。

        **验收用的文件一概不盖。** ``tests/``、``known_issues/``、``conftest.py``
        一律取原仓库的那份 —— **执行器改不动验收结论**。
        """
        for entry in workspace.iterdir():
            if entry.name in _RESERVED_DIRS or entry.name in _RESERVED_FILES:
                continue
            if entry.name in {"__pycache__", ".pytest_cache", ".coverage", ".git"}:
                continue
            target = verify / entry.name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if entry.is_dir():
                shutil.copytree(entry, target, ignore=_IGNORED)
            else:
                shutil.copy2(entry, target)

    def _touched_reserved(self, prepared: PreparedWorkspace) -> bool:
        """执行器有没有动验收文件 —— 记下来，但不改结论。

        **新增文件不算动。** "把修好的用例搬进 ``tests/``"是好习惯，不该被当成
        越界。只有**改动或删除了既有用例**才算动。
        """
        current_suite = _digest_dir(prepared.path / SUITE_DIR)
        changed = _changed_files(current_suite, prepared.suite_digest)
        current_known = _digest_file(prepared.path / KNOWN_ISSUES_FILE)
        return changed or current_known != prepared.known_issues_digest

    def _target_result(self, verify: Path, task: BenchmarkTask) -> bool | None:
        """复现用例过没过。``None`` 表示"没验成"。"""
        node = f"{KNOWN_ISSUES_FILE}::{task.target_test.rsplit('::', 1)[-1]}"
        code = _run_pytest(verify, [node])
        if code == _EXIT_OK:
            return True
        if code in {_EXIT_FAILED, _EXIT_NO_TESTS}:
            return False
        return None

    def _suite_result(self, verify: Path, label: str) -> tuple[int | None, bool]:
        """回归套件跑出几个失败。返回 ``(失败数, 跑没跑)``。"""
        report = self._root / f"junit-suite-{label}.xml"
        code = _run_pytest(verify, [SUITE_DIR], junit=report)
        if code == _EXIT_OK:
            return 0, True
        if code == _EXIT_FAILED:
            failures = _count_failures(report)
            return (failures, True) if failures is not None else (None, False)
        return None, False


def _decide(
    observation: ExecutionObservation,
    target_passed: bool | None,
    regressions: int | None,
) -> RunOutcome:
    """定案 —— 三态。"""
    if not observation.executed:
        return RunOutcome.NOT_RUN
    if target_passed is None or regressions is None:
        return RunOutcome.NOT_RUN
    if target_passed and regressions == 0:
        return RunOutcome.SUCCEEDED
    return RunOutcome.FAILED


def _detail_of(
    observation: ExecutionObservation,
    target_passed: bool | None,
    regressions: int | None,
    suite_ran: bool,
) -> str:
    """把这次运行的结论压成一句人话。"""
    parts: list[str] = []
    if observation.detail:
        parts.append(observation.detail)
    if target_passed is None:
        parts.append("复现用例没验成")
    elif not target_passed:
        parts.append("复现用例没过")
    if suite_ran and regressions:
        parts.append(f"回归套件有 {regressions} 个失败")
    return "；".join(parts)


def _run_pytest(
    workspace: Path,
    targets: Sequence[str],
    junit: Path | None = None,
) -> int:
    """在 ``workspace`` 里跑 pytest，返回退出码。

    **用 ``-p no:cacheprovider``** —— 不在别人的工作区里留下缓存目录，那会让
    "工作区是干净的"这条断言变成在测 gitignore。
    """
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header"]
    if junit is not None:
        argv.extend(("--junitxml", str(junit)))
    argv.extend(targets)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return 3
    except OSError:
        return 4
    return completed.returncode


def _count_failures(report: Path) -> int | None:
    """从 junit xml 里数出失败的用例数。

    **解析不出来就返回 ``None``，不是 0。** 解析失败却报 0，"回归率"会凭空变
    成 0% —— 那是个**假的安全信号**：报表上写着"没有回归"，其实根本没数出来。
    """
    if not report.exists():
        return None
    try:
        tree = ElementTree.parse(report)
    except ElementTree.ParseError:
        return None
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = 0
    for suite in suites:
        for key in ("failures", "errors"):
            try:
                total += int(suite.get(key, "0"))
            except ValueError:
                continue
    return total


def _changed_files(current: Mapping[str, str], baseline: Mapping[str, str]) -> bool:
    """既有的文件有没有被改动或删掉 —— 新增的不算。"""
    return any(current.get(path) != digest for path, digest in baseline.items())


def _digest_file(path: Path) -> str:
    """文件内容的摘要。"""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest_dir(path: Path) -> dict[str, str]:
    """目录下每个 ``.py`` 文件的摘要 —— 键是相对路径。"""
    if not path.is_dir():
        return {}
    return {
        str(item.relative_to(path)): _digest_file(item)
        for item in sorted(path.rglob("*.py"))
        if "__pycache__" not in item.parts
    }


def _snapshot_dir(path: Path) -> dict[str, str]:
    """整个目录的快照 —— **每个文件**都算，不只是 ``.py``。

    **为什么不用 :func:`_digest_dir`。** 那个只盯着 ``.py``，是给"回归套件变没
    变"用的；而"源仓库有没有被动过"要盯的是**任何**文件 —— 多出来的用例文件、
    改掉的配置、删掉的说明，全都要看见。

    **新增文件也算动过。** 执行器往源仓库里塞一个测试文件，比改一行源码更值得
    警惕：那是在**改验收的战场**。
    """
    if not path.is_dir():
        return {}
    snapshot: dict[str, str] = {}
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        if set(item.parts) & {"__pycache__", ".pytest_cache", ".git"}:
            continue
        if item.name in {".coverage", ".DS_Store"}:
            continue
        snapshot[str(item.relative_to(path))] = _digest_file(item)
    return snapshot


def _strip_xfail(source: str, test_name: str) -> str:
    """把某条用例上的 ``xfail`` 标记去掉。

    **去掉标记不是为了"让用例通过"** —— 而是因为 ``strict=True`` 的 xfail 用例
    在缺陷修好之后会变成 XPASS，而 strict 模式把 XPASS 算作**失败**：修对了反而
    报红，那不是我们要的验收语义。去掉标记之后，这条用例就成了一条普通的回归
    用例 —— 修好了它就绿，没修好它就红。

    **找不到标记就报错。** 静悄悄地什么都没改，就等于验收的是"标记还在"的
    那份 —— 而那永远失败，评测会报出一堆莫名其妙的"没修好"。
    """
    lines = source.splitlines(keepends=True)
    kept: list[str] = []
    removed = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if "@pytest.mark.xfail(" in line:
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead].lstrip().startswith("def "):
                lookahead += 1
            if lookahead < len(lines) and lines[lookahead].lstrip().startswith(f"def {test_name}("):
                removed = True
                index += 1
                continue
        kept.append(line)
        index += 1
    if not removed:
        raise ValueError(f"用例 {test_name} 上没有找到 xfail 标记 —— 任务集与仓库对不上")
    return "".join(kept)


def pilot_benchmark_tasks(repo: Path, *, timeout_seconds: int = 600) -> tuple[BenchmarkTask, ...]:
    """从试点仓库生成标准任务集。

    **任务集从仓库里读出来，而不是写死一份清单。** 写死的清单会跟仓库脱节 ——
    缺陷修好了、用例搬进 ``tests/`` 了，清单还在，那就成了在测一条已经不存的
    缺陷。
    """
    source = (repo / KNOWN_ISSUES_FILE).read_text(encoding="utf-8")
    tasks: list[BenchmarkTask] = []
    for match in _XFAIL_PATTERN.finditer(source):
        issue = match.group("issue")
        name = match.group("name")
        summary = match.group("reason").strip()
        tasks.append(
            BenchmarkTask(
                task_id=issue.lower(),
                issue_id=issue,
                summary=summary,
                target_test=f"{KNOWN_ISSUES_FILE}::{name}",
                title=f"修复 {issue}：{summary}",
                instruction=_instruction(issue, summary, name),
                repo=repo,
                timeout_seconds=timeout_seconds,
            )
        )
    if not tasks:
        raise ValueError(f"{repo} 里没找到任何已知缺陷用例 —— 任务集空了")
    return tuple(tasks)


def _instruction(issue: str, summary: str, test_name: str) -> str:
    """给执行器的指令 —— **所有执行器拿到的是同一段**。

    **为什么指令要写死。** 指令是自变量：换了执行器就换指令，比较就废了。而
    指令里必须写清楚"不许改验收文件" —— 不是为了防作弊（改了不起作用，见模块
    开头），而是省得执行器把时间花在搬用例上。
    """
    return f"""你正在 tinylib 这个 Python 项目上修一个已知缺陷。

缺陷编号：{issue}
现象：{summary}

复现命令（跑它应该失败）：
    python3 -m pytest {KNOWN_ISSUES_FILE}::{test_name} -q

要求：
1. 修好这个缺陷，让上面那条复现用例通过。
2. 只改 src/tinylib/ 下的代码。
3. 不要改 known_issues/ 与 tests/ 下的任何文件 —— 那是验收用的。
4. 可以新增测试文件，但不许改删既有的用例。
5. 不引入任何第三方依赖，不改公开 API 的签名与语义。
6. 不要 git commit —— 把改动留在工作区里。

做完后简短说明你改了什么、为什么。
"""


@dataclass(frozen=True, slots=True)
class ExecutorReport:
    """一个执行器在统一口径下的成绩单。"""

    #: 执行器名字。
    executor: str
    #: 全部运行记录。
    runs: tuple[RunRecord, ...]
    #: 可用率。
    availability: MetricResult
    #: 成功率。
    success_rate: MetricResult
    #: 回归率。
    regression_rate: MetricResult
    #: 成本中位数 —— **统一价目表折算出来的**。
    cost: MetricResult
    #: 工具自报成本中位数 —— **只记录，不比较**。
    reported_cost: MetricResult
    #: token 用量中位数。
    tokens: MetricResult
    #: 耗时中位数。
    duration: MetricResult
    #: 折算出来的逐次成本 —— 检验要的是原始样本，不是中位数。
    cost_samples: tuple[Decimal, ...] = ()

    @property
    def not_run(self) -> int:
        """没跑成的次数。"""
        return sum(1 for run in self.runs if run.outcome is RunOutcome.NOT_RUN)

    @property
    def retries(self) -> int:
        """因为环境级故障重试了几次。

        **这一列要摆出来。** 一个执行器次次都要重试才跑得通，那本身就是结论 ——
        藏起来，看板上就只剩一个干干净净的"成功率 90%"。
        """
        return sum(run.retries for run in self.runs)

    @property
    def source_touched(self) -> int:
        """动了源仓库的运行次数。

        **这一列更要摆出来。** 动了源仓库的运行，其数据**不可信** —— 工作区是
        从源仓库拷的，源仓库被改了，后面的运行就都建在污染过的地基上。
        """
        return sum(1 for run in self.runs if run.source_touched)

    @property
    def currency(self) -> str | None:
        """成本的币种 —— 折算成本用的是价目表的币种。"""
        detail = self.cost.detail
        value = detail.get("currency")
        return str(value) if value is not None else None

    @property
    def reported_currency(self) -> str | None:
        """自报成本的币种 —— 混着多个币种时是 ``None``。"""
        currencies = {run.currency for run in self.runs if run.cost is not None}
        return next(iter(currencies)) if len(currencies) == 1 else None


def evaluate_executor(
    runs: Sequence[RunRecord],
    *,
    prices: PriceTable | None = None,
) -> ExecutorReport:
    """把一个执行器的运行记录算成成绩单。

    **成本按统一价目表折算。** 给了 ``prices`` 就用 token 用量折出成本；没给就
    没有成本这一列 —— **宁可空着，也不要拿各家自报的数来比**（各家计价表不一
    样，比的是记账口径）。
    """
    total = len(runs)
    executed = [run for run in runs if run.executed]
    succeeded = [run for run in executed if run.outcome is RunOutcome.SUCCEEDED]
    measured = [run for run in runs if run.suite_ran]
    regressed = [run for run in measured if (run.regressions or 0) > 0]
    availability = rate(
        METRIC_AVAILABILITY,
        len(executed),
        total,
        detail={"total": total, "not_run": total - len(executed)},
    )
    success = rate(
        METRIC_SUCCESS_RATE,
        len(succeeded),
        len(executed),
        detail={
            "total": total,
            "executed": len(executed),
            "not_run": total - len(executed),
            "failed": len(executed) - len(succeeded),
        },
    )
    regression = rate(
        METRIC_REGRESSION_RATE,
        len(regressed),
        len(measured),
        detail={"measured": len(measured), "regressed": len(regressed)},
    )
    cost = _cost_metric(runs, prices)
    cost_samples = (
        tuple(prices.cost_of(run.tokens) for run in runs if run.tokens is not None)
        if prices is not None
        else ()
    )
    reported = _distribution_metric(
        METRIC_REPORTED_COST,
        [run.cost for run in runs],
        currencies={run.currency for run in runs if run.cost is not None},
    )
    tokens = _distribution_metric(
        METRIC_TOKENS,
        [Decimal(run.tokens.total) if run.tokens is not None else None for run in runs],
    )
    duration = _distribution_metric(METRIC_DURATION, [run.duration_seconds for run in runs])
    return ExecutorReport(
        executor=runs[0].executor if runs else "",
        runs=tuple(runs),
        availability=availability,
        success_rate=success,
        regression_rate=regression,
        cost=cost,
        reported_cost=reported,
        tokens=tokens,
        duration=duration,
        cost_samples=cost_samples,
    )


def _cost_metric(runs: Sequence[RunRecord], prices: PriceTable | None) -> MetricResult:
    """按统一价目表折算成本。"""
    if prices is None:
        return MetricResult(
            metric_id=METRIC_COST,
            state=MetricState.NO_SAMPLES,
            value=None,
            detail={"samples": 0, "reason": "没有给价目表 —— 自报成本不可比，不折算"},
        )
    costs = [prices.cost_of(run.tokens) for run in runs if run.tokens is not None]
    metric = _distribution_metric(METRIC_COST, costs)
    return MetricResult(
        metric_id=metric.metric_id,
        state=metric.state,
        value=metric.value,
        numerator=metric.numerator,
        denominator=metric.denominator,
        detail={**metric.detail, "currency": prices.currency, "price_source": prices.source},
    )


def _distribution_metric(
    metric_id: str,
    values: Sequence[Decimal | None],
    *,
    currencies: set[str | None] | None = None,
) -> MetricResult:
    """把一组数算成一个"中位数 + 分布"的指标。

    **币种混了就报"算不出来"，不换算。** R-039 要求成本同源同币种；这里悄悄
    按某个汇率折一下，报表上的数就没人能核对了。
    """
    present = [value for value in values if value is not None]
    if not present:
        return MetricResult(
            metric_id=metric_id,
            state=MetricState.NO_SAMPLES,
            value=None,
            detail={"samples": 0},
        )
    known = {item for item in (currencies or set()) if item is not None}
    if len(known) > 1:
        return MetricResult(
            metric_id=metric_id,
            state=MetricState.BASELINE_MISSING,
            value=None,
            denominator=Decimal(len(present)),
            detail={
                "samples": len(present),
                "currencies": sorted(known),
                "reason": "币种不一致，按 R-039 不做换算，中位数不可比",
            },
        )
    ordered = sorted(present)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        median = ordered[middle]
    else:
        median = (ordered[middle - 1] + ordered[middle]) / 2
    return MetricResult(
        metric_id=metric_id,
        state=MetricState.COMPUTED,
        value=median,
        denominator=Decimal(len(present)),
        detail={
            "samples": len(present),
            "min": str(ordered[0]),
            "max": str(ordered[-1]),
            "total": str(sum(ordered, Decimal(0))),
        },
    )


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """两个执行器在某一指标上的比较。"""

    #: 指标 id。
    metric: str
    #: 前一个执行器。
    first: str
    #: 后一个执行器。
    second: str
    #: 检验结论。
    test: TestResult
    #: 多重比较校正后的 p 值。
    adjusted_p_value: Decimal

    @property
    def significant(self) -> bool:
        """校正之后还显著吗。"""
        return self.adjusted_p_value <= self.test.alpha


@dataclass(frozen=True, slots=True)
class BenchmarkBoard:
    """对比看板 —— 成绩单 + 显著性结论。"""

    #: 各执行器的成绩单。
    reports: tuple[ExecutorReport, ...]
    #: 两两比较。
    comparisons: tuple[MetricComparison, ...]
    #: 显著性水平。
    alpha: Decimal = ALPHA

    @property
    def distinguishable(self) -> bool:
        """有没有至少一项指标在统计上可辨。"""
        return any(comparison.significant for comparison in self.comparisons)

    @property
    def significant_metrics(self) -> tuple[str, ...]:
        """哪些指标上存在显著差异。"""
        names = {comparison.metric for comparison in self.comparisons if comparison.significant}
        return tuple(sorted(names))

    def report_of(self, executor: str) -> ExecutorReport | None:
        """按名字取成绩单。"""
        for report in self.reports:
            if report.executor == executor:
                return report
        return None

    def render(self) -> str:
        """渲染成 Markdown 看板。"""
        header = (
            "| 执行器 | 可用率 | 成功率 | 回归率 | 成本中位数 | token 中位数 "
            "| 耗时中位数 | 工具自报成本 | 没跑成 | 重试 | 动了源仓库 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        )
        lines = list(header)
        for report in self.reports:
            cells = [
                report.executor,
                _format_rate(report.availability),
                _format_rate(report.success_rate),
                _format_rate(report.regression_rate),
                _format_distribution(report.cost, report.currency),
                _format_tokens(report.tokens),
                _format_distribution(report.duration, None),
                _format_distribution(report.reported_cost, report.reported_currency),
                str(report.not_run),
                str(report.retries),
                str(report.source_touched),
            ]
            lines.append("| " + " | ".join(cells) + " |")
        if not self.comparisons:
            lines.append("")
            lines.append("（样本不足，没有可做的两两比较）")
            return "\n".join(lines)
        lines.extend(
            [
                "",
                "| 指标 | A | B | 统计量 | p | p（Holm 校正） | 显著 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for comparison in self.comparisons:
            cells = [
                comparison.metric,
                comparison.first,
                comparison.second,
                f"{comparison.test.statistic:.4f}",
                f"{comparison.test.p_value:.4f}",
                f"{comparison.adjusted_p_value:.4f}",
                "是" if comparison.significant else "否",
            ]
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)


def _format_rate(metric: MetricResult) -> str:
    """把一个比率指标排版成 "3/4 (75.0%)"，没样本时写"无样本"。"""
    if not metric.is_computed or metric.value is None:
        return "无样本"
    numerator = int(metric.numerator or 0)
    denominator = int(metric.denominator or 0)
    return f"{numerator}/{denominator} ({metric.value * 100:.1f}%)"


def _format_distribution(metric: MetricResult, currency: str | None) -> str:
    """把一个分布指标排版成 "0.42 USD"，算不出来时写清为什么。"""
    if metric.state is MetricState.BASELINE_MISSING:
        return "不可比"
    if not metric.is_computed or metric.value is None:
        return "无样本"
    unit = f" {currency}" if currency else "s"
    return f"{metric.value:.4f}{unit}"


def _format_tokens(metric: MetricResult) -> str:
    """把 token 用量排版成 "36825" —— 这一列是整数的量，不带小数点。"""
    if not metric.is_computed or metric.value is None:
        return "无样本"
    return f"{int(metric.value)}"


def build_board(
    reports: Sequence[ExecutorReport],
    *,
    alpha: Decimal = ALPHA,
    options: StatisticsOptions | None = None,
) -> BenchmarkBoard:
    """把成绩单拼成看板 —— 两两比较 + 多重比较校正。

    **校正按指标分组。** 同一个指标上的 6 组两两比较是一族（教科书里"多重比
    较"说的就是它），族内用 Holm 步降法；不同指标各自成族。这样既不至于把
    噪声当发现，又不至于因为指标多而把真差异也淹掉。

    **成本不可比就不比。** 两个执行器报的币种不同，按 R-039 不换算 —— 那就
    **不出这一格的检验**，而不是拿一个换算过的数去比。
    """
    settings = options or StatisticsOptions()
    raw: list[tuple[str, str, str, TestResult]] = []
    for first_index, first in enumerate(reports):
        for second in reports[first_index + 1 :]:
            raw.extend(_compare_pair(first, second, settings, alpha))
    adjusted = _adjust_by_metric(raw)
    comparisons = tuple(
        MetricComparison(
            metric=metric,
            first=first,
            second=second,
            test=test,
            adjusted_p_value=adjusted[index],
        )
        for index, (metric, first, second, test) in enumerate(raw)
    )
    return BenchmarkBoard(reports=tuple(reports), comparisons=comparisons, alpha=alpha)


def _adjust_by_metric(
    raw: Sequence[tuple[str, str, str, TestResult]],
) -> list[Decimal]:
    """按指标分组做 Holm 校正 —— 每个指标一族。"""
    adjusted = [Decimal(0)] * len(raw)
    families: dict[str, list[int]] = {}
    for index, (metric, _first, _second, _test) in enumerate(raw):
        families.setdefault(metric, []).append(index)
    for indices in families.values():
        values = holm_adjust([raw[index][3].p_value for index in indices])
        for position, index in enumerate(indices):
            adjusted[index] = values[position]
    return adjusted


def _compare_pair(
    first: ExecutorReport,
    second: ExecutorReport,
    options: StatisticsOptions,
    alpha: Decimal,
) -> list[tuple[str, str, str, TestResult]]:
    """两个执行器的四项比较。"""
    results: list[tuple[str, str, str, TestResult]] = []

    def add(metric: str, test: TestResult | None) -> None:
        if test is not None:
            results.append((metric, first.executor, second.executor, test))

    first_success = first.success_rate
    second_success = second.success_rate
    if first_success.sample_size and second_success.sample_size:
        add(
            METRIC_SUCCESS_RATE,
            compare_success_rates(
                int(first_success.numerator or 0),
                int(first_success.denominator or 0),
                int(second_success.numerator or 0),
                int(second_success.denominator or 0),
                StatisticsOptions(
                    alternative=options.alternative,
                    alpha=alpha,
                    label=METRIC_SUCCESS_RATE,
                    seed=options.seed,
                ),
            ),
        )

    first_regression = first.regression_rate
    second_regression = second.regression_rate
    if first_regression.sample_size and second_regression.sample_size:
        add(
            METRIC_REGRESSION_RATE,
            compare_success_rates(
                int(first_regression.numerator or 0),
                int(first_regression.denominator or 0),
                int(second_regression.numerator or 0),
                int(second_regression.denominator or 0),
                StatisticsOptions(
                    alternative=options.alternative,
                    alpha=alpha,
                    label=METRIC_REGRESSION_RATE,
                    seed=options.seed,
                ),
            ),
        )

    if first.currency and first.currency == second.currency:
        first_costs = list(first.cost_samples)
        second_costs = list(second.cost_samples)
        if first_costs and second_costs:
            add(
                METRIC_COST,
                permutation_test(
                    first_costs,
                    second_costs,
                    StatisticsOptions(
                        alternative=options.alternative,
                        alpha=alpha,
                        label=METRIC_COST,
                        seed=options.seed,
                        max_exact=options.max_exact,
                        draws=options.draws,
                    ),
                ),
            )

    first_tokens = [Decimal(run.tokens.total) for run in first.runs if run.tokens is not None]
    second_tokens = [Decimal(run.tokens.total) for run in second.runs if run.tokens is not None]
    if first_tokens and second_tokens:
        add(
            METRIC_TOKENS,
            permutation_test(
                first_tokens,
                second_tokens,
                StatisticsOptions(
                    alternative=options.alternative,
                    alpha=alpha,
                    label=METRIC_TOKENS,
                    seed=options.seed,
                    max_exact=options.max_exact,
                    draws=options.draws,
                ),
            ),
        )

    first_durations = [
        run.duration_seconds for run in first.runs if run.duration_seconds is not None
    ]
    second_durations = [
        run.duration_seconds for run in second.runs if run.duration_seconds is not None
    ]
    if first_durations and second_durations:
        add(
            METRIC_DURATION,
            permutation_test(
                first_durations,
                second_durations,
                StatisticsOptions(
                    alternative=options.alternative,
                    alpha=alpha,
                    label=METRIC_DURATION,
                    seed=options.seed,
                    max_exact=options.max_exact,
                    draws=options.draws,
                ),
            ),
        )
    return results


def temporary_root(prefix: str = "executor-benchmark-") -> Path:
    """建一个临时的根目录 —— 工作区都铺在它下面。"""
    return Path(tempfile.mkdtemp(prefix=prefix))


def available_executors(binaries: Mapping[str, str]) -> dict[str, bool]:
    """哪些执行器在这台机器上装得起来。

    **"没装"是环境事实，不是执行器的成绩。** 所以它单独报成可用率，而不是被
    折进成功率 —— 一个没装的执行器如果被算成"成功率 0"，那是在说它能力差。
    """
    return {name: shutil.which(binary) is not None for name, binary in binaries.items()}


__all__ = [
    "KNOWN_ISSUES_FILE",
    "METRIC_AVAILABILITY",
    "METRIC_COST",
    "METRIC_DURATION",
    "METRIC_REGRESSION_RATE",
    "METRIC_REPORTED_COST",
    "METRIC_SUCCESS_RATE",
    "METRIC_TOKENS",
    "SUITE_DIR",
    "AdapterRunner",
    "BenchmarkBoard",
    "BenchmarkHarness",
    "BenchmarkTask",
    "CliRunner",
    "CliSpec",
    "ExecutionObservation",
    "ExecutorReport",
    "ExecutorRunner",
    "MetricComparison",
    "PreparedWorkspace",
    "PriceTable",
    "RunOutcome",
    "RunRecord",
    "TokenUsage",
    "available_executors",
    "build_board",
    "evaluate_executor",
    "pilot_benchmark_tasks",
    "temporary_root",
]
