"""TASK-P2-014 的演示 —— 六层独立验证。

跑法::

    uv run python -m scripts.demo_six_layer
    # 或
    .venv/bin/python scripts/demo_six_layer.py

**这个演示要证明的是 TASK-P2-014 的验收条件**："六层验证独立执行并产出
结构化多维报告，任一层失败独立给出修复诊断，不坍缩为单个布尔值"。

所以演示的**重点不是"六层都过了"** —— 那样什么也证明不了：全过的时候
一个布尔值就够了，六维报告和一个布尔值给出的信息完全一样。演示要让六层
**坏在不同的东西上**，然后看那份报告能不能同时说清六件事：

==========  ============================================  ==========
层          演示里它坏在哪                              结论
==========  ============================================  ==========
功能        ——                                          通过
代码        一处未使用的导入（真的 ruff 跑出来的）        未通过
架构        产品代码依赖测试代码（真的索引读出来的）      未通过
安全        工作区里有一把密钥（真的扫描器扫出来的）      未通过
性能        ——（没有基线）                              判不了
产品        人工评估没通过                              未通过
==========  ============================================  ==========

**三种结论同时出现在一份报告里** —— 这就是"不坍缩为单个布尔值"的意思。
一个布尔值在这里只能说"没过"；而"没过"不告诉你该改哪儿 —— 改测试？改
lint？把依赖拆开？撤掉密钥？这四件事的返工成本差着几个数量级。

**每条失败都自带该层特有的诊断。** 演示会检查诊断里有没有那一层独有的
东西：代码层给的是"文件:行号"，架构层给的是"哪条依赖边"，安全层给的是
"命中了哪条禁止项"。给不出这些的诊断等于"检查没通过" —— 那句话谁都会说。

**性能层是"判不了"，不是"通过"。** 演示里性能层先跑一遍（没有基线），
再给它一个基线跑一遍（退化被抓住）。两次都是同一层、同一份命令，结论却不
同 —— 因为多出来的那个依据变了。降级 G 写的是"基线未建立"，不是 0。

**不需要数据库，也不需要网络。** 全部命令都是真的起进程跑本地脚本。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from core.planning.roadmap import TaskType
from core.project.models import ComparisonOperator, TargetValue, ValueKind
from harness.verification.contract import (
    AcceptanceContract,
    ForbiddenRule,
    RequiredCheck,
    Threshold,
)
from harness.verification.engine import MetricSpec, build_verification_env
from harness.verification.six_layer import (
    ArchitectureLayerRunner,
    CodeLayerRunner,
    CommandSpec,
    FunctionalLayerRunner,
    GoalTarget,
    HumanReview,
    LayerOutcome,
    LayerResult,
    PerformanceBaseline,
    PerformanceLayerRunner,
    ProductLayerRunner,
    SecurityLayerRunner,
    SixLayerReport,
    SixLayerRequest,
    SixLayerVerifier,
    VerificationLayer,
)

#: 报告里应该有几层 —— design.md §3.5B 的六层表。
EXPECTED_LAYERS = 6

#: 一份多维报告里至少要同时出现几种结论。
#:
#: **只有一种结论的多维报告和一个布尔值没有区别。** 这个下限来自验收条件
#: 的原话："任一层失败独立给出修复诊断" —— 报告得能同时承载多种状态，
#: 否则"哪一层坏了"这个信息在生成报告的那一刻就丢了。
MIN_DISTINCT_OUTCOMES = 3

#: 每一层失败时，诊断里必须出现的那一小段**只有这一层会说的话**。
#:
#: 通用话术（"检查没通过，请修复"）不算数 —— 它对六层都成立，所以它没有
#: 提供任何信息。
EXPECTED_DIAGNOSIS_MARKERS: dict[VerificationLayer, str] = {
    VerificationLayer.CODE: ".py",
    VerificationLayer.ARCHITECTURE: "→",
    VerificationLayer.SECURITY: ForbiddenRule.SECRETS_IN_DIFF.value,
}

#: 演示用的假密钥。**分成两段写**，免得它出现在仓库里被密钥扫描器当成
#: 真凭证 —— 而它确实长成真凭证的样子。
FAKE_KEY = "AKIA" + "QWERTYUIOPASDFGH"

#: 扫密钥的脚本 —— 真的扫，真的按退出码说话。
SCANNER_SOURCE = """\
import re
import sys
from pathlib import Path

PATTERN = re.compile(r"AKIA[0-9A-Z]{16}")

hits = []
for path in sorted(Path(".").rglob("*")):
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        if PATTERN.search(line):
            hits.append(f"{path}:{lineno}")

for hit in hits:
    print(hit)
sys.exit(1 if hits else 0)
"""

#: 基准脚本 —— 打印一个指标。
BENCH_SOURCE = """\
print("latency: 120")
"""

#: 场景脚本 —— 跑通了。
SCENARIO_SOURCE = """\
print("场景通过")
"""


class DemoError(RuntimeError):
    """演示里某个前提不成立。"""


def _layer(report: SixLayerReport, layer: VerificationLayer) -> LayerResult:
    """取某一层的结果。

    **这一层不在报告里就是 bug** —— 六层少了一层，后面的数字就全都不作数
    了。与其让后面每一步都判一遍"结果是不是 None"，不如在这里炸掉。
    """
    result = report.result(layer)
    if result is None:
        raise DemoError(f"报告里没有 {layer.value} 层 —— 六层少了一层")
    return result


def _banner(step: str, title: str) -> None:
    """一节的分隔线。"""
    print(f"\n{'─' * 78}\n{step}  {title}\n{'─' * 78}")


def _fail(message: str) -> int:
    """说清楚哪里不行，然后返回一个非零退出码。"""
    print(f"\n❌ {message}")
    return 1


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_workspace(root: Path) -> None:
    """造一个工作区 —— 六层各自会在这里坏在不同的东西上。

    **坏法是分开摆的。** 未使用的导入、依赖方向的违规、密钥、基准值，
    各占各的地方 —— 这样每一层的诊断才各自指向自己那件事，而不是所有人
    都指向同一个错误。
    """
    # 产品代码：导入了一个没用的东西（代码层会报），而且依赖了测试代码
    # （架构层会报）。
    _write(root, "src/pkg/__init__.py", "")
    _write(
        root,
        "src/pkg/thing.py",
        "import tests.test_thing\n\n\ndef f():\n    return 1\n",
    )
    # 一条会通过的用例 —— 功能层要真的跑一遍 pytest，而不是假装跑过。
    _write(
        root,
        "tests/test_thing.py",
        "def test_demo():\n    assert True\n",
    )
    # 工具：扫描器、基准、场景。
    _write(root, "tools/scan_secrets.py", SCANNER_SOURCE)
    _write(root, "tools/bench.py", BENCH_SOURCE)
    _write(root, "tools/scenario.py", SCENARIO_SOURCE)
    # 一把假密钥 —— 只存在于这个临时工作区里，仓库里一个字节都没有。
    _write(root, "deploy.env", f"DEPLOY_TOKEN={FAKE_KEY}\n")


def _commands() -> dict[VerificationLayer, tuple[CommandSpec, ...]]:
    """六层各自的命令。

    **每一层跑的都是真命令。** 演示里没有一个替身 —— 演示要证明的正是
    "真的跑起来之后，结论还分得开"。
    """
    python = sys.executable
    return {
        VerificationLayer.FUNCTIONAL: (
            CommandSpec(
                "单元测试",
                (python, "-m", "pytest", "tests/test_thing.py", "-q", "-p", "no:cacheprovider"),
            ),
        ),
        VerificationLayer.CODE: (
            CommandSpec(
                "规范检查",
                (python, "-m", "ruff", "check", "--isolated", "src/pkg/thing.py"),
            ),
        ),
        VerificationLayer.ARCHITECTURE: (),
        VerificationLayer.SECURITY: (
            CommandSpec(
                ForbiddenRule.SECRETS_IN_DIFF.value,
                (python, "tools/scan_secrets.py"),
            ),
        ),
        VerificationLayer.PERFORMANCE: (
            CommandSpec(
                "基准",
                (python, "tools/bench.py"),
                metric=MetricSpec(name="latency", pattern=r"latency: (\d+)"),
            ),
        ),
        VerificationLayer.PRODUCT: (CommandSpec("场景测试", (python, "tools/scenario.py")),),
    }


def _verifier(
    *,
    baseline: PerformanceBaseline | None = None,
    review: HumanReview | None = None,
    targets: tuple[GoalTarget, ...] = (),
) -> SixLayerVerifier:
    """按演示需要配一套六层执行器。"""
    commands = _commands()
    return SixLayerVerifier(
        runners={
            VerificationLayer.FUNCTIONAL: FunctionalLayerRunner(
                commands=commands[VerificationLayer.FUNCTIONAL],
            ),
            VerificationLayer.CODE: CodeLayerRunner(
                commands=commands[VerificationLayer.CODE],
            ),
            VerificationLayer.ARCHITECTURE: ArchitectureLayerRunner(),
            VerificationLayer.SECURITY: SecurityLayerRunner(
                commands=commands[VerificationLayer.SECURITY],
            ),
            VerificationLayer.PERFORMANCE: PerformanceLayerRunner(
                commands=commands[VerificationLayer.PERFORMANCE],
                baseline=baseline,
            ),
            VerificationLayer.PRODUCT: ProductLayerRunner(
                commands=commands[VerificationLayer.PRODUCT],
                targets=targets,
                review=review,
            ),
        },
    )


def _print_report(report: SixLayerReport) -> None:
    """把多维报告按层打出来 —— **一层一段，不合并**。"""
    for layer in VerificationLayer:
        result = report.result(layer)
        if result is None:
            continue
        print(f"\n【{layer.value}】{result.outcome.value}")
        if result.detail:
            print(f"  说明：{result.detail}")
        for item in result.findings:
            print(f"  发现：[{item.severity.value}] {item.code} —— {item.message}")
        if result.diagnosis is not None:
            print(f"  诊断：{result.diagnosis.summary}")
            if result.diagnosis.location:
                print(f"  位置：{result.diagnosis.location}")
            print(f"  怎么修：{result.diagnosis.repair}")
    print(f"\n整体结论：{report.verdict.value}（放行：{report.passed}）")


def _step_build(root: Path) -> int:
    _banner("第 1 步", "造一个六层各自坏在不同地方的工作区")
    _build_workspace(root)
    print(f"工作区：{root}")
    print("  产品代码里有一处未使用的导入（代码层会报）")
    print("  产品代码依赖了测试代码（架构层会报）")
    print("  部署配置里有一把假密钥（安全层会报）")
    print("  基准脚本打印 latency: 120（性能层会量）")
    print("  一条会通过的用例（功能层会真的跑 pytest）")
    return 0


async def _step_report(root: Path) -> SixLayerReport:
    _banner("第 2 步", "六层各跑各的 —— 报告里同时有三种结论")
    report = await _verifier().verify(
        SixLayerRequest(task_id="demo-1", workspace=root, env=build_verification_env())
    )
    _print_report(report)
    return report


def _step_numbers(report: SixLayerReport) -> int:
    """T2 —— 把"多维"这件事变成可以核对的数字。"""
    _banner("第 3 步", "T2：可量化的数字")
    outcomes = {result.outcome for result in report.layers.values()}
    failed = [
        layer for layer, result in report.layers.items() if result.outcome is LayerOutcome.FAILED
    ]
    diagnosed = [layer for layer in failed if layer in report.diagnoses]

    print(f"报告里的独立层数：{len(report.layers)}（基线 {EXPECTED_LAYERS}）")
    print(f"同时出现的结论种类：{len(outcomes)}（下限 {MIN_DISTINCT_OUTCOMES}）")
    print(f"失败的层：{len(failed)}；其中各自给出了诊断的：{len(diagnosed)}")

    problems: list[str] = []
    if len(report.layers) != EXPECTED_LAYERS:
        problems.append(f"报告里的层数是 {len(report.layers)}，不是 {EXPECTED_LAYERS}")
    if len(outcomes) < MIN_DISTINCT_OUTCOMES:
        problems.append(f"只出现了 {len(outcomes)} 种结论，少于 {MIN_DISTINCT_OUTCOMES}")
    if len(diagnosed) != len(failed):
        problems.append(f"{len(failed) - len(diagnosed)} 个失败层没有自己的诊断")
    for layer, marker in EXPECTED_DIAGNOSIS_MARKERS.items():
        diagnosis = report.diagnoses.get(layer)
        if diagnosis is None:
            problems.append(f"{layer.value} 层没有诊断")
            continue
        if marker not in diagnosis.location and marker not in diagnosis.repair:
            problems.append(f"{layer.value} 层的诊断里没有「{marker}」")
        print(f"  {layer.value} 的诊断：{diagnosis.summary}｜{diagnosis.location}")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        return 1
    print("\n✅ 六层各判各的，报告是多维的，不是坍缩出来的布尔值")
    return 0


async def _step_performance(root: Path) -> int:
    """性能层：先没有基线，再给基线 —— 两次结论不同。"""
    _banner("第 4 步", "性能层：没有基线是「判不了」，有基线才谈退化")
    without = await _verifier().verify(
        SixLayerRequest(task_id="demo-2", workspace=root, env=build_verification_env())
    )
    result = _layer(without, VerificationLayer.PERFORMANCE)
    print(f"没有基线：{result.outcome.value} —— {result.detail}")
    print(f"  指标量到了：{ {k: str(v) for k, v in result.metrics.items()} }")

    baseline = PerformanceBaseline(
        metric="latency",
        value=Decimal(100),
        higher_is_better=False,
    )
    with_baseline = await _verifier(baseline=baseline).verify(
        SixLayerRequest(task_id="demo-3", workspace=root, env=build_verification_env())
    )
    judged = _layer(with_baseline, VerificationLayer.PERFORMANCE)
    print(f"给了基线 100：{judged.outcome.value} —— {judged.detail}")

    problems: list[str] = []
    if result is None or result.outcome is not LayerOutcome.NOT_JUDGED:
        problems.append("没有基线时性能层不是「判不了」")
    elif "基线未建立" not in result.detail:
        problems.append("没有基线时没有报「基线未建立」")
    # 指标**要**量出来 —— 报"判不了"不等于"不报数"。降级 G 要的是
    # "基线未建立"而不是把实测值当成 0：把 120 读成 0，恰好抹掉了
    # "现在到底多快"这个唯一有用的信息。
    measured = result.metrics.get("latency")
    if measured is None or measured.number != Decimal(120):
        problems.append(f"没有基线时指标读成了 {measured}，不是实测值 120")
    if judged is None or judged.outcome is not LayerOutcome.FAILED:
        problems.append("给了基线之后，退化没有被抓住")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        return 1
    print("\n✅ 「判不了」与「通过」没有混为一谈")
    return 0


async def _step_product(root: Path) -> int:
    """产品层：目标没达到是失败，人没评是判不了。"""
    _banner("第 5 步", "产品层：目标没达到先判失败，人工评估没来才算「判不了」")
    target = GoalTarget(
        name="响应时间",
        threshold=Threshold(
            operator=ComparisonOperator.LTE,
            value=TargetValue(kind=ValueKind.NUMBER, number=Decimal(100)),
        ),
        measured=TargetValue(kind=ValueKind.NUMBER, number=Decimal(250)),
    )
    report = await _verifier(targets=(target,)).verify(
        SixLayerRequest(task_id="demo-4", workspace=root, env=build_verification_env())
    )
    result = _layer(report, VerificationLayer.PRODUCT)
    print(f"目标没达到、也没人评：{result.outcome.value} —— {result.detail}")
    if result is not None and result.diagnosis is not None:
        print(f"  诊断：{result.diagnosis.summary}｜{result.diagnosis.location}")

    reviewed = await _verifier(
        review=HumanReview(reviewer="张三", accepted=False, comment="交互绕了三步"),
    ).verify(SixLayerRequest(task_id="demo-5", workspace=root, env=build_verification_env()))
    judged = _layer(reviewed, VerificationLayer.PRODUCT)
    print(f"人评了，没通过：{judged.outcome.value} —— {judged.detail}")

    problems: list[str] = []
    if result is None or result.outcome is not LayerOutcome.FAILED:
        problems.append("目标没达到时产品层不是「失败」")
    elif "未达到" not in result.detail:
        # 只说"场景命令通过"会误导：命令全过正是"目标没达到"的常见样子。
        problems.append("目标没达到，说明里却只说场景命令通过")
    if judged is None or judged.outcome is not LayerOutcome.FAILED:
        problems.append("人工评估没通过时产品层不是「失败」")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        return 1
    print("\n✅ 「没达标」与「没人评」没有混为一谈")
    return 0


async def _step_scope(root: Path) -> int:
    """R-077 —— 不同任务类型走不同的验收流程。"""
    _banner("第 6 步", "R-077：不同任务类型走不同的验收流程")
    feature = await _verifier().verify(
        SixLayerRequest(
            task_id="demo-6",
            workspace=root,
            task_type=TaskType.FEATURE,
            env=build_verification_env(),
        )
    )
    docs = await _verifier().verify(
        SixLayerRequest(
            task_id="demo-7",
            workspace=root,
            task_type=TaskType.DOCS,
            env=build_verification_env(),
        )
    )
    print(f"功能类任务要的层：{[layer.value for layer in feature.required_layers]}")
    print(f"文档类任务要的层：{[layer.value for layer in docs.required_layers]}")
    problems: list[str] = []
    if VerificationLayer.FUNCTIONAL not in feature.required_layers:
        problems.append("功能类任务没有要求功能层")
    if VerificationLayer.FUNCTIONAL in docs.required_layers:
        problems.append("文档类任务却要求了功能层")
    if VerificationLayer.PRODUCT not in docs.required_layers:
        problems.append("文档类任务没有要求产品层")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        return 1
    print("\n✅ 不同任务类型走的验收流程确实不一样")
    return 0


async def _step_contract(root: Path) -> int:
    """契约判定 —— 复用阶段一的那套规则。"""
    _banner("第 7 步", "契约判定：复用阶段一的规则，不另写一套")
    contract = AcceptanceContract(
        task_id="demo-8",
        required=(
            RequiredCheck.UNIT_TESTS_PASS,
            RequiredCheck.LINT_PASS,
            RequiredCheck.SECURITY_SCANS_PASS,
        ),
        forbidden=(ForbiddenRule.SECRETS_IN_DIFF,),
    )
    report = await _verifier().verify(
        SixLayerRequest(
            task_id="demo-8",
            workspace=root,
            contract=contract,
            env=build_verification_env(),
        )
    )
    evaluation = report.contract_evaluation
    if evaluation is None:
        print("❌ 没有拿到契约判定")
        return 1
    print(f"契约通过：{evaluation.passed}")
    for failure in evaluation.failures:
        print(f"  未通过：{failure}")
    for missing in evaluation.missing:
        print(f"  没有产出结果：{missing}")
    if evaluation.passed:
        print("❌ 命中了禁止项却判成了通过")
        return 1
    print("\n✅ 禁止项一票否决，其它层过了也不行")
    return 0


async def _run(args: argparse.Namespace) -> int:
    """跑一遍演示。

    **一步不行就停在那儿** —— 后面的步骤建立在前面的结论上，前面错了
    后面就没有意义。
    """
    root = Path(args.workspace) if args.workspace else Path(tempfile.mkdtemp(prefix="six-layer-"))
    try:
        if _step_build(root):
            return 1
        report = await _step_report(root)
        codes = [
            _step_numbers(report),
            await _step_performance(root),
            await _step_product(root),
            await _step_scope(root),
            await _step_contract(root),
        ]
    finally:
        if not args.workspace:
            shutil.rmtree(root, ignore_errors=True)
    if any(codes):
        return 1
    _banner("完成", "六层各判各的 —— 没有坍缩成一个布尔值")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TASK-P2-014 六层独立验证演示")
    parser.add_argument(
        "--workspace",
        help="用这个目录当工作区（默认建一个临时的，演示结束删掉）",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
