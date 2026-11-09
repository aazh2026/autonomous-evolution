"""TASK-P1-042 的反向验证 —— 逐个改坏覆盖率基线的口径，证明测试抓得住。

做法跟仓库里其它任务的反向验证一样：**改生产代码，然后跑测试，看它红。**
改完一定还原（``finally`` 里）。

**每一条变异都对着一条具体的设计要求。** 抓不住就说明那条要求没被验到。

**为什么这么多变异改的是"看起来无害"的地方。** ② 让没基线的模块拿 0 分、
④ 让批准形同虚设、⑥ 让"没批准"也放行、⑨ 让"没超过基线"也当成下调请求、
⑯ 让被拒的下调记成"成功" —— 这些改动都不会让任何东西报错，只会让**"标准
被悄悄降下来"这件事没人发现**。**这类错才是这条任务真正要防的**：报表照出，
而报表上那一栏本来就是空的。

**为什么要改三个文件。** 这条任务的口径分三截：口径本身
（``core/task/coverage_baseline.py``）、翻译成审计册子
（``harness/security/baseline_audit.py``）、地板的下限由数据库兜住
（``core/entities/project.py``）。**只改一处而测试全绿，就说明那一截没被
守住。**
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (编号, 目标文件, 说明, 原文, 改后)
MUTANTS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "①",
        "core/task/coverage_baseline.py",
        "阈值取小的那个 —— 模块基线压过全局地板，地板形同虚设",
        "        return max(baseline.coverage, self.floor)",
        "        return min(baseline.coverage, self.floor)",
    ),
    (
        "②",
        "core/task/coverage_baseline.py",
        "没基线的模块阈值给 0 —— “没量过”于是合法了",
        "        baseline = self.modules.get(module)\n"
        "        if baseline is None:\n"
        "            return self.floor",
        "        baseline = self.modules.get(module)\n"
        "        if baseline is None:\n"
        "            return Decimal(0)",
    ),
    (
        "③",
        "core/task/coverage_baseline.py",
        "地板的下限不守 —— 地板能被调到 0.1，标准就没了",
        "        if self.floor < GLOBAL_COVERAGE_FLOOR:",
        "        if self.floor < Decimal(0):",
    ),
    (
        "④",
        "core/task/coverage_baseline.py",
        "地板往下调也放行 —— “降低标准换通过”进了权限之内",
        "        if floor < self.floor:",
        "        if False:",
    ),
    (
        "⑤",
        "core/task/coverage_baseline.py",
        "批准形同虚设 —— 拿 A 模块的批准去降 B 模块",
        "        approved = approval is not None and approval.covers(\n"
        "            request.project_id, request.module, request.coverage\n"
        "        )",
        "        approved = True",
    ),
    (
        "⑥",
        "core/task/coverage_baseline.py",
        "没批准也放行 —— 下调基线不需要任何人点头",
        "        if not approved:",
        "        if False:",
    ),
    (
        "⑦",
        "core/task/coverage_baseline.py",
        "与现在一样也算一次下调请求 —— 审计表被自己的噪声淹掉",
        "        if request.coverage == previous:",
        "        if False:",
    ),
    (
        "⑧",
        "core/task/coverage_baseline.py",
        "往上抬也走审批那条路 —— 抬高本该是自动的",
        "        if request.coverage > previous:",
        "        if False:",
    ),
    (
        "⑨",
        "core/task/coverage_baseline.py",
        "新模块的“原来是多少”说成 0 —— 第一次建基线看着像从 0 降下来",
        "        previous = None if existing is None else existing.coverage",
        "        previous = existing.coverage if existing is not None else Decimal(0)",
    ),
    (
        "⑩",
        "core/task/coverage_baseline.py",
        "量得比基线低也算下调请求 —— 测一次飘了就能把标准拉下来",
        "            if existing is not None and measured <= existing.coverage:",
        "            if existing is not None and measured < Decimal(0):",
    ),
    (
        "⑪",
        "core/task/coverage_baseline.py",
        "下调次数不记 —— T2 的“基线下调次数 = 0”永远成立",
        "        if kind is BaselineUpdateKind.LOWERED:",
        "        if False:",
    ),
    (
        "⑫",
        "core/task/coverage_baseline.py",
        "不认识的键不报错 —— 拼错一个字段，那一列静默消失",
        "        if unknown:",
        "        if False:",
    ),
    (
        "⑬",
        "core/task/coverage_baseline.py",
        "模块键与模块名对不上也不报错 —— 那是两份数据",
        "            if baseline.module != name:",
        "            if False:",
    ),
    (
        "⑭",
        "core/task/coverage_baseline.py",
        "覆盖率的范围不检查 —— 1.5 也算一个覆盖率",
        "        if not Decimal(0) <= self.coverage <= Decimal(1):",
        "        if False:",
    ),
    (
        "⑮",
        "core/task/coverage_baseline.py",
        "审计写失败被吞掉 —— 这次下调“不算数”却照改基线",
        "        self.audit.record_lowering_attempt(\n"
        "            LoweringAttempt(\n"
        "                project_id=request.project_id,\n"
        "                module=request.module,\n"
        "                current=previous,\n"
        "                proposed=request.coverage,\n"
        "                requested_by=request.requested_by,\n"
        "                task_id=request.task_id,\n"
        "                approved=approved,\n"
        "                reason=reason,\n"
        "            )\n"
        "        )",
        "        try:\n"
        "            self.audit.record_lowering_attempt(\n"
        "                LoweringAttempt(\n"
        "                    project_id=request.project_id,\n"
        "                    module=request.module,\n"
        "                    current=previous,\n"
        "                    proposed=request.coverage,\n"
        "                    requested_by=request.requested_by,\n"
        "                    task_id=request.task_id,\n"
        "                    approved=approved,\n"
        "                    reason=reason,\n"
        "                )\n"
        "            )\n"
        "        except Exception:\n"
        "            pass",
    ),
    (
        "⑯",
        "harness/security/baseline_audit.py",
        "被拒的下调记成“成功” —— 审计册子里看不出有人动过标准",
        "            result=AuditResult.SUCCESS if approved else AuditResult.DENIED,",
        "            result=AuditResult.SUCCESS,",
    ),
    (
        "⑰",
        "harness/security/baseline_audit.py",
        "判定恒为放行 —— “被拒”与“放行”分不开",
        "            decision=AuditDecision.ALLOW if approved else AuditDecision.DENY,",
        "            decision=AuditDecision.ALLOW,",
    ),
    (
        "⑱",
        "harness/security/baseline_audit.py",
        "没有任务时留空串 —— 读的人以为“漏填了”，而不是“不适用”",
        "            task_id=attempt.task_id or NOT_APPLICABLE,",
        "            task_id=attempt.task_id,",
    ),
    (
        "⑲",
        "harness/security/baseline_audit.py",
        "理由里不写数字 —— 过几个月再看这一行，不知道它拦的是从多少降到多少",
        '        f"把 {attempt.module} 的覆盖率基线从 {before} 降到 {after} —— '
        '{verdict} —— {attempt.reason}"',
        '        f"{attempt.module} —— {verdict} —— {attempt.reason}"',
    ),
    (
        "⑳",
        "core/entities/project.py",
        "数据库不再兜住地板的下限 —— 手工 SQL 就能把地板调到 0.1",
        '            "floor >= 0.85",',
        '            "floor >= 0",',
    ),
)

#: 跑测试的命令。这一批全是单元测试 —— **不需要数据库、不花钱**。
CMD = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "pytest",
    "tests/unit/core/test_coverage_baseline.py",
    "tests/unit/harness/security/test_baseline_audit.py",
    "tests/unit/database/test_schema.py",
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
