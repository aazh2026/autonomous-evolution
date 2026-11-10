"""能力矩阵 —— 驱动脚本：真跑一遍（执行器 × 模型），产出二维看板。

设计依据：docs/tasks.md TASK-P4-030（交付物"执行器 × 模型的二维能力矩阵"；
验收条件"**能力矩阵由实测数据得出，且能指导自动选型**"；验证 T2）、R-111、
I-17、``TASK-P2-018``（一维评测 —— 口径必须与它完全一致）、``TASK-P4-020``
（多模型路由 —— 路由按"要有多强"挑模型，矩阵按"实测下来有多强"给证据）。

**口径在 :mod:`evaluation.agents.capability_matrix` 里，这个脚本只负责"把口径
跑起来"。** 换执行器、换模型、换机器，都不动口径 —— 口径一改，前面量出来的数
就废了。

**二维比一维多一样东西：模型必须被记下来。** 一维评测只比执行器，所以"这次用的
是哪个模型"是隐含前提（大家默认同一个）。二维评测里这个前提没了 —— 所以每条
运行记录都带 :attr:`~evaluation.executors.benchmark.RunRecord.model`，**记不下来
的运行切不进任何一格**（宁可少一格，也不要有一格是凭空的）。

**先探针，再矩阵。** 探针问的是"能不能干活"，与基准任务无关。一个（执行器 ×
模型）组合连"回答一个字 ok"都做不到（后端连不上、认证过期、参数不认），就别急着
跑 27 次真任务 —— 那是把同一个环境问题重复 27 遍，烧钱还烧时间。探针失败 → 该
组合的全部运行记为"没跑成"，理由写清楚。**探针本身也是证据**，一并存进报告。

**跑得断，跑得续。** 每跑完一次就往 JSONL 里追加一行，键是
``(执行器, 模型, 任务, 次数)``。中途断了、机器睡了、对话换了 —— 接着跑就行。
**看板也能只从原始记录重新算**（``--render-only``）—— 口径改了要重算，不该重跑
那几十次真任务。

**源仓库被动了就全停。** 工作区是源仓库的副本；源仓库被改过，后面每一次运行都
建在污染过的地基上 —— 那次失败、后面几次成功，全是假的，而且**看起来比真结果
还干净**。所以这一条是全局的，不是"停这一个组合"。

**额度用完了只停这一个组合。** 别的组合可能连的是另一个额度 —— 停整个矩阵，
等于把还能测的组合也扔了。停掉的那些记成"没跑成"，补了额度再跑一次，它们会被
顶掉。

**报告里不许有凭证。** CLI 的 stderr 里有可能带出 token，而报告是要进版本库
的。所以写盘前一律脱敏（脱敏规则与一维评测同一套）。

**用法**::

    python -m scripts.run_capability_matrix
    python -m scripts.run_capability_matrix --models MiniMax-M3 MiniMax-M3.1-Flash
    python -m scripts.run_capability_matrix --attempts 3 --workers 3
    python -m scripts.run_capability_matrix --render-only
    python -m scripts.run_capability_matrix --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from evaluation.agents.capability_matrix import (
    CapabilityBoard,
    build_board,
    dump_matrix,
    unclassified_runs,
)
from evaluation.executors.benchmark import (
    BenchmarkHarness,
    BenchmarkTask,
    CliRunner,
    CliSpec,
    PriceTable,
    RunOutcome,
    RunRecord,
    pilot_benchmark_tasks,
)
from scripts.run_executor_benchmark import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    PROBE_TIMEOUT,
    RETRY_BACKOFF_SECONDS,
    ProviderSetup,
    QuotaExhaustedError,
    SourceContaminatedError,
    executor_specs,
    is_quota_exhausted,
    is_transient_failure,
    probe,
    record_from_dict,
    redact,
    redact_output,
    token_steps,
    write_provider_setup,
)

#: 试点仓库 —— 标准任务集的来源。
DEFAULT_REPO = Path("samples/pilot_repo")

#: 原始记录（JSONL）。**每跑完一次就追加一行**，所以跑得断也跑得续。
DEFAULT_LOG = Path("docs/evaluation/capability-matrix-runs.jsonl")

#: 看板（Markdown）。
DEFAULT_BOARD = Path("docs/evaluation/capability-matrix-board.md")

#: 矩阵本身（JSON）—— **给运行时选型用的那份数据**。
DEFAULT_MATRIX = Path("docs/evaluation/capability-matrix.json")

#: 默认评测的模型 —— **两个模型，价格差一倍**。
#:
#: **为什么是两个。** 交互项检验至少要两个模型才能问"模型的效果是不是因执行器而
#: 异"；一个模型只能量出"哪个执行器好"，那就退回一维了。
#:
#: **为什么价格差一倍。** 两个模型同价的话，"做对率分不开时按成本挑"这条规则
#: 永远用不上 —— 而那恰恰是最实用的一条。
DEFAULT_MODELS: tuple[str, ...] = ("MiniMax-M3", "MiniMax-M3.1-Flash")

#: 默认评测的执行器 —— **不含 gemini-cli**。
#:
#: **为什么把它排除掉。** 一维实测（TASK-P2-018，108 次运行）里 gemini-cli 的 27
#: 次**全部"没跑成"** —— 它在这台机器上连"回答一个字 ok"都做不到。再跑 54 次
#: 只会把同一个环境问题重复 54 遍，**烧钱换不来一条数据**。等它能跑通了再把它
#: 加回来（那一格会是"没有实测值"，不是"成功率 0"）。
DEFAULT_EXECUTORS: tuple[str, ...] = ("claude-code", "codex", "opencode")

#: 统一价目表 —— **按模型给**。
#:
#: **价目表是数据不是口径。** 折算的公式在 :mod:`evaluation.executors.benchmark`
#: 里，数字在这儿，换模型就换这张表。出处是 models.dev 的 ``minimax`` 条目
#: （2026-11-10 取的值）。
PRICE_TABLES: dict[str, PriceTable] = {
    "MiniMax-M3": PriceTable(
        input=Decimal("0.30"),
        output=Decimal("1.20"),
        cache_read=Decimal("0.06"),
        #: 厂商价目表只列了缓存**读**价。缓存**写**按输入价计 —— 宁可算高一点，
        #: 也不凭空造出一笔便宜。
        cache_write=Decimal("0.30"),
        currency="USD",
        source="models.dev 的 minimax/MiniMax-M3 条目（2026-11-10 取值）",
    ),
    "MiniMax-M3.1-Flash": PriceTable(
        input=Decimal("0.15"),
        output=Decimal("0.60"),
        cache_read=Decimal("0.015"),
        cache_write=Decimal("0.15"),
        currency="USD",
        source="models.dev 的 minimax/MiniMax-M3.1-Flash 条目（2026-11-10 取值）",
    ),
}

#: 没有价目表的模型 —— **成本记"没测出来"，不记 0**。
#:
#: **0 不是免费，是没标价。** 把没标价当成 0，看板上就会冒出一条"便宜到离谱"的
#: 假差异 —— 那是记账口径的差别，不是能力差别。
DEFAULT_UNKNOWN_MODEL_PRICE = PriceTable(
    input=Decimal(0),
    output=Decimal(0),
    cache_read=Decimal(0),
    cache_write=Decimal(0),
    currency="USD",
    source="没找到该模型的价目表 —— 成本记为未计价",
)


def price_of(model: str) -> PriceTable | None:
    """某个模型的价目表 —— **没有就返回 ``None``**（记"没测出来"）。

    **返回 ``None`` 而不是一张全 0 的表。** 全 0 的表会让成本中位数算成 0，
    于是这一格在选型里变成"最便宜" —— 那是在用记账口径的差别冒充能力差别。
    """
    return PRICE_TABLES.get(model)


@dataclass(frozen=True, slots=True)
class RunPlan:
    """一轮评测跑什么。

    **参数打包成对象** —— 跑到一半才发现某个参数传错了，那种错在几十次真跑之
    后才显形，代价太大。
    """

    #: 试点仓库。
    repo: Path
    #: 任务集 —— **每个组合跑同一份**，指令是自变量。
    tasks: Sequence[BenchmarkTask]
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


def specs_for(setup: ProviderSetup, names: Sequence[str]) -> list[CliSpec]:
    """某个模型下要跑的执行器 —— **每个都记上模型名**。

    **模型名要进 ``CliSpec.model``。** 那一栏会一路走到每一条运行记录里 ——
    二维矩阵就是靠它切格子的。漏了记，矩阵上就会多出"模型不明"的空缺。
    """
    specs = executor_specs(setup)
    return [replace(specs[name], model=setup.model) for name in names]


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
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def append_log(path: Path, entry: Mapping[str, object]) -> None:
    """把一条原始记录写进日志。

    **"没跑成"的旧记录会被顶掉。** "没跑成"不是结果，是**空缺** —— 环境修好了
    就该重跑，重跑出来的数比"没跑成"更有价值。所以同一个
    ``(执行器, 模型, 任务, 次数)`` 上如果已经躺着一条"没跑成"，新记录会把它替
    掉；而"跑过了"的记录不会被替 —— 那是一次量好的数据，不该重跑。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = {**entry, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    line = json.dumps(stamped, ensure_ascii=False) + "\n"
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, existing in enumerate(lines):
            parsed = _parse_entry(existing)
            if parsed is None:
                continue
            if _entry_key(parsed) != _entry_key(stamped):
                continue
            if str(parsed.get("outcome")) == RunOutcome.NOT_RUN.value:
                lines[index] = line
                path.write_text("".join(lines), encoding="utf-8")
                return
            return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _parse_entry(line: str) -> dict[str, object] | None:
    """把一行日志解成字典 —— 解不出来就当没有。"""
    candidate = line.strip()
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _entry_key(entry: Mapping[str, object]) -> tuple[str, str, str, int]:
    """一条记录的键 —— ``(执行器, 模型, 任务, 次数)``。

    **模型进键里。** 少了它，同一个任务同一个次数的两条记录会互相顶掉 —— 换个
    模型重跑时，前一个模型的结果就消失了。
    """
    try:
        attempt = int(str(entry.get("attempt", 0)))
    except ValueError:
        attempt = 0
    return (
        str(entry.get("executor")),
        str(entry.get("model")),
        str(entry.get("task_id")),
        attempt,
    )


def _entry_of(record: RunRecord) -> dict[str, object]:
    """记录落盘前脱敏 —— 原始输出也要脱。

    **逐步的 token 明细一并存进去。** 口径改了要能从原始记录重算 —— 而原始输出
    是截断的，事后翻不回来；把每一步的数原样留下，就不必为了改一次折算方式重跑
    几十次真任务。
    """
    entry = record.to_dict()
    entry["output"] = redact_output(record.output)
    entry["token_steps"] = token_steps(record.output)
    return entry


def _record_from_log(log: Path, key: tuple[str, str, str, int]) -> RunRecord:
    """从原始记录里取一条 —— **连模型一起还原**。"""
    for entry in read_log(log):
        if _entry_key(entry) == key:
            return _record_from_entry(entry)
    raise KeyError(f"原始记录里没有 {key}")


def _record_from_entry(entry: Mapping[str, object]) -> RunRecord:
    """把原始记录还原成 :class:`RunRecord`。"""
    return replace(
        record_from_dict(entry),
        model=str(entry.get("model", "")),
    )


def _entry_of_not_run(
    task_id: str,
    executor: str,
    model: str,
    attempt: int,
    reason: str,
) -> RunRecord:
    """一条"没跑成"的记录。"""
    return RunRecord(
        task_id=task_id,
        executor=executor,
        attempt=attempt,
        outcome=RunOutcome.NOT_RUN,
        model=model,
        detail=reason,
    )


def _not_run_records(
    executor: str,
    model: str,
    tasks: Sequence[BenchmarkTask],
    *,
    attempts: int,
    reason: str,
) -> list[RunRecord]:
    """给"没跑成"的组合铺一份记录。

    **一次也不跑，也要留下痕迹。** 报表上少了一个组合，看板上就是一行空 —— 而
    "它为什么没跑"才是选型时最要紧的信息。
    """
    return [
        _entry_of_not_run(task.task_id, executor, model, attempt, reason)
        for task in tasks
        for attempt in range(1, attempts + 1)
    ]


async def _run_with_retries(
    harness: BenchmarkHarness,
    task: BenchmarkTask,
    attempt: int,
    *,
    retries: int,
) -> RunRecord:
    """跑一次；**只有环境级故障**才重试。

    **重试的是后端的限流，不是执行器的能力。** 后端说"我忙不过来"，那是环境抖
    了一下；后端说"我做完了"，那是能力。混在一起重试，能力差就会被环境抖掩盖。

    **超时不在重试之列。** 超时是执行器自己跑得慢 —— 重试只会把"它慢"盖成"它
    行"，而这次评测要量的恰恰是能力。

    重试次数记在记录里 —— 某个组合次次都要重试才跑得通，那本身就是结论。
    """
    record = await harness.run(task, attempt)
    count = 0
    while count < retries and is_transient_failure(record):
        count += 1
        await asyncio.sleep(RETRY_BACKOFF_SECONDS * count)
        record = await harness.run(task, attempt)
    return replace(record, retries=count)


@dataclass(slots=True)
class RunContext:
    """一次评测里所有运行共享的状态。

    **打包在一起是为了让函数签名短下来** —— 参数一多，"这个函数到底要什么"就
    要靠数参数个数才看得清。
    """

    #: 这一轮跑什么。
    plan: RunPlan
    #: 已经跑过的键 —— ``(执行器, 模型, 任务, 次数)``。
    done: set[tuple[str, str, str, int]]
    #: 写日志的锁 —— 并发时同一时刻只写一行。
    lock: asyncio.Lock


async def run_cell(
    spec: CliSpec,
    task: BenchmarkTask,
    attempt: int,
    context: RunContext,
) -> RunRecord:
    """跑一格 —— **一个（执行器 × 模型 × 任务 × 次数）**。"""
    plan = context.plan
    key = (spec.executor, spec.model, task.task_id, attempt)
    if key in context.done:
        return _record_from_log(plan.log, key)
    harness = BenchmarkHarness(
        CliRunner(spec),
        repo=plan.repo,
        root=plan.root / spec.executor / _slug(spec.model),
        timeout_seconds=plan.timeout,
    )
    record = await _run_with_retries(harness, task, attempt, retries=plan.retries)
    async with context.lock:
        append_log(plan.log, _entry_of(record))
    if is_quota_exhausted(record):
        # **额度用完了 —— 停下来，别再撞。** 实测 MiniMax 报的是 ``429`` +
        # ``"已达到 Token Plan 用量上限"``：429 看着像限流，按限流去重试，每次都
        # 还是同一句"额度用完了"，重试烧的是三倍的调用，换来的还是同一句话。
        # 补了额度再跑，这些"没跑成"会被顶掉。
        context.done.add(key)
        raise QuotaExhaustedError(
            f"{spec.executor}/{spec.model} 在 {task.task_id} 第{attempt}次撞上额度上限",
            completed=[record],
            remaining=[],
        )
    if record.source_touched:
        # **停下来。** 源仓库被改了 —— 工作区是从它拷的，后面的运行全都建在污染
        # 过的地基上。继续跑出来的数是假的，而且看起来是真的。
        raise SourceContaminatedError(
            f"{spec.executor}/{spec.model} 在 {task.task_id} 第{attempt}次动了源仓库 {plan.repo}"
        )
    context.done.add(key)
    print(
        f"  [{spec.executor}/{spec.model}] {task.task_id} 第{attempt}次："
        f"{record.outcome.value}（{redact(record.detail, limit=80)}）",
        flush=True,
    )
    return record


def _slug(text: str) -> str:
    """把模型名变成能当目录名的样子 —— **只留安全字符**。"""
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in text)


def _setup_for(model: str, root: Path, args: argparse.Namespace) -> ProviderSetup:
    """给某个模型写一份 provider 配置。

    **每个模型一份配置。** 模型名是写在配置文件里的（Codex 的 ``config.toml``、
    OpenCode 的 ``opencode.json``），两个模型不能共用一份 —— 否则第二个模型会悄悄
    用上第一个模型。
    """
    return write_provider_setup(
        root / _slug(model),
        model=model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
    )


async def _probe_pair(spec: CliSpec, plan: RunPlan, lock: asyncio.Lock, timeout: int) -> bool:
    """探针 —— **通了就返回 True，没通就把这一组合全记成"没跑成"**。

    **探针失败就不跑真任务。** 那是环境问题（连不上、认证过期、参数不认），不是
    能力问题 —— 把它重复 27 遍，烧的是钱，换不来一条数据。

    **探针的原始输出也带上** —— 只留一句结论，事后还是猜不出是认证过期还是参数
    不认。
    """
    print(f"\n=== {spec.executor} × {spec.model} ===")
    availability = await probe(spec, timeout)
    if availability.executed and not availability.timed_out:
        print(f"  探针通过（{redact(availability.detail, limit=60)}）")
        return True
    # **探针超时就等于"它压根没跑起来"** —— 不管它有没有吐出过一个字。让它接着
    # 跑 27 次真任务，27 次都会卡在同一个地方：那是把同一个环境问题重复 27 遍，
    # 还烧 27 遍的钱。
    reason = f"探针就没跑成：{redact(availability.detail)}"
    print(f"  {reason}")
    records = [
        replace(record, output=availability.output)
        for record in _not_run_records(
            spec.executor,
            spec.model,
            plan.tasks,
            attempts=plan.attempts,
            reason=reason,
        )
    ]
    for record in records:
        async with lock:
            append_log(plan.log, _entry_of(record))
    return False


def _select_tasks(repo: Path, args: argparse.Namespace) -> Sequence[BenchmarkTask] | None:
    """这一轮跑哪些任务 —— **任务集从仓库里读出来**。

    **返回 ``None`` 表示"选不出来"** —— 任务号写错了就别跑，跑到一半才发现是空
    的，等于白跑一遍探针。
    """
    all_tasks = pilot_benchmark_tasks(repo, timeout_seconds=args.timeout)
    if not args.tasks:
        return all_tasks
    wanted = set(args.tasks)
    tasks = [task for task in all_tasks if task.task_id in wanted]
    if not tasks:
        print(f"任务集里没有这些任务：{'、'.join(sorted(wanted))}", file=sys.stderr)
        return None
    return tasks


def _build_specs(
    executors: Sequence[str],
    models: Sequence[str],
    args: argparse.Namespace,
) -> list[CliSpec]:
    """要跑的（执行器 × 模型）组合 —— **每个模型一份 provider 配置**。

    **模型名是写在配置文件里的**（Codex 的 ``config.toml``、OpenCode 的
    ``opencode.json``），两个模型不能共用一份 —— 否则第二个模型会悄悄用上第一个
    模型。
    """
    provider_root = Path(tempfile.mkdtemp(prefix="capability-provider-"))
    specs: list[CliSpec] = []
    for executor in executors:
        for model in models:
            setup = _setup_for(model, provider_root, args)
            specs.extend(specs_for(setup, (executor,)))
    return specs


def _dry_run(specs: Sequence[CliSpec]) -> int:
    """只把命令行打印出来 —— 不真跑。"""
    for spec in specs:
        argv = " ".join(spec.argv("<工作区>", "<指令>"))
        print(f"  {spec.executor}/{spec.model}: {argv}")
    return 0


async def main_async(args: argparse.Namespace) -> int:
    """跑一遍，出看板。"""
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"仓库不存在：{repo}", file=sys.stderr)
        return 2
    log = Path(args.log)
    if args.render_only:
        return _render_from_log(log, args)

    tasks = _select_tasks(repo, args)
    if tasks is None:
        return 2
    models = tuple(args.models) if args.models else DEFAULT_MODELS
    executors = tuple(args.executors) if args.executors else DEFAULT_EXECUTORS
    specs = _build_specs(executors, models, args)

    print(
        f"任务集：{len(tasks)} 条 × {args.attempts} 次 × {len(models)} 个模型"
        f" × {len(executors)} 个执行器"
    )
    print(f"仓库：{repo}")
    print(f"模型：{'、'.join(models)}（{args.base_url}）")
    if args.dry_run:
        return _dry_run(specs)

    done = {
        _entry_key(entry)
        for entry in read_log(log)
        # **"没跑成"不算跑过。** 它是**空缺**，不是结果 —— 环境修好了就该重跑，
        # 重跑出来的数比"没跑成"更有价值。跑过了的（有通过有失败）才不再重跑。
        if str(entry.get("outcome")) != RunOutcome.NOT_RUN.value
    }
    plan = RunPlan(
        repo=repo,
        tasks=tasks,
        root=Path(tempfile.mkdtemp(prefix="capability-matrix-")),
        log=log,
        attempts=args.attempts,
        timeout=args.timeout,
        retries=args.retries,
    )
    context = RunContext(
        plan=plan,
        done=done,
        lock=asyncio.Lock(),
    )
    semaphore = asyncio.Semaphore(max(1, args.workers))
    all_records: list[RunRecord] = []

    # **先探针，再真任务。** 探针不过的组合直接出局 —— 它那一格在报表上是"没有
    # 实测值"，不是"成功率 0"。
    live: list[CliSpec] = []
    for spec in specs:
        if await _probe_pair(spec, plan, context.lock, args.probe_timeout):
            live.append(spec)

    # **按"次数 → 任务 → 组合"跑。**
    #
    # **为什么不是"一个组合跑完再换下一个"。** 那样的话中途停下来（额度用完、
    # 机器睡了、时间不够），手里就是**半张矩阵** —— 只有一格有数据。半张矩阵既
    # 没法比格子，也没法做交互项检验（那要至少两个执行器都有两个模型的成绩）。
    # 按这个次序跑，每停下来一次，每一格的样本量就是一样的。
    for attempt in range(1, args.attempts + 1):
        for task in tasks:
            results = await asyncio.gather(
                *(_guarded(spec, task, attempt, context, semaphore) for spec in live),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    if isinstance(result, QuotaExhaustedError):
                        print(f"\n停了：{result}", file=sys.stderr)
                        return _finish_quota(context, live, tasks, attempt, args)
                    raise result
                all_records.append(result)

    _write_outputs(all_records, args)
    return 0


async def _guarded(
    spec: CliSpec,
    task: BenchmarkTask,
    attempt: int,
    context: RunContext,
    semaphore: asyncio.Semaphore,
) -> RunRecord:
    """**同时只跑 ``--workers`` 个组合** —— 并发是有限度的，不是无限的。

    **为什么默认是 1。** 实测：同一个后端账号上同时跑两个会话，会撞上
    ``429 "已达到 Token Plan 用量上限"`` —— 那是额度被并发吃穿了，不是限流，
    重试只会再撞一次。**并发在这里是拿数据换的**，不值。
    """
    async with semaphore:
        return await run_cell(spec, task, attempt, context)


def _finish_quota(
    context: RunContext,
    live: Sequence[CliSpec],
    tasks: Sequence[BenchmarkTask],
    attempt: int,
    args: argparse.Namespace,
) -> int:
    """额度用完了 —— **把没跑的记成"没跑成"，然后出看板**。

    **"没跑成"是可以被顶掉的。** 补了额度再跑一次，同一个键上的"没跑成"会被新
    的记录替掉 —— 所以这一轮不是白跑。
    """
    plan = context.plan
    pending = [
        (spec, task, number)
        for number in range(attempt, plan.attempts + 1)
        for task in tasks
        for spec in live
        if (spec.executor, spec.model, task.task_id, number) not in context.done
    ]
    for spec, task, number in pending:
        record = _entry_of_not_run(
            task.task_id,
            spec.executor,
            spec.model,
            number,
            "额度用完了，没跑（不是没做到）",
        )
        append_log(plan.log, _entry_of(record))
    print(f"额度用完了：{len(pending)} 次没跑成，补了额度再跑一次就能接着来。", file=sys.stderr)
    _write_outputs([], args)
    return 3


def _render_from_log(log: Path, args: argparse.Namespace) -> int:
    """**只画板，不重跑** —— 看板必须能从原始记录重新生成。

    **看板是结论，原始记录才是事实。** 结论要能被重新算一遍 —— 所以这里给一条
    "从 JSONL 重新出看板"的路子：改了口径可以重算，不该重跑那几十次真任务。
    """
    entries = read_log(log)
    if not entries:
        print(f"原始记录是空的：{log}", file=sys.stderr)
        return 2
    records = [_record_from_entry(entry) for entry in entries]
    _write_outputs(records, args)
    return 0


def _prices() -> dict[str, PriceTable]:
    """价目表 —— **只有有表的模型才进这张字典**。

    **没有表的模型不进。** 进了就等于给它一张全 0 的表，成本会算成 0 —— 那是拿
    记账口径的差别冒充能力差别。
    """
    return dict(PRICE_TABLES)


def _write_outputs(records: Sequence[RunRecord], args: argparse.Namespace) -> None:
    """出看板 + 写矩阵 JSON。"""
    board = build_board(records, prices=_prices())
    print("\n" + board.render())
    missing = unclassified_runs(records)
    if missing:
        print(f"注意：有 {len(missing)} 条运行没记模型 —— 它们切不进任何一格。", file=sys.stderr)
    _write_board(board, args.board)
    _write_matrix(board, args.matrix)


def _write_board(board: CapabilityBoard, path: str) -> None:
    """把看板写盘。"""
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(board.render() + "\n", encoding="utf-8")
    print(f"看板已写入：{target}")


def _write_matrix(board: CapabilityBoard, path: str) -> None:
    """把矩阵写成 JSON —— **给运行时选型用的那份数据**。"""
    if not path:
        return
    target = Path(path)
    dump_matrix(board.matrix, target)
    print(f"矩阵已写入：{target}")


def build_parser() -> argparse.ArgumentParser:
    """命令行。"""
    parser = argparse.ArgumentParser(description="执行器 × 模型 二维能力矩阵")
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="试点仓库")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="原始记录（JSONL）")
    parser.add_argument("--board", default=str(DEFAULT_BOARD), help="把看板写到这个 Markdown 文件")
    parser.add_argument("--matrix", default=str(DEFAULT_MATRIX), help="把矩阵写到这个 JSON 文件")
    parser.add_argument("--attempts", type=int, default=3, help="每个任务跑几次")
    parser.add_argument("--timeout", type=int, default=300, help="单次运行的超时（秒）")
    parser.add_argument("--probe-timeout", type=int, default=PROBE_TIMEOUT, help="探针超时")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="同时跑几个（执行器 × 模型）组合",
    )
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
    parser.add_argument("--models", nargs="*", help="评测哪些模型")
    parser.add_argument(
        "--tasks",
        nargs="*",
        help="只跑这几条任务（默认全部）—— 冒烟测试用",
    )
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
