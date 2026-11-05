"""TASK-P1-014 —— Stop Conditions 求值与目标达成度计算。

验证（T1）：

- ① KR 达成度计算正确（构造已知指标值，断言达成 / 未达成）；
- ② 不可判定的 Stop Condition 在**启动时**报错，而非恒为 false。

另加 T2 的两条硬约束：达成度 ∈ [0, 1]，且随指标**单调**变化。

第二条（T1 ②）是这个模块存在的理由。一个引用了没人会提供的指标的条件，
和一个明确没满足的条件，含义完全不同：前者永远为 false，于是"停止演进"
这个能力形同虚设，而系统看上去一切正常。
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import pytest

from core.project.models import (
    Direction,
    KeyResult,
    MetricType,
    ProjectGoal,
    StopCondition,
    StopConditionKind,
    TargetValue,
    ValueKind,
)
from core.project.stop_conditions import (
    BUILTIN_FACTS,
    GoalAttainmentCalculator,
    PredicateError,
    StopConditionError,
    StopConditionEvaluator,
    evaluate_predicate,
    known_metrics_for,
    parse_predicate,
    validate_stop_conditions,
)

#: 每份目标都带一条"中性"停止条件。
#:
#: 六要素校验要求 Stop Conditions 非空，于是每份目标都得有一条。它选
#: ``human_stop_requested``：这些用例从不提供这个事实，所以它永远落在
#: "不可判定"里，既不会污染"已触发"的断言，也不影响达成度计算。
_BASE_CONDITION = StopCondition(
    id="SC-BASE",
    description="人工请求停止",
    kind=StopConditionKind.HUMAN_STOP_REQUESTED,
)

_DEFAULT_KEY_RESULTS: list[dict[str, object]] = [
    {"id": "KR-001", "metric": "e2e_success_rate", "target": ">= 0.95"},
]


def _goal(
    *stop_conditions: StopCondition,
    key_results: list[dict[str, object]] | None = None,
) -> ProjectGoal:
    """一份合法的目标：六要素齐全，KR 与停止条件可覆盖。"""
    return ProjectGoal.model_validate(
        {
            "objective": [{"id": "OBJ-001", "description": "把系统跑通"}],
            "key_results": key_results or _DEFAULT_KEY_RESULTS,
            "constraints": [
                {
                    "id": "C-001",
                    "description": "变更必须通过测试",
                    "kind": "all_changes_must_pass_tests",
                    "machine_checkable": True,
                    "expression": "tests_passed == true",
                }
            ],
            "non_goals": [{"id": "NG-001", "description": "不做移动端"}],
            "autonomy": {
                "rules": {
                    "analyze_project": {"level": "L4", "permission": "autonomous"},
                    "plan_tasks": {"level": "L4", "permission": "autonomous"},
                    "code_changes": {"level": "L4", "permission": "autonomous"},
                    "test_execution": {"level": "L4", "permission": "autonomous"},
                    "branch_creation": {"level": "L4", "permission": "autonomous"},
                    "merge_to_main": {"level": "L2", "permission": "approval_required"},
                    "production_deployment": {"level": "L1", "permission": "advisory_only"},
                    "modify_governance": {"level": "L1", "permission": "advisory_only"},
                }
            },
            "stop_conditions": list(stop_conditions) or [_BASE_CONDITION],
        }
    )


def _number(value: str) -> TargetValue:
    return TargetValue(kind=ValueKind.NUMBER, number=Decimal(value))


def _ids(conditions: tuple[StopCondition, ...]) -> set[str]:
    return {condition.id for condition in conditions}


class TestPredicateParsing:
    """表达式的三种形态。"""

    def test_comparison_expression_is_parsed(self) -> None:
        predicate = parse_predicate("cost_per_kr <= 12.5")

        assert predicate.metric == "cost_per_kr"
        assert predicate.operator is not None
        assert predicate.operator.value == "<="
        assert predicate.operand == Decimal("12.5")
        assert not predicate.use_threshold

    def test_bare_name_is_a_boolean_fact(self) -> None:
        predicate = parse_predicate("no_new_critical_defects")

        assert predicate.metric == "no_new_critical_defects"
        assert predicate.is_fact

    def test_threshold_token_is_recognised(self) -> None:
        predicate = parse_predicate("cost_per_kr <= X")

        assert predicate.use_threshold
        assert predicate.operand is None

    def test_dotted_metric_names_are_allowed(self) -> None:
        predicate = parse_predicate("tests.passed_rate >= 0.9")

        assert predicate.metric == "tests.passed_rate"

    @pytest.mark.parametrize(
        "expression",
        [
            "metric >> 3",  # 不存在的运算符
            "cost_per_kr <= abc",  # 右操作数不是数
            "",  # 空
            "metric >= 0.95 extra",  # 多余的东西
        ],
    )
    def test_unparseable_expression_is_rejected(self, expression: str) -> None:
        """解析不了就报错 —— 不能被当成"恒为 false"。"""
        with pytest.raises(PredicateError):
            parse_predicate(expression)

    def test_code_injection_attempt_is_rejected(self) -> None:
        """表达式来自外部配置，绝不能被当成代码执行。"""
        with pytest.raises(PredicateError):
            parse_predicate('__import__("os").system("rm -rf /")')


class TestPredicateEvaluation:
    """求值。"""

    def test_numeric_comparison_is_evaluated(self) -> None:
        predicate = parse_predicate("cost_per_kr <= 12.5")

        assert evaluate_predicate(predicate, {"cost_per_kr": Decimal("10")}) is True
        assert evaluate_predicate(predicate, {"cost_per_kr": Decimal("20")}) is False

    def test_boolean_fact_is_evaluated(self) -> None:
        predicate = parse_predicate("no_new_critical_defects")

        assert evaluate_predicate(predicate, {"no_new_critical_defects": True}) is True
        assert evaluate_predicate(predicate, {"no_new_critical_defects": False}) is False

    def test_missing_fact_raises_instead_of_returning_false(self) -> None:
        """缺事实与"没满足"必须能分开。"""
        predicate = parse_predicate("no_new_critical_defects")

        with pytest.raises(PredicateError):
            evaluate_predicate(predicate, {})

    def test_boolean_fact_cannot_be_compared(self) -> None:
        predicate = parse_predicate("budget_exhausted >= 1")

        with pytest.raises(PredicateError):
            evaluate_predicate(predicate, {"budget_exhausted": True})

    def test_threshold_operand_uses_the_condition_threshold(self) -> None:
        predicate = parse_predicate("cost_per_kr <= X")

        assert (
            evaluate_predicate(predicate, {"cost_per_kr": Decimal("10")}, threshold=Decimal("12"))
            is True
        )
        assert (
            evaluate_predicate(predicate, {"cost_per_kr": Decimal("10")}, threshold=Decimal("5"))
            is False
        )

    def test_threshold_operand_without_threshold_raises(self) -> None:
        predicate = parse_predicate("cost_per_kr <= X")

        with pytest.raises(PredicateError):
            evaluate_predicate(predicate, {"cost_per_kr": Decimal("10")})


class TestValidationAtStartup:
    """T1 ② —— 不可判定的停止条件必须在启动时报错。"""

    def test_valid_goal_passes_validation(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="预算耗尽就停",
                kind=StopConditionKind.BUDGET_EXHAUSTED,
            )
        )

        validate_stop_conditions(goal)  # 不抛错即通过

    def test_unregistered_metric_is_rejected(self) -> None:
        """引用了没人会提供的指标 —— 这条条件永远不会成立。"""
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="单位成本超了就停",
                kind=StopConditionKind.CUSTOM,
                expression="cost_per_kr <= 12.5",
            )
        )

        with pytest.raises(StopConditionError) as exc_info:
            validate_stop_conditions(goal)

        assert "cost_per_kr" in str(exc_info.value)
        assert exc_info.value.problems

    def test_registered_metric_is_accepted(self) -> None:
        """登记过的指标就能用 —— 这就是"登记"这个动作的意义。"""
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="单位成本超了就停",
                kind=StopConditionKind.CUSTOM,
                expression="cost_per_kr <= 12.5",
            )
        )

        validate_stop_conditions(goal, known_metrics={"cost_per_kr"})

    def test_key_result_metric_counts_as_known(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="成功率达标就停",
                kind=StopConditionKind.CUSTOM,
                expression="e2e_success_rate >= 0.99",
            )
        )

        validate_stop_conditions(goal)

    def test_custom_expression_needing_threshold_without_one_is_rejected(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="单位成本超了就停",
                kind=StopConditionKind.CUSTOM,
                expression="cost_per_kr <= X",
            )
        )

        with pytest.raises(StopConditionError) as exc_info:
            validate_stop_conditions(goal, known_metrics={"cost_per_kr"})

        assert "threshold" in str(exc_info.value)

    def test_unparseable_expression_is_rejected(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="坏掉的表达式",
                kind=StopConditionKind.CUSTOM,
                expression="metric >> 3",
            )
        )

        with pytest.raises(StopConditionError):
            validate_stop_conditions(goal)

    def test_missing_threshold_on_builtin_is_rejected(self) -> None:
        """模型层已强制这条，这里是纵深防御。

        绕过模型校验有两条路：``model_construct`` 造出不合法的条件，再用
        ``model_copy(update=...)`` 把它塞进目标 —— ``model_copy`` 不重跑校验器，
        所以这条非法状态能活到校验函数面前。走这条路是刻意的：模型层将来若
        放宽了约束，这道纵深防御得能独立地把关。
        """
        condition = StopCondition.model_construct(
            id="SC-001",
            description="跑太久了就停",
            kind=StopConditionKind.TIME_ELAPSED,
        )
        goal = _goal().model_copy(update={"stop_conditions": (condition,)})

        with pytest.raises(StopConditionError) as exc_info:
            validate_stop_conditions(goal)

        assert "threshold" in str(exc_info.value)

    def test_evaluator_validates_on_construction(self) -> None:
        """校验发生在构造时 —— 那就是"启动"。"""
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="单位成本超了就停",
                kind=StopConditionKind.CUSTOM,
                expression="cost_per_kr <= 12.5",
            )
        )

        with pytest.raises(StopConditionError):
            StopConditionEvaluator(goal)

    def test_known_metrics_include_builtins_and_key_results(self) -> None:
        goal = _goal()

        known = known_metrics_for(goal)

        assert known >= BUILTIN_FACTS
        assert "e2e_success_rate" in known


class TestStopEvaluation:
    """停止条件判定。"""

    def test_budget_exhausted_fires_when_the_fact_is_true(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="预算耗尽就停",
                kind=StopConditionKind.BUDGET_EXHAUSTED,
            )
        )
        evaluator = StopConditionEvaluator(goal)

        result = evaluator.evaluate({"budget_exhausted": True})

        assert result.stopped
        assert result.triggered_ids == ("SC-001",)

    def test_budget_exhausted_does_not_fire_when_the_fact_is_false(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="预算耗尽就停",
                kind=StopConditionKind.BUDGET_EXHAUSTED,
            )
        )
        evaluator = StopConditionEvaluator(goal)

        result = evaluator.evaluate({"budget_exhausted": False})

        assert not result.stopped
        assert result.triggered_ids == ()

    def test_consecutive_failures_fires_at_the_threshold(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="连续失败太多就停",
                kind=StopConditionKind.CONSECUTIVE_FAILURES_EXCEEDED,
                threshold=Decimal("3"),
            )
        )
        evaluator = StopConditionEvaluator(goal)

        assert not evaluator.evaluate({"consecutive_failures": Decimal("2")}).stopped
        assert evaluator.evaluate({"consecutive_failures": Decimal("3")}).stopped

    def test_no_new_high_severity_defects_fires_when_the_count_is_zero(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="没有新增高严重度缺陷就停",
                kind=StopConditionKind.NO_NEW_HIGH_SEVERITY_DEFECTS,
            )
        )
        evaluator = StopConditionEvaluator(goal)

        assert evaluator.evaluate({"new_high_severity_defects": Decimal("0")}).stopped
        assert not evaluator.evaluate({"new_high_severity_defects": Decimal("1")}).stopped

    def test_custom_expression_fires_when_its_fact_holds(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="单位成本超了就停",
                kind=StopConditionKind.CUSTOM,
                expression="cost_per_kr > 12.5",
            )
        )
        evaluator = StopConditionEvaluator(goal, known_metrics={"cost_per_kr"})

        assert evaluator.evaluate({"cost_per_kr": Decimal("20")}).stopped
        assert not evaluator.evaluate({"cost_per_kr": Decimal("5")}).stopped

    def test_all_key_results_met_fires_when_all_are_satisfied(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="关键结果全达成就停",
                kind=StopConditionKind.ALL_KEY_RESULTS_MET,
            )
        )
        evaluator = StopConditionEvaluator(goal)

        result = evaluator.evaluate({}, actuals={"KR-001": _number("0.96")})

        assert result.stopped
        assert result.triggered_ids == ("SC-001",)

    def test_all_key_results_met_does_not_fire_when_one_is_short(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="关键结果全达成就停",
                kind=StopConditionKind.ALL_KEY_RESULTS_MET,
            )
        )
        evaluator = StopConditionEvaluator(goal)

        result = evaluator.evaluate({}, actuals={"KR-001": _number("0.80")})

        assert not result.stopped
        assert result.triggered_ids == ()

    def test_missing_actuals_make_the_condition_undecidable_not_false(self) -> None:
        """没测 ≠ 没达成 —— 两者必须分开记。"""
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="关键结果全达成就停",
                kind=StopConditionKind.ALL_KEY_RESULTS_MET,
            )
        )
        evaluator = StopConditionEvaluator(goal)

        result = evaluator.evaluate({})

        assert not result.stopped
        assert "SC-001" in _ids(result.undecidable)
        assert "SC-001" not in _ids(result.triggered)

    def test_missing_fact_makes_the_condition_undecidable(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="预算耗尽就停",
                kind=StopConditionKind.BUDGET_EXHAUSTED,
            )
        )
        evaluator = StopConditionEvaluator(goal)

        result = evaluator.evaluate({})

        assert not result.stopped
        assert "SC-001" in _ids(result.undecidable)

    def test_reasons_are_human_readable(self) -> None:
        goal = _goal(
            StopCondition(
                id="SC-001",
                description="预算耗尽就停",
                kind=StopConditionKind.BUDGET_EXHAUSTED,
            )
        )
        evaluator = StopConditionEvaluator(goal)

        result = evaluator.evaluate({"budget_exhausted": True})

        assert result.reasons
        assert "SC-001" in result.reasons[0]
        assert "预算" in result.reasons[0]


class TestGoalAttainment:
    """T1 ① —— KR 达成度计算正确。"""

    def test_satisfied_key_result_has_attainment_one(self) -> None:
        goal = _goal()
        calculator = GoalAttainmentCalculator(goal)

        attainment = calculator.evaluate({"KR-001": _number("0.96")})

        assert attainment.per_key_result["KR-001"] == Decimal(1)
        assert attainment.satisfied == frozenset({"KR-001"})

    def test_unsatisfied_key_result_has_partial_attainment(self) -> None:
        """未达标不等于 0 —— 否则"差 1%"和"差 100%"看起来一样。"""
        goal = _goal()
        calculator = GoalAttainmentCalculator(goal)

        attainment = calculator.evaluate({"KR-001": _number("0.5")})

        assert attainment.per_key_result["KR-001"] == Decimal("0.5") / Decimal("0.95")

    def test_attainment_is_capped_at_one(self) -> None:
        goal = _goal()
        calculator = GoalAttainmentCalculator(goal)

        attainment = calculator.evaluate({"KR-001": _number("1.5")})

        assert attainment.per_key_result["KR-001"] == Decimal(1)

    def test_overall_is_the_mean_of_key_results(self) -> None:
        goal = _goal(
            key_results=[
                {"id": "KR-001", "metric": "e2e_success_rate", "target": ">= 0.95"},
                {"id": "KR-002", "metric": "svg_render_success_rate", "target": ">= 0.99"},
            ]
        )
        calculator = GoalAttainmentCalculator(goal)

        attainment = calculator.evaluate({"KR-001": _number("0.95"), "KR-002": _number("0.5")})

        expected = (Decimal(1) + Decimal("0.5") / Decimal("0.99")) / 2
        assert attainment.overall == expected

    def test_unmeasured_key_result_counts_as_zero_and_lowers_coverage(self) -> None:
        """没测不给通行券 —— 但"测得不全"要显式说出来。"""
        goal = _goal()
        calculator = GoalAttainmentCalculator(goal)

        attainment = calculator.evaluate({})

        assert attainment.per_key_result["KR-001"] == Decimal(0)
        assert attainment.overall == Decimal(0)
        assert attainment.coverage == Decimal(0)
        assert attainment.unmeasured == ("KR-001",)

    def test_coverage_counts_measured_key_results(self) -> None:
        goal = _goal()
        calculator = GoalAttainmentCalculator(goal)

        attainment = calculator.evaluate({"KR-001": _number("0.96")})

        assert attainment.coverage == Decimal(1)
        assert attainment.unmeasured == ()

    def test_lower_is_better_direction(self) -> None:
        """越小越好的指标：越接近目标，达成度越高。"""
        goal = _goal(
            key_results=[
                {
                    "id": "KR-001",
                    "metric": "p95_latency_ms",
                    "target": "<= 10",
                    "direction": Direction.LOWER_IS_BETTER.value,
                }
            ]
        )
        calculator = GoalAttainmentCalculator(goal)

        attainment = calculator.evaluate({"KR-001": _number("20")})

        assert attainment.per_key_result["KR-001"] == Decimal("0.5")

    def test_boolean_key_result(self) -> None:
        goal = _goal(
            key_results=[
                {
                    "id": "KR-001",
                    "metric": "has_smoke_test",
                    "target": "== true",
                    "metric_type": MetricType.BOOLEAN.value,
                }
            ]
        )
        calculator = GoalAttainmentCalculator(goal)

        attainment = calculator.evaluate({"KR-001": TargetValue(kind=ValueKind.BOOLEAN, flag=True)})

        assert attainment.per_key_result["KR-001"] == Decimal(1)
        assert attainment.satisfied == frozenset({"KR-001"})

    def test_delta_between_two_snapshots(self) -> None:
        """T2 —— "变好了"必须能被算出来，不能靠感觉。"""
        goal = _goal()
        calculator = GoalAttainmentCalculator(goal)

        before = calculator.evaluate({"KR-001": _number("0.80")})
        after = calculator.evaluate({"KR-001": _number("0.90")})

        assert before.delta(after) > 0
        assert after.delta(before) < 0


class TestAttainmentIsMonotonic:
    """T2 —— 达成度 ∈ [0, 1]，且随指标单调变化。"""

    def test_attainment_stays_within_zero_and_one(self) -> None:
        goal = _goal()
        calculator = GoalAttainmentCalculator(goal)

        for raw in ("-5", "0", "0.5", "0.94", "0.95", "1", "10"):
            attainment = calculator.evaluate({"KR-001": _number(raw)})
            assert Decimal(0) <= attainment.overall <= Decimal(1)
            assert Decimal(0) <= attainment.per_key_result["KR-001"] <= Decimal(1)

    def test_attainment_is_monotonic_in_a_higher_is_better_metric(self) -> None:
        goal = _goal()
        calculator = GoalAttainmentCalculator(goal)

        values = ["0", "0.1", "0.3", "0.5", "0.7", "0.9", "0.95", "1.0"]
        scores = [
            calculator.evaluate({"KR-001": _number(raw)}).per_key_result["KR-001"] for raw in values
        ]

        for earlier, later in pairwise(scores):
            assert later >= earlier

    def test_attainment_is_monotonic_in_a_lower_is_better_metric(self) -> None:
        """越小越好：指标变好时达成度不降。"""
        goal = _goal(
            key_results=[
                {
                    "id": "KR-001",
                    "metric": "p95_latency_ms",
                    "target": "<= 10",
                    "direction": Direction.LOWER_IS_BETTER.value,
                }
            ]
        )
        calculator = GoalAttainmentCalculator(goal)

        values = ["20", "15", "12", "10", "5", "1"]
        scores = [
            calculator.evaluate({"KR-001": _number(raw)}).per_key_result["KR-001"] for raw in values
        ]

        for earlier, later in pairwise(scores):
            assert later >= earlier


class TestProgress:
    """改进度 —— 基线到目标的差距关闭了多少。"""

    def test_progress_requires_a_baseline(self) -> None:
        """没有起点就说不出"走了多远"。"""
        goal = _goal()
        calculator = GoalAttainmentCalculator(goal)
        key_result = goal.key_result("KR-001")
        assert key_result is not None

        assert calculator.progress_for(key_result, _number("0.5")) is None

    def test_progress_measures_the_gap_closed(self) -> None:
        goal = _goal(
            key_results=[
                {
                    "id": "KR-001",
                    "metric": "e2e_success_rate",
                    "target": ">= 0.95",
                    "baseline": "0.50",
                }
            ]
        )
        calculator = GoalAttainmentCalculator(goal)
        key_result = goal.key_result("KR-001")
        assert key_result is not None

        progress = calculator.progress_for(key_result, _number("0.725"))

        assert progress == Decimal("0.5")

    def test_progress_is_one_when_satisfied(self) -> None:
        goal = _goal(
            key_results=[
                {
                    "id": "KR-001",
                    "metric": "e2e_success_rate",
                    "target": ">= 0.95",
                    "baseline": "0.50",
                }
            ]
        )
        calculator = GoalAttainmentCalculator(goal)
        key_result = goal.key_result("KR-001")
        assert key_result is not None

        assert calculator.progress_for(key_result, _number("0.96")) == Decimal(1)

    def test_progress_is_zero_at_the_baseline(self) -> None:
        goal = _goal(
            key_results=[
                {
                    "id": "KR-001",
                    "metric": "e2e_success_rate",
                    "target": ">= 0.95",
                    "baseline": "0.50",
                }
            ]
        )
        calculator = GoalAttainmentCalculator(goal)
        key_result = goal.key_result("KR-001")
        assert key_result is not None

        assert calculator.progress_for(key_result, _number("0.50")) == Decimal(0)


class TestKeyResultLookup:
    """``KeyResult`` 的取用不能靠猜。"""

    def test_key_result_can_be_looked_up_by_id(self) -> None:
        goal = _goal()

        key_result = goal.key_result("KR-001")

        assert isinstance(key_result, KeyResult)
        assert key_result.metric == "e2e_success_rate"

    def test_unknown_key_result_id_returns_none(self) -> None:
        goal = _goal()

        assert goal.key_result("KR-999") is None
