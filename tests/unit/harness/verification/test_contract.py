"""验收契约 —— "什么算完成"必须先定下来，而且不能被事后改松。

验证（T1，对应 docs/tasks.md TASK-P1-040）：

1. 缺 ``required`` 中任一项的契约被拒绝；
2. 缺 ``task_id`` 绑定被拒绝；
3. 阈值运算符与值类型校验（如 ``>=`` 配字符串值报错）；
4. ``forbidden`` 命中任一条即整体判定失败（**不因其他项通过而放行**）。

**这个文件存在的理由，是把"契约是准入闸门而不是事后报告"这句话变成结构。**
只要契约能在执行之后补上，它就退化成一份事后报告 —— 执行之后写
``coverage >= 0.85`` 与写 ``coverage >= 0.01``，效果完全一样。

所以这里钉住三件事：

- 契约**必须绑定任务**，且拿 A 任务的契约放行 B 任务要被拒绝；
- **没拿到结果的必检项算失败**，不是"算过"；
- **forbidden 一票否决**：别的都过了也不行。

第三条尤其要单独钉 —— 如果判定逻辑写成"计分"（过了几项算几分）而不是
"与"逻辑（任何一项不过就整体不过），那"三项通过、一项命中密钥"就会显示成
"基本通过"。这正是 design.md §3.5B 要防的那种假通过。
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest
from pydantic import ValidationError

from core.project.models import TargetValue, ValueKind
from harness.verification.contract import (
    AcceptanceContract,
    ArtifactKind,
    CheckOutcome,
    CheckResult,
    ContractEvidence,
    ForbiddenRule,
    RequiredCheck,
    Threshold,
    evaluate_contract,
)

TASK_ID = "TASK-1024"

#: 覆盖率阈值 0.85，实测 0.95 —— 达标。
_COVERAGE_METRIC = "test_coverage"
_GOOD_COVERAGE = TargetValue(kind=ValueKind.NUMBER, number=Decimal("0.95"))
_ALL_ARTIFACTS = frozenset({ArtifactKind.GIT_DIFF, ArtifactKind.TEST_REPORT})


def _threshold(operator: str, value: object) -> Threshold:
    """造一条阈值。

    走 ``model_validate`` 而不是构造函数：阈值接受裸标量（``value: 0.85``），
    那是在 JSON / YAML 边界上的形态；构造函数静态签名要求已经是
    :class:`TargetValue`。
    """
    return Threshold.model_validate({"operator": operator, "value": value})


def _contract(**overrides: object) -> AcceptanceContract:
    """造一份契约。默认是"四项必检 + 一个覆盖率阈值 + 三条禁止项"。"""
    payload: dict[str, object] = {
        "task_id": TASK_ID,
        "required": (
            RequiredCheck.UNIT_TESTS_PASS,
            RequiredCheck.INTEGRATION_TESTS_PASS,
            RequiredCheck.LINT_PASS,
            RequiredCheck.ARCHITECTURE_RULES_PASS,
        ),
        "thresholds": {"test_coverage": _threshold(">=", 0.85)},
        "forbidden": (
            ForbiddenRule.SECRETS_IN_DIFF,
            ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE,
            ForbiddenRule.PUBLIC_API_BREAKING_CHANGE,
        ),
        "artifacts": (ArtifactKind.GIT_DIFF, ArtifactKind.TEST_REPORT),
    }
    payload.update(overrides)
    return AcceptanceContract.model_validate(payload)


def _all_passed() -> dict[RequiredCheck, CheckResult]:
    """四项必检全过。"""
    return {check: CheckResult(check, CheckOutcome.PASSED) for check in RequiredCheck}


def _evidence(
    *,
    task_id: str = TASK_ID,
    checks: dict[RequiredCheck, CheckResult] | None = None,
    metrics: dict[str, TargetValue] | None = None,
    violations: frozenset[ForbiddenRule] = frozenset(),
    produced_artifacts: frozenset[ArtifactKind] = _ALL_ARTIFACTS,
) -> ContractEvidence:
    """造一份"全部通过"的证据。"""
    return ContractEvidence(
        task_id=task_id,
        checks=_all_passed() if checks is None else checks,
        metrics={_COVERAGE_METRIC: _GOOD_COVERAGE} if metrics is None else metrics,
        violations=violations,
        produced_artifacts=produced_artifacts,
    )


class TestTheContractMustDeclareSomething:
    """required 不得为空 —— 什么都不要求的契约等于没有契约。"""

    def test_an_empty_required_list_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="required 不得为空"):
            _contract(required=())

    def test_a_contract_without_required_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AcceptanceContract.model_validate({"task_id": TASK_ID})

    def test_duplicate_required_entries_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="重复声明"):
            _contract(required=(RequiredCheck.LINT_PASS, RequiredCheck.LINT_PASS))

    def test_an_unknown_check_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _contract(required=("quality_wellness_pass",))


class TestTheContractIsBoundToATask:
    """契约必须有归属。"""

    def test_a_blank_task_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _contract(task_id="")

    def test_a_contract_without_task_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AcceptanceContract.model_validate(
                {"required": [RequiredCheck.UNIT_TESTS_PASS.value]}
            )

    def test_a_contract_for_another_task_does_not_pass_this_one(self) -> None:
        """拿 A 任务的契约放行 B 任务 —— 等于没有契约。"""
        contract = _contract(task_id="TASK-OTHER")

        result = evaluate_contract(contract, _evidence(task_id=TASK_ID))

        assert result.passed is False
        assert any("不符" in reason for reason in result.failures)


class TestTheThresholdsAreTypeChecked:
    """运算符与值类型必须匹配 —— 比不出结果不能被当成达标。"""

    def test_greater_or_equal_with_a_string_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="只能与数值阈值配对"):
            _threshold(">=", "高一点就行")

    def test_greater_than_with_a_boolean_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="只能与数值阈值配对"):
            _threshold(">", True)

    def test_equal_with_a_text_value_is_accepted(self) -> None:
        """``==`` 比的是相等，不是大小 —— 文本值合法。"""
        threshold = _threshold("==", "ok")

        assert threshold.value.kind is ValueKind.TEXT

    def test_a_numeric_literal_is_accepted(self) -> None:
        threshold = _threshold(">=", 0.85)

        assert threshold.value.kind is ValueKind.NUMBER
        assert threshold.value.number == Decimal("0.85")

    def test_a_numeric_string_is_read_as_a_number(self) -> None:
        threshold = _threshold(">=", "0.85")

        assert threshold.value.kind is ValueKind.NUMBER

    def test_a_text_threshold_is_not_satisfied_by_a_number(self) -> None:
        """类型不匹配要报错，而不是悄悄返回 False。"""
        threshold = _threshold("==", "ok")

        with pytest.raises(ValueError, match="无法与"):
            threshold.evaluate(TargetValue(kind=ValueKind.NUMBER, number=Decimal("1")))


class TestTheThresholdsCannotBeLoosenedInPlace:
    """阈值是验收口径，不能在进程内就地改松。"""

    def test_the_threshold_mapping_rejects_item_assignment(self) -> None:
        contract = _contract()

        with pytest.raises(TypeError, match="只读"):
            contract.thresholds["test_coverage"] = _threshold(">=", 0.01)

    def test_the_threshold_mapping_rejects_update(self) -> None:
        contract = _contract()

        with pytest.raises(TypeError, match="只读"):
            contract.thresholds.update({"x": _threshold(">=", 1)})

    def test_the_contract_itself_cannot_be_reassigned(self) -> None:
        contract = _contract()

        with pytest.raises(ValidationError):
            contract.task_id = "TASK-OTHER"


class TestTheEvaluation:
    """判定规则。"""

    def test_everything_passing_passes(self) -> None:
        result = evaluate_contract(_contract(), _evidence())

        assert result.passed is True
        assert result.failures == ()

    def test_a_missing_required_check_is_a_failure_not_a_pass(self) -> None:
        """没产出结果的必检项算失败 —— "没验过"不等于"验过了"。"""
        checks = _all_passed()
        del checks[RequiredCheck.LINT_PASS]

        result = evaluate_contract(_contract(), _evidence(checks=checks))

        assert result.passed is False
        assert RequiredCheck.LINT_PASS.value in result.missing
        assert any("没有产出结果" in reason for reason in result.failures)

    def test_a_failed_required_check_fails_the_contract(self) -> None:
        checks = _all_passed()
        checks[RequiredCheck.UNIT_TESTS_PASS] = CheckResult(
            RequiredCheck.UNIT_TESTS_PASS, CheckOutcome.FAILED, "3 个用例失败"
        )

        result = evaluate_contract(_contract(), _evidence(checks=checks))

        assert result.passed is False
        assert any("3 个用例失败" in reason for reason in result.failures)

    def test_an_errored_check_is_distinguished_from_a_failed_one(self) -> None:
        """"没跑成"与"跑了没过"必须分开 —— 处置完全不同。"""
        checks = _all_passed()
        checks[RequiredCheck.UNIT_TESTS_PASS] = CheckResult(
            RequiredCheck.UNIT_TESTS_PASS, CheckOutcome.ERROR, "pytest 装不上"
        )

        result = evaluate_contract(_contract(), _evidence(checks=checks))

        assert result.passed is False
        assert any("执行出错" in reason for reason in result.failures)
        assert not any("未通过" in reason for reason in result.failures)

    def test_a_threshold_below_the_target_fails_the_contract(self) -> None:
        low = TargetValue(kind=ValueKind.NUMBER, number=Decimal("0.80"))

        result = evaluate_contract(_contract(), _evidence(metrics={_COVERAGE_METRIC: low}))

        assert result.passed is False
        assert any("未达标" in reason for reason in result.failures)

    def test_a_missing_metric_fails_the_contract(self) -> None:
        """缺实测值算失败 —— "无法判断"不能被当成"没问题"。"""
        result = evaluate_contract(_contract(), _evidence(metrics={}))

        assert result.passed is False
        assert _COVERAGE_METRIC in result.missing

    def test_an_incomparable_metric_fails_rather_than_crashing(self) -> None:
        """类型不匹配算失败，不是异常 —— 判定不该把调用方炸穿。"""
        text = TargetValue(kind=ValueKind.TEXT, text="很高")

        result = evaluate_contract(_contract(), _evidence(metrics={_COVERAGE_METRIC: text}))

        assert result.passed is False
        assert any("未能判定" in reason for reason in result.failures)

    def test_a_missing_artifact_fails_the_contract(self) -> None:
        """证据缺失时"没证据"不等于"没问题"。"""
        result = evaluate_contract(
            _contract(), _evidence(produced_artifacts=frozenset({ArtifactKind.GIT_DIFF}))
        )

        assert result.passed is False
        assert ArtifactKind.TEST_REPORT.value in result.missing


class TestTheForbiddenRulesAreOneShot:
    """forbidden 一票否决 —— 不因其他项通过而放行。"""

    def test_a_secrets_violation_fails_even_when_everything_else_passes(self) -> None:
        result = evaluate_contract(
            _contract(),
            _evidence(violations=frozenset({ForbiddenRule.SECRETS_IN_DIFF})),
        )

        assert result.passed is False
        assert any("命中禁止项" in reason for reason in result.failures)
        assert any("secrets_in_diff" in reason for reason in result.failures)

    def test_every_forbidden_rule_can_veto(self) -> None:
        for rule in ForbiddenRule:
            result = evaluate_contract(
                _contract(), _evidence(violations=frozenset({rule}))
            )

            assert result.passed is False, f"{rule.value} 应当一票否决"

    def test_a_violation_of_a_rule_outside_the_contract_is_not_a_contract_failure(self) -> None:
        """契约没声明的禁止项不由契约判定 —— 它由安全门自己把关。"""
        contract = _contract(forbidden=(ForbiddenRule.SECRETS_IN_DIFF,))

        result = evaluate_contract(
            contract,
            _evidence(violations=frozenset({ForbiddenRule.PUBLIC_API_BREAKING_CHANGE})),
        )

        assert result.passed is True

    def test_the_veto_is_not_measured_by_a_score(self) -> None:
        """判定是"与"逻辑，不是计分 —— 三项通过抵不掉一项命中。"""
        checks = _all_passed()
        checks[RequiredCheck.UNIT_TESTS_PASS] = CheckResult(
            RequiredCheck.UNIT_TESTS_PASS, CheckOutcome.FAILED
        )

        result = evaluate_contract(
            _contract(),
            _evidence(checks=checks, violations=frozenset({ForbiddenRule.SECRETS_IN_DIFF})),
        )

        assert result.passed is False


class TestTheDocumentRoundTrip:
    """契约要能落库，也要能原样取回。"""

    def test_a_contract_survives_the_round_trip(self) -> None:
        contract = _contract()

        restored = AcceptanceContract.from_document(contract.to_document())

        assert restored == contract

    def test_the_document_is_readable(self) -> None:
        """这一列会被人直接查 —— 读不懂的形态等于没有。"""
        document = _contract().to_document()
        thresholds = cast(dict[str, dict[str, str]], document["thresholds"])

        assert document["task_id"] == TASK_ID
        assert document["required"] == [check.value for check in RequiredCheck]
        assert thresholds["test_coverage"]["operator"] == ">="

    def test_a_broken_document_is_rejected_rather_than_defaulted(self) -> None:
        """还原失败要当场抛错 —— 默默补默认值等于绕过准入闸门。"""
        with pytest.raises(ValueError, match="必须是对象"):
            AcceptanceContract.from_document({"thresholds": "不是对象"})

    def test_a_document_with_a_blank_task_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AcceptanceContract.from_document(
                {"task_id": "", "required": [RequiredCheck.LINT_PASS.value]}
            )
