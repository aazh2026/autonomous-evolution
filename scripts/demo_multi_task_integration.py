"""TASK-P2-017 的演示 —— 跨任务集成引擎与冲突检测。

跑法::

    uv run python -m scripts.demo_multi_task_integration
    # 或
    .venv/bin/python scripts/demo_multi_task_integration.py

**这个演示要证明的是 TASK-P2-017 的验收条件**："构造两个各自单测通过但联合
运行时产生符号冲突的任务，集成引擎精准检出并阻断合并"。

所以演示分三步，每一步都在**真 git** 上跑：

**第一步：符号冲突。** 两个任务各自新增一个同名的 ``calculate``，但加在不同
位置 —— git 合得干干净净，Python 也照跑不误（后一个定义把前一个盖掉）。
**引擎要精准检出**（说得出哪个文件、哪个符号、哪两个任务），并且**阻断合并**
—— main 一个提交都不许前进。

**第二步：联合回归。** 两个任务各自都是绿的，合在一起不绿（各自带着一份与
main 对不上的期望）。**这就是 R-057 说的那件事**，也是最难自己发现的一类 ——
每个任务单独看都好好的。

**第三步：都通。** 一批任务真的落到 main 上，而且审批是真的要人批。

**为什么符号冲突要单独演一遍。** 文本冲突 git 自己会报 —— 那一类不需要这
一层。**git 挡不住的那一类才是这一层的存在理由**：静默的错误比报出来的错误
危险。

**不需要数据库，也不需要网络。** 全部都是真的 git 与真的文件。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from core.policy.approval import InMemoryApprovalStore
from harness.approval.gate import ApprovalGate
from harness.integration.multi_task import (
    IntegrationStage,
    MergeStage,
    MultiTaskIntegrationEngine,
    MultiTaskRequest,
    SubprocessIntegrationGit,
    TaskBranch,
)
from harness.verification.contract import AcceptanceContract, RequiredCheck
from harness.verification.engine import CheckSpec, VerificationEngine, VerificationPlan

#: 这一批的标识。
BATCH = "demo-batch"

#: 集成分支名。**必须带 ``integration/`` 前缀** —— 引擎只复位带这个前缀的
#: 分支，别的分支不是草稿。
BRANCH = "integration/demo-batch"

#: ``git rev-list --parents`` 对一个合并提交给出三段：提交号 + 两个父提交号。
MERGE_COMMIT_FIELDS = 3

#: 仓库里的自检脚本。
#:
#: **它收集所有 ``*_expected.txt``，而不是只读一份。** 合并前每个分支各自只
#: 有一份期望文件；合并后多出来的那几份就是"别的分支带来的期望"。两个期望
#: 不一致，就是"合并之后跑不过"的最小复现 —— 而且报错是人话，不是 git 的英文。
_CHECK_SCRIPT = """\
import glob

from dep import f

files = sorted(glob.glob("*_expected.txt"))
if not files:
    raise SystemExit("没有任何期望文件 —— 无从判断")

values = {path: open(path, encoding="utf-8").read().strip() for path in files}
if len(set(values.values())) != 1:
    raise SystemExit(f"期望文件互相矛盾：{values}")

expected = next(iter(values.values()))
actual = str(f())
if actual != expected:
    raise SystemExit(f"dep.f() == {actual}，期望 {expected}")
print("ok")
"""


class DemoError(RuntimeError):
    """演示里有一处对不上。"""


def _banner(title: str, subtitle: str = "") -> None:
    """打一条分节标题。"""
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    if subtitle:
        print(subtitle)


def _git(cwd: Path, *args: str) -> str:
    """跑一条 git 命令，返回 stdout。"""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DemoError(f"git {' '.join(args)} 失败：\n{result.stdout}\n{result.stderr}")
    return result.stdout


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-m", message)


def _green(workspace: Path) -> bool:
    """工作区里的自检过不过。"""
    result = subprocess.run(
        [sys.executable, "-B", "check.py"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _engine() -> MultiTaskIntegrationEngine:
    """真的集成引擎 —— 真的 git、真的子进程、真的审批门。"""
    return MultiTaskIntegrationEngine(
        git=SubprocessIntegrationGit(),
        engine=VerificationEngine(),
        gate=ApprovalGate(InMemoryApprovalStore()),
    )


def _plan() -> VerificationPlan:
    """验证计划 —— 自检命令就是在工作区里跑 ``check.py``。

    ``-B`` 是刻意的：不写字节码，工作区才不会多出 ``__pycache__``。
    """
    return VerificationPlan(
        checks=(
            CheckSpec(
                check=RequiredCheck.UNIT_TESTS_PASS,
                command=(sys.executable, "-B", "check.py"),
            ),
        )
    )


def _task(task_id: str, workspace: Path) -> TaskBranch:
    """一个待集成的任务分支。"""
    return TaskBranch(
        task_id=task_id,
        branch=f"agent/{task_id}",
        contract=AcceptanceContract(task_id=task_id, required=(RequiredCheck.UNIT_TESTS_PASS,)),
        plan=_plan(),
        workspace=workspace,
    )


def _request(
    repo: Path,
    integration: Path,
    tasks: tuple[TaskBranch, ...],
) -> MultiTaskRequest:
    """一次多任务集成请求。"""
    return MultiTaskRequest(
        batch_id=BATCH,
        project_id="demo",
        agent_id="agent-1",
        integration_branch=BRANCH,
        integration_workspace=integration,
        main_workspace=repo,
        tasks=tasks,
        joint_contract=AcceptanceContract(task_id=BATCH, required=(RequiredCheck.UNIT_TESTS_PASS,)),
        joint_plan=_plan(),
        requested_by="agent-1",
        diff_summary="两个任务合到一起",
    )


def _build_repo(root: Path) -> tuple[Path, Path, str]:
    """建一个仓库，返回 (main 工作区, 旧基线提交号, 集成工作区)。

    **旧基线留着有用** —— 第二步要的是"从旧基线拉出来的任务分支"，那才
    能造出"各自都绿、合起来不绿"。
    """
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "ases@example.invalid")
    _git(repo, "config", "user.name", "ASES")
    (repo / "dep.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "mod.py").write_text("def base():\n    return 0\n", encoding="utf-8")
    (repo / "check.py").write_text(_CHECK_SCRIPT, encoding="utf-8")
    (repo / "base_expected.txt").write_text("1\n", encoding="utf-8")
    _commit(repo, "初始提交")
    base = _git(repo, "rev-parse", "HEAD").strip()

    # main 往前走一步：依赖改成返回 2，期望文件同步跟上。**main 自己得是绿的。**
    (repo / "dep.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    (repo / "base_expected.txt").write_text("2\n", encoding="utf-8")
    _commit(repo, "main 改依赖")

    _git(repo, "branch", BRANCH)
    integration = root / "integration"
    _git(repo, "worktree", "add", str(integration), BRANCH)
    return repo, integration, base


def _worktree(repo: Path, root: Path, branch: str, start: str) -> Path:
    """从 ``start`` 拉一个分支，检出成独立工作区。"""
    _git(repo, "branch", branch, start)
    worktree = root / branch.replace("/", "-")
    _git(repo, "worktree", "add", str(worktree), branch)
    return worktree


async def _step_symbol_conflict(root: Path) -> None:
    """第一步 —— 符号冲突：检得出，挡得住。"""
    _banner("第一步：符号冲突（git 挡不住的那一类）")
    repo, integration, head = _build_repo(root)

    task_a = _worktree(repo, root, "agent/task-a", head)
    task_b = _worktree(repo, root, "agent/task-b", head)
    # 两个任务都在 mod.py 里新增一个同名的 calculate，但**加在不同位置** ——
    # 于是 git 合得干干净净，而合出来的文件里有两个 calculate。
    (task_a / "mod.py").write_text(
        "def calculate():\n    return 1\n\n\ndef base():\n    return 0\n", encoding="utf-8"
    )
    _commit(task_a, "任务 A 加 calculate")
    (task_b / "mod.py").write_text(
        "def base():\n    return 0\n\n\ndef calculate():\n    return 2\n", encoding="utf-8"
    )
    _commit(task_b, "任务 B 加 calculate")

    print(f"任务 A 单独跑：{'绿' if _green(task_a) else '红'}")
    print(f"任务 B 单独跑：{'绿' if _green(task_b) else '红'}")
    if not (_green(task_a) and _green(task_b)):
        raise DemoError("两个任务各自应该都是绿的 —— 否则证明不了什么")

    before = _git(repo, "rev-parse", "HEAD").strip()
    engine = _engine()
    request = _request(repo, integration, (_task("task-a", task_a), _task("task-b", task_b)))

    outcome = await engine.integrate(request)

    print(f"\n集成结论：{outcome.stage}")
    for reason in outcome.reasons:
        print(f"  · {reason}")
    if outcome.stage is not IntegrationStage.SYMBOL_CONFLICTED:
        raise DemoError(f"应该检出符号冲突，实际是 {outcome.stage}")
    if not outcome.conflicts:
        raise DemoError("检出冲突了，却说不出是哪个符号")
    for conflict in outcome.conflicts:
        print(
            f"\n冲突：{conflict.path} 里的 {conflict.name}"
            f" —— 任务 {'、'.join(conflict.tasks)} 各自定义了一份"
        )

    after = _git(repo, "rev-parse", "HEAD").strip()
    print(f"\nmain 的 HEAD：{before[:12]} → {after[:12]}")
    if before != after:
        raise DemoError("main 前进了 —— 冲突的批次不许合进 main")
    print("main 一个提交都没前进 ✅")


async def _step_joint_regression(root: Path) -> None:
    """第二步 —— 联合回归：各自都绿，合起来不绿。"""
    _banner("第二步：联合回归（R-057 —— 单任务通过 ≠ 集成后通过）")
    repo = root / "repo"
    integration = root / "integration"
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD").strip()

    # 两个任务都从**旧基线**拉出来 —— 它们的期望文件说的是旧值。
    task_c = _worktree(repo, root, "agent/task-c", base)
    task_d = _worktree(repo, root, "agent/task-d", base)
    (task_c / "c_expected.txt").write_text("1\n", encoding="utf-8")
    _commit(task_c, "任务 C 加自己的期望")
    (task_d / "d_expected.txt").write_text("1\n", encoding="utf-8")
    _commit(task_d, "任务 D 加自己的期望")

    print(f"任务 C 单独跑：{'绿' if _green(task_c) else '红'}")
    print(f"任务 D 单独跑：{'绿' if _green(task_d) else '红'}")
    if not (_green(task_c) and _green(task_d)):
        raise DemoError("两个任务各自应该都是绿的 —— 否则证明不了什么")

    before = _git(repo, "rev-parse", "HEAD").strip()
    engine = _engine()
    request = _request(repo, integration, (_task("task-c", task_c), _task("task-d", task_d)))

    outcome = await engine.integrate(request)

    print(f"\n集成结论：{outcome.stage}")
    for reason in outcome.reasons:
        print(f"  · {reason}")
    if outcome.stage is not IntegrationStage.REGRESSION_FAILED:
        raise DemoError(f"应该被联合回归挡下，实际是 {outcome.stage}")
    print(f"已汇入集成分支的任务：{'、'.join(outcome.integrated_tasks)}")

    after = _git(repo, "rev-parse", "HEAD").strip()
    if before != after:
        raise DemoError("main 前进了 —— 没过的批次不许合进 main")
    print("main 一个提交都没前进 ✅")

    # 清场：集成分支拨回 main 原处。
    if _git(integration, "rev-parse", "HEAD").strip() != after:
        raise DemoError("集成分支没有拨回 main 原处")
    print("集成分支已拨回 main 原处 ✅")


async def _step_happy_path(root: Path) -> None:
    """第三步 —— 都通：一批任务真的落到 main 上。"""
    _banner("第三步：一批任务落到 main（审批真的要人批）")
    repo = root / "repo"
    integration = root / "integration"
    head = _git(repo, "rev-parse", "HEAD").strip()

    task_e = _worktree(repo, root, "agent/task-e", head)
    task_f = _worktree(repo, root, "agent/task-f", head)
    (task_e / "e_feature.txt").write_text("任务 E 的功能\n", encoding="utf-8")
    (task_e / "e_expected.txt").write_text("2\n", encoding="utf-8")
    _commit(task_e, "任务 E 加功能")
    (task_f / "f_feature.txt").write_text("任务 F 的功能\n", encoding="utf-8")
    (task_f / "f_expected.txt").write_text("2\n", encoding="utf-8")
    _commit(task_f, "任务 F 加功能")

    engine = _engine()
    request = _request(repo, integration, (_task("task-e", task_e), _task("task-f", task_f)))

    outcome = await engine.integrate(request)
    print(f"集成结论：{outcome.stage}")
    if outcome.stage is not IntegrationStage.INTEGRATED:
        raise DemoError(f"应该集成成功，实际是 {outcome.stage}")
    if outcome.integration_commit is None:
        raise DemoError("集成成功了，却拿不到集成分支的提交号")
    print(f"集成分支的提交：{outcome.integration_commit[:12]}")

    waiting = await engine.merge_to_main(request, integration_commit=outcome.integration_commit)
    print(f"合并结论：{waiting.stage}")
    if waiting.stage is not MergeStage.AWAITING_APPROVAL:
        raise DemoError(f"合并到 main 需要人批，实际是 {waiting.stage}")
    print("→ 停在等审批上：引擎不推到底，等人表态")

    await engine.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
    print("→ alice 批了")

    merge = await engine.merge_to_main(request, integration_commit=outcome.integration_commit)
    print(f"合并结论：{merge.stage}")
    if merge.stage is not MergeStage.MERGED:
        raise DemoError(f"应该合进 main，实际是 {merge.stage}")

    if not (repo / "e_feature.txt").exists() or not (repo / "f_feature.txt").exists():
        raise DemoError("任务的功能没落到 main 上")
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    print(f"main 的新提交有 {len(parents) - 1} 个父提交（合并提交）")
    if len(parents) != MERGE_COMMIT_FIELDS:
        raise DemoError("main 上的新提交不是合并提交")
    if not _green(repo):
        raise DemoError("main 合完之后自检没过")
    print("main 合完之后自检仍然是绿的 ✅")


async def _run(args: argparse.Namespace) -> int:
    """跑一遍演示。"""
    root = Path(args.workspace) if args.workspace else Path(tempfile.mkdtemp(prefix="multi-task-"))
    root.mkdir(parents=True, exist_ok=True)
    try:
        await _step_symbol_conflict(root)
        await _step_joint_regression(root)
        await _step_happy_path(root)
    except DemoError as error:
        print(f"\n❌ {error}")
        return 1
    finally:
        if not args.workspace:
            shutil.rmtree(root, ignore_errors=True)
    _banner("完成", "检得出、挡得住、落得下")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TASK-P2-017 跨任务集成引擎与冲突检测演示")
    parser.add_argument(
        "--workspace",
        help="用这个目录当根目录（默认建一个临时的，演示结束删掉）",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
