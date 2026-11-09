"""M1 单轮闭环的**本地执行器** —— 不联网、不调模型、按脚本改代码。

设计依据：docs/tasks.md TASK-P1-081（"手工注入一个 Candidate，走完 决策 →
任务 → 执行 → 独立验证 → 记录 全链"）。

**为什么 M1 用它，而不是真的 Coding Agent。** M1 要证明的是**链路**：
决策 → 任务 → 执行 → 独立验证 → 记录，每一步都落库、每一步都能查。
链路上真正需要判断的地方（ Critic 审查、契约判定、合并闸门）全部是真的；
唯一被替换掉的是"写代码的那一段" —— 因为它要么需要 API 凭据，要么需要
网络，两者都会让"这条链能不能跑通"这个问题依赖于环境，而不是依赖于代码。

**它做的事与真执行器一样。** 建会话、拿工作区、按指令改文件、提交、
报告改了哪些文件、报一次执行的成本与时长。它**不**做的事只有一件：
不自己决定改什么 —— 改什么由 :data:`PATCHES` 说死，而那份清单在下面
一眼能看完。**换掉执行器不改变被验证的东西**：改完之后跑的仍是
真 pytest、真的独立验证引擎、真的验收契约。

**它报的成本是记账用的，不是模型账单。** 真执行器报的是模型调用换算出来的
钱；这里报的是一个**写死的名义成本**。这样 T2 的"耗时与成本落在基线 2 倍
以内"才有东西可比 —— 而这个数在 :data:`NOMINAL_COST` 里，**写在代码里而不是
藏在某次运行里**，免得换个环境就换了个答案。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from core.task.outcome import ExecutionStatus
from executors.base import (
    AgentSession,
    ExecutionArtifacts,
    ExecutionHandle,
    ExecutionMetrics,
    ExecutionResult,
    ExecutionTask,
    SelfAssessment,
    SessionConfig,
    build_idempotency_key,
)

#: 这个执行器的名字 —— 落进会话行与事件载荷，事后查得出"是谁干的"。
EXECUTOR_NAME: Final[str] = "m1-local-patcher"

#: 名义成本（USD）。**写死是为了可比** —— T2 要拿它跟基线比。
NOMINAL_COST: Final[Decimal] = Decimal("0.631445")

#: 名义币种。
NOMINAL_CURRENCY: Final[str] = "USD"


@dataclass(frozen=True, slots=True)
class Patch:
    """一处改动 —— 改哪个文件、把哪一段换成哪一段。

    **用"整段替换"而不是"行号编辑"。** 行号会漂：文件一改，后面的行号
    就不对了，而一个"改错了行号"的补丁会安静地把别的地方改坏。这里记的是
    **原文片段**与**替换后的片段** —— 原文不在就报错，绝不"改个大概"。

    **``old`` 为空表示"新建这个文件"。** 新文件没有原文可匹配，所以
    "原文为空"就是"这个文件还不存在"的约定 —— 而文件真的存在却要新建时，
    那份补丁会被拒掉（见 :meth:`apply`）：一个说不清是"新建"还是"清空
    内容"的补丁，不该被默默执行。
    """

    path: str
    old: str
    new: str

    def apply(self, root: Path) -> bool:
        """把这一处改动落到 ``root`` 下。**原文不在就抛错。**"""
        target = root / self.path
        if not target.exists():
            if self.old:
                raise LookupError(f"{self.path} 不存在 —— 补丁对不上")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.new, encoding="utf-8")
            return True
        if not self.old:
            raise LookupError(f"{self.path} 已经存在 —— 这份补丁却要新建它")
        text = target.read_text(encoding="utf-8")
        if self.old not in text:
            raise LookupError(f"{self.path} 里找不到要替换的那一段 —— 补丁对不上")
        target.write_text(text.replace(self.old, self.new, 1), encoding="utf-8")
        return True


#: PILOT-001 的修复 —— 三处改动。
#:
#: **一、加空输入守卫。** ``stats`` 分支在没有数字时不该把异常抛到用户脸上。
#: **二、把复现用例搬回 ``tests/``。** 缺陷修好了，复现用例就该变成常规
#: 回归 —— 留在 ``known_issues/`` 里，它下一次跑会变成 XPASS，而 strict 的
#: xfail 把 XPASS 算作失败（见 ``samples/pilot_repo/known_issues/`` 的说明）。
#: **三、从复现清单里删掉那一条。** 它不再是"还没修的缺陷"了。
#:
#: 第二处补丁的原文里那一行 ``@pytest.mark.xfail(...)`` 顶上写着 ``'''\`` ——
#: 那是行尾续行：它让那一行**在源文件里独占一行**，因为这一行里的中文
#: 按显示宽度算已经超过 100 列，而补丁原文必须与试点仓库逐字节相同，
#: 不能为了排版改写它。
PATCHES: Final[tuple[Patch, ...]] = (
    Patch(
        path="src/tinylib/cli.py",
        old="""    if args.command == "stats":
        values = _numbers(args.values)
        if args.percentile is not None:""",
        new="""    if args.command == "stats":
        values = _numbers(args.values)
        if not values:
            print("用法：tinylib stats <逗号分隔的数>", file=sys.stderr)
            return 2
        if args.percentile is not None:""",
    ),
    Patch(
        path="known_issues/test_known_issues.py",
        old='''\
@pytest.mark.xfail(strict=True, reason="PILOT-001：空输入时 CLI 抛未捕获的 ValueError")
def test_the_cli_reports_an_empty_stats_input_instead_of_crashing() -> None:
    """空输入应当是一句用法错误 + 退出码，不是 traceback。"""
    assert cli.main(["stats", ""]) == 2


''',
        new="",
    ),
    Patch(
        path="tests/test_cli.py",
        old="",
        new='''"""CLI 入口的回归用例 —— 修好的缺陷回到这里成为常规回归。"""

from __future__ import annotations

from tinylib import cli


def test_the_cli_reports_an_empty_stats_input_instead_of_crashing() -> None:
    """空输入应当是一句用法错误 + 退出码，不是 traceback。

    这条用例原本是 ``known_issues/`` 里的 PILOT-001 复现；缺陷修好之后
    它搬回 ``tests/``，从此是常规回归的一部分。
    """
    assert cli.main(["stats", ""]) == 2
''',
    ),
)


class M1PatchExecutor:
    """按固定补丁改代码的执行器。

    **它实现的是 :class:`orchestration.loop_a.ExecutorPort` 那四个方法** ——
    编排层用的就这些，多一个少一个都不影响。

    **它不读指令。** 施工说明对它没有约束力：改什么由 :data:`PATCHES` 说死。
    这不是缺陷，是这一版的定位 —— M1 演的是链路，不是"读懂指令"。
    """

    __slots__ = ("_patches", "_sessions", "_started_at", "_tasks")

    def __init__(self, patches: tuple[Patch, ...] = PATCHES) -> None:
        self._patches = patches
        self._sessions: dict[str, tuple[str, str]] = {}
        self._started_at: dict[str, datetime] = {}
        self._tasks: dict[str, str] = {}

    @property
    def name(self) -> str:
        """执行器名字。"""
        return EXECUTOR_NAME

    async def create_session(self, config: SessionConfig) -> AgentSession:
        """开一次会话 —— 记下工作区与分支。"""
        session_id = f"m1-session-{len(self._sessions) + 1:03d}"
        self._sessions[session_id] = (config.workspace, config.branch or "")
        self._started_at[session_id] = datetime.now(UTC)
        return AgentSession(
            session_id=session_id,
            executor=EXECUTOR_NAME,
            workspace=config.workspace,
            created_at=self._started_at[session_id],
        )

    async def execute(self, task: ExecutionTask) -> ExecutionHandle:
        """按补丁改代码并提交。

        **改完必须提交。** 合并闸门合的是**提交**，不是工作区里的散改动 ——
        没有提交就没有可合的东西，而那一步会报"没有可合并的提交"，看不出
        是执行器没干活还是别的地方出了问题。
        """
        workspace, _branch = self._sessions[task.session_id]
        root = Path(workspace)
        for patch in self._patches:
            patch.apply(root)
        idempotency_key = build_idempotency_key(task.task_id, task.attempt)
        _git(root, "add", "-A")
        _git(root, "commit", "-m", f"{task.title}\n\n幂等键：{idempotency_key}")
        self._tasks[task.session_id] = task.task_id
        return ExecutionHandle(
            session_id=task.session_id,
            task_id=task.task_id,
            idempotency_key=idempotency_key,
            attempt=task.attempt,
            accepted_at=datetime.now(UTC),
        )

    async def collect_result(self, session_id: str) -> ExecutionResult:
        """收结果 —— 报告改了哪些文件、提交在哪、花了多少钱。"""
        workspace, branch = self._sessions[session_id]
        root = Path(workspace)
        started = self._started_at.get(session_id, datetime.now(UTC))
        duration = Decimal(str((datetime.now(UTC) - started).total_seconds()))
        commit = _git(root, "rev-parse", "HEAD").strip()
        changed = _git(root, "show", "--name-only", "--pretty=format:", "HEAD")
        changed_files = len([line for line in changed.splitlines() if line.strip()])
        task_id = self._tasks.get(session_id, "")
        return ExecutionResult(
            task_id=task_id,
            executor=EXECUTOR_NAME,
            status=ExecutionStatus.COMPLETED,
            artifacts=ExecutionArtifacts(branch=branch, commit=commit, changed_files=changed_files),
            execution=ExecutionMetrics(
                duration=duration,
                tool_calls=len(self._patches),
                cost=NOMINAL_COST,
                currency=NOMINAL_CURRENCY,
            ),
            self_assessment=SelfAssessment(completed=True),
            idempotency_key=build_idempotency_key(task_id, 1) if task_id else "",
        )


def _git(root: Path, *args: str) -> str:
    """跑一条 git 命令。**不开 shell** —— 参数是外部输入。"""
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{completed.stderr.strip()}")
    return completed.stdout


__all__ = ["EXECUTOR_NAME", "NOMINAL_COST", "PATCHES", "M1PatchExecutor", "Patch"]
