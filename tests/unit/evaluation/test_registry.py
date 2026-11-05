"""TASK-P1-071 —— 核心指标注册表（R-117 / R-118）。

验证（T1）：

- ② 任务数 / Token 数 / 提交量**不在**核心指标集合中；
- 并且注册表**主动拒绝**它们 —— 不只是初始集合里没有。

第二条比第一条更重要。"初始集合里没有"只防住"一开始就没加"；而 R-117 要
防的是"后来被加进去"—— 某个指标好算，就被加进看板；看板上的数字被当成
成绩；系统于是开始优化那个数字。任务数是最容易优化、也最不该被优化的东西：
让 Agent 多派任务，多派任务不费什么力气。

所以这里断言的是**注册表会拒绝**，而不只是"清单里没有"。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable

import pytest

from evaluation.common import MetricLayer, MetricResult
from evaluation.evolution_evaluation.decision import (
    decision_adoption_rate,
    false_positive_rate,
)
from evaluation.evolution_evaluation.discovery import (
    duplicate_discovery_rate,
    valid_discovery_rate,
)
from evaluation.project_evaluation.attainment import attainment_delta
from evaluation.registry import (
    ACTIVITY_SUBJECTS,
    CORE_REGISTRY,
    ForbiddenMetricError,
    MetricDefinition,
    MetricError,
    MetricRegistry,
    is_forbidden,
    normalize,
)
from evaluation.task_evaluation.delivery import (
    regression_defect_rate,
    rework_rate,
    task_success_rate,
)

#: R-117 点名的三个量，以及它们最常见的改写形式。
#:
#: 只测精确形式是不够的 —— 绕过它只要改个名字。列出来的每一条都是
#: "同一个量，不同写法"。
_ACTIVITY_VARIANTS = (
    "task_count",
    "token_count",
    "commit_count",
    "total_task_count",
    "daily_token_count",
    "commit_count_total",
    "num_tasks",
    "task_number",
    "tasks_total",
    "tasks_created",
    "tasks_dispatched",
    "token_usage",
    "tokens_used",
    "total_tokens",
    "commit_frequency",
    "commits_sent",
)

#: 这些名字里带 "task"，但说的不是"任务有多少" —— R-117 不禁它们。
_LEGITIMATE_TASK_METRICS = (
    "task_success_rate",
    "task_rework_rate",
    "regression_defect_rate",
    "decision_adoption_rate",
    "valid_discovery_rate",
    "duplicate_discovery_rate",
    "false_positive_rate",
    "attainment_delta",
)


def _definition(metric_id: str, layer: MetricLayer = MetricLayer.DELIVERY) -> MetricDefinition:
    return MetricDefinition(
        metric_id=metric_id,
        layer=layer,
        name=metric_id,
        unit="ratio",
        description="测试用定义",
        provider="evaluation.registry:_define",
    )


class TestTheActivityCountsAreNotCoreMetrics:
    """T1 ② —— 三类活动量不在核心指标集合中。"""

    @pytest.mark.parametrize("metric_id", _ACTIVITY_VARIANTS)
    def test_the_activity_counts_are_not_registered(self, metric_id: str) -> None:
        assert metric_id not in CORE_REGISTRY

    @pytest.mark.parametrize("metric_id", _ACTIVITY_VARIANTS)
    def test_the_registry_refuses_to_register_them(self, metric_id: str) -> None:
        """不是"没加"，是"加了会被拒绝"。"""
        registry = MetricRegistry()

        with pytest.raises(ForbiddenMetricError):
            registry.register(_definition(metric_id))

    def test_the_registry_rejection_does_not_leave_a_half_registered_metric(self) -> None:
        """被拒绝的登记不能留下痕迹 —— 否则"拒绝"就只是抛了个异常。"""
        registry = MetricRegistry()

        with pytest.raises(ForbiddenMetricError):
            registry.register(_definition("task_count"))

        assert len(registry) == 0
        assert "task_count" not in registry


class TestTheBoundaryOfTheRule:
    """规则的两侧都要站得住。"""

    @pytest.mark.parametrize("metric_id", _LEGITIMATE_TASK_METRICS)
    def test_metrics_that_are_not_activity_counts_are_allowed(self, metric_id: str) -> None:
        """带 "task" 不等于活动量 —— "任务成功率"恰恰是核心指标。"""
        assert is_forbidden(metric_id) is False

        registry = MetricRegistry()
        registry.register(_definition(metric_id))
        assert metric_id in registry

    @pytest.mark.parametrize(
        "metric_id",
        ["Task Count", "task-count", "TASK_COUNT", " task_count ", "Task\tCount"],
    )
    def test_renaming_does_not_get_around_the_rule(self, metric_id: str) -> None:
        """大小写、空格、连字符的差异都不该绕开检查。"""
        assert is_forbidden(metric_id) is True

    def test_the_rule_normalizes_before_matching(self) -> None:
        assert normalize("Total-Token Count") == "total_token_count"

    def test_the_subjects_are_exactly_the_three_named_by_r117(self) -> None:
        """主体词就是 R-117 点名的三类。多加一个就是擅自扩大禁令。"""
        assert set(ACTIVITY_SUBJECTS) == {"task", "token", "commit"}


class TestRegistryMechanics:
    def test_duplicate_registration_is_refused(self) -> None:
        registry = MetricRegistry()
        registry.register(_definition("some_metric"))

        with pytest.raises(MetricError, match="已经登记过了"):
            registry.register(_definition("some_metric"))

    def test_getting_an_unknown_metric_raises(self) -> None:
        registry = MetricRegistry()

        with pytest.raises(MetricError, match="未登记"):
            registry.get("never_registered")

    def test_metrics_can_be_looked_up_by_layer(self) -> None:
        registry = MetricRegistry()
        registry.register(_definition("a", MetricLayer.DISCOVERY))
        registry.register(_definition("b", MetricLayer.DELIVERY))

        assert [item.metric_id for item in registry.by_layer(MetricLayer.DISCOVERY)] == ["a"]
        assert [item.metric_id for item in registry.by_layer(MetricLayer.DELIVERY)] == ["b"]

    def test_registering_returns_the_definition(self) -> None:
        registry = MetricRegistry()
        definition = _definition("some_metric")

        assert registry.register(definition) is definition


class TestTheCoreRegistryContents:
    """核心指标集合本身。"""

    def test_the_registry_is_not_empty(self) -> None:
        assert len(CORE_REGISTRY) > 0

    def test_every_layer_has_at_least_one_metric(self) -> None:
        """§10 的四层都要有指标 —— 缺一层就等于放弃了对那一层的度量。"""
        for layer in MetricLayer:
            assert len(CORE_REGISTRY.by_layer(layer)) > 0, f"{layer} 层没有核心指标"

    def test_the_registered_ids_are_the_expected_ones(self) -> None:
        """指标集合是**确定的** —— 多一个少一个都会改变看板的含义。"""
        assert set(CORE_REGISTRY.metric_ids()) == {
            "valid_discovery_rate",
            "duplicate_discovery_rate",
            "decision_adoption_rate",
            "false_positive_rate",
            "task_success_rate",
            "rework_rate",
            "regression_defect_rate",
            "attainment_delta",
        }

    def test_every_registered_metric_has_a_resolvable_provider(self) -> None:
        """登记的 provider 必须真的指向一个存在的函数。

        登记条目里的 provider 是给文档与看板用的 —— 如果它指向一个不存在的
        路径，"这个指标由谁算"就成了一句空话，而且没人会发现。
        """
        for definition in CORE_REGISTRY.all():
            module_path, _, attribute = definition.provider.partition(":")
            module = importlib.import_module(module_path)
            provider: Callable[..., object] = getattr(module, attribute)
            assert callable(provider), f"{definition.metric_id} 的 provider 不可调用"

    def test_every_registered_metric_has_a_description(self) -> None:
        """指标的含义得写下来 —— 一个没有解释的数字进了看板就只是数字。"""
        for definition in CORE_REGISTRY.all():
            assert definition.description.strip()
            assert definition.name.strip()
            assert definition.unit.strip()


class TestTheRegistryAndTheComputationsAgree:
    """登记表与计算函数说的是同一批指标。

    两边各写各的时候，看板会去取注册表里的名字，而计算函数报出另一个名字
    —— 那个指标于是永远显示"暂无数据"，而没有人会知道是哪里对不上。
    """

    def test_every_computed_metric_id_is_registered(self) -> None:
        computed = [
            valid_discovery_rate([]),
            duplicate_discovery_rate([]),
            decision_adoption_rate([]),
            false_positive_rate([]),
            task_success_rate([]),
            rework_rate([]),
            regression_defect_rate([]),
            attainment_delta([]),
        ]

        for result in computed:
            assert result.metric_id in CORE_REGISTRY, f"{result.metric_id} 没有登记"

    def test_every_registered_metric_is_computed_by_its_provider(self) -> None:
        """注册条目指向的 provider 应当真的产出这个 metric_id。

        provider 路径写错、或者函数被改名，注册表就会指向一个不存在的东西 ——
        而看板照样会显示"这个指标来自哪里"。
        """
        samples: dict[str, Callable[[], MetricResult]] = {
            "evaluation.evolution_evaluation.discovery:valid_discovery_rate": (
                lambda: valid_discovery_rate([])
            ),
            "evaluation.evolution_evaluation.discovery:duplicate_discovery_rate": (
                lambda: duplicate_discovery_rate([])
            ),
            "evaluation.evolution_evaluation.decision:decision_adoption_rate": (
                lambda: decision_adoption_rate([])
            ),
            "evaluation.evolution_evaluation.decision:false_positive_rate": (
                lambda: false_positive_rate([])
            ),
            "evaluation.task_evaluation.delivery:task_success_rate": (
                lambda: task_success_rate([])
            ),
            "evaluation.task_evaluation.delivery:rework_rate": lambda: rework_rate([]),
            "evaluation.task_evaluation.delivery:regression_defect_rate": (
                lambda: regression_defect_rate([])
            ),
            "evaluation.project_evaluation.attainment:attainment_delta": (
                lambda: attainment_delta([])
            ),
        }

        for definition in CORE_REGISTRY.all():
            provider = samples[definition.provider]
            assert provider().metric_id == definition.metric_id
