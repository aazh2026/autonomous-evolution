"""TASK-P1-010 —— 项目目标结构化六要素模型。

验证三件事：非法目标载入报错并指出缺哪一项；同一 metric 可配不同 unit 与
eval_method；不硬编码任何指标名。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from core.policy.autonomy import Operation, Permission
from core.project.models import (
    Constraint,
    ConstraintKind,
    Direction,
    GoalValidationError,
    KeyResult,
    MetricType,
    NonGoal,
    Objective,
    Project,
    ProjectGoal,
    StopCondition,
    StopConditionKind,
    TargetExpression,
    TargetValue,
    load_project,
)

# 完整合法的六要素基线
VALID_GOAL: dict[str, object] = {
    "objective": [{"id": "OBJ-001", "description": "建成完整的在线PPT制作平台"}],
    "key_results": [
        {
            "id": "KR-001",
            "metric": "e2e_success_rate",
            "target": ">= 0.95",
        }
    ],
    "constraints": [
        {
            "id": "C-001",
            "description": "所有代码变更必须通过自动化测试",
            "kind": "all_changes_must_pass_tests",
            "machine_checkable": True,
            "expression": "tests_passed == true",
        }
    ],
    "non_goals": [{"id": "NG-001", "description": "不做移动端适配"}],
    "autonomy": {
        "rules": {
            "code_changes": {"level": "L4", "permission": "autonomous"},
            "test_execution": {"level": "L4", "permission": "autonomous"},
            "branch_creation": {"level": "L4", "permission": "autonomous"},
            "merge_to_main": {"level": "L2", "permission": "approval_required"},
            "production_deployment": {"level": "L1", "permission": "advisory_only"},
            "modify_governance": {"level": "L1", "permission": "advisory_only"},
            "analyze_project": {"level": "L4", "permission": "autonomous"},
            "plan_tasks": {"level": "L4", "permission": "autonomous"},
        }
    },
    "stop_conditions": [
        {
            "id": "SC-001",
            "description": "全部关键结果达成",
            "kind": "all_key_results_met",
        }
    ],
}


def _project_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "my-product",
        "name": "在线PPT制作平台",
        "goal": dict(VALID_GOAL),
        "repository": {"url": "https://github.com/example/my-product"},
        "budget": {"currency": "USD", "max_daily_cost": "50"},
    }
    payload.update(overrides)
    return payload


class TestSixElementsRequired:
    """六要素缺一不可，且错误要指出缺哪一项。"""

    @pytest.mark.parametrize(
        ("element", "expected_label"),
        [
            ("objective", "Objective"),
            ("key_results", "Key Results"),
            ("constraints", "Constraints"),
            ("non_goals", "Non-goals"),
            ("autonomy", "Autonomy Policy"),
            ("stop_conditions", "Stop Conditions"),
        ],
    )
    def test_missing_element_is_reported(self, element: str, expected_label: str) -> None:
        goal = dict(VALID_GOAL)
        del goal[element]

        with pytest.raises(GoalValidationError) as exc_info:
            ProjectGoal.model_validate(goal)

        assert exc_info.value.missing_elements == [expected_label]
        assert expected_label in str(exc_info.value)

    def test_multiple_missing_elements_are_all_reported(self) -> None:
        goal = dict(VALID_GOAL)
        del goal["objective"]
        del goal["stop_conditions"]

        with pytest.raises(GoalValidationError) as exc_info:
            ProjectGoal.model_validate(goal)

        assert set(exc_info.value.missing_elements) == {"Objective", "Stop Conditions"}

    def test_empty_list_counts_as_missing(self) -> None:
        goal = dict(VALID_GOAL)
        goal["key_results"] = []

        with pytest.raises(GoalValidationError) as exc_info:
            ProjectGoal.model_validate(goal)

        assert exc_info.value.missing_elements == ["Key Results"]

    def test_valid_goal_loads(self) -> None:
        goal = ProjectGoal.model_validate(VALID_GOAL)
        assert len(goal.objective) == 1
        assert len(goal.key_results) == 1
        assert (
            goal.autonomy.rule_for(Operation.MERGE_TO_MAIN).permission
            is Permission.APPROVAL_REQUIRED
        )


class TestKeyResultValidation:
    """KR 的 metric 与 target 校验。"""

    def test_missing_metric_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            KeyResult.model_validate({"id": "KR-001", "target": ">= 0.95"})

    def test_blank_metric_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="metric"):
            KeyResult.model_validate({"id": "KR-001", "metric": "   ", "target": ">= 0.95"})

    def test_non_comparable_target_is_rejected(self) -> None:
        """target 不是可比较表达式 → 载入报错。"""
        with pytest.raises(GoalValidationError, match="不可比较"):
            TargetExpression.parse("大约零点九五")

    def test_target_without_operator_is_rejected(self) -> None:
        with pytest.raises(GoalValidationError, match="不可比较"):
            TargetExpression.parse("0.95")

    def test_metric_type_must_match_target_type(self) -> None:
        with pytest.raises(ValueError, match="不匹配"):
            KeyResult.model_validate(
                {
                    "id": "KR-001",
                    "metric": "feature_flag",
                    "target": ">= 0.95",
                    "metric_type": MetricType.BOOLEAN,
                }
            )


class TestMetricIsNotHardcoded:
    """同一 metric 可配不同 unit 与 eval_method —— 不硬编码 e2e_success_rate。"""

    def test_same_metric_different_unit_and_eval_method(self) -> None:
        first = KeyResult.model_validate(
            {
                "id": "KR-001",
                "metric": "coverage",
                "target": ">= 0.85",
                "unit": "ratio",
                "eval_method": "ci_measurement",
            }
        )
        second = KeyResult.model_validate(
            {
                "id": "KR-002",
                "metric": "coverage",
                "target": ">= 85",
                "unit": "percent",
                "eval_method": "external_query",
            }
        )
        assert first.metric == second.metric
        assert first.unit != second.unit
        assert first.eval_method is not second.eval_method

    def test_arbitrary_metric_names_are_accepted(self) -> None:
        for metric in ["e2e_success_rate", "p99_latency_ms", "任意中文指标", "x"]:
            key_result = KeyResult.model_validate({"id": "KR", "metric": metric, "target": ">= 1"})
            assert key_result.metric == metric

    def test_metric_type_and_direction_are_configurable(self) -> None:
        key_result = KeyResult.model_validate(
            {
                "id": "KR-001",
                "metric": "p99_latency_ms",
                "target": "<= 250",
                "unit": "ms",
                "metric_type": "duration",
                "eval_method": "automated_test",
                "direction": "lower_is_better",
            }
        )
        assert key_result.metric_type is MetricType.DURATION
        assert key_result.direction is Direction.LOWER_IS_BETTER


class TestTargetEvaluation:
    """目标求值。"""

    def test_numeric_comparison(self) -> None:
        target = TargetExpression.parse(">= 0.95")
        assert target.evaluate(TargetValue.parse("0.96")) is True
        assert target.evaluate(TargetValue.parse("0.94")) is False

    def test_decimal_precision_is_exact(self) -> None:
        """用 Decimal 而非 float —— 0.1 + 0.2 类误差不能影响达标判定。"""
        target = TargetExpression.parse(">= 0.30")
        assert target.evaluate(TargetValue.parse("0.1")) is False
        assert target.evaluate(TargetValue.parse("0.30")) is True

    def test_boolean_comparison(self) -> None:
        target = TargetExpression.parse("== true")
        assert target.evaluate(TargetValue.parse("true")) is True
        assert target.evaluate(TargetValue.parse("false")) is False

    def test_mismatched_types_raise(self) -> None:
        target = TargetExpression.parse(">= 0.95")
        with pytest.raises(GoalValidationError, match="无法比较"):
            target.evaluate(TargetValue.parse("true"))

    def test_gap_calculation(self) -> None:
        key_result = KeyResult.model_validate(
            {"id": "KR-001", "metric": "coverage", "target": ">= 0.9"}
        )
        gap = key_result.gap(TargetValue.parse("0.75"))
        assert gap == Decimal("0.15")

    def test_gap_is_zero_when_satisfied(self) -> None:
        key_result = KeyResult.model_validate(
            {"id": "KR-001", "metric": "coverage", "target": ">= 0.9"}
        )
        assert key_result.gap(TargetValue.parse("0.95")) == Decimal("0")


class TestConstraintsAndStopConditions:
    """约束与停止条件必须机器可判定。"""

    def test_machine_checkable_requires_expression(self) -> None:
        with pytest.raises(ValueError, match="expression"):
            Constraint(
                id="C-001",
                description="变更必须通过测试",
                kind=ConstraintKind.ALL_CHANGES_MUST_PASS_TESTS,
                machine_checkable=True,
            )

    def test_expression_requires_machine_checkable(self) -> None:
        with pytest.raises(ValueError, match="machine_checkable"):
            Constraint(
                id="C-001",
                description="变更必须通过测试",
                kind=ConstraintKind.CUSTOM,
                expression="tests_passed == true",
            )

    def test_custom_stop_condition_requires_expression(self) -> None:
        with pytest.raises(ValueError, match="expression"):
            StopCondition(
                id="SC-001",
                description="自定义停止条件",
                kind=StopConditionKind.CUSTOM,
            )

    def test_threshold_required_for_count_based_stop_condition(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            StopCondition(
                id="SC-001",
                description="连续失败超过上限",
                kind=StopConditionKind.CONSECUTIVE_FAILURES_EXCEEDED,
            )

    def test_threshold_accepted_when_provided(self) -> None:
        condition = StopCondition(
            id="SC-001",
            description="连续失败超过上限",
            kind=StopConditionKind.CONSECUTIVE_FAILURES_EXCEEDED,
            threshold=Decimal("3"),
        )
        assert condition.threshold == Decimal("3")


class TestYamlLoading:
    """YAML 载入。"""

    def test_load_from_yaml(self) -> None:
        project = Project.from_yaml(
            """
            project:
              id: my-product
              name: 在线PPT制作平台
              objective:
                - 建成完整的在线PPT制作平台
              key_results:
                - id: KR-001
                  metric: e2e_success_rate
                  target: ">= 0.95"
              constraints:
                - id: C-001
                  description: 所有代码变更必须通过自动化测试
                  kind: all_changes_must_pass_tests
                  machine_checkable: true
                  expression: tests_passed == true
              non_goals:
                - id: NG-001
                  description: 不做移动端适配
              autonomy:
                rules:
                  code_changes: {level: L4, permission: autonomous}
                  test_execution: {level: L4, permission: autonomous}
                  branch_creation: {level: L4, permission: autonomous}
                  merge_to_main: {level: L2, permission: approval_required}
                  production_deployment: {level: L1, permission: advisory_only}
                  modify_governance: {level: L1, permission: advisory_only}
                  analyze_project: {level: L4, permission: autonomous}
                  plan_tasks: {level: L4, permission: autonomous}
              stop_conditions:
                - id: SC-001
                  description: 全部关键结果达成
                  kind: all_key_results_met
              repository:
                url: https://github.com/example/my-product
            """
        )
        assert project.id == "my-product"
        assert len(project.goal.key_results) == 1

    def test_missing_element_in_yaml_reports_which(self) -> None:
        with pytest.raises(GoalValidationError) as exc_info:
            Project.from_yaml(
                """
                id: my-product
                name: x
                objective:
                  - 建成平台
                repository:
                  url: https://example.com/x
                """
            )
        assert "Key Results" in exc_info.value.missing_elements

    def test_objective_accepts_plain_string(self) -> None:
        objective = Objective.model_validate("建成完整的在线PPT制作平台")
        assert objective.description == "建成完整的在线PPT制作平台"
        assert objective.id.startswith("OBJ-")

    def test_non_goal_accepts_plain_string(self) -> None:
        non_goal = NonGoal.model_validate("不做移动端适配")
        assert non_goal.description == "不做移动端适配"

    def test_example_project_file_is_valid(self) -> None:
        """docs/examples/project.yaml 必须是合法示例。"""
        example = Path(__file__).resolve().parents[3] / "docs" / "examples" / "project.yaml"
        assert example.exists(), f"缺少示例文件：{example}"
        project = load_project(example)
        assert project.goal.key_results
        assert project.goal.objective
