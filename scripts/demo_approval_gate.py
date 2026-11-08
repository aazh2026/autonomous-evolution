"""TASK-P1-046 的 T3 演示 —— 走一次"任务验证通过 → 停在审批门 → 人工批准 → 合并到 main"。

跑法::

    uv run python -m scripts.demo_approval_gate
    # 或
    make demo-approval

**这个演示里没有假的东西。** 它真的建一个 git 仓库、真的在任务分支上跑测试、
真的用策略层判定、真的用 ``git merge`` 合并、真的在合并后的 main 上再跑一遍
测试。演示里唯一被模拟的是"人" —— 那一按必须由人来按，而演示不可能真等一个人
来按。

**为什么值得看。** 审批门最容易出的错不是"门不工作"，而是**门被绕过了还没人
发现**：某个路径直接调了合并，没走策略判定。这个演示把那条路径摆在明面上 ——
如果哪一步的输出显示"没有审批记录也放行了"，那才是要担心的事。

演示走两条路：

- **正路**：验证通过 → 停在审批门 → 人批准 → 合并进 main → main 上再验一遍。
- **岔路**（第二段）：验证通过 → 停在审批门 → 人拒绝 → 任务进 ``BLOCKED`` →
  **没有合并**。

岔路才是正路能成立的前提：只有"拒绝会真的挡住合并"被演示过，"批准就放行"
才不是一句空话。
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

from core.policy.approval import ApprovalOutcome, InMemoryApprovalStore
from core.policy.autonomy import DecisionKind, Operation, PolicyManager, TaskContext
from core.task.state_machine import (
    TaskAction,
    TaskState,
    TransitionContext,
    apply_action,
)
from harness.approval.gate import ApprovalGate
from harness.verification.contract import (
    AcceptanceContract,
    ArtifactKind,
    RequiredCheck,
)
from harness.verification.engine import (
    CheckSpec,
    VerificationEngine,
    VerificationPlan,
    VerificationRequest,
)

TASK_ID = "TASK-P1-046-DEMO"
BRANCH = "task/TASK-P1-046-demo"
APPROVER = "alice（人）"


def _git(repo: Path, *args: str) -> str:
    """跑一条 git 命令。"""
    result = subprocess.run(
        ("git", *args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def _git_succeeded(repo: Path, *args: str) -> bool:
    """跑一条 git 命令，只问"成没成"。

    用于**退出码本身就是答案**的命令 —— 比如 ``git merge-base
    --is-ancestor``：它返回 1 就表示"不是祖先"，那不是错误，是结论。
    """
    result = subprocess.run(
        ("git", *args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _banner(step: str, title: str) -> None:
    print(f"\n{'─' * 78}\n{step}  {title}\n{'─' * 78}")


def _make_repo(root: Path) -> Path:
    """造一个真实的 git 仓库：main 分支，一个会通过的测试。"""
    repo = root / "demo-repo"
    (repo / "tests").mkdir(parents=True)
    _git(root, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "demo@ases.local")
    _git(repo, "config", "user.name", "ASES Demo")

    (repo / "README.md").write_text("# 演示项目\n\n第一行。\n", encoding="utf-8")
    (repo / "tests" / "test_demo.py").write_text(
        '''"""演示项目的基线测试。"""


def test_readme_has_a_title() -> None:
    """README 第一行是标题。"""
    from pathlib import Path

    readme = Path(__file__).resolve().parents[1] / "README.md"
    assert readme.read_text(encoding="utf-8").startswith("# 演示项目")
''',
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "初始状态：README 与基线测试")
    return repo


def _make_task_branch(repo: Path) -> None:
    """在任务分支上改一行 README，并补一个测试。

    改动要**小而真实** —— 演示的价值在于"人看到的就是要合并的那份改动"，
    而一个空仓库或一个凭空捏造的 diff 说明不了这件事。
    """
    _git(repo, "checkout", "-b", BRANCH)
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\n第二行：任务分支加的。\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_new_line.py").write_text(
        '''"""任务分支带来的新断言。"""


def test_readme_has_the_new_line() -> None:
    """README 里有任务分支加的那一行。"""
    from pathlib import Path

    readme = Path(__file__).resolve().parents[1] / "README.md"
    assert "第二行：任务分支加的。" in readme.read_text(encoding="utf-8")
''',
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "给 README 加一行，并补一个对应测试")


def _diff(repo: Path) -> str:
    """任务分支相对 main 的改动。"""
    return _git(repo, "diff", "main...HEAD")


def _diff_summary(repo: Path) -> str:
    """改动摘要 —— 人看到的那一份。"""
    return _git(repo, "diff", "--stat", "main...HEAD").strip()


def _evidence_ref(diff: str) -> str:
    """证据引用。

    **指向改动本身的内容哈希。** 人批的是"这一份 diff"，那么证据就得能唯一指
    回这一份 —— 事后代码变了，这份批准就不再指向任何东西。

    阶段一的证据归档（对象存储里的 URI）由 TASK-P1-048 负责；这里先指回内容
    本身，语义是一样的。
    """
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return f"artifact://sha256:{digest}"


async def _verify(repo: Path) -> bool:
    """在任务分支上跑一遍**真的**独立验证。"""
    contract = AcceptanceContract(
        task_id=TASK_ID,
        required=(RequiredCheck.UNIT_TESTS_PASS,),
        artifacts=(ArtifactKind.TEST_REPORT, ArtifactKind.EXECUTION_TRACE),
    )
    plan = VerificationPlan(
        checks=(
            CheckSpec(
                check=RequiredCheck.UNIT_TESTS_PASS,
                command=(sys.executable, "-m", "pytest", "-q"),
                timeout_seconds=120,
                produces=(ArtifactKind.TEST_REPORT,),
            ),
        ),
    )
    run = await VerificationEngine().verify(
        VerificationRequest(task_id=TASK_ID, workspace=repo, contract=contract, plan=plan)
    )
    report = run.to_report()
    print(f"  验证结论：{report.verdict.value}")
    for reason in report.reasons:
        print(f"    · {reason}")
    print(f"  执行轨迹：{'; '.join(run.traces)}")
    return report.verdict.value == "passed"


def _advance(state: TaskState, action: TaskAction) -> TaskState:
    """走一步状态机迁移。"""
    result = apply_action(
        state,
        action,
        TransitionContext(task_id=TASK_ID, acceptance_contract_bound=True),
    )
    print(f"  任务状态：{result.previous.value} → {result.current.value}")
    return result.current


async def _demo_the_happy_path(repo: Path) -> None:
    """正路 —— 验证通过 → 停在审批门 → 人批准 → 合并进 main。"""
    _banner("[1/6]", "造一个真实的 git 仓库与任务分支")
    _make_task_branch(repo)
    print(f"  仓库：{repo}")
    print(f"  任务分支：{BRANCH}")
    print(f"  改动摘要：{_diff_summary(repo)}")

    _banner("[2/6]", "在任务分支上跑独立验证（真的跑测试）")
    state = TaskState.READY
    state = _advance(state, TaskAction.START)
    state = _advance(state, TaskAction.VERIFY)
    passed = await _verify(repo)
    if not passed:
        print("  验证没过 —— 演示到此为止（门根本没到）。")
        return
    state = _advance(state, TaskAction.PASS)
    state = _advance(state, TaskAction.INTEGRATE)
    print(f"  当前状态：{state.value} —— 任务停在合并之前。")

    _banner("[3/6]", "合并前的策略判定：没有审批记录")
    gate = ApprovalGate(InMemoryApprovalStore())
    context = TaskContext(agent_id="agent-1", task_id=TASK_ID, project_id="demo")
    decision = await PolicyManager.default().authorize(
        Operation.MERGE_TO_MAIN, context, approval=gate
    )
    print(f"  判定：{decision.kind.value}（{decision.reason}）")
    print(f"  审批结论：{decision.approval_outcome}")
    assert decision.kind is DecisionKind.REQUIRE_APPROVAL
    assert decision.approval_outcome is ApprovalOutcome.MISSING
    print("  → 没有审批记录就不放行。合并这一步还没发生。")

    _banner("[4/6]", "发起审批请求（人将看到的内容）")
    diff = _diff(repo)
    summary = _diff_summary(repo)
    request = await gate.request(
        task_id=TASK_ID,
        operation=Operation.MERGE_TO_MAIN,
        diff_summary=summary,
        evidence_ref=_evidence_ref(diff),
        requested_by="agent-1",
    )
    print(f"  请求 id：{request.request_id}")
    print(f"  有效期至：{request.expires_at}")
    print("  人看到的那份改动：")
    for line in diff.splitlines():
        print(f"    {line}")

    _banner("[5/6]", "人批准")
    approved = await gate.approve(request_id=request.request_id, approver=APPROVER)
    print(f"  审批结果：{approved.status.value}（审批人 {approved.decided_by}）")
    decision = await PolicyManager.default().authorize(
        Operation.MERGE_TO_MAIN, context, approval=gate
    )
    print(f"  判定：{decision.kind.value}（{decision.reason}）")
    assert decision.is_allowed is True

    _banner("[6/6]", "合并进 main，并在 main 上再验一遍")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", BRANCH, "-m", "合并 TASK-P1-046-DEMO（已获人工审批）")
    state = _advance(state, TaskAction.MERGE)
    state = _advance(state, TaskAction.COMPLETE)
    print(f"  合并后的 main：{_git(repo, 'log', '--oneline', '-1').strip()}")
    still_green = await _verify(repo)
    print(f"  合并后 main 上再验一遍：{'通过' if still_green else '没通过'}")
    print(f"\n  ✅ 正路走完：{state.value}")


async def _demo_the_rejected_path(repo: Path) -> None:
    """岔路 —— 人拒绝，任务进 BLOCKED，合并不发生。

    **另起一个分支。** 岔路不能复用正路那个分支 —— 正路已经把它合进 main
    了，"合并没有发生"就无从谈起。分开两个分支，两条路各验各的。
    """
    _banner("[附]", "岔路：人拒绝 → 任务进 BLOCKED → 没有合并")
    rejected_branch = f"{BRANCH}-rejected"
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", rejected_branch)
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\n第三行：这次会被人拒绝。\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "一行会被拒绝的改动")
    branch_head = _git(repo, "rev-parse", "HEAD").strip()

    gate = ApprovalGate(InMemoryApprovalStore())
    context = TaskContext(agent_id="agent-1", task_id=TASK_ID, project_id="demo")

    request = await gate.request(
        task_id=TASK_ID,
        operation=Operation.MERGE_TO_MAIN,
        diff_summary=_diff_summary(repo),
        evidence_ref=_evidence_ref(_diff(repo)),
        requested_by="agent-1",
    )
    rejection = await gate.reject(
        request_id=request.request_id,
        approver=APPROVER,
        comment="这行改动没有对应的理由说明",
        task_state=TaskState.INTEGRATING,
    )
    print(f"  拒绝原因：{rejection.reason}")
    assert rejection.blocked is not None
    print(f"  任务状态：{rejection.blocked.previous.value} → {rejection.blocked.current.value}")

    decision = await PolicyManager.default().authorize(
        Operation.MERGE_TO_MAIN, context, approval=gate
    )
    print(f"  判定：{decision.kind.value}（{decision.reason}）")
    print(f"  是人明确拒绝的：{decision.is_rejected_by_human}")
    assert decision.kind is DecisionKind.DENY
    assert decision.is_rejected_by_human is True

    merged = _git_succeeded(repo, "merge-base", "--is-ancestor", branch_head, "main")
    print(f"  被拒分支的提交 {branch_head[:8]} 是否已进入 main：{'是' if merged else '否'}")
    assert not merged, "被拒绝的改动不该出现在 main 上"


async def main() -> int:
    print("=" * 78)
    print("ASES 人工审批门演示（TASK-P1-046）")
    print("=" * 78)
    print("  演示用内存存储；生产路径是数据库实现")
    print("（infrastructure/database/approval.py，跨进程可见）。")
    print("  数据库行为由 tests/integration/test_approval_store.py 验。")

    with tempfile.TemporaryDirectory(prefix="ases-demo-approval-") as tmp:
        root = Path(tmp)
        repo = _make_repo(root)
        await _demo_the_happy_path(repo)
        await _demo_the_rejected_path(repo)

    print("\n" + "=" * 78)
    print("演示结束")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
