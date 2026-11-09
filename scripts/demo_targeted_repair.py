"""TASK-P2-015 的演示 —— 自动失败诊断与定向修复。

跑法::

    uv run python -m scripts.demo_targeted_repair
    # 或
    .venv/bin/python scripts/demo_targeted_repair.py

**这个演示要证明的是 TASK-P2-015 的验收条件**："遇到不同类型验证失败时，
自动定位失败原因并生成 Targeted 修复指令，**修复次数与预算严格受全局配额
限制**"。

所以演示的重点有两条：

**第一条：同一个"必检项没过"能拆成好几种错。** 阶段一的
:class:`core.task.repair.FailureKind` 只有七类，其中"必检项没过"这一类
里装着语法错、依赖缺失、类型不匹配、断言失败、测试自身出错 —— **改法
完全不一样**。演示里真的起 ruff / mypy / pytest 跑一遍，让它们各自坏在
不同的地方，然后看分类结论是不是真的分开了。

**第二条：拆开了，还得修得起。** 分类说"改这三行"，但**能不能修、修几次、
花多少**是另一回事 —— 那由全局账本说了算。演示里两个任务各修一次、上限
是 1，第二个必须被拒；被拒的理由要说得清撞的是哪一条上限。

**为什么配额是全局的，而不是每个任务各一份。** 每任务独立的上限不是上
限，是配额：两个任务各修 3 次，看着都没超，合起来就是 6 次，而上限说的
是 5 次。

**不能自动修的压根不占额度。** 没修就占一格，等于凭空少了一截却没人真
的修过 —— 演示最后一步专门验这一条。

**不需要数据库，也不需要网络。** 全部命令都是真的起进程跑本地工具。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from agents.diagnosis.failure_classifier import (
    Classification,
    FailureType,
    ToolFailure,
    ToolOutput,
    classify,
    tool_failure,
)
from core.task.repair import FailureKind, InMemoryRepairLedger, RepairLimits
from core.task.state_machine import TaskState
from orchestration.repair import RepairCoordinator, RepairRequest
from orchestration.targeted_repair import TargetedRepairPlanner

#: 全局修复次数上限。**演示里故意设成 1** —— 那样第二次必然被拒。
MAX_ATTEMPTS = 1

#: 全局成本上限（演示里够用，不设成瓶颈 —— 瓶颈要单独看）。
MAX_COST = Decimal("10.00")

#: 一个项目 —— 额度是按项目共享的。
PROJECT = "demo-project"


class DemoError(RuntimeError):
    """演示里某个前提不成立。"""


def _banner(step: str, title: str) -> None:
    """一节的分隔线。"""
    print(f"\n{'─' * 78}\n{step}  {title}\n{'─' * 78}")


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_workspace(root: Path) -> None:
    """造一个坏在六个不同地方的工作区。

    **每个坏法都放在自己的文件里。** 混在一个文件里，工具报的诊断就混在
    一起，分类器挑出哪一个主因就会变成"碰巧" —— 那样演示证明不了"拆得
    开"，只证明得了"当时那个文件里恰好有这一种错"。
    """
    _write(root, "src/pkg/__init__.py", "")
    # 规范违规：导入没用上的东西。
    _write(root, "src/pkg/thing.py", "import os\n\n\ndef f() -> int:\n    return 1\n")
    # 语法错：代码根本解析不了。
    _write(root, "src/pkg/broken.py", "def f(:\n    return 1\n")
    # 类型不匹配：返回的类型不对。
    _write(root, "src/pkg/typed.py", 'def f() -> int:\n    return "not an int"\n')
    # 依赖缺失：模块压根不存在。
    _write(root, "src/pkg/dep.py", "import nonexistent_module_xyz\n")
    # 断言失败：跑起来了，结果不对。
    _write(root, "tests/test_assert.py", "def test_one():\n    assert 1 == 2\n")
    # 测试自身出错：夹具没了，**用例压根没跑起来**。
    _write(
        root, "tests/test_broken_fixture.py", "def test_two(missing_fixture):\n    assert True\n"
    )


def _run_tool(root: Path, command: Sequence[str]) -> tuple[str, int]:
    """真的起一个进程跑工具。

    **返回原始输出与退出码** —— 分类器读的就是这两样。工具的退出码说"它
    觉得自己失败了"，输出说"为什么"。
    """
    completed = subprocess.run(
        list(command),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout + completed.stderr, completed.returncode


def _ruff(root: Path, target: str) -> ToolFailure:
    """真的跑一遍 ruff。"""
    payload, code = _run_tool(
        root,
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--output-format=json",
            target,
        ),
    )
    return tool_failure(
        kind=FailureKind.CHECK_FAILED,
        tool="ruff",
        output=ToolOutput(payload, code),
        root=str(root.resolve()),
    )


def _mypy(root: Path, target: str) -> ToolFailure:
    """真的跑一遍 mypy。"""
    payload, code = _run_tool(
        root,
        (
            sys.executable,
            "-m",
            "mypy",
            "--output=json",
            "--no-site-packages",
            target,
        ),
    )
    return tool_failure(
        kind=FailureKind.CHECK_FAILED,
        tool="mypy",
        output=ToolOutput(payload, code),
        root=str(root.resolve()),
    )


def _pytest(root: Path, target: str) -> ToolFailure:
    """真的跑一遍 pytest —— **读它的 JUnit XML**，不是读它的终端输出。"""
    payload, code = _run_tool(
        root,
        (
            sys.executable,
            "-m",
            "pytest",
            target,
            "-q",
            "-p",
            "no:cacheprovider",
            "--junitxml=report.xml",
        ),
    )
    report = root / "report.xml"
    xml = report.read_text(encoding="utf-8") if report.exists() else payload
    return tool_failure(
        kind=FailureKind.CHECK_FAILED,
        tool="pytest",
        output=ToolOutput(xml, code),
        root=str(root.resolve()),
    )


def _step_build(root: Path) -> int:
    _banner("第 1 步", "造一个坏在六个不同地方的工作区")
    _build_workspace(root)
    print(f"工作区：{root}")
    print("  src/pkg/thing.py            —— 一处未使用的导入（规范违规）")
    print("  src/pkg/broken.py           —— 一处语法错")
    print("  src/pkg/typed.py            —— 返回值类型不匹配")
    print("  src/pkg/dep.py              —— 依赖缺失")
    print("  tests/test_assert.py        —— 一条断言失败")
    print("  tests/test_broken_fixture.py —— 夹具没了，用例压根没跑起来")
    return 0


def _step_classify(root: Path) -> list[Classification]:
    """真的跑工具、真的归类 —— **六种错要给出六种结论**。"""
    _banner("第 2 步", "真的跑工具，把机器可读输出收进来")
    failures: list[tuple[str, ToolFailure]] = [
        ("规范违规", _ruff(root, "src/pkg/thing.py")),
        ("语法错", _ruff(root, "src/pkg/broken.py")),
        ("类型不匹配", _mypy(root, "src/pkg/typed.py")),
        ("依赖缺失", _mypy(root, "src/pkg/dep.py")),
        ("断言失败", _pytest(root, "tests/test_assert.py")),
        ("测试自身出错", _pytest(root, "tests/test_broken_fixture.py")),
    ]
    classifications: list[Classification] = []
    for expected, failure in failures:
        classification = classify(failure)
        classifications.append(classification)
        found = failure.findings
        print(f"\n【{expected}】工具：{failure.tool}（退出码 {failure.exit_code}）")
        print(f"  机器可读输出读出 {len(found)} 条发现")
        print(f"  归类：{classification.failure_type.value}｜{classification.category.value}")
        print(f"  理由：{classification.reason}")
        if classification.evidence:
            print(f"  证据：{'; '.join(classification.evidence)}")
    return classifications


def _step_distinct(classifications: list[Classification]) -> int:
    """T2 —— 拆开了没有。"""
    _banner("第 3 步", "拆开了没有：六种坏法给出六个不同的结论")
    types = {classification.failure_type for classification in classifications}
    print(f"出现的细类：{len(types)} 种 / 共 {len(classifications)} 次失败")
    for failure_type in sorted(types, key=lambda item: item.value):
        print(f"  {failure_type.value}")

    problems: list[str] = []
    if len(types) != len(classifications):
        problems.append(f"{len(classifications)} 次失败只分出 {len(types)} 种结论")
    for classification in classifications:
        if classification.failure_type is FailureType.UNKNOWN:
            problems.append(f"「{classification.reason}」被判成了原因不明")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        return 1
    print("\n✅ 每种坏法都落进了自己的类别，没有被笼统的「必检项没过」吞掉")
    return 0


def _step_instruction(classifications: list[Classification]) -> int:
    """施工说明要说到行、说到改什么。"""
    _banner("第 4 步", "定向施工说明：说到行、说到改什么")
    for classification in classifications:
        print(f"\n【{classification.failure_type.value}】")
        print(f"  {classification.targeted_instruction()}")

    problems: list[str] = []
    for classification in classifications:
        instruction = classification.targeted_instruction()
        if not instruction.strip():
            problems.append(f"{classification.failure_type.value} 的施工说明是空的")
        if classification.targets and "：" not in instruction:
            problems.append(f"{classification.failure_type.value} 的施工说明没说要改什么")
    lint = next(
        (item for item in classifications if item.failure_type is FailureType.LINT_VIOLATION),
        None,
    )
    if lint is None:
        problems.append("没有拿到规范违规那一类")
    else:
        instruction = lint.targeted_instruction()
        if "src/pkg/thing.py:1" not in instruction:
            problems.append(f"规范违规的施工说明里没有文件与行号：{instruction}")
        if "F401" not in instruction:
            problems.append(f"规范违规的施工说明里没有工具自己的码：{instruction}")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        return 1
    print("\n✅ 每条施工说明都落到了具体的文件与行上")
    return 0


def _request(task_id: str) -> RepairRequest:
    return RepairRequest(
        task_id=task_id,
        project_id=PROJECT,
        current_state=TaskState.VERIFYING,
        cost=Decimal("1.00"),
    )


def _planner() -> TargetedRepairPlanner:
    """一台协调器 —— **一个项目一本账**。"""
    coordinator = RepairCoordinator(
        InMemoryRepairLedger(),
        limits=RepairLimits(max_attempts=MAX_ATTEMPTS, max_cost=MAX_COST),
    )
    return TargetedRepairPlanner(coordinator)


async def _step_quota(root: Path) -> int:
    """全局配额 —— 上限是 1，第二个任务必须被拒。"""
    _banner("第 5 步", "全局配额：修得了第一次，第二次就没额度了")
    planner = _planner()
    failure = _ruff(root, "src/pkg/thing.py")

    first = await planner.plan(_request("T-1"), failure=failure)
    print(f"第一个任务：准修 = {first.allowed}｜{first.outcome.reason}")
    print(f"  施工说明：{first.instruction}")

    second = await planner.plan(_request("T-2"), failure=failure)
    print(f"\n第二个任务：准修 = {second.allowed}｜{second.outcome.reason}")
    print(f"  撞到的上限：{second.outcome.limit}")
    print(f"  施工说明：{second.instruction or '（没有 —— 被拒了就不排任务）'}")

    problems: list[str] = []
    if not first.allowed:
        problems.append("第一次修复没有被批准")
    if first.instruction == "":
        problems.append("第一次修复没有排任务")
    if second.allowed:
        problems.append(f"上限是 {MAX_ATTEMPTS}，第二个任务却批了")
    if second.outcome.limit is None:
        problems.append("被拒了却没说是撞了哪一条上限")
    if second.instruction != "":
        problems.append("被拒了还排了任务 —— 那就等于修了两遍")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        return 1
    print(f"\n✅ 上限 {MAX_ATTEMPTS} 是**全局**的那一份，不是每个任务各一份")
    return 0


async def _step_no_quota_for_human(root: Path) -> int:
    """不能自动修的压根不占额度。"""
    _banner("第 6 步", "不能自动修的压根不占额度")
    planner = _planner()
    failure = _ruff(root, "src/pkg/thing.py")

    cancelled = await planner.plan(
        _request("T-1"),
        failure=ToolFailure(kind=FailureKind.CANCELLED),
    )
    print(f"先来一个已取消的：准修 = {cancelled.allowed}｜{cancelled.outcome.reason}")
    print(f"  撞到的上限：{cancelled.outcome.limit}")
    print(f"  问过账本没有：{cancelled.outcome.usage is None}")

    after = await planner.plan(_request("T-2"), failure=failure)
    print(f"\n再来一个能修的：准修 = {after.allowed}｜{after.outcome.reason}")

    third = await planner.plan(_request("T-3"), failure=failure)
    print(f"\n再来一个能修的：准修 = {third.allowed}｜{third.outcome.reason}")
    print(f"  撞到的上限：{third.outcome.limit}")

    problems: list[str] = []
    if cancelled.allowed:
        problems.append("已取消的任务被判成了可自动修")
    if cancelled.outcome.usage is not None:
        problems.append("已取消的任务去问过账本 —— 它压根不该占额度")
    if not after.allowed:
        problems.append("额度被一个压根没修的任务吃掉了")
    if third.allowed:
        problems.append(f"上限是 {MAX_ATTEMPTS}，第二个能修的任务却批了")
    if third.outcome.limit is None:
        problems.append("额度用完了却没说是撞了哪一条上限")
    for problem in problems:
        print(f"❌ {problem}")
    if problems:
        return 1
    print("\n✅ 没修就占一格的事没有发生 —— 额度只留给真的要修的")
    return 0


async def _run(args: argparse.Namespace) -> int:
    """跑一遍演示。

    **一步不行就停在那儿** —— 后面的步骤建立在前面的结论上，前面错了
    后面就没有意义。
    """
    root = (
        Path(args.workspace)
        if args.workspace
        else Path(tempfile.mkdtemp(prefix="targeted-repair-"))
    )
    try:
        if _step_build(root):
            return 1
        classifications = _step_classify(root)
        codes = [
            _step_distinct(classifications),
            _step_instruction(classifications),
            await _step_quota(root),
            await _step_no_quota_for_human(root),
        ]
    finally:
        if not args.workspace:
            shutil.rmtree(root, ignore_errors=True)
    if any(codes):
        return 1
    _banner("完成", "拆得开、说得清、修得起")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TASK-P2-015 自动失败诊断与定向修复演示")
    parser.add_argument(
        "--workspace",
        help="用这个目录当工作区（默认建一个临时的，演示结束删掉）",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
