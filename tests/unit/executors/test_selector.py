"""动态执行器选择器单元测试（TASK-P2-011）。

覆盖要求：
- R-038：能力契约硬性匹配与过滤（缺少硬性声明的执行器不可被选）；
- R-111：阶段二验收（不同执行器可执行同一种任务契约，同类型任务可动态选不同执行器）；
- T1：自动化验证打分模型、成本约束、并发上限、健康检查与落库；
- T3：可解释性决策记录（decision_rationale 结构与明细）。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.entities.task import Task
from core.planning.roadmap import TaskType
from executors.base import Capability
from executors.selector import (
    CostSensitivity,
    NoEligibleExecutorError,
    SelectionStrategy,
    TaskRequirements,
    create_default_selector,
)


class TestCapabilityFiltering:
    """R-038: 契约硬性能力匹配与过滤。"""

    def test_filter_out_missing_capabilities(self) -> None:
        selector = create_default_selector()
        # inline 执行器只有 STREAM_EVENTS 和 STRUCTURED_OUTPUT，缺少 INTERRUPT
        reqs = TaskRequirements(
            task_id="t-001",
            task_type=TaskType.TEST.value,
            required_capabilities=frozenset({Capability.INTERRUPT}),
        )
        result = selector.select(reqs)
        # 选出的执行器必须拥有 INTERRUPT
        profile = selector.get_profile(result.selected_executor)
        assert profile is not None
        assert Capability.INTERRUPT in profile.capabilities
        # inline 必须被记在 filtered_out 里并给出原因
        assert "inline" in result.filtered_out
        assert "缺少必要能力契约" in result.filtered_out["inline"]

    def test_raise_when_no_executor_matches(self) -> None:
        selector = create_default_selector()
        reqs = TaskRequirements(
            task_id="t-unmatched",
            task_type="exotic_task",
            required_capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.INTERRUPT,
                    Capability.PARALLEL_SESSIONS,
                    Capability.STRUCTURED_OUTPUT,
                }
            ),
            max_cost_tier=0.1,  # 只有 inline/session 的 cost_tier <= 0.1，但它们没有全部 4 个能力
        )
        with pytest.raises(NoEligibleExecutorError) as exc_info:
            selector.select(reqs)

        err = exc_info.value
        assert err.task_id == "t-unmatched"
        assert len(err.reasons) > 0


class TestSelectionStrategies:
    """R-111: 多策略选择与解释能力。"""

    def test_lowest_cost_strategy(self) -> None:
        selector = create_default_selector()
        reqs = TaskRequirements(
            task_id="t-cost",
            task_type=TaskType.BUGFIX.value,
            required_capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.INTERRUPT,
                    Capability.PARALLEL_SESSIONS,
                    Capability.STRUCTURED_OUTPUT,
                }
            ),
            cost_sensitivity=CostSensitivity.HIGH,
        )
        result = selector.select(reqs, strategy=SelectionStrategy.LOWEST_COST)
        assert result.selected_executor == "opencode"
        assert result.strategy_used == SelectionStrategy.LOWEST_COST
        assert "lowest_cost" in result.decision_rationale

    def test_affinity_strategy(self) -> None:
        selector = create_default_selector()
        reqs_docs = TaskRequirements(
            task_id="t-docs",
            task_type=TaskType.DOCS.value,
            required_capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.INTERRUPT,
                    Capability.PARALLEL_SESSIONS,
                    Capability.STRUCTURED_OUTPUT,
                }
            ),
        )
        result_docs = selector.select(reqs_docs, strategy=SelectionStrategy.AFFINITY)
        assert result_docs.selected_executor == "gemini-cli"

        reqs_feat = TaskRequirements(
            task_id="t-feat",
            task_type=TaskType.FEATURE.value,
            required_capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.INTERRUPT,
                    Capability.PARALLEL_SESSIONS,
                    Capability.STRUCTURED_OUTPUT,
                }
            ),
        )
        result_feat = selector.select(reqs_feat, strategy=SelectionStrategy.AFFINITY)
        assert result_feat.selected_executor == "claude-code"

    def test_round_robin_strategy(self) -> None:
        selector = create_default_selector()
        reqs = TaskRequirements(
            task_id="t-rr",
            task_type=TaskType.BUGFIX.value,
            required_capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.INTERRUPT,
                    Capability.PARALLEL_SESSIONS,
                    Capability.STRUCTURED_OUTPUT,
                }
            ),
        )
        selected_sequence = [
            selector.select(reqs, strategy=SelectionStrategy.ROUND_ROBIN).selected_executor
            for _ in range(4)
        ]
        assert len(set(selected_sequence)) >= 3

    def test_exploration_strategy_selects_different_executors(self) -> None:
        """R-111: 同一任务类型多次运行可能选中不同执行器（探索评测分支）。"""
        selector = create_default_selector()
        reqs = TaskRequirements(
            task_id="t-explore",
            task_type=TaskType.BUGFIX.value,
            required_capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.INTERRUPT,
                    Capability.PARALLEL_SESSIONS,
                    Capability.STRUCTURED_OUTPUT,
                }
            ),
        )
        res1 = selector.select(reqs, strategy=SelectionStrategy.EXPLORATION, random_seed=42)
        res2 = selector.select(reqs, strategy=SelectionStrategy.EXPLORATION, random_seed=100)
        assert res1.selected_executor in res1.eligible_executors
        assert res2.selected_executor in res2.eligible_executors


class TestRuntimeConstraints:
    """运行时动态约束（熔断、负载、排除名单、偏好名单）。"""

    def test_excluded_executors(self) -> None:
        selector = create_default_selector()
        reqs = TaskRequirements(
            task_id="t-ex",
            task_type=TaskType.FEATURE.value,
            excluded_executors=frozenset({"claude-code"}),
            required_capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.INTERRUPT,
                    Capability.PARALLEL_SESSIONS,
                    Capability.STRUCTURED_OUTPUT,
                }
            ),
        )
        result = selector.select(reqs)
        assert result.selected_executor != "claude-code"
        assert "claude-code" in result.filtered_out
        assert "排除名单" in result.filtered_out["claude-code"]

    def test_unhealthy_executor_is_filtered(self) -> None:
        selector = create_default_selector()
        selector.update_metrics("claude-code", is_healthy=False)
        reqs = TaskRequirements(
            task_id="t-health",
            task_type=TaskType.FEATURE.value,
        )
        result = selector.select(reqs)
        assert result.selected_executor != "claude-code"
        assert "claude-code" in result.filtered_out
        assert "不健康" in result.filtered_out["claude-code"]

    def test_concurrency_limit_is_respected(self) -> None:
        selector = create_default_selector()
        selector.update_metrics("claude-code", load_delta=4)
        reqs = TaskRequirements(
            task_id="t-load",
            task_type=TaskType.FEATURE.value,
            required_capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.INTERRUPT,
                    Capability.PARALLEL_SESSIONS,
                    Capability.STRUCTURED_OUTPUT,
                }
            ),
        )
        result = selector.select(reqs)
        assert result.selected_executor != "claude-code"
        assert "claude-code" in result.filtered_out
        assert "并发上限" in result.filtered_out["claude-code"]

    def test_success_rate_degradation(self) -> None:
        selector = create_default_selector()
        for _ in range(5):
            selector.update_metrics("codex", success=False)
        profile = selector.get_profile("codex")
        assert profile is not None
        assert profile.success_rate < 0.5


class TestPersistenceAndAuditability:
    """T1 & T3: 决策可解释性与落库持久化验证。"""

    def test_rationale_structure_and_serialization(self) -> None:
        selector = create_default_selector()
        reqs = TaskRequirements(
            task_id="t-audit",
            task_type=TaskType.BUGFIX.value,
        )
        result = selector.select(reqs)
        assert "### 执行器选择决策" in result.decision_rationale
        assert "t-audit" in result.decision_rationale
        assert "合格候选者综合打分" in result.decision_rationale

        dict_form = result.as_dict()
        assert dict_form["task_id"] == "t-audit"
        assert dict_form["selected_executor"] == result.selected_executor
        assert "candidate_scores" in dict_form

    @pytest.mark.asyncio
    async def test_select_and_assign_db_persistence(self) -> None:
        selector = create_default_selector()
        task_uuid = uuid.uuid4()
        task = Task(
            project_id="pilot-project",
            decision_id=uuid.uuid4(),
            title="修复已知缺陷",
            type=TaskType.BUGFIX.value,
            priority=Decimal("0.85"),
            extra={},
        )
        task.id = task_uuid

        mock_repo = MagicMock()
        mock_repo.assign_executor = AsyncMock()

        result = await selector.select_and_assign(
            task=task,
            repository=mock_repo,
            strategy=SelectionStrategy.BALANCED,
        )

        mock_repo.assign_executor.assert_awaited_once_with(task_uuid, result.selected_executor)
        assert task.extra is not None
        selection_extra = task.extra.get("executor_selection")
        assert isinstance(selection_extra, dict)
        assert selection_extra["selected_executor"] == result.selected_executor
