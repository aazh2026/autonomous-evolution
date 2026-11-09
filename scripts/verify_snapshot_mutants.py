"""TASK-P1-023 的反向验证 —— 逐个改坏快照口径，证明测试抓得住。

做法跟仓库里其它任务的反向验证一样：**改生产代码，然后跑测试，看它红。**
改完一定还原（``finally`` 里）。

**每一条变异都对着一条具体的设计要求。** 抓不住就说明那条要求没被验到。

**为什么这么多变异改的是"看起来无害"的地方。** ① 把采集时点混进元数据、
② 把"没采到"写成 0、③ 报告缺失时记成"跑了但零个用例" —— 这些改动都不会
让任何东西报错，只会让**缺掉的那一样没人发现**。**这类错才是这条任务真正
要防的**：报表照出，而报表上那一栏本来就是空的。
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
        "core/project/snapshot.py",
        "采集时点混进元数据 —— “同一 commit 两次一致”这条要求就永远立不住",
        '            "commit": self.commit,\n'
        '            "branch": self.branch,\n'
        '            "code": self.metrics.code.to_document(),',
        '            "commit": self.commit,\n'
        '            "branch": self.branch,\n'
        '            "collected_at": self.collected_at,\n'
        '            "code": self.metrics.code.to_document(),',
    ),
    (
        "②",
        "core/project/snapshot.py",
        "取不到的那一项报 0 —— “没量过”看着像“量出来是 0”",
        "            return self.metrics.coverage.overall if self.metrics.coverage else None",
        "            return self.metrics.coverage.overall if self.metrics.coverage else 0.0",
    ),
    (
        "③",
        "core/project/snapshot.py",
        "往返时把没采到的文件数变成 0 —— 重建出来的快照比原来多了一个事实",
        '            files=_optional_int(raw.get("files"), where="代码统计的文件数"),',
        '            files=_as_int(raw.get("files") or 0, where="代码统计的文件数"),',
    ),
    (
        "④",
        "core/project/snapshot.py",
        "覆盖率的范围不检查 —— 1.5 也算一个覆盖率",
        "        if self.overall is not None and not 0.0 <= self.overall <= 1.0:\n"
        '            raise SnapshotError("整体覆盖率必须在 0 与 1 之间")',
        '        if False:\n            raise SnapshotError("整体覆盖率必须在 0 与 1 之间")',
    ),
    (
        "⑤",
        "core/project/snapshot.py",
        "布尔也当整数 —— “通过数是 True”会被当成 1",
        "    if isinstance(raw, bool) or not isinstance(raw, int):",
        "    if not isinstance(raw, int):",
    ),
    (
        "⑥",
        "core/project/snapshot.py",
        "版本号 0 也放行 —— 快照的版本从哪儿开始就说不清了",
        "        if self.version < 1:",
        "        if self.version < 0:",
    ),
    (
        "⑦",
        "core/project/snapshot.py",
        "空 commit 也放行 —— 快照指向不了任何代码",
        "        if not self.commit.strip():\n"
        "            raise SnapshotError("
        '"快照必须指向一个 commit —— 没有 commit 的快照指向不了任何代码")',
        "        if False:\n"
        "            raise SnapshotError("
        '"快照必须指向一个 commit —— 没有 commit 的快照指向不了任何代码")',
    ),
    (
        "⑧",
        "core/project/snapshot.py",
        "空 URI 也放行 —— 产物指了个空地方",
        "        if not self.uri.strip():\n"
        '            raise SnapshotError("产物的 URI 不能是空白")',
        '        if False:\n            raise SnapshotError("产物的 URI 不能是空白")',
    ),
    (
        "⑨",
        "core/project/snapshot.py",
        "按模块的覆盖率可以改 —— 事后改一个数，就是改证据",
        '        object.__setattr__(self, "by_module", MappingProxyType(dict(self.by_module)))',
        '        object.__setattr__(self, "by_module", dict(self.by_module))',
    ),
    (
        "⑩",
        "harness/project/snapshot_probes.py",
        "没有测试报告时记成“跑了但零个用例” —— 那是一句假话",
        "        path = workspace / self._report_name\n"
        "        if not path.is_file():\n"
        "            return None\n"
        "        try:\n"
        "            root = ElementTree.parse(path).getroot()",
        "        path = workspace / self._report_name\n"
        "        if not path.is_file():\n"
        "            return SuiteOutcome(passed=0, failed=0)\n"
        "        try:\n"
        "            root = ElementTree.parse(path).getroot()",
    ),
    (
        "⑪",
        "harness/project/snapshot_probes.py",
        "没有覆盖率报告时记成 0% —— 看着像“测出来一行都没覆盖”",
        "        path = workspace / self._report_name\n"
        "        if not path.is_file():\n"
        "            return None\n"
        "        try:\n"
        '            raw = json.loads(path.read_text(encoding="utf-8"))',
        "        path = workspace / self._report_name\n"
        "        if not path.is_file():\n"
        "            return CoverageReport(overall=0.0)\n"
        "        try:\n"
        '            raw = json.loads(path.read_text(encoding="utf-8"))',
    ),
    (
        "⑫",
        "harness/project/snapshot_probes.py",
        "游离 HEAD 时分支给空串 —— “没有分支”与“分支名是空的”分不开",
        "        if code == 1:\n            return None",
        '        if code == 1:\n            return ""',
    ),
    (
        "⑬",
        "infrastructure/project/snapshot_documents.py",
        "“最新”取的是最早的那份 —— 达成度会拿旧快照当新的比",
        "        _, newest = max(versions, key=lambda item: item[0])",
        "        _, newest = min(versions, key=lambda item: item[0])",
    ),
)

#: 跑测试的命令。这一批全是单元与系统测试 —— **不需要数据库、不花钱**。
CMD = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "pytest",
    "tests/unit/core/test_snapshot.py",
    "tests/system/test_snapshot_collection.py",
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
