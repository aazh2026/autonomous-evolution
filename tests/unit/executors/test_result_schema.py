"""统一执行结果 schema 的校验。

验证（T1 ③）：``task_id`` / ``executor`` / ``status`` /
``artifacts{branch,commit,changed_files}`` / ``execution{duration,tool_calls,cost}`` /
``self_assessment{completed,limitations}`` 字段齐全且类型正确。

**这个 schema 是执行器与系统之间唯一的正式接口。** 所以它要挡的不是"少填一个
字段"，而是**看起来填了、其实类型不对** —— 那是最难查的一类问题：结果照收，
看板照显示，只是数字从此不再可信。

三条硬规矩：

1. **字段名不许漂移**（``extra="forbid"``）。多出来的字段说明执行器与契约已经
   分叉，而分叉在这里不会被发现。
2. **类型不许将就**（``strict=True``）。``changed_files="12"`` 要被拒 —— 那是
   执行器没把 schema 当回事；``cost=0.1``（浮点）也要被拒 —— 浮点数丢精度的
   地方正是要记钱的地方。
3. **十进制字面量要能进**。跨 JSON 边界时金额与时长就是字符串（``"2.4"``），
   浮点数只是它的十进制写法。它们经**字面量**转成 ``Decimal``，不走二进制
   精确值 —— 后者会把作者写的 0.1 变成
   0.1000000000000000055511151231257827。

**相对 design.md §3.4 的示例多了两个字段**：``execution.currency``（成本没有
币种就没法和预算比，R-039 要求三处成本同源同币种）与 ``idempotency_key``
（没有它就无从核对"同一幂等键返回同一结果"，I-01）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.task.outcome import ExecutionStatus
from executors.base import (
    ExecutionArtifacts,
    ExecutionMetrics,
    ExecutionResult,
    SelfAssessment,
)

#: design.md §3.4 的示例结果，补上本模块说明里的两个字段。
_DESIGN_EXAMPLE: dict[str, object] = {
    "task_id": "TASK-1024",
    "executor": "claude-code",
    "status": "completed",
    "artifacts": {"branch": "agent/TASK-1024", "commit": "abc123", "changed_files": 12},
    "execution": {"duration": 360, "tool_calls": 35, "cost": 2.4, "currency": "USD"},
    "self_assessment": {"completed": True, "limitations": []},
    "idempotency_key": "TASK-1024:1",
}


class TestTheFieldsAreAllThere:
    """字段齐全 —— 少一个就说明契约漂了。"""

    def test_the_result_has_exactly_the_contract_fields(self) -> None:
        assert set(ExecutionResult.model_fields) == {
            "task_id",
            "executor",
            "status",
            "artifacts",
            "execution",
            "self_assessment",
            "idempotency_key",
        }

    def test_the_artifacts_have_exactly_the_contract_fields(self) -> None:
        assert set(ExecutionArtifacts.model_fields) == {"branch", "commit", "changed_files"}

    def test_the_metrics_have_exactly_the_contract_fields(self) -> None:
        assert set(ExecutionMetrics.model_fields) == {
            "duration",
            "tool_calls",
            "cost",
            "currency",
            "cost_deviation",
        }

    def test_the_self_assessment_has_exactly_the_contract_fields(self) -> None:
        assert set(SelfAssessment.model_fields) == {"completed", "limitations"}

    def test_the_design_example_validates(self) -> None:
        """design.md §3.4 的示例结果必须能落进这个 schema。"""
        result = ExecutionResult.model_validate(_DESIGN_EXAMPLE)

        assert result.task_id == "TASK-1024"
        assert result.status is ExecutionStatus.COMPLETED
        assert result.artifacts.changed_files == 12
        assert result.execution.cost == Decimal("2.4")
        assert result.self_assessment.completed is True

    @pytest.mark.parametrize(
        "missing",
        [
            "task_id",
            "executor",
            "status",
            "artifacts",
            "execution",
            "self_assessment",
            "idempotency_key",
        ],
    )
    def test_a_missing_field_is_rejected(self, missing: str) -> None:
        payload = {key: value for key, value in _DESIGN_EXAMPLE.items() if key != missing}

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)


class TestTheTypesAreChecked:
    """类型正确 —— "看起来填了"不算。"""

    def test_a_string_is_not_a_number(self) -> None:
        payload = {
            **_DESIGN_EXAMPLE,
            "artifacts": {"branch": "b", "commit": "c", "changed_files": "12"},
        }

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)

    def test_a_fractional_number_is_not_a_count(self) -> None:
        payload = {
            **_DESIGN_EXAMPLE,
            "execution": {"duration": 1, "tool_calls": 1.5, "cost": 1, "currency": "USD"},
        }

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)

    def test_a_string_is_not_a_boolean(self) -> None:
        payload = {**_DESIGN_EXAMPLE, "self_assessment": {"completed": "yes", "limitations": []}}

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)

    def test_an_unknown_status_is_rejected(self) -> None:
        payload = {**_DESIGN_EXAMPLE, "status": "done"}

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)

    def test_an_unparsable_decimal_is_rejected(self) -> None:
        payload = {
            **_DESIGN_EXAMPLE,
            "execution": {"duration": 1, "tool_calls": 1, "cost": "两块钱", "currency": "USD"},
        }

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)

    def test_a_boolean_is_not_a_cost(self) -> None:
        """``True`` 是 1 —— 把它当成本收下，等于凭空记了一块钱。"""
        payload = {
            **_DESIGN_EXAMPLE,
            "execution": {"duration": 1, "tool_calls": 1, "cost": True, "currency": "USD"},
        }

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("duration", -1),
            ("tool_calls", -1),
            ("cost", -Decimal("0.01")),
        ],
    )
    def test_a_negative_number_is_rejected(self, field: str, value: object) -> None:
        execution = {"duration": 1, "tool_calls": 1, "cost": "1", "currency": "USD"}
        payload = {**_DESIGN_EXAMPLE, "execution": {**execution, field: value}}

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)

    def test_a_negative_file_count_is_rejected(self) -> None:
        payload = {
            **_DESIGN_EXAMPLE,
            "artifacts": {"branch": "b", "commit": "c", "changed_files": -1},
        }

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)

    def test_an_extra_field_is_rejected(self) -> None:
        """多出来的字段说明执行器与契约分叉了 —— 而分叉在这里不会被发现。"""
        payload = {**_DESIGN_EXAMPLE, "surprise": 1}

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)

    def test_an_empty_currency_is_rejected(self) -> None:
        """成本没有币种就没法和预算比。"""
        payload = {
            **_DESIGN_EXAMPLE,
            "execution": {"duration": 1, "tool_calls": 1, "cost": "1", "currency": ""},
        }

        with pytest.raises(ValidationError):
            ExecutionResult.model_validate(payload)


class TestTheDecimalBoundary:
    """跨 JSON 边界时，数字就是它的字面量。"""

    def test_a_decimal_literal_becomes_a_decimal(self) -> None:
        metrics = ExecutionMetrics.model_validate(
            {"duration": "360", "tool_calls": 35, "cost": "2.4", "currency": "USD"}
        )

        assert metrics.cost == Decimal("2.4")
        assert metrics.duration == Decimal("360")

    def test_a_float_is_converted_through_its_decimal_literal(self) -> None:
        """``Decimal(0.1)`` 是 0.1000000000000000055511151231257827。

        作者写的是 0.1 —— 所以按字面量转，不按二进制精确值转。
        """
        metrics = ExecutionMetrics.model_validate(
            {"duration": 1, "tool_calls": 1, "cost": 0.1, "currency": "USD"}
        )

        assert metrics.cost == Decimal("0.1")
        # 二进制精确值的指数是 -55（0.1000000000000000055511151231257827），
        # 而按字面量转出来是 -1（0.1）。
        assert metrics.cost.as_tuple().exponent == -1

    def test_an_integer_is_exact(self) -> None:
        metrics = ExecutionMetrics.model_validate(
            {"duration": 360, "tool_calls": 35, "cost": 2, "currency": "USD"}
        )

        assert metrics.cost == Decimal(2)

    def test_the_serialized_form_keeps_the_precision(self) -> None:
        """JSON 里存的是字符串 —— 浮点往返会掉精度，十进制不会。"""
        result = ExecutionResult.model_validate(
            {
                **_DESIGN_EXAMPLE,
                "execution": {
                    "duration": "1",
                    "tool_calls": 1,
                    "cost": "0.1",
                    "currency": "USD",
                },
            }
        )

        assert '"cost":"0.1"' in result.model_dump_json()


class TestTheResultIsFrozen:
    """结果落库之后不该被就地改写。"""

    def test_assigning_to_a_field_is_rejected(self) -> None:
        result = ExecutionResult.model_validate(_DESIGN_EXAMPLE)

        with pytest.raises(ValidationError):
            result.status = ExecutionStatus.FAILED

    def test_the_limitations_list_becomes_a_tuple(self) -> None:
        """跨 JSON 边界过来的是数组，落进模型就该是不可变的元组。"""
        result = ExecutionResult.model_validate(_DESIGN_EXAMPLE)

        assert result.self_assessment.limitations == ()
        assert isinstance(result.self_assessment.limitations, tuple)

    def test_a_non_empty_limitations_list_is_kept(self) -> None:
        payload = {
            **_DESIGN_EXAMPLE,
            "self_assessment": {"completed": False, "limitations": ["没写测试"]},
        }

        result = ExecutionResult.model_validate(payload)

        assert result.self_assessment.limitations == ("没写测试",)
