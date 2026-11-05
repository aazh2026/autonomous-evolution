"""TASK-P1-011 —— Policy Manager 与四级自主权判定器。

验证 design.md §7 的初始自主权限基线，并证明策略**来自外部配置**而非硬编码。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from core.policy.autonomy import (
    AutonomyLevel,
    AutonomyPolicy,
    DecisionKind,
    Operation,
    OperationRule,
    Permission,
    PolicyManager,
    TaskContext,
    default_policy,
    load_policy,
)

#: "L4 + 自主执行" —— 用它把某个操作的权限**往更宽松的方向**挪。
_L4_AUTONOMOUS = OperationRule(level=AutonomyLevel.L4_OPERATIONAL, permission=Permission.AUTONOMOUS)


def _force_set(
    rules: Mapping[Operation, OperationRule], key: Operation, value: OperationRule
) -> None:
    """强行就地改写规则映射。

    类型系统已经拦住了（``rules`` 声明为 ``Mapping``，不支持下标赋值），
    这里再手动绕过一次 —— 确认**运行时**同样拒绝。两道防线都要在：
    类型防线防"手滑"，运行时防线防"故意"。
    """
    writable = cast("dict[Operation, OperationRule]", rules)
    writable[key] = value


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


class TestPolicyCannotBeMutatedInProcess:
    """策略在进程内**不可改写** —— 这是 T3 的技术前提。

    design.md §7 / R-102~R-104 要求"policy 是外部配置 + 变更需审批"。
    如果进程内能直接改掉正在生效的规则，那条边界就是一句空话：
    谁改的、谁批的、改了什么，全都查不到。
    """

    def test_rules_mapping_rejects_item_assignment(self) -> None:
        policy = default_policy()
        with pytest.raises(TypeError, match="只读"):
            _force_set(policy.rules, Operation.MERGE_TO_MAIN, _L4_AUTONOMOUS)

    @pytest.mark.parametrize(
        "mutator",
        ["clear", "pop", "popitem", "setdefault", "update"],
    )
    def test_rules_mapping_rejects_every_mutator(self, mutator: str) -> None:
        """只挡住 ``__setitem__`` 是不够的 —— ``dict.update`` 是 C 实现的，
        不会走 ``__setitem__``，所以必须逐个堵。"""
        policy = default_policy()
        method = getattr(policy.rules, mutator)
        with pytest.raises(TypeError, match="只读"):
            if mutator in {"pop", "popitem"}:
                method(Operation.MERGE_TO_MAIN)
            elif mutator == "setdefault":
                method(Operation.MERGE_TO_MAIN, _L4_AUTONOMOUS)
            else:
                method({Operation.MERGE_TO_MAIN: _L4_AUTONOMOUS})

    def test_mutating_a_policy_does_not_affect_a_manager(self) -> None:
        """尝试改写被拒绝之后，判定结果必须原封不动。"""
        manager = PolicyManager.default()
        with pytest.raises(TypeError):
            _force_set(manager.policy.rules, Operation.MERGE_TO_MAIN, _L4_AUTONOMOUS)
        assert manager.check(Operation.MERGE_TO_MAIN).kind is DecisionKind.REQUIRE_APPROVAL
        assert manager.check(Operation.MERGE_TO_MAIN).kind is DecisionKind.REQUIRE_APPROVAL

    def test_policy_manager_exposes_no_mutating_api(self) -> None:
        """判定器上没有"改权限"的方法 —— 放宽权限的唯一路径是外部配置 + 审批。"""
        forbidden = ("set_", "update", "grant", "revoke", "override", "patch", "mutate", "relax")
        public = [name for name in dir(PolicyManager) if not name.startswith("_")]
        assert [name for name in public if name.startswith(forbidden)] == []

    def test_policy_still_serializes(self) -> None:
        """只读化不能破坏持久化 —— 策略要随项目落到 ``project.policy`` 列。"""
        dumped = default_policy().model_dump(mode="json")
        assert dumped["rules"]["merge_to_main"]["permission"] == "approval_required"
        assert AutonomyPolicy.model_validate(dumped) == default_policy()


class TestTaskContext:
    """任务级授权上下文（R-063：Agent 身份不能替代任务身份）。"""

    def _ctx(self, **kwargs: object) -> TaskContext:
        base: dict[str, object] = {"agent_id": "agent-1", "task_id": "task-1"}
        base.update(kwargs)
        return TaskContext(**base)  # type: ignore[arg-type]

    def test_without_context_policy_decision_stands(self) -> None:
        decision = PolicyManager.default().check(Operation.MERGE_TO_MAIN)
        assert decision.kind is DecisionKind.REQUIRE_APPROVAL
        assert decision.task_ctx is None

    def test_operation_outside_task_scope_is_denied(self) -> None:
        """策略允许改代码，但这个任务只被授权分析 —— 以任务范围为准。"""
        ctx = self._ctx(allowed_operations=frozenset({Operation.ANALYZE_PROJECT}))
        decision = PolicyManager.default().check(Operation.CODE_CHANGES, ctx)
        assert decision.kind is DecisionKind.DENY
        assert "授权范围" in decision.reason

    def test_operation_inside_task_scope_is_allowed(self) -> None:
        ctx = self._ctx(allowed_operations=frozenset({Operation.CODE_CHANGES}))
        decision = PolicyManager.default().check(Operation.CODE_CHANGES, ctx)
        assert decision.kind is DecisionKind.ALLOW

    def test_empty_scope_does_not_narrow(self) -> None:
        """空范围表示"不额外收窄"，不是"什么都不许做"。"""
        ctx = self._ctx()
        decision = PolicyManager.default().check(Operation.CODE_CHANGES, ctx)
        assert decision.kind is DecisionKind.ALLOW

    def test_expired_authorization_is_denied(self) -> None:
        past = datetime.now(UTC) - timedelta(minutes=1)
        ctx = self._ctx(expiry=past)
        decision = PolicyManager.default().check(Operation.CODE_CHANGES, ctx)
        assert decision.kind is DecisionKind.DENY
        assert "过期" in decision.reason

    def test_unexpired_authorization_passes(self) -> None:
        future = datetime.now(UTC) + timedelta(minutes=5)
        ctx = self._ctx(expiry=future)
        assert PolicyManager.default().check(Operation.CODE_CHANGES, ctx).kind is DecisionKind.ALLOW

    def test_task_scope_cannot_widen_a_stricter_policy(self) -> None:
        """任务范围更宽、策略更严时以策略为准 —— 任务上下文只能收紧。"""
        ctx = self._ctx(allowed_operations=frozenset(Operation))
        decision = PolicyManager.default().check(Operation.MERGE_TO_MAIN, ctx)
        assert decision.kind is DecisionKind.REQUIRE_APPROVAL

    def test_decision_records_attribution(self) -> None:
        """判定结果要带上任务身份，审计才追得到人。"""
        ctx = self._ctx(project_id="proj-1", workspace_id="ws-1")
        decision = PolicyManager.default().check(Operation.CODE_CHANGES, ctx)
        assert decision.task_ctx is ctx
        assert decision.task_ctx.task_id == "task-1"
        assert decision.task_ctx.agent_id == "agent-1"
        assert decision.task_ctx.project_id == "proj-1"
        assert decision.task_ctx.workspace_id == "ws-1"

    def test_same_agent_in_another_task_has_its_own_scope(self) -> None:
        """同一个 Agent 在 task A 里能改代码，在 task B 里不能 —— 身份不通用。"""
        manager = PolicyManager.default()
        in_a = self._ctx(task_id="task-a", allowed_operations=frozenset({Operation.CODE_CHANGES}))
        in_b = self._ctx(
            task_id="task-b", allowed_operations=frozenset({Operation.ANALYZE_PROJECT})
        )
        assert manager.check(Operation.CODE_CHANGES, in_a).kind is DecisionKind.ALLOW
        assert manager.check(Operation.CODE_CHANGES, in_b).kind is DecisionKind.DENY
