"""TASK-P1-083 的反向验证 —— 逐个改坏标注集与误报率的口径，证明测试抓得住。

做法跟仓库里其它任务的反向验证一样：**改生产代码，然后跑测试，看它红。**
改完一定还原（``finally`` 里）。

**每一条变异都对着一条具体的设计要求。** 抓不住就说明那条要求没被验到。

**为什么这些变异改的都是"看起来无害"的地方。** ③ 让 ``is_valid_finding``
写成 ``1`` 也放过、⑦ 让 19 条标注集也能用、⑩ 让每条标注都算"有效"、
⑯ 让标注集缺失时报 0 —— 这些改动都不会让任何东西报错，只会让**"误报率 0%"
这个最危险的数出现在看板上**。"还没标"与"全是无效"是相反的两件事，报表上
却长得一模一样，读的人会以为系统很准。

**为什么要改两个文件。** 这条任务的口径分两截：标注集本身
（``evaluation/labels/pilot.py``）与误报率怎么用它
（``evaluation/evolution_evaluation/decision.py``）。**只改一处而测试全绿，
就说明那一截没被守住。**
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
        "evaluation/labels/pilot.py",
        "缺字段不报错 —— 少一个字段的标注照样能进分母",
        "        if missing:",
        "        if False:",
    ),
    (
        "②",
        "evaluation/labels/pilot.py",
        "多字段不报错 —— 写的人以为它有意义，机器装作没看见",
        "        if extra:",
        "        if False:",
    ),
    (
        "③",
        "evaluation/labels/pilot.py",
        "`is_valid_finding` 不校验布尔 —— 写成 1 也放过",
        "        if not isinstance(finding, bool):",
        "        if False:",
    ),
    (
        "④",
        "evaluation/labels/pilot.py",
        "时间戳不校验时区 —— 没有时区的时间戳指的是哪个时刻？",
        "    if moment.tzinfo is None:",
        "    if False:",
    ),
    (
        "⑤",
        "evaluation/labels/pilot.py",
        "空白字符串放过 —— 空串与“没填”是两回事",
        "    if not isinstance(value, str) or not value.strip():",
        "    if not isinstance(value, str):",
    ),
    (
        "⑥",
        "evaluation/labels/pilot.py",
        "重复的 issue_ref 放过 —— 同一个问题标两次就变成“最后写的人赢”",
        "            if label.issue_ref in seen:",
        "            if False:",
    ),
    (
        "⑦",
        "evaluation/labels/pilot.py",
        "条数检查失效 —— 19 条标注集也能当 20 条用",
        "        if self._expected_count is not None and len(label_set) != self._expected_count:",
        "        if False:",
    ),
    (
        "⑧",
        "evaluation/labels/pilot.py",
        "标注集不在时返回空集 —— “基线未建立”与“标注集是空的”混成一件事",
        "        except FileNotFoundError as exc:\n"
        '            raise LabelSetMissingError(f"标注集不在：{self._path}") from exc',
        "        except FileNotFoundError:\n            return LabelSet(labels=())",
    ),
    (
        "⑨",
        "evaluation/labels/pilot.py",
        "条数常量被改 —— 分母的规模悄悄变了",
        "EXPECTED_LABEL_COUNT: Final[int] = 20",
        "EXPECTED_LABEL_COUNT: Final[int] = 19",
    ),
    (
        "⑩",
        "evaluation/labels/pilot.py",
        "每条标注都算有效 —— 误报率永远是 0",
        "        return Verdict.VALID if self.is_valid_finding else Verdict.INVALID",
        "        return Verdict.VALID",
    ),
    (
        "⑪",
        "evaluation/labels/pilot.py",
        "没标过的问题也给出结论 —— 没审过的被当成审过的",
        "                return label.verdict\n        return None",
        "                return label.verdict\n        return Verdict.INVALID",
    ),
    (
        "⑫",
        "evaluation/labels/pilot.py",
        "不是对象的行也放过 —— 标注集里混进别的结构",
        "    if not isinstance(raw, dict):",
        "    if False:",
    ),
    (
        "⑬",
        "evaluation/labels/pilot.py",
        "坏掉的 JSON 不报错 —— 读坏了的标注集被当成“没有标注”",
        "    try:\n"
        "        raw = json.loads(text)\n"
        "    except json.JSONDecodeError as exc:\n"
        '        raise LabelSetError(f"{where} 不是合法 JSON：{exc.msg}") from exc',
        "    raw = json.loads(text)",
    ),
    (
        "⑭",
        "evaluation/evolution_evaluation/decision.py",
        "分母按决策 id 匹配 —— 标注集对不上候选，分母整个错位",
        "        replace(item, candidate_label=label_set.verdict_of(item.candidate_id))"
        " for item in decisions",
        "        replace(item, candidate_label=label_set.verdict_of(item.decision_id))"
        " for item in decisions",
    ),
    (
        "⑮",
        "evaluation/evolution_evaluation/decision.py",
        "调用方塞的标注被保留 —— 分母可以由被评价的一方决定",
        "        replace(item, candidate_label=label_set.verdict_of(item.candidate_id))"
        " for item in decisions",
        "        replace(item) for item in decisions",
    ),
    (
        "⑯",
        "evaluation/evolution_evaluation/decision.py",
        "标注集缺失时报 0 —— “还没标”被显示成“一条误报都没有”",
        "    except LabelSetMissingError as exc:\n"
        '        return _missing_baseline(f"缺少人工标注基线（TASK-P1-083）：{exc}")',
        "    except LabelSetMissingError as exc:\n"
        '        return rate(FALSE_POSITIVE_RATE, 0, 1, detail={"reason": str(exc)})',
    ),
    (
        "⑰",
        "evaluation/evolution_evaluation/decision.py",
        "分母只算有效的 —— 无效的那几条从分母里消失了",
        "    labelled = [item for item in proposals if item.candidate_label is not None]",
        "    labelled = [item for item in proposals"
        " if item.candidate_label is not Verdict.INVALID]",
    ),
    (
        "⑱",
        "evaluation/evolution_evaluation/decision.py",
        "标注集根本没被用上 —— 分母与标注集无关",
        "    return tuple(\n"
        "        replace(item, candidate_label=label_set.verdict_of(item.candidate_id))"
        " for item in decisions\n"
        "    )",
        "    return tuple(decisions)",
    ),
)

#: 跑测试的命令。单元测试 + 系统测试 —— **不需要数据库、不花钱**。
CMD = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "pytest",
    "tests/unit/evaluation/test_pilot_labels.py",
    "tests/unit/evaluation/test_decision.py",
    "tests/system/test_pilot_labels.py",
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
