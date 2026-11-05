"""TASK-P1-013 —— Project Registry。

核心断言：**注册 2 个项目后，按 id 查询各自返回独立的目标与 policy，互不污染**。
隔离是多项目场景下最容易出问题、后果又最重的地方 —— 一个项目拿了另一个项目的
权限去合并代码，比单项目跑不通危险得多。
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from core.policy.autonomy import AutonomyLevel, Operation, OperationRule, Permission
from core.project.models import Project
from core.project.registry import (
    DuplicateProjectError,
    MissingApprovalError,
    ProjectNotFoundError,
    ProjectRegistry,
)


def _goal(
    objective: str,
    metric: str,
    merge_level: str = "L2",
    merge_permission: str = "approval_required",
) -> dict[str, object]:
    """一份完整的六要素。``merge_to_main`` 的档位可调 —— 用它区分两个项目的权限。"""
    return {
        "objective": [{"id": "OBJ-001", "description": objective}],
        "key_results": [{"id": "KR-001", "metric": metric, "target": ">= 0.95"}],
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
                "merge_to_main": {"level": merge_level, "permission": merge_permission},
                "production_deployment": {"level": "L1", "permission": "advisory_only"},
                "modify_governance": {"level": "L1", "permission": "advisory_only"},
                "analyze_project": {"level": "L4", "permission": "autonomous"},
                "plan_tasks": {"level": "L4", "permission": "autonomous"},
            }
        },
        "stop_conditions": [
            {"id": "SC-001", "description": "全部关键结果达成", "kind": "all_key_results_met"}
        ],
    }


def _project(project_id: str, **overrides: object) -> Project:
    """构造一个合法项目。``overrides`` 直接覆盖 payload 里的字段。"""
    payload: dict[str, object] = {
        "id": project_id,
        "name": f"项目 {project_id}",
        "goal": _goal("建成在线PPT平台", "e2e_success_rate"),
        "repository": {"url": "https://github.com/example/my-product"},
        "budget": {"currency": "USD", "max_daily_cost": "50"},
        "tech_stack": ["python"],
        "environment": {},
    }
    payload.update(overrides)
    return Project.model_validate(payload)


class TestRegistration:
    """注册、查询、列举。"""

    def test_register_and_get(self) -> None:
        registry = ProjectRegistry()
        project = _project("alpha")
        registry.register(project)
        assert registry.get("alpha") is project

    def test_duplicate_id_is_rejected(self) -> None:
        """静默覆盖等于允许把一个项目的策略换成另一个的，且不留痕迹。"""
        registry = ProjectRegistry([_project("alpha")])
        with pytest.raises(DuplicateProjectError) as exc_info:
            registry.register(_project("alpha", goal=_goal("别的目标", "other_metric")))
        assert exc_info.value.project_id == "alpha"
        assert registry.get("alpha").goal.objective[0].description == "建成在线PPT平台"

    def test_get_missing_project_raises(self) -> None:
        registry = ProjectRegistry()
        with pytest.raises(ProjectNotFoundError) as exc_info:
            registry.get("nope")
        assert exc_info.value.project_id == "nope"

    def test_find_returns_none_for_missing(self) -> None:
        assert ProjectRegistry().find("nope") is None

    def test_list_all_is_sorted_by_id(self) -> None:
        registry = ProjectRegistry([_project("beta"), _project("alpha")])
        assert [p.id for p in registry.list_all()] == ["alpha", "beta"]

    def test_len_contains_and_iter(self) -> None:
        registry = ProjectRegistry([_project("alpha"), _project("beta")])
        assert len(registry) == 2
        assert "alpha" in registry
        assert "gamma" not in registry
        assert [p.id for p in registry] == ["alpha", "beta"]

    def test_by_repository(self) -> None:
        registry = ProjectRegistry(
            [
                _project("alpha", repository={"url": "https://github.com/example/a"}),
                _project("beta", repository={"url": "https://github.com/example/b"}),
            ]
        )
        found = registry.by_repository("https://github.com/example/b")
        assert [p.id for p in found] == ["beta"]
        assert registry.by_repository("https://github.com/example/none") == ()


class TestIsolation:
    """两个项目互不污染 —— 这条是这个模块存在的理由。"""

    def test_each_project_keeps_its_own_goal(self) -> None:
        registry = ProjectRegistry(
            [
                _project("alpha", goal=_goal("A 的目标", "a_metric")),
                _project("beta", goal=_goal("B 的目标", "b_metric")),
            ]
        )
        assert registry.get("alpha").goal.objective[0].description == "A 的目标"
        assert registry.get("beta").goal.objective[0].description == "B 的目标"
        assert registry.get("alpha").goal.key_results[0].metric == "a_metric"
        assert registry.get("beta").goal.key_results[0].metric == "b_metric"

    def test_each_project_keeps_its_own_policy(self) -> None:
        """A 要审批合并、B 自动合并 —— 两个项目的权限不能串。"""
        registry = ProjectRegistry(
            [
                _project("alpha", goal=_goal("A 的目标", "a_metric")),
                _project(
                    "beta",
                    goal=_goal("B 的目标", "b_metric", "L4", "autonomous"),
                ),
            ]
        )
        alpha_rule = registry.get("alpha").goal.autonomy.rule_for(Operation.MERGE_TO_MAIN)
        beta_rule = registry.get("beta").goal.autonomy.rule_for(Operation.MERGE_TO_MAIN)
        assert alpha_rule.permission is Permission.APPROVAL_REQUIRED
        assert alpha_rule.level is AutonomyLevel.L2_DEVELOPMENT
        assert beta_rule.permission is Permission.AUTONOMOUS
        assert beta_rule.level is AutonomyLevel.L4_OPERATIONAL

    def test_each_project_keeps_its_own_budget(self) -> None:
        registry = ProjectRegistry(
            [
                _project("alpha", budget={"currency": "USD", "max_daily_cost": "50"}),
                _project("beta", budget={"currency": "CNY", "max_daily_cost": "500"}),
            ]
        )
        assert registry.get("alpha").budget.currency == "USD"
        assert registry.get("alpha").budget.max_daily_cost == Decimal("50")
        assert registry.get("beta").budget.currency == "CNY"
        assert registry.get("beta").budget.max_daily_cost == Decimal("500")

    def test_each_project_keeps_its_own_environment(self) -> None:
        registry = ProjectRegistry(
            [
                _project("alpha", environment={"target": "staging"}),
                _project("beta", environment={"target": "prod", "region": "cn-north-1"}),
            ]
        )
        assert dict(registry.get("alpha").environment) == {"target": "staging"}
        assert dict(registry.get("beta").environment) == {"target": "prod", "region": "cn-north-1"}

    def test_registered_project_is_immutable(self) -> None:
        """注册之后再改不动 —— 隔离不是靠约定，是靠类型。"""
        registry = ProjectRegistry([_project("alpha")])
        rules = registry.get("alpha").goal.autonomy.rules
        with pytest.raises(TypeError):
            # 类型系统已禁止下标赋值；这里手动绕过一次，确认运行时也拦得住。
            writable = cast("dict[Operation, OperationRule]", rules)
            writable[Operation.MERGE_TO_MAIN] = writable[Operation.CODE_CHANGES]

    def test_environment_mapping_is_immutable(self) -> None:
        environment = _project("alpha", environment={"target": "staging"}).environment
        with pytest.raises(TypeError):
            writable = cast("dict[str, str]", environment)
            writable["target"] = "prod"


class TestPolicyChangeNeedsApproval:
    """策略变更是治理动作，必须留审批痕迹。"""

    def test_replace_requires_approval_ref(self) -> None:
        registry = ProjectRegistry([_project("alpha")])
        with pytest.raises(MissingApprovalError):
            registry.replace(
                _project("alpha", goal=_goal("A 的目标", "a_metric", "L4", "autonomous")),
                approval_ref="  ",
            )

    def test_replace_updates_project_and_records_audit(self) -> None:
        registry = ProjectRegistry([_project("alpha")])
        updated = _project("alpha", goal=_goal("A 的目标", "a_metric", "L4", "autonomous"))
        registry.replace(updated, approval_ref="PR-42")

        assert (
            registry.get("alpha").goal.autonomy.rule_for(Operation.MERGE_TO_MAIN).permission
            is Permission.AUTONOMOUS
        )
        changes = registry.changes()
        assert len(changes) == 1
        assert changes[0].project_id == "alpha"
        assert changes[0].approval_ref == "PR-42"
        assert (
            changes[0].previous.goal.autonomy.rule_for(Operation.MERGE_TO_MAIN).permission
            is Permission.APPROVAL_REQUIRED
        )

    def test_replace_does_not_touch_other_projects(self) -> None:
        """改 A 的策略不能影响 B —— 治理变更同样有隔离要求。"""
        registry = ProjectRegistry([_project("alpha"), _project("beta")])
        registry.replace(
            _project("alpha", goal=_goal("A 的目标", "a_metric", "L4", "autonomous")),
            approval_ref="PR-42",
        )
        assert (
            registry.get("beta").goal.autonomy.rule_for(Operation.MERGE_TO_MAIN).permission
            is Permission.APPROVAL_REQUIRED
        )

    def test_replace_unknown_project_raises(self) -> None:
        registry = ProjectRegistry()
        with pytest.raises(ProjectNotFoundError):
            registry.replace(_project("alpha"), approval_ref="PR-42")
