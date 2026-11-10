"""动态执行器选择器（TASK-P2-011）。

设计依据：
- docs/design.md §3.4 统一执行器架构（ACP/MCP 边界与多执行器抽象）；
- docs/design.md §3.2 七种任务类型与不同验收口径；
- docs/tasks.md TASK-P2-011（R-038 补齐执行器能力、R-111 阶段二验收可比较与可评估）。

核心设计原则：
1. **能力契约优先（Capabilities First）**：
   任务声明所需的硬性能力，执行器必须完全满足才具备候选资格；缺任何一项直接硬性剔除。
2. **多策略适配（Multi-Strategy Selection）**：
   支持均衡策略（Balanced）、任务类型偏好（Affinity）、成本优先（Lowest Cost）、
   轮询（Round Robin）与探索策略（Exploration / Epsilon-Greedy）。
3. **决策可解释与可审计（Explainable & Auditable）**：
   每次选择产出结构化 rationale，详细说明过滤原因、各候选打分明细及胜出原因，
   支持直接持久化至数据库任务实体的 extra["executor_selection"] 与 executor 字段。
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from core.planning.roadmap import TaskType
from executors.base import Capability

if TYPE_CHECKING:
    from core.entities.task import Task
    from infrastructure.database.repositories import TaskRepository


class SelectionStrategy(StrEnum):
    """选择策略。"""

    BALANCED = "balanced"
    AFFINITY = "affinity"
    LOWEST_COST = "lowest_cost"
    ROUND_ROBIN = "round_robin"
    EXPLORATION = "exploration"


class CostSensitivity(StrEnum):
    """任务对成本的敏感程度。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SelectorError(Exception):
    """选择器基类异常。"""


class NoEligibleExecutorError(SelectorError):
    """没有满足条件的执行器。"""

    def __init__(self, task_id: str, reasons: Mapping[str, str]) -> None:
        lines = [f"任务 {task_id!r} 无法匹配任何可用执行器："]
        for name, reason in sorted(reasons.items()):
            lines.append(f"  - {name}: {reason}")
        super().__init__("\n".join(lines))
        self.task_id = task_id
        self.reasons = dict(reasons)


@dataclass(frozen=True, slots=True)
class ExecutorProfile:
    """执行器画像与能力声明。"""

    name: str
    capabilities: frozenset[Capability]
    supported_task_types: frozenset[str] = field(default_factory=frozenset)
    cost_tier: float = 1.0
    success_rate: float = 1.0
    max_concurrency: int = 5
    current_load: int = 0
    is_healthy: bool = True
    task_affinity: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def is_available(self) -> bool:
        """是否在线且有剩余容量。"""
        return self.is_healthy and self.current_load < self.max_concurrency


@dataclass(frozen=True, slots=True)
class TaskRequirements:
    """任务对执行器的要求。"""

    task_id: str
    task_type: str
    required_capabilities: frozenset[Capability] = field(default_factory=frozenset)
    cost_sensitivity: CostSensitivity = CostSensitivity.MEDIUM
    max_cost_tier: float | None = None
    priority: float = 0.5
    preferred_executors: Sequence[str] = field(default_factory=tuple)
    excluded_executors: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ExecutorSelectionResult:
    """执行器选择决策结果。"""

    task_id: str
    selected_executor: str
    strategy_used: SelectionStrategy
    decision_rationale: str
    candidate_scores: Mapping[str, float]
    eligible_executors: tuple[str, ...]
    filtered_out: Mapping[str, str]
    selected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, object]:
        """序列化为可持久化存储的字典结构。"""
        return {
            "task_id": self.task_id,
            "selected_executor": self.selected_executor,
            "strategy_used": self.strategy_used.value,
            "decision_rationale": self.decision_rationale,
            "candidate_scores": dict(self.candidate_scores),
            "eligible_executors": list(self.eligible_executors),
            "filtered_out": dict(self.filtered_out),
            "selected_at": self.selected_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class _RationaleContext:
    requirements: TaskRequirements
    strategy: SelectionStrategy
    candidates: Sequence[ExecutorProfile]
    scores: Mapping[str, float]
    filtered_out: Mapping[str, str]
    chosen: ExecutorProfile
    note: str


class DynamicExecutorSelector:
    """动态执行器选择器。"""

    def __init__(self, profiles: Sequence[ExecutorProfile] | None = None) -> None:
        self._profiles: dict[str, ExecutorProfile] = {}
        self._round_robin_index = 0
        if profiles:
            for p in profiles:
                self.register(p)

    def register(self, profile: ExecutorProfile) -> None:
        """注册或更新执行器画像。"""
        self._profiles[profile.name] = profile

    def unregister(self, name: str) -> None:
        """注销执行器。"""
        self._profiles.pop(name, None)

    def get_profile(self, name: str) -> ExecutorProfile | None:
        """获取执行器画像。"""
        return self._profiles.get(name)

    def list_profiles(self) -> Sequence[ExecutorProfile]:
        """列出所有已注册的执行器画像。"""
        return tuple(self._profiles.values())

    def update_metrics(
        self,
        name: str,
        *,
        success: bool | None = None,
        load_delta: int = 0,
        is_healthy: bool | None = None,
    ) -> None:
        """动态更新执行器运行时指标与负载。"""
        current = self._profiles.get(name)
        if current is None:
            return

        new_load = max(0, current.current_load + load_delta)
        new_healthy = is_healthy if is_healthy is not None else current.is_healthy
        new_success_rate = current.success_rate
        if success is not None:
            alpha = 0.2
            val = 1.0 if success else 0.0
            new_success_rate = round((1 - alpha) * current.success_rate + alpha * val, 4)

        updated = ExecutorProfile(
            name=current.name,
            capabilities=current.capabilities,
            supported_task_types=current.supported_task_types,
            cost_tier=current.cost_tier,
            success_rate=new_success_rate,
            max_concurrency=current.max_concurrency,
            current_load=new_load,
            is_healthy=new_healthy,
            task_affinity=current.task_affinity,
            metadata=current.metadata,
        )
        self._profiles[name] = updated

    def _filter_candidates(
        self, requirements: TaskRequirements
    ) -> tuple[list[ExecutorProfile], dict[str, str]]:
        """按硬性契约过滤候选执行器。"""
        eligible: list[ExecutorProfile] = []
        filtered_out: dict[str, str] = {}

        for p in self._profiles.values():
            if p.name in requirements.excluded_executors:
                filtered_out[p.name] = "在任务排除名单中 (excluded_executors)"
                continue
            if not p.is_healthy:
                filtered_out[p.name] = "执行器当前不健康或已熔断"
                continue
            if p.current_load >= p.max_concurrency:
                filtered_out[p.name] = f"已达最大并发上限 ({p.current_load}/{p.max_concurrency})"
                continue
            missing_caps = requirements.required_capabilities - p.capabilities
            if missing_caps:
                caps_str = ", ".join(sorted(c.value for c in missing_caps))
                filtered_out[p.name] = f"缺少必要能力契约: {caps_str}"
                continue
            if p.supported_task_types and requirements.task_type not in p.supported_task_types:
                filtered_out[p.name] = (
                    f"不支持任务类型 {requirements.task_type!r} "
                    f"(支持类型: {', '.join(sorted(p.supported_task_types))})"
                )
                continue
            if requirements.max_cost_tier is not None and p.cost_tier > requirements.max_cost_tier:
                filtered_out[p.name] = (
                    f"成本指数 ({p.cost_tier:.2f}) 超过任务硬性上限 "
                    f"({requirements.max_cost_tier:.2f})"
                )
                continue

            eligible.append(p)

        return eligible, filtered_out

    def select(
        self,
        requirements: TaskRequirements,
        strategy: SelectionStrategy = SelectionStrategy.BALANCED,
        *,
        random_seed: int | None = None,
    ) -> ExecutorSelectionResult:
        """根据任务需求和指定策略选择执行器。"""
        rng = random.Random(random_seed) if random_seed is not None else random.Random()
        eligible, filtered_out = self._filter_candidates(requirements)

        if not eligible:
            raise NoEligibleExecutorError(requirements.task_id, filtered_out)

        scores = {p.name: self._calculate_score(p, requirements, strategy) for p in eligible}
        sorted_candidates = sorted(eligible, key=lambda ep: scores[ep.name], reverse=True)

        chosen, note = self._pick_by_strategy(sorted_candidates, strategy, rng)
        ctx = _RationaleContext(
            requirements=requirements,
            strategy=strategy,
            candidates=sorted_candidates,
            scores=scores,
            filtered_out=filtered_out,
            chosen=chosen,
            note=note,
        )
        rationale = self._format_rationale(ctx)

        return ExecutorSelectionResult(
            task_id=requirements.task_id,
            selected_executor=chosen.name,
            strategy_used=strategy,
            decision_rationale=rationale,
            candidate_scores=scores,
            eligible_executors=tuple(ep.name for ep in eligible),
            filtered_out=filtered_out,
        )

    def _pick_by_strategy(
        self,
        sorted_candidates: list[ExecutorProfile],
        strategy: SelectionStrategy,
        rng: random.Random,
    ) -> tuple[ExecutorProfile, str]:
        """根据策略选取目标候选者。"""
        if strategy == SelectionStrategy.ROUND_ROBIN:
            idx = self._round_robin_index % len(sorted_candidates)
            chosen = sorted_candidates[idx]
            self._round_robin_index += 1
            return chosen, f"轮询选中 `{chosen.name}` (索引: {idx})"

        if strategy == SelectionStrategy.EXPLORATION:
            epsilon = 0.20
            if rng.random() < epsilon and len(sorted_candidates) > 1:
                chosen = rng.choice(sorted_candidates[1:])
                return chosen, (
                    f"探索分支触发 (ε={epsilon:.2f})，"
                    f"随机探索次优执行器 `{chosen.name}` 用于评估比对。"
                )
            chosen = sorted_candidates[0]
            return chosen, f"贪心分支选中综合评分最高者 `{chosen.name}`。"

        chosen = sorted_candidates[0]
        return chosen, f"选中最高得分者 `{chosen.name}`。"

    def _format_rationale(self, ctx: _RationaleContext) -> str:
        """格式化决策可解释性说明文档。"""
        reqs = ctx.requirements
        caps_str = ", ".join(c.value for c in sorted(reqs.required_capabilities)) or "无"
        lines: list[str] = [
            f"### 执行器选择决策 (策略: `{ctx.strategy.value}`)",
            f"- **任务标识**: `{reqs.task_id}`",
            f"- **任务类型**: `{reqs.task_type}`",
            f"- **硬性能力要求**: `{caps_str}`",
            f"- **成本敏感度**: `{reqs.cost_sensitivity.value}`",
        ]

        if ctx.filtered_out:
            lines.append("\n**被过滤候选者及原因**:")
            for name, reason in sorted(ctx.filtered_out.items()):
                lines.append(f"- `{name}`: {reason}")

        lines.append("\n**合格候选者综合打分**:")
        for ep in ctx.candidates:
            lines.append(
                f"- `{ep.name}`: 总分 {ctx.scores[ep.name]:.3f} "
                f"(成功率: {ep.success_rate:.2f}, 成本指数: {ep.cost_tier:.2f}, "
                f"负载: {ep.current_load}/{ep.max_concurrency})"
            )

        lines.append(f"\n**决策结果**: {ctx.note} (得分: {ctx.scores[ctx.chosen.name]:.3f})")
        return "\n".join(lines)

    def _calculate_score(
        self,
        profile: ExecutorProfile,
        requirements: TaskRequirements,
        strategy: SelectionStrategy,
    ) -> float:
        """根据策略计算候选执行器的匹配得分。"""
        pref_bonus = 0.3 if profile.name in requirements.preferred_executors else 0.0
        affinity = profile.task_affinity.get(requirements.task_type, 0.5)
        cost_eff = 1.0 / (1.0 + max(0.0, profile.cost_tier))
        cap_free = profile.max_concurrency - profile.current_load
        free_ratio = max(0.0, cap_free / profile.max_concurrency)
        success_score = profile.success_rate

        if strategy == SelectionStrategy.LOWEST_COST:
            return 0.60 * cost_eff + 0.20 * success_score + 0.10 * free_ratio + 0.10 * pref_bonus
        if strategy == SelectionStrategy.AFFINITY:
            return 0.40 * affinity + 0.35 * success_score + 0.15 * free_ratio + 0.10 * pref_bonus

        # 默认 BALANCED
        if requirements.cost_sensitivity == CostSensitivity.HIGH:
            w_cost, w_aff, w_succ, w_free = 0.45, 0.20, 0.20, 0.15
        elif requirements.cost_sensitivity == CostSensitivity.LOW:
            w_cost, w_aff, w_succ, w_free = 0.10, 0.40, 0.35, 0.15
        else:
            w_cost, w_aff, w_succ, w_free = 0.25, 0.30, 0.30, 0.15

        return (
            w_cost * cost_eff
            + w_aff * affinity
            + w_succ * success_score
            + w_free * free_ratio
            + pref_bonus
        )

    async def select_and_assign(
        self,
        task: Task,
        repository: TaskRepository,
        *,
        requirements: TaskRequirements | None = None,
        strategy: SelectionStrategy = SelectionStrategy.BALANCED,
    ) -> ExecutorSelectionResult:
        """为数据库中的任务实体动态选择执行器并持久化（落库）。"""
        reqs = requirements or TaskRequirements(
            task_id=str(task.id),
            task_type=task.type,
            cost_sensitivity=CostSensitivity.MEDIUM,
            priority=float(task.priority),
        )
        result = self.select(reqs, strategy=strategy)

        await repository.assign_executor(task.id, result.selected_executor)

        current_extra = dict(task.extra or {})
        current_extra["executor_selection"] = result.as_dict()
        task.extra = current_extra

        return result


def create_default_selector() -> DynamicExecutorSelector:
    """创建预置有系统 6 款执行器画像的默认动态选择器。"""
    standard_caps = frozenset(
        {
            Capability.STREAM_EVENTS,
            Capability.INTERRUPT,
            Capability.PARALLEL_SESSIONS,
            Capability.STRUCTURED_OUTPUT,
        }
    )

    profiles = [
        ExecutorProfile(
            name="claude-code",
            capabilities=standard_caps,
            cost_tier=1.2,
            success_rate=0.96,
            max_concurrency=4,
            task_affinity={
                TaskType.FEATURE.value: 0.95,
                TaskType.REFACTORING.value: 0.95,
                TaskType.BUGFIX.value: 0.90,
                TaskType.PERFORMANCE.value: 0.90,
            },
            metadata={"description": "Anthropic Claude Code CLI 适配器"},
        ),
        ExecutorProfile(
            name="codex",
            capabilities=standard_caps,
            cost_tier=0.9,
            success_rate=0.92,
            max_concurrency=5,
            task_affinity={
                TaskType.BUGFIX.value: 0.95,
                TaskType.TEST.value: 0.95,
                TaskType.FEATURE.value: 0.85,
                TaskType.DEPENDENCY.value: 0.85,
            },
            metadata={"description": "OpenAI Codex CLI 适配器"},
        ),
        ExecutorProfile(
            name="gemini-cli",
            capabilities=standard_caps,
            cost_tier=0.6,
            success_rate=0.88,
            max_concurrency=6,
            task_affinity={
                TaskType.DOCS.value: 0.98,
                TaskType.TEST.value: 0.90,
                TaskType.DEPENDENCY.value: 0.90,
                TaskType.BUGFIX.value: 0.82,
            },
            metadata={"description": "Google Gemini CLI 适配器"},
        ),
        ExecutorProfile(
            name="opencode",
            capabilities=standard_caps,
            cost_tier=0.3,
            success_rate=0.82,
            max_concurrency=8,
            task_affinity={
                TaskType.BUGFIX.value: 0.85,
                TaskType.TEST.value: 0.85,
                TaskType.DOCS.value: 0.85,
            },
            metadata={"description": "OpenCode 开源执行器"},
        ),
        ExecutorProfile(
            name="session",
            capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.INTERRUPT,
                    Capability.PARALLEL_SESSIONS,
                }
            ),
            cost_tier=0.0,
            success_rate=1.0,
            max_concurrency=10,
            task_affinity={TaskType.TEST.value: 1.0},
            metadata={"description": "内存测试会话执行器"},
        ),
        ExecutorProfile(
            name="inline",
            capabilities=frozenset(
                {
                    Capability.STREAM_EVENTS,
                    Capability.STRUCTURED_OUTPUT,
                }
            ),
            cost_tier=0.0,
            success_rate=1.0,
            max_concurrency=20,
            task_affinity={TaskType.TEST.value: 1.0},
            metadata={"description": "内存同步简单执行器"},
        ),
    ]

    return DynamicExecutorSelector(profiles)
