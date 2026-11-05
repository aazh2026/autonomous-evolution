"""TASK-P1-020 —— 领域模型与持久化实体之间的转换层。

``core.entities.Project.from_domain`` / ``to_domain`` 是**唯一**把校验过的
领域模型与数据库行连起来的地方。这一层此前没有测试覆盖，于是两类 bug 可以
长期潜伏 —— 而且都是"悄悄变差"的类型：不报错，只是结果不对。

1. **字段搬运漏了**：领域模型加了 ``environment``，转换层忘了搬 →
   环境配置落库即丢，而项目配置看起来完全正常。
2. **治理策略被悄悄改写**：``policy`` 列是 ``goal.autonomy`` 的审计副本。
   design.md §7 / R-103 要求治理策略必须以**可读的形式存在**、且变更需审批
   —— 往返一次之后它必须与原值完全相同，否则"系统不得自主修改治理策略"
   就只剩一句口号。

还有一条：**库里的数据可能被人改过**。``to_domain`` 必须让残缺的目标
**当场暴露**，而不是带着残缺继续跑（见 ``to_domain`` 的文档字符串）。

这些断言都不需要数据库 —— 转换层只碰属性，所以这里是纯单元测试。
落库那一侧的往返由 ``tests/integration/test_entity_crud.py`` 负责。
"""

from __future__ import annotations

import pytest

from core.entities.project import Project as ProjectEntity
from core.project.models import GoalValidationError, Project

# 一份完整合法的项目配置（YAML）—— 项目配置的真实来源就是 YAML。
_PROJECT_YAML = """
project:
  id: my-product
  name: 在线PPT制作平台
  goal:
    objective:
      - id: OBJ-001
        description: 建成完整的在线PPT制作平台
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
        analyze_project: {level: L4, permission: autonomous}
        plan_tasks: {level: L4, permission: autonomous}
        code_changes: {level: L4, permission: autonomous}
        test_execution: {level: L4, permission: autonomous}
        branch_creation: {level: L4, permission: autonomous}
        merge_to_main: {level: L2, permission: approval_required}
        production_deployment: {level: L1, permission: advisory_only}
        modify_governance: {level: L1, permission: advisory_only}
    stop_conditions:
      - id: SC-001
        description: 全部关键结果达成
        kind: all_key_results_met
  repository:
    url: https://github.com/example/my-product
  budget:
    currency: USD
    max_daily_cost: "50"
  tech_stack: [python, fastapi]
  environment:
    stage: staging
    region: cn-north-1
"""


def _spec() -> Project:
    """从 YAML 载入领域模型。"""
    return Project.from_yaml(_PROJECT_YAML)


class TestEnvironmentSurvivesTheConversion:
    """环境配置必须一路带到库里、再带回来。

    ``environment`` 是 design.md §3.1 明确交给 Project Registry 管的三样东西
    之一（仓库、环境、技术栈）。它一旦在转换层被静默吞掉，系统会照常运行，
    只是每一步都按"环境未配置"行事 —— 这类错误最难被发现。
    """

    def test_from_domain_carries_environment(self) -> None:
        entity = ProjectEntity.from_domain(_spec())

        assert entity.environment == {"stage": "staging", "region": "cn-north-1"}

    def test_to_domain_restores_environment(self) -> None:
        entity = ProjectEntity.from_domain(_spec())

        restored = entity.to_domain()

        assert dict(restored.environment) == {"stage": "staging", "region": "cn-north-1"}

    def test_roundtrip_keeps_environment(self) -> None:
        restored = ProjectEntity.from_domain(_spec()).to_domain()

        assert dict(restored.environment) == {"stage": "staging", "region": "cn-north-1"}

    def test_missing_environment_becomes_empty_mapping_not_none(self) -> None:
        """没配环境的项目也是合法的 —— 不能因为搬运环节就变成 ``None``。

        ``None`` 会在下游炸出一个与真正原因无关的 ``TypeError``；
        空字典则是"确实没有环境配置"这个事实的如实表达。
        """
        spec = _spec()
        bare = spec.model_copy(update={"environment": {}})

        entity = ProjectEntity.from_domain(bare)
        assert entity.environment == {}

        restored = entity.to_domain()
        assert dict(restored.environment) == {}


class TestFullRoundTrip:
    """领域模型 → 实体 → 领域模型，每个字段都要还在。"""

    def test_identity_and_repository_survive(self) -> None:
        restored = ProjectEntity.from_domain(_spec()).to_domain()

        assert restored.id == "my-product"
        assert restored.name == "在线PPT制作平台"
        assert restored.repository.url == "https://github.com/example/my-product"
        assert restored.repository.default_branch == "main"

    def test_tech_stack_survives(self) -> None:
        restored = ProjectEntity.from_domain(_spec()).to_domain()

        assert restored.tech_stack == ("python", "fastapi")

    def test_budget_survives(self) -> None:
        restored = ProjectEntity.from_domain(_spec()).to_domain()

        assert restored.budget.currency == "USD"
        assert restored.budget.max_daily_cost is not None
        assert restored.budget.max_daily_cost == 50

    def test_goal_six_elements_survive(self) -> None:
        restored = ProjectEntity.from_domain(_spec()).to_domain()

        assert [item.id for item in restored.goal.objective] == ["OBJ-001"]
        assert [item.id for item in restored.goal.key_results] == ["KR-001"]
        assert [item.id for item in restored.goal.constraints] == ["C-001"]
        assert [item.id for item in restored.goal.non_goals] == ["NG-001"]
        assert [item.id for item in restored.goal.stop_conditions] == ["SC-001"]

    def test_roundtrip_is_idempotent(self) -> None:
        """往返两次不能越走越偏。

        每一次 ``from_domain`` 都会用领域模型重新生成 ``policy`` 列。如果
        每次生成的值不完全相同，那么"读出来再写回去"这个看似无害的动作
        就是在**篡改治理策略** —— 而这正是 R-103 明令禁止的。
        """
        first = ProjectEntity.from_domain(_spec())
        second = ProjectEntity.from_domain(first.to_domain())

        assert second.policy == first.policy
        assert second.goal == first.goal
        assert second.budget == first.budget
        assert second.environment == first.environment


class TestGovernanceStaysReadable:
    """治理策略要以可读形式存在，且改动能被察觉。

    design.md §7：系统**不得自主修改控制自身的治理策略**。这条约束落地的
    前提是治理策略必须能被外部读到、被审计到 —— 存在数据库的 JSON 列里
    是前提，光存在领域对象的内存里不是。
    """

    def test_policy_column_mirrors_goal_autonomy(self) -> None:
        """``policy`` 列与 ``goal`` 里的 autonomy 是同一份东西的两处副本。

        单列一份会丢失"六要素里也含治理策略"这个事实；只放在 goal 里又
        没法用一条 SQL 直接审计。两处都要有，且必须一致。
        """
        entity = ProjectEntity.from_domain(_spec())

        assert entity.policy == entity.goal["autonomy"]

    def test_policy_column_is_readable_without_parsing_the_goal(self) -> None:
        """审计者要能只看 ``policy`` 列就回答"这个项目的合并需要审批吗"。"""
        entity = ProjectEntity.from_domain(_spec())

        rules = entity.policy["rules"]
        assert isinstance(rules, dict)

        merge_to_main = rules["merge_to_main"]
        assert isinstance(merge_to_main, dict)
        assert merge_to_main["permission"] == "approval_required"

        production_deployment = rules["production_deployment"]
        assert isinstance(production_deployment, dict)
        assert production_deployment["permission"] == "advisory_only"

    def test_to_domain_rejects_a_row_whose_goal_was_tampered_with(self) -> None:
        """库里被改坏的目标必须**当场暴露**。

        还原时若放行残缺的目标，系统就会带着"缺停止条件""缺非目标"继续跑 ——
        而这两项缺失会让后续的演进决策失去依据（见 core/project/models.py）。
        """
        entity = ProjectEntity.from_domain(_spec())
        # 模拟有人直接在库里改了 goal —— 只剩 Objective，六要素残缺。
        entity.goal = {"objective": [{"id": "OBJ-001", "description": "只剩目标"}]}

        with pytest.raises(GoalValidationError) as exc_info:
            entity.to_domain()

        assert "Key Results" in exc_info.value.missing_elements
        assert "Stop Conditions" in exc_info.value.missing_elements
