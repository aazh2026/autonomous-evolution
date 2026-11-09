"""TASK-P2-015 —— 失败分类：把"必检项没过"拆成"哪种错、改哪儿"。

设计依据：docs/tasks.md TASK-P2-015（"阶段一已有的有界修复扩展到多失败
类型"；"自动定位失败原因并生成 Targeted 修复指令"）、R-098、I-04。

**这个文件钉住的是"定位"这件事，不是"分类"这件事。** 分类对不对，
人看一眼就知道；**定位对不对，Agent 得照着改一遍才知道** —— 而照着改
一遍的代价，就是一次真实的返工。所以每条用例都盯着"结论里有没有说出
改哪儿、改什么"。

拆成四组：

1. **拆得开**（``TestTheCoarseKindIsRefined``）—— 同一个"必检项没过"，
   语法错、依赖缺失、断言失败、类型不匹配要给出四种不同的结论。
2. **读的是机器可读输出**（``TestTheParsersReadMachineOutput``）——
   ruff 的 JSON、mypy 的 JSON 行、pytest 的 JUnit XML、Python 自己抛的
   SyntaxError，**没有一个是读人话猜出来的**。
3. **说不出来就说说不出来**（``TestTheUnreadableOutputIsNotGuessed``）——
   读不出来的输出归"原因不明"、归"需人工"，**不猜一个类别**。
4. **说得清改哪儿**（``TestTheInstructionIsTargeted``）—— 施工说明里
   要有文件、行、和要改的那个东西。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.diagnosis.failure_classifier import (
    FailureType,
    ToolFailure,
    ToolFinding,
    ToolOutput,
    build_targeted_repair_task,
    classify,
    parse_junit_xml,
    parse_mypy_json,
    parse_python_syntax_error,
    parse_ruff_json,
    parse_tool_output,
    tool_failure,
)
from core.task.execution_loop import FailureCategory
from core.task.repair import FailureKind

#: ruff ``--output-format=json`` 的一段真实输出（裁剪过）。
_RUFF_JSON = """
[
  {
    "code": "F401",
    "message": "`os` imported but unused",
    "filename": "src/pkg/thing.py",
    "location": {"row": 1, "column": 8},
    "end_location": {"row": 1, "column": 10}
  }
]
"""

#: mypy ``--output=json`` 的一段真实输出 —— 一行一个 JSON 对象。
_MYPY_JSON = (
    '{"file": "/w/a.py", "line": 3, "column": 8, "code": "import-not-found",'
    ' "message": "Cannot find module named \\"nope\\""}\n'
    '{"file": "/w/a.py", "line": 7, "column": 5, "code": "return-value",'
    ' "message": "Incompatible return value type"}\n'
)

#: pytest ``--junitxml`` 的一段真实输出。
_JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="1" skipped="0" tests="2">
    <testcase classname="tests.test_thing" name="test_one" file="tests/test_thing.py" line="5">
      <failure message="assert 1 == 2">def test_one(): assert 1 == 2</failure>
    </testcase>
    <testcase classname="tests.test_thing" name="test_two" file="tests/test_thing.py" line="9">
      <error message="fixture 'tmp' not found">E   fixture 'tmp' not found</error>
    </testcase>
  </testsuite>
</testsuites>
"""


def _ruff_failure() -> ToolFailure:
    return tool_failure(
        kind=FailureKind.CHECK_FAILED, tool="ruff", output=ToolOutput(_RUFF_JSON, 1)
    )


def _mypy_failure() -> ToolFailure:
    return tool_failure(
        kind=FailureKind.CHECK_FAILED, tool="mypy", output=ToolOutput(_MYPY_JSON, 1)
    )


def _pytest_failure() -> ToolFailure:
    return tool_failure(
        kind=FailureKind.CHECK_FAILED, tool="pytest", output=ToolOutput(_JUNIT_XML, 1)
    )


def _pytest_assertion_failure() -> ToolFailure:
    """只有一条断言失败（夹具没坏）。"""
    payload = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_thing" name="test_one" file="tests/test_thing.py" line="5">
    <failure message="assert 1 == 2">def test_one(): assert 1 == 2</failure>
  </testcase>
</testsuite></testsuites>
"""
    return tool_failure(kind=FailureKind.CHECK_FAILED, tool="pytest", output=ToolOutput(payload, 1))


class TestTheCoarseKindIsRefined:
    """同一个"必检项没过"，要能拆成不同的结论。"""

    def test_a_syntax_error_is_not_a_lint_violation(self) -> None:
        """语法错要先于其它结论 —— 模块根本解析不了时别的都是连锁反应。"""
        failure = ToolFailure(
            kind=FailureKind.CHECK_FAILED,
            tool="ruff",
            findings=(
                ToolFinding(file="src/pkg/a.py", line=3, code="invalid-syntax", raw_code="E999"),
                ToolFinding(file="src/pkg/b.py", line=9, code="lint-violation", raw_code="F401"),
            ),
        )

        classification = classify(failure)

        assert classification.failure_type is FailureType.SYNTAX_ERROR

    def test_a_missing_import_is_a_missing_import(self) -> None:
        classification = classify(_mypy_failure())

        assert classification.failure_type is FailureType.IMPORT_ERROR

    def test_an_assertion_failure_is_an_assertion_failure(self) -> None:
        classification = classify(_pytest_assertion_failure())

        assert classification.failure_type is FailureType.TEST_ASSERTION

    def test_a_broken_fixture_wins_over_an_assertion_failure(self) -> None:
        """夹具坏了，那条用例压根没跑 —— **它的断言失败是假象**。"""
        classification = classify(_pytest_failure())

        assert classification.failure_type is FailureType.TEST_SETUP_ERROR

    def test_a_broken_fixture_is_not_an_assertion_failure(self) -> None:
        """**测试压根没跑起来**，去改断言是改错地方。"""
        payload = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_thing" name="test_two" file="tests/test_thing.py" line="9">
    <error message="fixture 'tmp' not found">boom</error>
  </testcase>
</testsuite></testsuites>
"""
        failure = tool_failure(
            kind=FailureKind.CHECK_FAILED, tool="pytest", output=ToolOutput(payload, 1)
        )

        classification = classify(failure)

        assert classification.failure_type is FailureType.TEST_SETUP_ERROR

    def test_a_lint_violation_is_a_lint_violation(self) -> None:
        classification = classify(_ruff_failure())

        assert classification.failure_type is FailureType.LINT_VIOLATION

    def test_a_type_mismatch_is_a_type_mismatch(self) -> None:
        payload = '{"file": "/w/a.py", "line": 7, "column": 5, "code": "return-value"}\n'
        failure = tool_failure(
            kind=FailureKind.CHECK_FAILED, tool="mypy", output=ToolOutput(payload, 1)
        )

        classification = classify(failure)

        assert classification.failure_type is FailureType.TYPE_ERROR

    def test_a_compile_stage_failure_is_not_a_syntax_error(self) -> None:
        """编译这一步过不去，与语法错不是一回事。"""
        failure = ToolFailure(
            kind=FailureKind.CHECK_FAILED,
            tool="python",
            findings=(
                ToolFinding(file="src/pkg/a.py", code="compile-error", raw_code="ValueError"),
            ),
        )

        classification = classify(failure)

        assert classification.failure_type is FailureType.COMPILE_ERROR


class TestTheCoarseKindAloneStillDecides:
    """粗类已经能定性的，不看工具输出。"""

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (FailureKind.CANCELLED, FailureType.CANCELLED),
            (FailureKind.TIMED_OUT, FailureType.TIMEOUT),
            (FailureKind.SPEC_CONFLICT, FailureType.SPEC_CONFLICT),
            (FailureKind.VERIFIER_ERROR, FailureType.VERIFIER_ERROR),
            (FailureKind.EXECUTOR_ERROR, FailureType.ENVIRONMENT_ERROR),
        ],
    )
    def test_the_coarse_kind_is_kept(self, kind: FailureKind, expected: FailureType) -> None:
        classification = classify(ToolFailure(kind=kind, tool="ruff", findings=()))

        assert classification.failure_type is expected

    def test_a_spec_conflict_is_not_repairable(self) -> None:
        """前提不成立 —— 再修也是白修。"""
        classification = classify(ToolFailure(kind=FailureKind.SPEC_CONFLICT))

        assert classification.category is FailureCategory.UNREPAIRABLE

    def test_a_cancelled_task_needs_a_human(self) -> None:
        classification = classify(ToolFailure(kind=FailureKind.CANCELLED))

        assert classification.category is FailureCategory.NEEDS_HUMAN


class TestTheParsersReadMachineOutput:
    """解析器读的是机器可读输出。"""

    def test_the_ruff_json_is_read(self) -> None:
        findings = parse_ruff_json(_RUFF_JSON)

        assert len(findings) == 1
        assert findings[0].file == "src/pkg/thing.py"
        assert findings[0].line == 1
        assert findings[0].column == 8
        assert findings[0].raw_code == "F401"

    def test_the_mypy_json_lines_are_read(self) -> None:
        findings = parse_mypy_json(_MYPY_JSON)

        assert len(findings) == 2
        assert findings[0].raw_code == "import-not-found"
        assert findings[1].line == 7

    def test_the_junit_xml_is_read(self) -> None:
        findings = parse_junit_xml(_JUNIT_XML)

        assert len(findings) == 2
        assert findings[0].symbol == "tests.test_thing::test_one"
        assert findings[0].file == "tests/test_thing.py"
        assert findings[0].line == 5
        assert findings[1].code == "test-error"

    def test_the_junit_message_is_kept_to_one_line(self) -> None:
        """pytest 把整份报告塞进 ``message`` —— 整段塞进施工说明就没法读了。"""
        payload = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<testsuites><testsuite name="pytest">'
            '<testcase classname="tests.test_thing" name="test_two">'
            '<error message="failed on setup with&#10;E   fixture missing&#10;'
            'available: tmp_path">boom</error>'
            "</testcase></testsuite></testsuites>"
        )
        findings = parse_junit_xml(payload)

        assert findings[0].message == "failed on setup with"

    def test_the_python_syntax_error_carries_the_position(self) -> None:
        finding = parse_python_syntax_error("def f(:\n    pass\n", filename="src/pkg/a.py")

        assert finding is not None
        assert finding.line == 1
        assert finding.file == "src/pkg/a.py"
        assert finding.code == "invalid-syntax"

    def test_a_compilable_file_has_no_syntax_error(self) -> None:
        assert parse_python_syntax_error("x = 1\n") is None

    def test_an_unknown_tool_gets_no_findings(self) -> None:
        """工具不认识就什么也不给 —— **不硬套一个解析器**。"""
        assert parse_tool_output("some-other-linter", _RUFF_JSON) == ()


class TestTheUnreadableOutputIsNotGuessed:
    """读不出来就说读不出来。"""

    def test_a_garbled_payload_does_not_crash(self) -> None:
        failure = tool_failure(
            kind=FailureKind.CHECK_FAILED, tool="ruff", output=ToolOutput("not json at all {", 1)
        )

        classification = classify(failure)

        assert classification.failure_type is FailureType.UNKNOWN

    def test_an_unreadable_failure_needs_a_human(self) -> None:
        """猜出来的类别决定"要不要自己修" —— 猜错就是一次不该发生的返工。"""
        failure = tool_failure(
            kind=FailureKind.UNKNOWN, tool="ruff", output=ToolOutput("not json", 1)
        )

        classification = classify(failure)

        assert classification.category is FailureCategory.NEEDS_HUMAN

    def test_the_check_level_target_is_still_structured(self) -> None:
        """工具输出认不出来 —— 但**哪几项必检项没过**是知道的。"""
        failure = ToolFailure(
            kind=FailureKind.CHECK_FAILED,
            tool="ruff",
            findings=(),
            failed_checks=("lint_pass", "unit_tests_pass"),
        )

        classification = classify(failure)

        assert classification.category is FailureCategory.AUTO_REPAIRABLE
        assert classification.targets[0].symbol == "lint_pass"


class TestTheInstructionIsTargeted:
    """施工说明要说到行、说到改什么。"""

    def test_the_instruction_names_the_file_and_the_line(self) -> None:
        classification = classify(_ruff_failure())

        instruction = classification.targeted_instruction()

        assert "src/pkg/thing.py:1" in instruction
        assert "F401" in instruction

    def test_the_instruction_names_the_test_that_failed(self) -> None:
        classification = classify(_pytest_assertion_failure())

        instruction = classification.targeted_instruction()

        assert "tests.test_thing::test_one" in instruction

    def test_the_instruction_is_never_empty(self) -> None:
        """空的指令等于"随便改改" —— 那是无限修复循环的起点。"""
        for failure in (_ruff_failure(), _mypy_failure(), _pytest_failure()):
            instruction = classify(failure).targeted_instruction()

            assert instruction.strip()

    def test_a_long_list_says_how_many_were_left_out(self) -> None:
        """截断而不说，读的人会以为就这么多。"""
        findings = tuple(
            ToolFinding(
                file=f"src/pkg/a{index}.py",
                line=index,
                code="lint-violation",
                raw_code="F401",
            )
            for index in range(1, 9)
        )
        failure = ToolFailure(kind=FailureKind.CHECK_FAILED, tool="ruff", findings=findings)

        classification = classify(failure)

        instruction = classification.targeted_instruction()

        assert "8 处" in instruction
        assert "另有 3 处同类问题" in instruction

    def test_the_evidence_keeps_the_machine_identifiers(self) -> None:
        classification = classify(_ruff_failure())

        assert classification.evidence == ("src/pkg/thing.py:1 F401",)

    def test_the_diagnosis_carries_the_targeted_instruction(self) -> None:
        """翻译成阶段一那份诊断时，**定向的那句要带过去**。"""
        classification = classify(_ruff_failure())

        diagnosis = classification.to_diagnosis()

        assert diagnosis.repair_hint == classification.targeted_instruction()
        assert diagnosis.category is FailureCategory.AUTO_REPAIRABLE


class TestTheTargetedRepairTask:
    """定向修复任务的生成。"""

    def test_an_auto_repairable_failure_gets_a_task(self) -> None:
        classification = classify(_ruff_failure())

        task = build_targeted_repair_task(task_id="T-1", attempt=1, classification=classification)

        assert task.attempt == 1
        assert "src/pkg/thing.py:1" in task.instruction

    def test_a_failure_that_needs_a_human_gets_no_task(self) -> None:
        """生成"随便改改"的施工说明等于把无限修复循环的起点交出去。"""
        classification = classify(ToolFailure(kind=FailureKind.CANCELLED))

        with pytest.raises(ValueError, match="不能自动修"):
            build_targeted_repair_task(task_id="T-1", attempt=1, classification=classification)

    def test_the_task_is_built_from_the_same_diagnosis(self) -> None:
        """任务里的诊断与分类结论是同一份 —— 两处各写一遍就会走偏。"""
        classification = classify(_ruff_failure())

        task = build_targeted_repair_task(task_id="T-1", attempt=1, classification=classification)

        assert task.diagnosis == classification.to_diagnosis()


class TestTheRealToolOutput:
    """真的工具输出 —— 不靠手写的样本。"""

    def test_a_real_syntax_error_is_located(self, tmp_path: Path) -> None:
        source = tmp_path / "broken.py"
        source.write_text("def f(:\n    return 1\n", encoding="utf-8")
        text = source.read_text(encoding="utf-8")

        finding = parse_python_syntax_error(text, filename=str(source))

        assert finding is not None
        assert finding.line == 1
        assert finding.code == "invalid-syntax"

    def test_a_real_null_byte_in_the_source_is_a_syntax_error(self, tmp_path: Path) -> None:
        """源码里有 null 字节 —— Python 自己报的是**语法错**。"""
        source = tmp_path / "nul.py"
        source.write_bytes(b"x = 1\x00\n")
        text = source.read_bytes().decode("utf-8")

        finding = parse_python_syntax_error(text, filename=str(source))

        assert finding is not None
        assert finding.code == "invalid-syntax"

    def test_a_real_uncompilable_name_is_a_compile_error(self) -> None:
        """**编译这一步过不去**（文件名本身带 null 字节），但**不是语法错**。"""
        finding = parse_python_syntax_error("x = 1\n", filename="a\x00b.py")

        assert finding is not None
        assert finding.code == "compile-error"
