"""TASK-P1-065 的反向验证 —— 逐个改坏演练 runner，证明测试抓得住。

做法跟仓库里其它任务的反向验证一样：**改生产代码，然后跑测试，看它红。**
改完一定还原。

**为什么这次改的是测试代码。** 这一次的"生产代码"就是演练 runner 本身 ——
它不在 ``core/`` 里，也不在 ``orchestration/`` 里，它在 ``tests/`` 下。
交付物是 runner 与它的测试，所以"测试能不能抓住 runner 的错误"就是这条任务
自己的验收。

**每一条变异都对着一条具体的设计要求。** 抓不住就说明那条要求没被验到。

**为什么有的变异改的是 ``core/``。** 演练 runner 的价值在于"它真的走了
真的规则" —— 决策规则、评分装配、状态机、闸门。**要是把 ``core/`` 里的规则
改坏了演练照样绿，那这个演练就白演了。** 所以有几条变异故意改的是
``core/evolution/priority.py`` —— 那些规则本来就不该由演练 runner 自己实现，
但演练必须能验出它们被改坏了。

**抓不住的那一条，也要写清楚为什么。** 等价变异（改了但结论没变）不是失败，
但也不能装作没有 —— 写清楚"这条要求没被验到"，比含糊过去强。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/aaronzh/Documents/GitHub/autonomous-evolution")

#: (编号, 目标文件, 说明, 原文, 改后)
MUTANTS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "①",
        "tests/system/replay_runner.py",
        "固定时刻改成墙钟 —— 两次回放就不再一样",
        "REPLAY_TIME: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)",
        "REPLAY_TIME: Final[datetime] = datetime.now(UTC)",
    ),
    (
        "②",
        "tests/system/replay_runner.py",
        "打桩验证一律判通过 —— 修复路径整条不见",
        "            verdict=scripted.verdict,\n",
        "            verdict=VerificationVerdict.PASSED,\n",
    ),
    (
        "③",
        "tests/system/replay_runner.py",
        "打桩执行器每次都取第一份脚本 —— 返修拿到的是首跑的结果",
        "index = min(task.attempt - 1, len(self._attempts) - 1) if self._attempts else 0\n",
        "index = 0\n",
    ),
    (
        "④",
        "tests/system/replay_runner.py",
        "放行凭据不交给路线图 —— 被跳过的那条决策说不清是谁不放行的",
        "        return EvolutionPlan(drafts=drafts, approvals=tuple(approvals))",
        "        return EvolutionPlan(drafts=drafts, approvals=())",
    ),
    (
        "⑤",
        "tests/system/replay_runner.py",
        "否决的那一项不算否决 —— Critic 说的话没人听",
        "                        vetoes=True,\n",
        "                        vetoes=False,\n",
    ),
    (
        "⑥",
        "tests/system/replay_runner.py",
        "可否决检查的校验被跳过 —— 四问也能否决",
        "        if self.veto and self.check not in VETOING_CHECKS:",
        "        if False:",
    ),
    (
        "⑦",
        "tests/system/replay_runner.py",
        "草稿引用的候选不再校验 —— 脚本能引到不存在的东西",
        "            if draft.candidate_id not in known:",
        "            if False:",
    ),
    (
        "⑧",
        "tests/system/replay_runner.py",
        "任务状态迁移不再记 —— 留痕里少了一半",
        'raw = event.payload.get("task_states", ())',
        "raw = ()",
    ),
    (
        "⑨",
        "tests/system/replay_runner.py",
        "失败归类改成要人工 —— 自动修复那条路走不通",
        "            category=self.failure_category,",
        "            category=FailureCategory.NEEDS_HUMAN,",
    ),
    (
        "⑩",
        "tests/system/replay_runner.py",
        "被跳过的决策不再留理由 —— 事后看不出为什么没做",
        "skipped = tuple((item.decision_id, item.reason) for item in state.roadmap.skipped)",
        "skipped = ()",
    ),
    (
        "⑪",
        "core/evolution/priority.py",
        "评分里的置信度不取候选自己的 —— 决策的依据与候选对不上",
        "            confidence=candidate.confidence,",
        '            confidence=Decimal("0.10"),',
    ),
    (
        "⑫",
        "tests/system/replay_runner.py",
        "合并闸门一律判合不上 —— 合入那条路不见了",
        '        return MergeOutcome(merged=True, commit="c0ffee")',
        '        return MergeOutcome(merged=False, commit="c0ffee")',
    ),
)

#: 跑测试的命令。集成测试要一个干净的库 —— 用独立库名，别跟别人的并发跑撞上。
CMD = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "pytest",
    "tests/system/test_replay_runner.py",
    "tests/integration/test_replay.py",
    "-o",
    "addopts=--strict-markers --strict-config -p no:langsmith_plugin",
    "-q",
]


def _run() -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("ASES_TEST_DB_NAME", "ases_test_mine")
    done = subprocess.run(CMD, cwd=ROOT, capture_output=True, text=True, env=env, check=False)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _summary(output: str) -> str:
    """取测试输出最后一行有意义的。"""
    for line in reversed(output.strip().splitlines()):
        if line.strip():
            return line.strip()
    return ""


def main() -> int:
    # 每个目标文件读一次原文 —— 还原时用。
    originals: dict[str, str] = {}
    for _, target, *_ in MUTANTS:
        if target not in originals:
            originals[target] = (ROOT / target).read_text(encoding="utf-8")

    code, output = _run()
    if code != 0:
        print("基线就红了 —— 先修好再谈变异：")
        print(output[-3000:])
        return 1
    print(f"基线绿（{_summary(output)}）")

    caught = 0
    missed: list[str] = []
    try:
        for number, target, why, before, after in MUTANTS:
            original = originals[target]
            if before not in original:
                missed.append(f"{number}（原文没找到，变异没生效：{target}）")
                print(f"{number} 跳过 —— 原文没找到（脚本过期了）")
                continue
            if before == after:
                missed.append(f"{number}（变异与原文相同，等价变异）")
                print(f"{number} 等价变异 —— {why}")
                continue
            path = ROOT / target
            path.write_text(original.replace(before, after, 1), encoding="utf-8")
            try:
                code, output = _run()
            finally:
                path.write_text(original, encoding="utf-8")
            if code != 0:
                caught += 1
                tail = [line for line in output.splitlines() if line.startswith("FAILED")]
                print(f"{number} 抓住了 —— {why}（{len(tail)} 条用例转红）")
            else:
                missed.append(f"{number}（{why}）")
                print(f"{number} 没抓住 —— {why}  ← 这条要求没被验到")
    finally:
        for target, original in originals.items():
            (ROOT / target).write_text(original, encoding="utf-8")

    print()
    print(f"抓住 {caught}/{len(MUTANTS)}")
    for item in missed:
        print(f"  没抓住：{item}")
    return 0 if caught == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
