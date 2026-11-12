"""金标矩阵的变异验证 —— **每个变异体都必须被抓住**。

**这份脚本验的不是"测试是不是绿的"，是"测试绿得有没有意义"。**

一个从没失败过的断言证明不了任何事 —— 它可能只是还没碰上那种情况。所以
这里把矩阵里的每一行**真的改坏一次**，然后看那条断言会不会红。**红不了，
就说明那条断言是在装饰。**

**为什么要有工作树隔离。** 改坏的是生产代码。脚本中途崩了、被 Ctrl-C 了、
机器断电了，那份被改坏的代码就留在仓库里了 —— 下一次 ``git add -A`` 就会
把它混进提交里。所以这里在 **git 工作树副本**里改：主仓库一个字节都不动。
（仓库里已有的 ``verify_*_mutants.py`` 是就地改、``finally`` 里还原的 ——
那在正常路径上没问题，但"正常路径"不等于"所有路径"。）

**三条不能省的检查。** 少任何一条，这脚本都会递出一张**假通行证**：

1. **变异前，红目标必须是绿的。** 目标本来就红的话，变异之后它还是红 ——
   脚本会报"抓住了"，其实什么都没证明。**这一条最容易漏**：目标写错了、
   或者这个分支本来就没跑过，基线就是红的。
2. **变异必须真的写进去了。** 片段对不上（代码挪了位）、或者片段命中两处
   （改到了另一处），变异就等于没发生。
3. **跑测试的那个进程必须加载的是工作树里的代码。** 靠一个探针验：它把被
   改的那个模块的 ``origin`` 打出来，跟真正被改的文件不是同一个，就当场停。
   **这一条是静默的** —— 加载错了主仓库的代码，变异在那边根本没发生，
   表现是"没抓住"，而"没抓住"会被读成"这条要求没被验到"，**方向正好反了**。

**为什么逐条对，而不是看退出码。** "退出码非零"只说明"有东西红了"，不说明
"声明的那几条红了"。只红一条而另一条没红，是**半个真相** —— 必须说出来，
不能算成抓住。

**为什么结构自检不当红目标。** 矩阵里那个
``test_every_mutant_fragment_hits_exactly_once`` 在**任何**变异下都会红
（因为 ``before`` 片段真的不见了）。把它算作红目标，等于用"脚本还活着"
冒充"行为有人守" —— 那样每一行都会"通过"，整个验证就白做了。它验的是
**脚本**，不是**行为**，所以它只在基线里跑，不在变异里当证据。

用法::

    uv run python -m scripts.verify_golden_mutants               # 全部变异体
    uv run python -m scripts.verify_golden_mutants --only FC-03  # 只跑一行
    uv run python -m scripts.verify_golden_mutants --only MUT-ENG-01
    uv run python -m scripts.verify_golden_mutants --keep        # 留着工作树
    uv run python -m scripts.verify_golden_mutants --list        # 只列计划
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tests.golden.matrix import CASES, FailClosedCase, Mutant

ROOT: Path = Path(__file__).resolve().parents[1]

#: 工作树的临时目录前缀。
WORKTREE_PREFIX: str = "ases-golden-mutants-"


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------

#: 变异体被抓住了 —— 声明的红目标逐条都红了。
CAUGHT: str = "抓住"

#: 变异体没被抓住 —— 目标还是绿的。这条要求没被验到。
MISSED: str = "没抓住"

#: 变异前目标就是红的 —— 这次"抓住"不作数。
BASELINE_RED: str = "基线本来就红"

#: 变异压根没写进去（片段对不上 / 命中多处 / 模块加载的不是被改的那个文件）。
NOT_APPLIED: str = "变异没生效"

#: 变异把代码改到 pytest 收集不起来 —— 变异体本身写坏了。
COLLECTION_ERROR: str = "变异改到不能收集"


@dataclass(frozen=True, slots=True)
class Outcome:
    """一个变异体的验结果。"""

    mutant_id: str
    status: str
    detail: str
    why: str
    caught: bool


@dataclass(frozen=True, slots=True)
class Run:
    """一次 pytest 的结果。"""

    code: int
    output: str


# --------------------------------------------------------------------------
# 工作树
# --------------------------------------------------------------------------


def _make_worktree() -> Path:
    """建一个工作树副本。

    **从 HEAD 建，不是从工作树建** —— HEAD 是已提交的那份。真正要带过去的是
    **未提交的那部分**，那是 :func:`_overlay` 的活。
    """
    base = Path(tempfile.mkdtemp(prefix=WORKTREE_PREFIX)).resolve()
    target = base / "wt"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(target), "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return target


def _remove_worktree(target: Path) -> None:
    """拆掉工作树。**拆不掉也要把目录删了** —— 留着半个工作树比没有更坏。"""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(target)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    shutil.rmtree(target.parent, ignore_errors=True)
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _overlay(target: Path) -> list[str]:
    """把主仓库工作树的状态搬到工作树里。

    **搬的是"已跟踪但被改了"与"还没跟踪"这两类。** 已跟踪且没改的文件，
    工作树里本来就是 HEAD 那份，不用搬。

    **为什么要搬未跟踪的文件。** 金标这一批（``tests/golden/``）还没进版本
    库 —— 它们只存在于工作树里。不搬的话，工作树里根本没有这些测试，
    跑起来是"0 个用例" —— 而**0 个用例的退出码是非零**，脚本会把它读成
    "抓住了"。那是本脚本能犯的最坏的一个错。

    :returns: 搬了什么的清单，给人看的。
    """
    done = subprocess.run(
        ["git", "status", "--porcelain", "-z", "-uall", "--no-renames"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    notes: list[str] = []
    for record in done.stdout.split("\0"):
        if not record:
            continue
        status = record[:2]
        relative = record[3:]
        source = ROOT / relative
        destination = target / relative

        if "D" in status:
            # 主仓库里删掉了 —— 工作树里也得删，否则 pytest 会收到一份
            # 主仓库已经不要了的文件。
            destination.unlink(missing_ok=True)
            notes.append(f"删除 {relative}")
            continue

        if status == "??" or any(mark in status for mark in ("M", "A", "T")):
            if not source.is_file():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            notes.append(f"覆盖 {relative}")
    return notes


# --------------------------------------------------------------------------
# 探针
# --------------------------------------------------------------------------


def _module_name(path: str) -> str:
    """文件路径 → 模块名。"""
    stem = path[:-3] if path.endswith(".py") else path
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


def _probe_origin(worktree: Path, module: str) -> tuple[bool, str]:
    """问一句"这个模块是从哪个文件加载的"。

    **用 ``find_spec`` 而不是 import** —— ``find_spec`` 只找文件，不执行模块
    体。被改坏的模块导入时会炸，而那正是我们要测的情况，不能让探针自己
    先炸掉。

    :returns: (是不是工作树里那个文件, 说明)
    """
    done = subprocess.run(
        [sys.executable, "-c", _PROBE_SOURCE, module],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        env=_child_env(worktree),
    )
    output = (done.stdout or "").strip()
    if done.returncode != 0:
        return False, f"探针失败：{output or (done.stderr or '').strip()[:400]}"
    return True, output


#: 探针的源码。**只做一件事**：把模块的来源文件打出来。
_PROBE_SOURCE: str = """
import importlib.util
import sys

name = sys.argv[1]
try:
    spec = importlib.util.find_spec(name)
except Exception as error:
    print("探针找不到模块：" + name + " —— " + repr(error))
    raise SystemExit(1)
if spec is None or spec.origin is None:
    print("模块没有来源文件：" + name)
    raise SystemExit(1)
print(spec.origin)
"""


# --------------------------------------------------------------------------
# 跑测试
# --------------------------------------------------------------------------


def _child_env(worktree: Path) -> dict[str, str]:
    """子进程的环境。

    **``PYTHONPATH`` 指向工作树。** 虚拟环境里的可编辑安装是一个指向**主
    仓库**的 ``.pth`` 文件；它排在 ``PYTHONPATH`` 后面，所以工作树优先。
    不显式指一次的话，主仓库那份会赢 —— 而那样**主仓库里被改的那份代码
    压根没参与测试**，脚本会报"没抓住"。

    **``PYTHONDONTWRITEBYTECODE=1``**：不留 ``.pyc``。改了源码又还原之后，
    缓存的时间戳可能刚好对得上，于是跑的是旧的字节码 —— 那种错是静默的，
    而且症状是"变异好像没生效"。

    **清掉 ``PYTEST_ADDOPTS``**：外面传进来的选项会污染这一次调用
    （``--lf`` 之类会直接改掉要跑哪些用例）。这一次调用的范围由本脚本说了算。
    """
    environment = dict(os.environ)
    pieces = [str(worktree)]
    inherited = environment.get("PYTHONPATH")
    if inherited:
        pieces.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(pieces)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    return environment


def _run_pytest(worktree: Path, node_ids: Sequence[str]) -> Run:
    """在工作树里跑指定的那几条用例。"""
    command = [
        sys.executable,
        "-m",
        "pytest",
        *node_ids,
        "--tb=no",
        "-rf",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    done = subprocess.run(
        command,
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        env=_child_env(worktree),
    )
    return Run(done.returncode, (done.stdout or "") + (done.stderr or ""))


def _red_nodes(output: str) -> set[str]:
    """从输出里挑出"红了"的用例编号。"""
    nodes: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        for marker in ("FAILED ", "ERROR "):
            if stripped.startswith(marker):
                node = stripped[len(marker) :].split(" - ", 1)[0].strip()
                nodes.add(node)
                break
    return nodes


def _collection_broken(output: str) -> bool:
    """这次跑是"收集阶段就炸了"，还是"跑起来了但没过"。"""
    return "ERRORS" in output or "INTERNALERROR" in output or "Interrupted" in output


def _matches(declared: str, reported: str) -> bool:
    """声明的节点 id 与 pytest 报出来的那个，对不对得上。

    **参数化用例的编号带参数后缀。** 声明
    ``...::test_an_empty_identity_field_is_refused`` 时，pytest 报出来的是
    ``...::test_an_empty_identity_field_is_refused[workspace_id]`` —— 少了
    后缀就算没红，**会把"其实红了"读成"没红"**。方向又反了一次：明明抓住
    了，脚本说没抓住。

    所以这里按"前缀 + 方括号"来配。
    """
    return reported == declared or reported.startswith(declared + "[")


# --------------------------------------------------------------------------
# 验一个变异体
# --------------------------------------------------------------------------


def _not_applied(mutant: Mutant, detail: str) -> Outcome:
    """变异压根没生效。"""
    return Outcome(mutant.id, NOT_APPLIED, detail, mutant.why, caught=False)


def _baseline_is_red(mutant: Mutant, baseline: Run) -> Outcome | None:
    """基线本来就红的话，这次"抓住"不作数。

    :returns: 作数就返回 ``None``，不作数就返回那条结论。
    """
    if baseline.code == 0:
        return None
    return Outcome(
        mutant.id,
        BASELINE_RED,
        "变异前就已经是红的 —— 这次『抓住』不作数",
        mutant.why,
        caught=False,
    )


def _decide(
    mutant: Mutant,
    case: FailClosedCase,
    baseline: Run,
    result: Run,
) -> Outcome:
    """拿两次跑的证据下结论。

    **这一段是纯函数** —— 不碰工作树、不跑 pytest，只看证据。所以它能被
    单测直接验：脚本自己"会红"这件事，是这里保证的。
    """
    early = _baseline_is_red(mutant, baseline)
    if early is not None:
        return early

    declared = set(case.red_targets)
    reported = _red_nodes(result.output)
    red = {target for target in declared if any(_matches(target, seen) for seen in reported)}

    if result.code == 0:
        return Outcome(
            mutant.id,
            MISSED,
            f"目标还是绿的（{len(declared)} 条都没红）",
            mutant.why,
            caught=False,
        )
    if red == declared:
        return Outcome(
            mutant.id,
            CAUGHT,
            f"{len(red)}/{len(declared)} 条转红",
            mutant.why,
            caught=True,
        )
    if _collection_broken(result.output):
        return Outcome(
            mutant.id,
            COLLECTION_ERROR,
            "变异把代码改到 pytest 收集不起来 —— 是变异体本身写坏了，不是行为有没有人守的问题",
            mutant.why,
            caught=False,
        )
    still_green = sorted(declared - red)
    return Outcome(
        mutant.id,
        MISSED,
        f"只有 {len(red)}/{len(declared)} 条转红；仍然绿着：{still_green}",
        mutant.why,
        caught=False,
    )


def _apply_and_probe(
    worktree: Path,
    mutant: Mutant,
    target: Path,
    original: str,
) -> Outcome | None:
    """把变异写进去，并确认它真的写进去了。

    :returns: 没问题就返回 ``None``；有问题就返回那条结论。
    """
    target.write_text(original.replace(mutant.before, mutant.after, 1), encoding="utf-8")

    after_write = target.read_text(encoding="utf-8")
    if mutant.after not in after_write or mutant.before in after_write:
        return _not_applied(mutant, "写回去之后内容不对 —— 变异没生效")

    # --- 探针：跑测试的那个进程加载的是不是被改的这个文件 ------------------
    ok, detail = _probe_origin(worktree, _module_name(mutant.path))
    if not ok:
        return _not_applied(mutant, f"探针没通过：{detail}")
    origin = Path(detail).resolve()
    if origin != target.resolve():
        return _not_applied(
            mutant,
            f"模块是从 {origin} 加载的，不是被改的 {target.resolve()} —— "
            f"变异在跑测试的那个进程里不存在",
        )
    return None


def _verify_one(worktree: Path, mutant: Mutant, case: FailClosedCase) -> Outcome:
    """验一个变异体：改坏一次，看声明的红目标会不会红。"""
    target = worktree / mutant.path
    if not target.exists():
        return _not_applied(mutant, f"目标文件不存在：{mutant.path}")

    original = target.read_text(encoding="utf-8")
    hits = original.count(mutant.before)
    if hits != 1:
        return _not_applied(
            mutant,
            f"片段在 {mutant.path} 里命中 {hits} 次（必须恰好一次）",
        )

    # --- 基线：这些目标本来就该是绿的 -----------------------------------
    baseline = _run_pytest(worktree, case.red_targets)
    early = _baseline_is_red(mutant, baseline)
    if early is not None:
        return early

    # --- 改坏它，再跑一次 -------------------------------------------------
    try:
        problem = _apply_and_probe(worktree, mutant, target, original)
        if problem is not None:
            return problem
        result = _run_pytest(worktree, case.red_targets)
    finally:
        target.write_text(original, encoding="utf-8")

    return _decide(mutant, case, baseline, result)


# --------------------------------------------------------------------------
# 计划与报告
# --------------------------------------------------------------------------


def _plan(only: str | None) -> list[tuple[Mutant, FailClosedCase]]:
    """要验哪些。``only`` 既可以给变异体编号，也可以给矩阵行号。"""
    pairs: list[tuple[Mutant, FailClosedCase]] = []
    for case in CASES:
        for mutant in case.mutants:
            pairs.append((mutant, case))
    if only is None:
        return pairs
    picked = [(mutant, case) for mutant, case in pairs if only in (mutant.id, case.id)]
    if not picked:
        raise SystemExit(f"没有 id 为 {only!r} 的变异体或矩阵行")
    return picked


def _report(outcomes: Sequence[Outcome], worktree: Path | None) -> int:
    """打印结果。**返回退出码。**"""
    width = max((len(item.mutant_id) for item in outcomes), default=0)
    for item in outcomes:
        marker = "✓" if item.caught else "✗"
        print(f"{marker} {item.mutant_id:<{width}}  {item.status:<14} {item.detail}")
        print(f"{'':<{width + 2}}  └─ {item.why}")

    caught = sum(1 for item in outcomes if item.caught)
    print()
    print(f"抓住 {caught}/{len(outcomes)}")
    if worktree is not None:
        print(f"工作树：{worktree}")
    missed = [item for item in outcomes if not item.caught]
    if missed:
        print()
        print("没抓住的：")
        for item in missed:
            print(f"  {item.mutant_id}（{item.status}）：{item.why}")
        return 1
    print("每一条都有牙 —— 变异体全被抓住。")
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="金标矩阵的变异验证：每个变异体都必须被抓住。",
    )
    parser.add_argument(
        "--only",
        help="只验一个变异体（MUT-XXX）或一行矩阵（FC-XX）。",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="验完不拆工作树（用来手工看改坏之后的样子）。",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列计划，不跑。",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    pairs = _plan(args.only)

    if args.list:
        for mutant, case in pairs:
            print(f"{mutant.id}  {case.id}  {mutant.why}")
            print(f"    改：{mutant.path}")
            print(f"    红目标：{', '.join(case.red_targets)}")
        print(f"共 {len(pairs)} 个变异体")
        return 0

    worktree = _make_worktree()
    kept = False
    try:
        notes = _overlay(worktree)
        print(f"工作树：{worktree}")
        print(f"同步了 {len(notes)} 个未提交/未跟踪的文件")
        print()

        outcomes: list[Outcome] = []
        for mutant, case in pairs:
            outcome = _verify_one(worktree, mutant, case)
            outcomes.append(outcome)
            marker = "✓" if outcome.caught else "✗"
            print(f"{marker} {outcome.mutant_id}  {outcome.status}  {outcome.detail}")
    except KeyboardInterrupt:
        print("\n中断了 —— 工作树正在拆掉，主仓库没有被改过。")
        return 130
    finally:
        if args.keep:
            kept = True
            print(f"\n工作树留着：{worktree}")
        else:
            _remove_worktree(worktree)

    return _report(outcomes, worktree if kept else None)


if __name__ == "__main__":
    sys.exit(main())
