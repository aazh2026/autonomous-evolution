"""TASK-P1-011 —— Policy Manager 与四级自主权判定器。

验证 design.md §7 的初始自主权限基线，并证明策略**来自外部配置**而非硬编码。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.policy.autonomy import (
    AutonomyLevel,
    DecisionKind,
    Operation,
    OperationRule,
    Permission,
    PolicyManager,
    default_policy,
    load_policy,
)


class TestInitialBaseline:
    """design.md §7「建议的初始自主权限」基线。"""

    @pytest.mark.parametrize(
        "operation",
        [
            Operation.ANALYZE_PROJECT,
            Operation.PLAN_TASKS,
            Operation.CODE_CHANGES,
            Operation.TEST_EXECUTION,
            Operation.BRANCH_CREATION,
        ],
    )
    def test_autonomous_operations_are_allowed(self, operation: Operation) -> None:
        decision = PolicyManager.default().check(operation)
        assert decision.kind is DecisionKind.ALLOW
        assert decision.granted_level is AutonomyLevel.L4_OPERATIONAL

    def test_merge_to_main_requires_approval(self) -> None:
        decision = PolicyManager.default().check(Operation.MERGE_TO_MAIN)
        assert decision.kind is DecisionKind.REQUIRE_APPROVAL
        assert decision.needs_approval is True
        assert decision.granted_level is AutonomyLevel.L2_DEVELOPMENT

    @pytest.mark.parametrize(
        "operation",
        [Operation.PRODUCTION_DEPLOYMENT, Operation.MODIFY_GOVERNANCE],
    )
    def test_dangerous_operations_are_denied(self, operation: Operation) -> None:
        """生产部署与自改治理策略都只允许产出建议，不得执行。"""
        decision = PolicyManager.default().check(operation)
        assert decision.kind is DecisionKind.DENY
        assert decision.is_allowed is False
        assert decision.granted_level is AutonomyLevel.L1_ANALYSIS

    def test_modify_governance_is_advisory_only(self) -> None:
        """系统不得自主修改控制自身的治理策略。"""
        manager = PolicyManager.default()
        decision = manager.check(Operation.MODIFY_GOVERNANCE)
        assert decision.kind is DecisionKind.DENY
        assert "建议" in decision.reason


class TestPolicyIsExternal:
    """策略必须从外部配置读取 —— 否则系统无法"修改自身治理策略"。"""

    def test_changing_external_file_changes_decision(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(
            """
            rules:
              merge_to_main:
                level: L4
                permission: autonomous
              production_deployment:
                level: L4
                permission: autonomous
              modify_governance:
                level: L2
                permission: approval_required
              analyze_project:
                level: L4
                permission: autonomous
              plan_tasks:
                level: L4
                permission: autonomous
              code_changes:
                level: L4
                permission: autonomous
              test_execution:
                level: L4
                permission: autonomous
              branch_creation:
                level: L4
                permission: autonomous
            """,
            encoding="utf-8",
        )

        manager = PolicyManager.from_file(policy_file)

        assert manager.check(Operation.MERGE_TO_MAIN).kind is DecisionKind.ALLOW
        assert manager.check(Operation.PRODUCTION_DEPLOYMENT).kind is DecisionKind.ALLOW
        assert manager.check(Operation.MODIFY_GOVERNANCE).kind is DecisionKind.REQUIRE_APPROVAL

    def test_baseline_and_external_policy_differ(self, tmp_path: Path) -> None:
        """同一操作，内置基线与外部配置给出不同结论 —— 证明判定结果来自配置。"""
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(
            """
            rules:
              merge_to_main: {level: L4, permission: autonomous}
              production_deployment: {level: L4, permission: autonomous}
              modify_governance: {level: L4, permission: autonomous}
              analyze_project: {level: L4, permission: autonomous}
              plan_tasks: {level: L4, permission: autonomous}
              code_changes: {level: L4, permission: autonomous}
              test_execution: {level: L4, permission: autonomous}
              branch_creation: {level: L4, permission: autonomous}
            """,
            encoding="utf-8",
        )

        baseline = PolicyManager.default().check(Operation.MERGE_TO_MAIN)
        configured = PolicyManager.from_file(policy_file).check(Operation.MERGE_TO_MAIN)

        assert baseline.kind is DecisionKind.REQUIRE_APPROVAL
        assert configured.kind is DecisionKind.ALLOW

    def test_missing_file_falls_back_to_conservative_baseline(self, tmp_path: Path) -> None:
        """文件缺失时回退到内置基线 —— 回退结果仍必须保守。"""
        manager = load_policy(tmp_path / "does-not-exist.yaml")
        assert manager.check(Operation.MERGE_TO_MAIN).kind is (DecisionKind.REQUIRE_APPROVAL)
        assert manager.check(Operation.PRODUCTION_DEPLOYMENT).kind is DecisionKind.DENY

    def test_incomplete_policy_is_rejected(self, tmp_path: Path) -> None:
        """缺一条操作规则就拒绝加载 —— 缺条规则等于把决定权交给隐式默认值。"""
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(
            "rules:\n  merge_to_main: {level: L2, permission: approval_required}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="缺少以下操作的规则"):
            PolicyManager.from_file(policy_file)


class TestRuleConsistency:
    """等级与权限的组合必须自洽。"""

    def test_autonomous_requires_l4(self) -> None:
        with pytest.raises(ValueError, match="只有 L4"):
            OperationRule(level=AutonomyLevel.L3_INTEGRATION, permission=Permission.AUTONOMOUS)

    def test_advisory_only_requires_l1(self) -> None:
        with pytest.raises(ValueError, match="只有 L1"):
            OperationRule(level=AutonomyLevel.L2_DEVELOPMENT, permission=Permission.ADVISORY_ONLY)

    def test_approval_required_is_allowed_at_l2(self) -> None:
        rule = OperationRule(
            level=AutonomyLevel.L2_DEVELOPMENT, permission=Permission.APPROVAL_REQUIRED
        )
        assert rule.permission is Permission.APPROVAL_REQUIRED

    def test_default_policy_covers_every_operation(self) -> None:
        policy = default_policy()
        assert set(policy.rules) == set(Operation)
