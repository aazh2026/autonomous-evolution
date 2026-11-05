"""Stop Conditions 求值与目标达成度计算。

设计依据：docs/design.md §3.1「Stop Conditions：满足什么条件后停止演进」、
§10「项目演进」指标层、§12「每一轮演进都应该让项目状态、目标达成度或工程质量
得到可验证的改善」；docs/tasks.md TASK-P1-014。

这个模块回答两个问题：

1. **该停了吗？** —— 停止条件是否触发。
2. **变好了吗？** —— 目标达成度是多少，比上一轮高了还是低了。

两个答案都必须是**机器可判定**的。design.md §3.1 把 Stop Conditions 与六要素里的
Non-goals 并列，理由是一样的：缺了停止条件，系统不知道"什么时候该停"；
缺了 Non-goals，系统不知道"什么算跑偏"。这两项缺失会让后续的演进决策失去依据。

**不用 eval()。** 停止条件的表达式来自项目配置，是外部输入 —— 用 eval 求值等于把
代码执行权交给配置文件。表达式只支持一小撮明确的形式（见 :class:`Predicate`），
解析不了就在启动时报错，而不是在运行期恒为 false。

**不可判定 ≠ 未触发。** 一个引用了没人会提供的指标的条件，和一个明确没满足的
条件，含义完全不同：前者永远为 false，于是"停止"这个能力形同虚设；后者是真的
还没到。所以两种情况要分开记（``StopEvaluation.undecidable``）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from core.project.models import (
    ComparisonOperator,
    Direction,
    GoalValidationError,
    KeyResult,
    MetricType,
    ProjectGoal,
    StopCondition,
    StopConditionKind,
    TargetExpression,
    TargetValue,
    ValueKind,
)

__all__ = [
    "BUILTIN_FACTS",
    "Facts",
    "GoalAttainment",
    "GoalAttainmentCalculator",
    "Predicate",
    "PredicateError",
    "StopConditionError",
    "StopConditionEvaluator",
    "StopEvaluation",
    "evaluate_predicate",
    "known_metrics_for",
    "parse_predicate",
    "validate_stop_conditions",
]


#: 观察到的世界状态。键是事实名，值是数值或布尔。
#:
#: 用 ``Mapping`` 而不是 dataclass —— 事实是采集方给过来的动态集合，字段随
#: 阶段推进而增加；定死字段等于让采集方为了填一个 dataclass 而造假字段。
Facts = Mapping[str, Decimal | bool]


class StopConditionError(ValueError):
    """停止条件不可判定。

    携带结构化的问题清单 —— 调用方无需解析错误文本就能知道**哪几条**有问题。
    """

    def __init__(self, message: str, *, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.problems = list(problems)


class PredicateError(StopConditionError):
    """表达式不可解析，或求值时缺数据。"""


# ---------------------------------------------------------------------------
# 表达式
# ---------------------------------------------------------------------------

#: 指标名：字母或下划线开头，可含点（``tests.passed_rate``）。
_METRIC_NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"

#: 表达式。比较式必须显式写出运算符与右操作数；裸名字是布尔事实。
#:
#: 刻意**不**允许"看起来像表达式但其实不是"的形式（比如 ``no_new_defects!``）
#: —— 解析失败要报错，不能被当成一个奇怪的指标名。
_PREDICATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^\s*(?P<metric>{_METRIC_NAME})"
    r"(?:\s*(?P<operator>>=|<=|==|!=|>|<)\s*(?P<operand>\S+))?\s*$"
)

#: 右操作数里表示"这条停止条件自己的 threshold"的符号。
#:
#: 写 ``X`` 是因为项目配置里习惯这么写；但 ``X`` 这个字母太随意，额外接受
#: 含义明确的 ``threshold``。
_THRESHOLD_TOKENS: Final[frozenset[str]] = frozenset({"threshold", "x"})


@dataclass(frozen=True, slots=True)
class Predicate:
    """一条已解析的停止条件表达式。

    三种形态：

    - **事实**：``no_new_critical_defects`` —— 读一个布尔事实，为真即触发。
      事实名读作一句**陈述句**："没有新的严重缺陷"这件事成不成立，由采集方把
      **结论**写进事实，而不是把计数写进来（计数该叫 ``new_critical_defects``）。
      名字与含义相反是这类设计最容易踩的坑，所以这里明确写成约定。
    - **数值比较**：``cost_per_kr <= 12.5``
    - **阈值比较**：``cost_per_kr <= X`` —— 右边的 ``X`` 是这条停止条件自己的
      ``threshold`` 字段。
    """

    metric: str
    raw: str
    operator: ComparisonOperator | None = None
    operand: Decimal | None = None
    use_threshold: bool = False

    @property
    def is_fact(self) -> bool:
        """是否是裸事实（无比较）。"""
        return self.operator is None


def parse_predicate(text: str) -> Predicate:
    """解析停止条件表达式。

    解析不了就抛 :class:`PredicateError` —— **不返回"恒为 false"的谓词**。
    一个解析不了的表达式如果被当成 false，这条停止条件就永远不触发，而系统
    看上去一切正常（"还没到停止条件"）。
    """
    match = _PREDICATE_PATTERN.match(text)
    if match is None:
        raise PredicateError(f"停止条件表达式无法解析：{text!r}")

    metric = match.group("metric")
    operator_text = match.group("operator")
    if operator_text is None:
        return Predicate(metric=metric, raw=text)

    operator = ComparisonOperator(operator_text)
    operand_text = match.group("operand")

    if operand_text.lower() in _THRESHOLD_TOKENS:
        return Predicate(metric=metric, raw=text, operator=operator, use_threshold=True)

    try:
        operand = Decimal(operand_text)
    except InvalidOperation as exc:
        raise PredicateError(
            f"停止条件表达式的右操作数无法解析：{operand_text!r}（应为数字或 X）"
        ) from exc
    if not operand.is_finite():
        raise PredicateError(f"停止条件表达式的右操作数必须是有限数：{operand_text!r}")

    return Predicate(metric=metric, raw=text, operator=operator, operand=operand)


def evaluate_predicate(
    predicate: Predicate,
    facts: Facts,
    *,
    threshold: Decimal | None = None,
) -> bool:
    """对事实求值。

    缺事实时报错而不是返回 false —— 理由同上：分不清"没满足"和"没法判断"。
    """
    if predicate.metric not in facts:
        raise PredicateError(f"事实 {predicate.metric!r} 不存在 —— 无法判定这条停止条件")
    actual = facts[predicate.metric]

    if predicate.operator is None:
        if not isinstance(actual, bool):
            raise PredicateError(f"事实 {predicate.metric!r} 必须是布尔值，实际是 {actual!r}")
        return actual

    operand = predicate.operand
    if predicate.use_threshold:
        if threshold is None:
            raise PredicateError(f"表达式 {predicate.raw!r} 需要阈值，但这条停止条件没有 threshold")
        operand = threshold
    if operand is None:  # pragma: no cover —— 解析出来的比较式必有右操作数
        raise PredicateError(f"表达式 {predicate.raw!r} 缺少右操作数")

    if isinstance(actual, bool):
        raise PredicateError(f"事实 {predicate.metric!r} 是布尔值，不能做数值比较")

    expression = TargetExpression(
        operator=predicate.operator,
        value=TargetValue(kind=ValueKind.NUMBER, number=operand),
    )
    try:
        return expression.evaluate(TargetValue(kind=ValueKind.NUMBER, number=Decimal(actual)))
    except GoalValidationError as exc:  # pragma: no cover —— 类型已在上面对齐
        raise PredicateError(f"表达式 {predicate.raw!r} 无法求值：{exc}") from exc


# ---------------------------------------------------------------------------
# 内置停止条件
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BuiltinRule:
    """内置停止条件-kind 到"读哪个事实、怎么比"的映射。

    把映射写死在这里而不是让调用方去猜，是为了让"预算耗尽"只有一种读法 ——
    事实名不同就意味着两个地方各读各的，其中一个迟早读不到。
    """

    fact: str
    operator: ComparisonOperator | None = None
    constant: Decimal | None = None
    needs_threshold: bool = False


_BUILTIN_RULES: Final[dict[StopConditionKind, _BuiltinRule]] = {
    StopConditionKind.NO_NEW_HIGH_SEVERITY_DEFECTS: _BuiltinRule(
        "new_high_severity_defects", ComparisonOperator.EQ, Decimal(0)
    ),
    StopConditionKind.BUDGET_EXHAUSTED: _BuiltinRule("budget_exhausted"),
    StopConditionKind.CONSECUTIVE_FAILURES_EXCEEDED: _BuiltinRule(
        "consecutive_failures", ComparisonOperator.GTE, needs_threshold=True
    ),
    StopConditionKind.TIME_ELAPSED: _BuiltinRule(
        "elapsed_hours", ComparisonOperator.GTE, needs_threshold=True
    ),
    StopConditionKind.HUMAN_STOP_REQUESTED: _BuiltinRule("human_stop_requested"),
}

#: 内置停止条件会读的事实名 —— 系统自己会采集的那些。
BUILTIN_FACTS: Final[frozenset[str]] = frozenset(rule.fact for rule in _BUILTIN_RULES.values())

#: 触发原因的模板。
_REASON_TEMPLATES: Final[dict[StopConditionKind, str]] = {
    StopConditionKind.ALL_KEY_RESULTS_MET: "全部关键结果已达成",
    StopConditionKind.NO_NEW_HIGH_SEVERITY_DEFECTS: "没有新增高严重度缺陷",
    StopConditionKind.BUDGET_EXHAUSTED: "预算已耗尽",
    StopConditionKind.CONSECUTIVE_FAILURES_EXCEEDED: "连续失败次数超过阈值 {threshold}",
    StopConditionKind.TIME_ELAPSED: "耗时超过阈值 {threshold}",
    StopConditionKind.HUMAN_STOP_REQUESTED: "人工请求停止",
    StopConditionKind.CUSTOM: "自定义表达式 {expression!r} 成立",
}


def known_metrics_for(goal: ProjectGoal, extra: Iterable[str] = ()) -> frozenset[str]:
    """这个项目"已知"的指标集合。

    = 内置事实 + 目标里 Key Results 用的指标 + 调用方额外登记的指标。

    登记这件事本身是有意义的：指标要先登记，才可能被停止条件引用。没登记的
    指标被引用了，说明**没有任何采集方会提供它** —— 那条停止条件就永远
    不可能成立。
    """
    return BUILTIN_FACTS | {kr.metric for kr in goal.key_results} | frozenset(extra)


def validate_stop_conditions(
    goal: ProjectGoal,
    *,
    known_metrics: Iterable[str] | None = None,
) -> None:
    """校验停止条件是否**可判定**。

    不可判定的必须在启动时报错（而不是运行期恒为 false）—— 见模块文档。
    """
    known = known_metrics_for(goal) if known_metrics is None else frozenset(known_metrics)
    problems: list[str] = []

    for condition in goal.stop_conditions:
        if condition.kind is StopConditionKind.ALL_KEY_RESULTS_MET:
            continue

        if condition.kind is StopConditionKind.CUSTOM:
            if not condition.expression:  # pragma: no cover —— 模型层已强制
                problems.append(f"停止条件 {condition.id}（custom）缺少表达式")
                continue
            try:
                predicate = parse_predicate(condition.expression)
            except PredicateError as exc:
                problems.append(f"停止条件 {condition.id}：{exc}")
                continue
            if predicate.metric not in known:
                problems.append(
                    f"停止条件 {condition.id} 引用了未登记的指标 {predicate.metric!r}"
                    " —— 没有任何采集方会提供它，这条条件永远不会成立"
                )
            if predicate.use_threshold and condition.threshold is None:
                problems.append(f"停止条件 {condition.id} 的表达式用到了阈值，但 threshold 为空")
            continue

        rule = _BUILTIN_RULES.get(condition.kind)
        if rule is None:  # pragma: no cover —— StopConditionKind 已穷举
            problems.append(
                f"停止条件 {condition.id} 的 kind={condition.kind.value} 没有对应的求值规则"
            )
            continue
        if rule.needs_threshold and condition.threshold is None:
            problems.append(f"停止条件 {condition.id}（{condition.kind.value}）缺少 threshold")

    if problems:
        raise StopConditionError(
            "存在不可判定的停止条件：\n" + "\n".join(f"  - {item}" for item in problems),
            problems=tuple(problems),
        )


@dataclass(frozen=True, slots=True)
class StopEvaluation:
    """停止条件的判定结果。

    ``triggered`` 与 ``undecidable`` 分开：前者是"条件满足了"，后者是"条件
    没法判断"。把两者混在一个 false 里，就等于让"停止"这个能力在没有数据的
    时候静默失效。
    """

    triggered: tuple[StopCondition, ...] = ()
    undecidable: tuple[StopCondition, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def stopped(self) -> bool:
        """是否应当停止演进。"""
        return bool(self.triggered)

    @property
    def triggered_ids(self) -> tuple[str, ...]:
        return tuple(condition.id for condition in self.triggered)


class StopConditionEvaluator:
    """停止条件判定器。

    构造时即校验 —— 不可判定的停止条件在这里就抛错，那一刻就是"启动"。
    """

    def __init__(self, goal: ProjectGoal, *, known_metrics: Iterable[str] = ()) -> None:
        self._goal = goal
        self._known = known_metrics_for(goal, known_metrics)
        self._predicates: dict[str, Predicate] = {}
        for condition in goal.stop_conditions:
            if condition.kind is StopConditionKind.CUSTOM and condition.expression:
                self._predicates[condition.id] = parse_predicate(condition.expression)
        validate_stop_conditions(goal, known_metrics=self._known)

    @property
    def known_metrics(self) -> frozenset[str]:
        """本判定器认得的指标集合。"""
        return self._known

    def evaluate(
        self,
        facts: Facts,
        *,
        actuals: Mapping[str, TargetValue] | None = None,
    ) -> StopEvaluation:
        """对事实求值，判断是否应当停止演进。

        ``actuals`` 是 Key Results 的实测值（键为 KR id）。只有
        ``all_key_results_met`` 这类条件用得上。
        """
        triggered: list[StopCondition] = []
        undecidable: list[StopCondition] = []
        reasons: list[str] = []

        for condition in self._goal.stop_conditions:
            fired: bool | None = self._evaluate_one(condition, facts, actuals or {})
            if fired is True:
                triggered.append(condition)
                reasons.append(self._reason(condition))
            elif fired is None:
                undecidable.append(condition)

        return StopEvaluation(
            triggered=tuple(triggered),
            undecidable=tuple(undecidable),
            reasons=tuple(reasons),
        )

    def _evaluate_one(
        self,
        condition: StopCondition,
        facts: Facts,
        actuals: Mapping[str, TargetValue],
    ) -> bool | None:
        """判定单条条件。返回 None 表示"没法判断"。"""
        if condition.kind is StopConditionKind.ALL_KEY_RESULTS_MET:
            return self._evaluate_key_results(actuals)
        if condition.kind is StopConditionKind.CUSTOM:
            return self._evaluate_custom(condition, facts)
        return self._evaluate_builtin(condition, facts)

    def _evaluate_key_results(self, actuals: Mapping[str, TargetValue]) -> bool | None:
        """判定"全部关键结果达成"。

        缺任何一项的实测值都判不了 —— 而不是当成"没达成"。两者含义不同：
        "测了，没达到"与"没测"，不该混成同一个 false。
        """
        if not actuals:
            return None
        for key_result in self._goal.key_results:
            actual = actuals.get(key_result.id)
            if actual is None:
                return None
            if not key_result.is_satisfied_by(actual):
                return False
        return True

    def _evaluate_custom(self, condition: StopCondition, facts: Facts) -> bool | None:
        """自定义表达式。

        求值失败（缺事实等）判不了 —— 构造时已经校验过表达式本身，这里缺的是
        运行期的数据。
        """
        predicate = self._predicates.get(condition.id)
        if predicate is None:
            return None
        try:
            return evaluate_predicate(predicate, facts, threshold=condition.threshold)
        except PredicateError:
            return None

    def _evaluate_builtin(self, condition: StopCondition, facts: Facts) -> bool | None:
        """内置条件 —— 读哪个事实、怎么比，写死在 :data:`_BUILTIN_RULES` 里。"""
        rule = _BUILTIN_RULES.get(condition.kind)
        if rule is None:  # pragma: no cover —— 构造时已校验
            return None
        if rule.fact not in facts:
            return None

        actual = facts[rule.fact]
        if rule.operator is None:
            return actual if isinstance(actual, bool) else None

        operand = rule.constant
        if rule.needs_threshold:
            if condition.threshold is None:
                return None
            operand = condition.threshold
        if operand is None or isinstance(actual, bool):
            return None
        expression = TargetExpression(
            operator=rule.operator,
            value=TargetValue(kind=ValueKind.NUMBER, number=operand),
        )
        return expression.evaluate(TargetValue(kind=ValueKind.NUMBER, number=Decimal(actual)))

    @staticmethod
    def _reason(condition: StopCondition) -> str:
        """人可读的触发原因。"""
        template = _REASON_TEMPLATES.get(condition.kind)
        if template is None:  # pragma: no cover —— StopConditionKind 已穷举
            template = "已触发"
        detail = template.format(threshold=condition.threshold, expression=condition.expression)
        return f"停止条件 {condition.id}：{detail}"


# ---------------------------------------------------------------------------
# 目标达成度
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoalAttainment:
    """目标达成度。

    ``overall`` 是各 Key Result 达成度的**平均**。没测到的 Key Result 记 0 ——
    不给"没测"发通行券，否则一个只测了一项的项目就能宣称整体达成。

    ``coverage`` 单独给出已测比例，把"测得不全"这件事显式摆出来。
    """

    per_key_result: Mapping[str, Decimal]
    overall: Decimal
    coverage: Decimal
    satisfied: frozenset[str] = frozenset()
    unmeasured: tuple[str, ...] = ()

    def delta(self, other: GoalAttainment) -> Decimal:
        """与另一次快照的差值。

        差值 > 0 表示"变好了"。这是 design.md §12「每一轮演进都应该产生可验证的
        改善」的判据 —— **改善必须能被算出来**，不能靠感觉。
        """
        return other.overall - self.overall


def _clamp(value: Decimal) -> Decimal:
    """把值夹到 [0, 1]。"""
    if value < 0:
        return Decimal(0)
    if value > 1:
        return Decimal(1)
    return value


class GoalAttainmentCalculator:
    """按 Key Result 逐项计算目标达成度。"""

    def __init__(self, goal: ProjectGoal) -> None:
        self._goal = goal

    def evaluate(self, actuals: Mapping[str, TargetValue]) -> GoalAttainment:
        """计算达成度。

        ``actuals`` 键为 Key Result 的 id，值为实测值。
        """
        per: dict[str, Decimal] = {}
        satisfied: set[str] = set()
        unmeasured: list[str] = []

        for key_result in self._goal.key_results:
            actual = actuals.get(key_result.id)
            if actual is None:
                per[key_result.id] = Decimal(0)
                unmeasured.append(key_result.id)
                continue
            if key_result.is_satisfied_by(actual):
                satisfied.add(key_result.id)
            per[key_result.id] = self.attainment_for(key_result, actual)

        total = len(per)
        overall = sum(per.values(), Decimal(0)) / total if total else Decimal(0)
        coverage = Decimal(total - len(unmeasured)) / total if total else Decimal(0)

        return GoalAttainment(
            per_key_result=per,
            overall=overall,
            coverage=coverage,
            satisfied=frozenset(satisfied),
            unmeasured=tuple(unmeasured),
        )

    def attainment_for(self, key_result: KeyResult, actual: TargetValue) -> Decimal:
        """单项 Key Result 的达成度，取值 [0, 1]。

        达标即 1.0。未达标时给出**接近程度**，而不是 0 —— 0 与"完全没做"
        混为一谈会让"离目标还差 1%"和"离目标还差 100%"看起来一样。

        - 越大越好的指标：``实际 / 目标``。
        - 越小越好的指标：``目标 / 实际``。
        """
        if key_result.is_satisfied_by(actual):
            return Decimal(1)
        if key_result.metric_type is MetricType.BOOLEAN:
            return Decimal(0)

        actual_number = actual.as_number()
        target = key_result.target.value.number
        if actual_number is None or target is None:  # pragma: no cover —— 类型已对齐
            return Decimal(0)

        return _clamp(self._ratio(key_result, actual_number, target))

    def progress_for(self, key_result: KeyResult, actual: TargetValue) -> Decimal | None:
        """单项 Key Result 的**改进度** —— 基线到目标的差距关闭了多少。

        没有基线就返回 ``None``：不知道从哪儿出发，就说不出"走了多远"。
        这与达成度是两件事 —— 达成度是"离目标多近"，改进度是"这一轮推进了多少"。
        """
        baseline = key_result.baseline
        if baseline is None:
            return None
        if key_result.is_satisfied_by(actual):
            return Decimal(1)

        actual_number = actual.as_number()
        target = key_result.target.value.number
        if actual_number is None or target is None:  # pragma: no cover —— 类型已对齐
            return None

        if key_result.direction is Direction.HIGHER_IS_BETTER:
            span = target - baseline
            ratio = (actual_number - baseline) / span if span != 0 else Decimal(0)
        else:
            span = baseline - target
            ratio = (baseline - actual_number) / span if span != 0 else Decimal(0)
        return _clamp(ratio)

    @staticmethod
    def _ratio(key_result: KeyResult, actual: Decimal, target: Decimal) -> Decimal:
        """未达标时的接近程度。"""
        if key_result.direction is Direction.HIGHER_IS_BETTER:
            return actual / target if target > 0 else Decimal(0)
        return target / actual if actual > 0 else Decimal(0)
