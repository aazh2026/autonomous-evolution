"""失败诊断 —— "哪儿坏了、改哪儿"。

设计依据：docs/design.md §6（失败诊断与有界修复）；docs/tasks.md
TASK-P2-015、R-098、I-04。

**这一层回答的是阶段一没回答的那半句。** 阶段一的
:class:`core.task.repair.FailureKind` 说"能不能自己修"；这一层说"是哪种错、
改哪一行"。两半合起来才够排一次修复：

- :mod:`agents.diagnosis.failure_classifier` —— 归类器与定向修复任务的生成。
  输入是**工具的机器可读输出**（ruff 的 JSON、mypy 的 JSON 行、pytest 的
  JUnit XML、Python 自己抛的 SyntaxError），输出是
  :class:`~agents.diagnosis.failure_classifier.Classification`。

**有界修复不在这一层。** 三重上限、每任务闸门与全局账本都是阶段一
（TASK-P1-063）的东西，这一层不另立一套 —— 归类结果翻译成阶段一那份
:class:`core.task.execution_loop.FailureDiagnosis` 就完事了。
"""

from __future__ import annotations

from agents.diagnosis.failure_classifier import (
    Classification,
    FailureType,
    RepairTarget,
    ToolFailure,
    ToolFinding,
    ToolOutput,
    build_targeted_repair_task,
    classify,
    parse_junit_xml,
    parse_mypy_json,
    parse_python_syntax_error,
    parse_ruff_json,
    parse_structured_diagnostics,
    parse_tool_output,
    tool_failure,
)

__all__ = [
    "Classification",
    "FailureType",
    "RepairTarget",
    "ToolFailure",
    "ToolFinding",
    "ToolOutput",
    "build_targeted_repair_task",
    "classify",
    "parse_junit_xml",
    "parse_mypy_json",
    "parse_python_syntax_error",
    "parse_ruff_json",
    "parse_structured_diagnostics",
    "parse_tool_output",
    "tool_failure",
]
