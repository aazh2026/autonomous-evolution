"""TASK-P1-073 的反向验证 —— 逐个改坏轨迹口径，证明测试抓得住。

做法跟仓库里其它任务的反向验证一样：**改生产代码，然后跑测试，看它红。**
改完一定还原（``finally`` 里）。

**每一条变异都对着一条具体的设计要求。** 抓不住就说明那条要求没被验到。

**为什么这么多变异改的是"看起来无害"的地方。** ① 让"缺了会话标识的
Agent 会话"也能记进去、⑤ 把没带的标识填成空串、⑦ 让两笔账永远说"对得上"
—— 这些改动都不会让任何东西报错，只会让**缺掉的那一样没人发现**。
**这类错才是 R-062 真正要防的**：报表照出，而报表上那一栏本来就是空的。
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
        "core/observability/trace.py",
        "Agent 会话不带会话标识也放行 —— 事后没人说得清是谁开的会话",
        '        TraceKind.AGENT_SESSION: ("task_id", "agent_session_id"),',
        '        TraceKind.AGENT_SESSION: ("task_id",),',
    ),
    (
        "②",
        "core/observability/trace.py",
        "工具调用不带会话标识也放行 —— R-062 的“主体”没了",
        '        TraceKind.TOOL_CALL: ("task_id", "agent_session_id"),',
        '        TraceKind.TOOL_CALL: ("task_id",),',
    ),
    (
        "③",
        "core/observability/trace.py",
        "验证运行不带任务也放行 —— 事后没人说得清验的是哪个任务",
        '        TraceKind.VERIFICATION_RUN: ("task_id",),',
        "        TraceKind.VERIFICATION_RUN: (),",
    ),
    (
        "④",
        "core/observability/trace.py",
        "决策节点不带周期也放行 —— 一轮演进的决策查不到",
        '        TraceKind.DECISION_NODE: ("cycle_id",),',
        "        TraceKind.DECISION_NODE: (),",
    ),
    (
        "⑤",
        "core/observability/trace.py",
        "没带的标识也填成空串 —— “没有这个标识”与“这个标识是空的”分不开",
        "        if self.task_id is not None:\n"
        '            result["task_id"] = self.task_id\n'
        "        if self.cycle_id is not None:\n"
        '            result["cycle_id"] = self.cycle_id\n'
        "        if self.agent_session_id is not None:\n"
        '            result["agent_session_id"] = self.agent_session_id',
        '        result["task_id"] = self.task_id or ""\n'
        '        result["cycle_id"] = self.cycle_id or ""\n'
        '        result["agent_session_id"] = self.agent_session_id or ""',
    ),
    (
        "⑥",
        "core/observability/trace.py",
        "查不到轨迹时返回一条空轨迹 —— “没有轨迹”与“轨迹是空的”混成一件",
        "        return self._by_task.get(task_id)",
        "        return self._by_task.get(task_id, "
        "TaskTrace(context=TraceContext(task_id=task_id)))",
    ),
    (
        "⑦",
        "core/observability/trace.py",
        "两笔账永远说对得上 —— 轨迹漏了工具调用也没人喊",
        "        return self.reported is None or self.reported == self.recorded",
        "        return True",
    ),
    (
        "⑧",
        "infrastructure/observability/tracing.py",
        "工具调用数把“结束”事件也数进去 —— 数出来的是两倍",
        "            if event.kind in TOOL_CALL_KINDS:",
        "            if event.kind in TOOL_CALL_KINDS or event.kind in TOOL_RESULT_KINDS:",
    ),
    (
        "⑨",
        "infrastructure/observability/tracing.py",
        "工具调用的结果不贴回去 —— R-062 的“结果”那一栏空了",
        '    attributes["result"] = content',
        '    attributes["result"] = None',
    ),
    (
        "⑩",
        "infrastructure/observability/tracing.py",
        "工具调用不带主体与任务 —— R-062 的前两样没了",
        '        "subject": context.agent_session_id,\n        "task": context.task_id,',
        '        "subject": None,\n        "task": None,',
    ),
    (
        "⑪",
        "infrastructure/observability/tracing.py",
        "span 上不写工具调用数 —— 看板上那个数得人去数事件",
        "            TRACE_TOOL_CALLS_ATTRIBUTE: task_trace.tool_calls.recorded,",
        "            TRACE_TOOL_CALLS_ATTRIBUTE: 0,",
    ),
    (
        "⑫",
        "infrastructure/observability/tracing.py",
        "观测口抛异常就把主流程带崩 —— 观测故障升级成业务故障",
        "            except Exception as exc:\n"
        "                get_logger(__name__).warning(\n"
        '                    "trace_observer_failed",\n'
        "                    degraded=True,\n"
        '                    reason=f"{type(exc).__name__}",\n'
        "                )",
        "            except Exception:\n                raise",
    ),
    (
        "⑬",
        "infrastructure/observability/tracing.py",
        "不记那条 Agent 会话 —— 轨迹里答不出“这次是谁做的”",
        "        if context.agent_session_id is not None:",
        "        if False:",
    ),
)

#: 跑测试的命令。这一批全是单元与系统测试 —— **不需要数据库、不花钱**。
CMD = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "pytest",
    "tests/unit/infrastructure/test_trace_observability.py",
    "tests/system/test_trace_reporting.py",
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
