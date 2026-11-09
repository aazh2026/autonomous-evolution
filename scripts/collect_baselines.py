"""采集基线 —— 在试点仓库上跑 N 个代表性任务，把量测写进基线表。

设计依据：docs/tasks.md TASK-P1-072（交付物 ``evaluation/baselines.py`` +
基线表 ``docs/evaluation/baselines.md``；T1 ① 基线表可被程序读取并用于阈值
比较；T1 ② 基线缺失时相关 T2 指标明确报"基线未建立"而非返回 0；T2 基线表
中位数为后续成本预算提供依据）。

**采集与口径分开。** 这一层负责"把任务跑起来、把量测到、把结果落盘"；
口径（取中位数、比阈值、校准预算）在 ``evaluation/baselines.py``。分开的
理由：采集要起进程、跑测试、读覆盖率，而口径必须能被单元测试单独证伪。

**跑的是真的。** 每个任务都真的起一个 Claude Code CLI 子进程（打桩模式
下换成打桩 CLI），真的改文件，真的跑测试，真的量覆盖率。**量到的四样东西
都是执行器报上来的事实**：金额与用量来自结果事件，时长来自执行器，工具
调用数数的是 ``tool_use`` 内容块。

**每个任务跑在一份全新的副本上。** 十个任务互不干扰 —— 否则第二个任务
看到的是第一个任务的产出，量到的是"两件事一起花了多少"，那不是单任务的
基线。

**读不到的量不填 0。** 这次执行没报用量、没报金额、没量到覆盖率，那就是
"没测到" —— 记成 0 会让基线偏低，而基线偏低意味着**预算偏松**：那正是
最不该出错的地方。

**失败的任务照样记。** 一次跑飞的任务也要花钱，而"典型任务要花多少"里
就该包含它。**不记失败等于把最贵的那种情况从预算里抹掉** —— 而那正是
预算最该管住的地方。任务的结局写在 ``source`` 里，看得见。

**覆盖率按模块分开记。** 总覆盖率会掩盖问题：新增一个没测的模块，总覆盖
率掉 0.3%，而那个模块其实是 0。模块级的基线是 TASK-P1-042
（``threshold = max(模块基线, 全局地板)``）的输入。

**用法**::

    python -m scripts.collect_baselines                      # 用真的 CLI 采
    python -m scripts.collect_baselines --binary <打桩CLI>   # 不花钱地采
    python -m scripts.collect_baselines --pilot <另一个仓库>  # 换试点仓库
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from core.metrics.cost import CostAmount, TokenUsage
from evaluation.baselines import BaselineError, BaselineTable, TaskMeasurement
from executors.base import ExecutionTask, SessionConfig
from executors.claude_code import ClaudeCodeAdapter
from executors.cli.adapter import EnvProvider

ROOT = Path(__file__).resolve().parents[1]

#: 试点仓库 —— 默认是仓库里那份小项目。
DEFAULT_PILOT = ROOT / "samples" / "pilot_repo"

#: 基线表落在哪。
DEFAULT_OUTPUT = ROOT / "docs" / "evaluation"

#: 覆盖率报告文件名 —— 落在副本里，不落在真仓库里。
COVERAGE_REPORT_NAME = ".coverage-baseline.json"


@dataclass(frozen=True, slots=True)
class RepresentativeTask:
    """一个代表性任务。

    **每一类任务都是自包含的** —— 它在一个**全新的**试点仓库副本上跑，
    所以它不能依赖别的任务留下的东西。
    """

    #: 任务类别 —— 基线按它归类。
    kind: str
    #: 标题 —— 给人看的。
    title: str
    #: 正文 —— 给 Agent 的。
    instruction: str


#: 十个代表性任务。
#:
#: **覆盖面是刻意挑的**：改代码、补测试、修缺陷、重构、写文档、加接口、
#: 调性能、补类型、写变更记录 —— 一个真实项目里最常见的十种活。
REPRESENTATIVE_TASKS: tuple[RepresentativeTask, ...] = (
    RepresentativeTask(
        kind="bug_fix",
        title="修一个截断长度的缺陷",
        instruction=(
            "tinylib/strings.py 里的 truncate 在真正截断时，返回的字符串比 limit 长一个"
            "字符 —— 省略号没有算进长度里，而它的 docstring 明说省略号算在长度之内。"
            "修掉这个不一致，并补一个能抓住它的测试。"
            "只改这两个文件，不要动别的。改完跑一遍 python -m pytest，确认全绿。"
        ),
    ),
    RepresentativeTask(
        kind="feature_add",
        title="给金额加一个按权重分摊的函数",
        instruction=(
            "给 tinylib/money.py 加一个模块级函数 allocate(total, weights)：把 total "
            "按 weights 的比例分摊成一组 Money，余数（分不掉的零头）给最后一份，"
            "并保证每一份都量化到分。weights 为空或全为 0 时抛 ValueError。"
            "在 tests/test_money.py 里补上对应的测试。改完跑一遍 python -m pytest，"
            "确认全绿。"
        ),
    ),
    RepresentativeTask(
        kind="test_add",
        title="给 slugify 补测试",
        instruction=(
            "tests/test_strings.py 里对 slugify 的覆盖很薄，只测了两个简单例子。"
            "给它补上边界情况的测试：全是标点的输入、含大写字母与空格的输入、"
            "含中文的输入（中文会被丢掉）、空字符串。"
            "只改 tests/test_strings.py，不要动实现。改完跑一遍 python -m pytest，"
            "确认全绿。"
        ),
    ),
    RepresentativeTask(
        kind="refactor",
        title="重写调度模块里嵌套的条件",
        instruction=(
            "tinylib/schedule.py 里 is_due 的实现把多个条件层层嵌套，读起来很绕。"
            "把它重写成若干个各自独立的小判断（可以拆成私有辅助函数），"
            "**行为必须完全不变**。重写之后跑一遍 python -m pytest，"
            "确认原有测试全绿 —— 行为变了就会红。"
        ),
    ),
    RepresentativeTask(
        kind="doc_update",
        title="给 README 补一节用法",
        instruction=(
            "README.md 的“用法”一节只列了几个例子。把 tinylib/stats.py 里 "
            "percentile 与 variance 的用法也补进去，各给一个可运行的例子，"
            "并说清楚 percentile 的 p 是 0 到 100 之间的百分位。"
            "只改 README.md。改完确认里面的例子是对的。"
        ),
    ),
    RepresentativeTask(
        kind="error_handling",
        title="给配置读取一个像样的错误",
        instruction=(
            "tinylib/config.py 的 load_config 在文件不存在时抛的是裸的 "
            "FileNotFoundError —— 读的人看不出是哪个路径出的问题。"
            "定义一个 ConfigError 异常（继承 Exception），让 load_config 在文件"
            "不存在时抛它，并在消息里带上那个路径；文件存在但内容不合法时"
            "也抛 ConfigError。在 tests/test_config.py 里补上对应的测试。"
            "改完跑一遍 python -m pytest，确认全绿。"
        ),
    ),
    RepresentativeTask(
        kind="api_extension",
        title="给折行加一个分隔符参数",
        instruction=(
            "给 tinylib/strings.py 的 word_wrap 加一个可选参数 separator，默认是空格 "
            '" "。传入别的分隔符时用它连接同一行里的词。'
            "在 tests/test_strings.py 里补上对应的测试。改完跑一遍 "
            "python -m pytest，确认全绿。"
        ),
    ),
    RepresentativeTask(
        kind="perf_tweak",
        title="把滑动平均改成滚动计算",
        instruction=(
            "tinylib/stats.py 的 moving_average 每算一个窗口就把整个窗口重新求和，"
            "整体是 O(n*w)。改成滚动计算：第一个窗口求一次和，之后每次减去离开"
            "窗口的那个数、加上新进来的那个数，整体 O(n)。"
            "**结果必须完全一样**（浮点累加的误差也要注意 —— 如果逐项累加与"
            "先求和再除的结果对不上，就保留原来的求和方式）。"
            "在 tests/test_stats.py 里补一个能抓住回归的测试。"
        ),
    ),
    RepresentativeTask(
        kind="type_annotate",
        title="给字符串模块补类型标注",
        instruction=(
            "tinylib/strings.py 没有写类型标注。给这个模块的每个公开函数补上"
            "参数与返回值的类型标注（str / int / list[str] 之类），"
            "并把 from __future__ import annotations 加上。"
            "**不要改任何行为。** 改完跑一遍 python -m pytest，确认全绿。"
        ),
    ),
    RepresentativeTask(
        kind="changelog",
        title="写一条变更记录",
        instruction=(
            "在 CHANGELOG.md 的“未发布”一节里，把“还没有条目”换成一条真实的条目，"
            "描述这个版本带来的三处变化：Money 支持按权重分摊（allocate）、"
            "配置读取会抛出带路径的 ConfigError、word_wrap 支持自定义分隔符。"
            "条目格式参考文件里已有的写法（如果文件里没有别的版本，就用"
            "“### 标题 + 一句话”的最简形式）。只改 CHANGELOG.md。"
        ),
    ),
)


def _usage_of(payload: object) -> TokenUsage | None:
    """从结果事件的 ``usage`` 里读 token 用量 —— **读不到就是 None**。

    **四类 token 分开记。** 缓存读比普通输入便宜、缓存写比普通输入贵，
    并进 ``input_tokens`` 的话，按用量推算出来的钱会偏小。

    **四个字段一个都没认出来就是 None**，不是"四个 0" —— 那两件事必须分得
    开："这次没用 token"与"我们没读到用量"。
    """
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None

    def _count(key: str) -> int | None:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    counts = {key: _count(key) for key in keys}
    if all(value is None for value in counts.values()):
        return None
    return TokenUsage(
        input_tokens=counts["input_tokens"] or 0,
        output_tokens=counts["output_tokens"] or 0,
        cache_read_tokens=counts["cache_read_input_tokens"] or 0,
        cache_write_tokens=counts["cache_creation_input_tokens"] or 0,
    )


def _cost_of(payload: object) -> Decimal | None:
    """从结果事件的 ``total_cost_usd`` 里读金额 —— **读不到就是 ``None``**。

    **没读到不是 0。** 一次超时的执行：CLI 被掐了，没吐出结果事件，金额就
    没读到 —— 而那次执行**明明花了钱**。记成 0 等于把这最贵的一类任务说成
    "免费"，而报表上那一栏看上去跟"真的不花钱"一模一样。
    """
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("total_cost_usd")
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    if not isinstance(raw, str):
        return None
    try:
        return Decimal(raw.strip())
    except InvalidOperation:
        return None


def _env_provider(extra_path: Path) -> EnvProvider:
    """造一个子进程环境 —— **在原来的基础上把解释器放到 PATH 前面**。

    **为什么需要它。** Agent 在副本里要跑 ``python -m pytest``。若 PATH 上
    的 ``python`` 不是装了 pytest 的那个解释器，Agent 就会看到"命令找不到"
    —— 而那不是任务的一部分，是**采集环境没配好**，量出来的时长与工具
    调用数也会被这些无谓的重试污染。

    **不改父进程的环境。** 只改子进程看到的那个。
    """

    def provider(_config: SessionConfig) -> Mapping[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{extra_path}{os.pathsep}{env.get('PATH', '')}"
        return env

    return provider


def _prepare_workspace(pilot: Path, workspace: Path) -> None:
    """把试点仓库复制成一份可跑的副本。

    **副本是一次性的** —— 十个任务各一份，互不干扰。

    **给它一个 git 仓库。** 真实的项目是 git 仓库；Agent 在里面会提交、
    会看 diff、会回滚。没有 git 的副本会让 Agent 撞上一堆"这个命令不存在"
    —— 那不是任务的一部分。
    """
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(pilot, workspace)
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "--all")
    _git(
        workspace,
        "-c",
        "user.email=baseline@ases.local",
        "-c",
        "user.name=ASES Baseline",
        "commit",
        "--quiet",
        "-m",
        "基线起点",
    )


def _git(workspace: Path, *args: str) -> None:
    """在副本里跑一条 git 命令 —— **失败就算了，不报错**。

    没有 git 或者 git 拒绝了这个操作时，采集照样能继续 —— git 只是让副本
    更像真实项目，不是采集的前提。
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


async def _collect_one(
    task: RepresentativeTask,
    *,
    pilot: Path,
    workdir: Path,
    binary: str,
    model: str | None,
    permission_mode: str,
    timeout_seconds: int,
    with_coverage: bool,
) -> TaskMeasurement:
    """跑一个代表性任务，量一次。

    **每个任务一份全新的副本** —— 十个任务互不干扰。
    """
    workspace = workdir / task.kind
    _prepare_workspace(pilot, workspace)

    adapter = ClaudeCodeAdapter(
        binary=binary,
        permission_mode=permission_mode,
        env_provider=_env_provider(Path(sys.executable).parent),
    )
    config = SessionConfig(
        workspace=str(workspace),
        branch=f"agent/baseline-{task.kind}",
    )
    if model:
        config = replace(config, model=model)
    session = await adapter.create_session(config)
    execution = ExecutionTask(
        session_id=session.session_id,
        task_id=f"BASELINE-{task.kind}",
        title=task.title,
        instruction=task.instruction,
        timeout_seconds=timeout_seconds,
    )

    await adapter.execute(execution)
    result = await adapter.collect_result(session.session_id)

    usage, reported_cost, model_name = await _read_usage_cost_and_model(adapter, session.session_id)

    coverage: Decimal | None = None
    coverage_by_module: dict[str, Decimal] = {}
    if with_coverage:
        coverage, coverage_by_module = _measure_coverage(workspace)

    metrics = result.execution
    source = f"{adapter.name}:{model_name or 'unknown'}:{result.status.value}"
    return TaskMeasurement(
        task_kind=task.kind,
        duration_seconds=metrics.duration,
        tool_calls=metrics.tool_calls,
        usage=usage,
        cost=None if reported_cost is None else CostAmount(metrics.cost, metrics.currency),
        coverage=coverage,
        coverage_by_module=coverage_by_module,
        source=source,
    )


async def _read_usage_cost_and_model(
    adapter: ClaudeCodeAdapter,
    session_id: str,
) -> tuple[TokenUsage | None, Decimal | None, str]:
    """从事件流里读用量、金额与模型名。

    **用量与金额只在 ``result`` 事件里，模型名只在 ``session.started`` 事件里。**
    找错了地方就什么都读不到 —— 而"读不到"会被记成"没有"，那是在编。

    **统一结果 schema 里没有用量字段**（design.md §3.4）—— 那是契约的
    边界，不是疏漏：用量是**诊断量**，塞进契约就会逼着每个执行器都报它，
    而报不出来的执行器就只能填 0。所以这里从事件流里读。

    **金额同样"读到才算"。** CLI 报上来的金额才进这张表；没报就是没读到，
    不是"这次没花钱"（详见 :func:`_cost_of`）。
    """
    usage: TokenUsage | None = None
    cost: Decimal | None = None
    model_name = ""
    async for event in await adapter.stream_events(session_id):
        if event.kind == "result":
            found = _usage_of(event.payload)
            if found is not None:
                usage = found
            reported = _cost_of(event.payload)
            if reported is not None:
                cost = reported
        elif event.kind == "session.started":
            raw_model = event.payload.get("model")
            if isinstance(raw_model, str):
                model_name = raw_model
    return usage, cost, model_name


def _measure_coverage(workspace: Path) -> tuple[Decimal | None, dict[str, Decimal]]:
    """量一份副本的覆盖率 —— **整体与按模块**。

    **用真实的覆盖率工具跑真实的测试**，不自己算比例 —— 自己算就得自己
    挑分母，而分母挑错了那个数就是错的。

    **量不到就报"没测到"**（``None``），不报 0。
    """
    report = workspace / COVERAGE_REPORT_NAME
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--cov=src",
                f"--cov-report=json:{report}",
                "-p",
                "no:langsmith_plugin",
                "-q",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        return None, {}
    if not report.exists():
        return None, {}
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {}
    if not isinstance(data, Mapping):
        return None, {}

    overall: Decimal | None = None
    totals = data.get("totals")
    if isinstance(totals, Mapping):
        percent = totals.get("percent_covered")
        if isinstance(percent, (int, float)):
            overall = _fraction(percent)
    modules: dict[str, Decimal] = {}
    files = data.get("files")
    if isinstance(files, Mapping):
        for path, detail in files.items():
            if not isinstance(detail, Mapping):
                continue
            summary = detail.get("summary")
            if not isinstance(summary, Mapping):
                continue
            percent = summary.get("percent_covered")
            if not isinstance(percent, (int, float)):
                continue
            modules[_module_name(str(path))] = _fraction(percent)
    return overall, modules


def _fraction(percent: float | int) -> Decimal:
    """把百分比变成比例 —— ``87.5`` → ``0.875``。"""
    return (Decimal(str(percent)) / Decimal(100)).quantize(Decimal("0.000001"))


def _module_name(path: str) -> str:
    """覆盖率报告里的路径 → 模块名（``src/tinylib/stats.py`` → ``tinylib.stats``）。

    **按路径分段，不靠字符串替换** —— ``src`` 后面那几段才是包名，
    ``str.replace("src/", "")`` 会把 ``resources/`` 里的 ``src/`` 也换掉。
    """
    parts = Path(path).with_suffix("").parts
    if "src" in parts:
        parts = parts[parts.index("src") + 1 :]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or Path(path).name


def _collect(
    tasks: Sequence[RepresentativeTask],
    *,
    pilot: Path,
    workdir: Path,
    binary: str,
    model: str | None,
    permission_mode: str,
    timeout_seconds: int,
    with_coverage: bool,
    resume: Mapping[str, TaskMeasurement] | None = None,
) -> tuple[TaskMeasurement, ...]:
    """跑所有任务 —— **一个一个来**。

    **不并行。** 并行跑会让几个 Agent 同时抢 CPU 与网络，量到的时长里
    混进了排队时间 —— 那不是"一个任务要多久"，是"十个任务挤在一起要多久"。

    **量过的可以复用**（``resume``）—— 见 :func:`_is_reusable`。
    """
    reusable = resume or {}
    measurements: list[TaskMeasurement] = []
    for task in tasks:
        previous = reusable.get(task.kind)
        if previous is not None and _is_reusable(previous):
            print(f"—— 复用 {task.kind} 的上一次测量（{previous.source}）", flush=True)
            measurements.append(previous)
            continue
        print(f"—— 采集 {task.kind}：{task.title}", flush=True)
        measurement = asyncio.run(
            _collect_one(
                task,
                pilot=pilot,
                workdir=workdir,
                binary=binary,
                model=model,
                permission_mode=permission_mode,
                timeout_seconds=timeout_seconds,
                with_coverage=with_coverage,
            )
        )
        tokens = "没读到" if measurement.usage is None else measurement.usage.total_tokens
        cost = "没读到" if measurement.cost is None else str(measurement.cost)
        coverage = "没测到" if measurement.coverage is None else str(measurement.coverage)
        print(
            f"   成本 {cost}，时长 {measurement.duration_seconds} 秒，"
            f"工具调用 {measurement.tool_calls} 次，token {tokens}，覆盖率 {coverage}",
            flush=True,
        )
        measurements.append(measurement)
    return tuple(measurements)


def _is_reusable(measurement: TaskMeasurement) -> bool:
    """这条测量能不能直接用 —— **量齐了才算数**。

    **没量到的要重跑。** 一次超时跑出来的测量：金额没读到、用量没读到 ——
    而那正是最该重新量的一次。它留下的不是事实，是"没量到"这件事本身；
    把它留在表里，下一个人会以为这一类任务"便宜""不费 token"。
    """
    return measurement.cost is not None and measurement.usage is not None


def _load_previous(output: Path, pilot: str) -> dict[str, TaskMeasurement]:
    """读上一次采集的表 —— **读不出来就报错，不返回半份**。

    **只复用同一个试点仓库的测量。** 换一个仓库重采，量的是另一堆代码 ——
    拿上一堆的数字顶上来，那张表就答非所问，而表上没有任何地方写着这一点。
    """
    path = output / "baselines.yaml"
    if not path.is_file():
        print(f"—— 没有可复用的旧表（{path} 不存在）", flush=True)
        return {}
    try:
        table = BaselineTable.from_yaml(path.read_text(encoding="utf-8"))
    except (BaselineError, OSError) as error:
        raise BaselineError(f"旧基线表读不出来：{path} —— {error}") from error
    if table.pilot != pilot:
        print(
            f"—— 旧表是另一个试点仓库（{table.pilot}），不复用",
            flush=True,
        )
        return {}
    return {item.task_kind: item for item in table.measurements}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集成本、时延与覆盖率基线")
    parser.add_argument("--pilot", type=Path, default=DEFAULT_PILOT, help="试点仓库")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="基线表落在哪")
    parser.add_argument("--binary", default="claude", help="执行器可执行文件")
    parser.add_argument("--model", default=None, help="模型名（不给就用 CLI 自己的默认）")
    parser.add_argument(
        "--permission-mode",
        default="bypassPermissions",
        help="CLI 的权限模式（副本是一次性的，所以放开）",
    )
    parser.add_argument("--timeout", type=int, default=900, help="单个任务的超时秒数")
    parser.add_argument(
        "--tasks",
        type=int,
        default=len(REPRESENTATIVE_TASKS),
        help="跑前几个任务",
    )
    parser.add_argument("--no-coverage", action="store_true", help="不量覆盖率")
    parser.add_argument("--keep", action="store_true", help="保留任务副本（调试用）")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="复用上一次已经量全的测量（只重跑没量到的那些）",
    )
    args = parser.parse_args(argv)

    if args.tasks <= 0:
        parser.error("--tasks 必须是正数")
    pilot = args.pilot.resolve()
    if not pilot.is_dir():
        parser.error(f"试点仓库不存在：{pilot}")

    tasks = REPRESENTATIVE_TASKS[: args.tasks]
    workdir = Path(tempfile.mkdtemp(prefix="ases-baseline-"))
    resume = _load_previous(args.output, pilot.name) if args.resume else {}
    try:
        measurements = _collect(
            tasks,
            pilot=pilot,
            workdir=workdir,
            binary=args.binary,
            model=args.model,
            permission_mode=args.permission_mode,
            timeout_seconds=args.timeout,
            with_coverage=not args.no_coverage,
            resume=resume,
        )
    finally:
        if args.keep:
            print(f"副本留在了 {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    table = BaselineTable(measurements, pilot=pilot.name)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "baselines.yaml").write_text(table.to_yaml(), encoding="utf-8")
    (args.output / "baselines.md").write_text(table.render_markdown(), encoding="utf-8")
    print(f"基线表写到了 {args.output / 'baselines.yaml'} 与 {args.output / 'baselines.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
