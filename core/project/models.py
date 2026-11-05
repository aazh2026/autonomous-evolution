"""项目目标六要素模型。

设计依据：docs/design.md §3.1「项目目标不能只是一段自然语言」——
Objective / Key Results / Constraints / Non-goals / Autonomy Policy / Stop Conditions；
docs/tasks.md TASK-P1-010。

三条硬约束：

1. **六要素缺一不可**，载入时逐项校验，错误信息必须指出**缺哪一项**。
   缺停止条件，系统无法判断"什么时候该停"；缺非目标，系统无法判断"什么算跑偏"。
   这两项缺失会让后续的演进决策失去依据。
2. **不硬编码指标**。metric / unit / eval_method 全是自由字符串。设计文档明确
   要求"支持不同指标类型、单位、评测方法"——把 ``e2e_success_rate`` 写进枚举
   等于把整个系统锁死在一个项目上。同一个 metric 配不同 unit / eval_method
   必须都能被接受。
3. **谓词可机器判定**。Constraint 与 Stop Condition 必须能求值，不能是只能给人看的
   自然语言 —— 演进循环靠它们自动判断"是否触顶""是否越界"。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.config import ExtraValues

from core.policy.autonomy import AutonomyPolicy, default_policy
from core.policy.budget import Budget


class GoalValidationError(ValueError):
    """目标不合法。

    携带结构化信息，调用方无需解析错误文本就能知道缺什么。
    """

    def __init__(
        self,
        message: str,
        *,
        missing_elements: Sequence[str] = (),
        problems: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.missing_elements = list(missing_elements)
        self.problems = list(problems)


def _rethrow_structured(exc: ValidationError) -> NoReturn:
    """把 Pydantic 包装过的 :class:`GoalValidationError` 还原后抛出。

    Pydantic 会把 ``ValueError`` 子类统一包装成 ``ValidationError``，原始异常
    只留在 ``errors()[i]["ctx"]["error"]`` 里。本项目把"缺哪一项"当作一等信息
    —— 校验器、API 层、日志都要用它 —— 所以在校验入口把它取回来。
    """
    for error in exc.errors():
        original = error.get("ctx", {}).get("error")
        if isinstance(original, GoalValidationError):
            raise original from exc
    raise exc


class MetricType(StrEnum):
    """指标的数据类型。决定 target 允许的取值形态。"""

    NUMBER = "number"
    PERCENTAGE = "percentage"
    DURATION = "duration"
    COUNT = "count"
    BOOLEAN = "boolean"


class Direction(StrEnum):
    """指标方向 —— 越大越好还是越小越好。"""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class EvalMethod(StrEnum):
    """评测方法 —— 该指标怎么被测出来。

    "评测方法可配置"是 design.md §3.1 的明文要求。阶段一先固定这几种常见形态，
    但取值来自枚举而非写死在判定逻辑里，新增一种不需要改判定代码。
    """

    AUTOMATED_TEST = "automated_test"
    CI_MEASUREMENT = "ci_measurement"
    MANUAL_REVIEW = "manual_review"
    EXTERNAL_QUERY = "external_query"


class ComparisonOperator(StrEnum):
    """比较运算符。"""

    GTE = ">="
    LTE = "<="
    GT = ">"
    LT = "<"
    EQ = "=="
    NE = "!="


class ValueKind(StrEnum):
    """比较值的类型。"""

    NUMBER = "number"
    BOOLEAN = "boolean"
    TEXT = "text"


class TargetValue(BaseModel):
    """比较目标的取值。

    刻意做成带类型标记的三选一，而不是 ``Decimal | bool | str`` 联合类型 ——
    后者在 Pydantic 的智能合并下会出现 ``"true"`` 被当成字符串的歧义，
    而目标值判错方向会让"达标/未达标"整体反过来。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ValueKind
    number: Decimal | None = None
    flag: bool | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        match self.kind:
            case ValueKind.NUMBER:
                if self.number is None:
                    raise ValueError("kind=number 时必须提供 number")
            case ValueKind.BOOLEAN:
                if self.flag is None:
                    raise ValueError("kind=boolean 时必须提供 flag")
            case ValueKind.TEXT:
                if self.text is None:
                    raise ValueError("kind=text 时必须提供 text")
        return self

    @classmethod
    def parse(cls, raw: str) -> TargetValue:
        """从原始文本解析取值。"""
        text = raw.strip()
        lowered = text.lower()
        if lowered in {"true", "yes"}:
            return cls(kind=ValueKind.BOOLEAN, flag=True)
        if lowered in {"false", "no"}:
            return cls(kind=ValueKind.BOOLEAN, flag=False)
        try:
            number = Decimal(text)
        except InvalidOperation:
            return cls(kind=ValueKind.TEXT, text=text)
        if not number.is_finite():
            raise GoalValidationError(f"目标值必须是有限数，收到 {raw!r}")
        return cls(kind=ValueKind.NUMBER, number=number)

    def as_number(self) -> Decimal | None:
        return self.number

    def as_bool(self) -> bool | None:
        return self.flag

    def as_text(self) -> str | None:
        return self.text

    def __str__(self) -> str:
        match self.kind:
            case ValueKind.NUMBER:
                return str(self.number)
            case ValueKind.BOOLEAN:
                return "true" if self.flag else "false"
            case ValueKind.TEXT:
                return self.text or ""


_OPERATOR_PATTERN = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*(\S.*?)\s*$")


class TargetExpression(BaseModel):
    """可比较的目标表达式，如 ``">= 0.95"``。

    **不用 ``eval()``**：目标表达式来自项目配置，是外部输入。
    用 eval 求值等于把代码执行权交给配置文件。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator: ComparisonOperator
    value: TargetValue

    @model_validator(mode="before")
    @classmethod
    def _accept_plain_string(cls, data: object) -> object:
        """允许直接写 ``target: ">= 0.95"``。

        注意必须返回**字典**而不是模型实例 —— Pydantic 在 ``before`` 校验器
        里收到模型实例时会重新走一遍模型校验，而该实例不是字典，会被
        "Input should be a valid dictionary or instance of ..." 拒掉。
        """
        if isinstance(data, str):
            parsed = cls.parse(data)
            return {"operator": parsed.operator, "value": parsed.value}
        return data

    @classmethod
    def parse(cls, raw: str) -> TargetExpression:
        """解析 ``">= 0.95"`` 形式的表达式。

        解析失败抛 :class:`GoalValidationError` —— 目标不可比较，
        就无法判断"是否达标"，整个 KeyResult 失去意义。
        """
        match = _OPERATOR_PATTERN.match(raw)
        if match is None:
            raise GoalValidationError(f"目标表达式不可比较：{raw!r}（应形如 '>= 0.95'）")
        operator_text, value_text = match.groups()
        operator = ComparisonOperator(operator_text)
        return cls(operator=operator, value=TargetValue.parse(value_text))

    def evaluate(self, actual: TargetValue) -> bool:
        """把实际值与目标比较。类型不匹配时抛错而不是静默返回 False。"""
        if actual.kind is not self.value.kind:
            raise GoalValidationError(
                f"无法比较不同类型的取值：实际 {actual.kind} vs 目标 {self.value.kind}"
            )
        match actual.kind:
            case ValueKind.NUMBER:
                return self._compare_numbers(actual.number, self.value.number)
            case ValueKind.BOOLEAN:
                return self._compare_bools(actual.flag, self.value.flag)
            case ValueKind.TEXT:
                return self._compare_texts(actual.text, self.value.text)

    def _compare_numbers(self, actual: Decimal | None, expected: Decimal | None) -> bool:
        if actual is None or expected is None:
            raise GoalValidationError("数值比较缺少操作数")
        match self.operator:
            case ComparisonOperator.GTE:
                return actual >= expected
            case ComparisonOperator.LTE:
                return actual <= expected
            case ComparisonOperator.GT:
                return actual > expected
            case ComparisonOperator.LT:
                return actual < expected
            case ComparisonOperator.EQ:
                return actual == expected
            case ComparisonOperator.NE:
                return actual != expected

    def _compare_bools(self, actual: bool | None, expected: bool | None) -> bool:
        if actual is None or expected is None:
            raise GoalValidationError("布尔比较缺少操作数")
        match self.operator:
            case ComparisonOperator.EQ:
                return actual == expected
            case ComparisonOperator.NE:
                return actual != expected
            case _:
                raise GoalValidationError(f"布尔值不支持运算符 {self.operator.value}")

    def _compare_texts(self, actual: str | None, expected: str | None) -> bool:
        if actual is None or expected is None:
            raise GoalValidationError("文本比较缺少操作数")
        match self.operator:
            case ComparisonOperator.EQ:
                return actual == expected
            case ComparisonOperator.NE:
                return actual != expected
            case _:
                raise GoalValidationError(f"文本值不支持运算符 {self.operator.value}")

    def __str__(self) -> str:
        return f"{self.operator.value} {self.value}"


class Objective(BaseModel):
    """目标条目 —— "希望实现什么"。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _accept_plain_string(cls, data: object) -> object:
        if isinstance(data, str):
            return {"id": f"OBJ-{abs(hash(data)) % 1000:03d}", "description": data}
        return data


class NonGoal(BaseModel):
    """非目标 —— "明确不做什么"。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _accept_plain_string(cls, data: object) -> object:
        if isinstance(data, str):
            return {"id": f"NG-{abs(hash(data)) % 1000:03d}", "description": data}
        return data


class ConstraintKind(StrEnum):
    """约束的类型。"""

    NO_UNAUTHORIZED_PRODUCTION_CHANGE = "no_unauthorized_production_change"
    ALL_CHANGES_MUST_PASS_TESTS = "all_changes_must_pass_tests"
    NO_BYPASS_SECURITY_POLICY = "no_bypass_security_policy"
    NO_LONG_TERM_SECRETS_IN_PROMPT = "no_long_term_secrets_in_prompt"
    WITHIN_BUDGET = "within_budget"
    CUSTOM = "custom"


class Constraint(BaseModel):
    """约束 —— "哪些条件不能违反"。

    ``machine_checkable`` 为真时 ``expression`` 必须存在，且该表达式要能被
    约束求值器解析。不可机器判定的约束仍然允许存在（有些约束本来就需要人看），
    但必须显式标注 ``machine_checkable=False``，而不是含糊地混在一起。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: ConstraintKind = ConstraintKind.CUSTOM
    expression: str | None = None
    machine_checkable: bool = False

    @model_validator(mode="after")
    def _check_expression(self) -> Self:
        if self.machine_checkable and not self.expression:
            raise ValueError(f"约束 {self.id} 声明 machine_checkable=True 但缺少 expression")
        if self.expression and self.kind is ConstraintKind.CUSTOM and not self.machine_checkable:
            raise ValueError(
                f"约束 {self.id} 提供了 expression，必须同时声明 machine_checkable=True"
            )
        return self


class StopConditionKind(StrEnum):
    """停止条件的类型。

    这些都是演进循环能自动判定的条件 —— 满足即停止演进。
    """

    ALL_KEY_RESULTS_MET = "all_key_results_met"
    NO_NEW_HIGH_SEVERITY_DEFECTS = "no_new_high_severity_defects"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONSECUTIVE_FAILURES_EXCEEDED = "consecutive_failures_exceeded"
    TIME_ELAPSED = "time_elapsed"
    HUMAN_STOP_REQUESTED = "human_stop_requested"
    CUSTOM = "custom"


class StopCondition(BaseModel):
    """停止条件 —— "满足什么条件后停止演进"。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: StopConditionKind
    expression: str | None = None
    threshold: Decimal | None = None

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> Self:
        if self.kind is StopConditionKind.CUSTOM and not self.expression:
            raise ValueError(f"停止条件 {self.id} 为 custom 时必须提供 expression")
        if (
            self.kind
            in {
                StopConditionKind.CONSECUTIVE_FAILURES_EXCEEDED,
                StopConditionKind.TIME_ELAPSED,
            }
            and self.threshold is None
        ):
            raise ValueError(f"停止条件 {self.id} 的 kind={self.kind.value} 必须提供 threshold")
        return self


class KeyResult(BaseModel):
    """关键结果 —— "通过哪些指标判断实现"。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    target: TargetExpression
    unit: str = ""
    metric_type: MetricType = MetricType.NUMBER
    eval_method: EvalMethod = EvalMethod.AUTOMATED_TEST
    direction: Direction = Direction.HIGHER_IS_BETTER
    baseline: Decimal | None = None
    description: str = ""

    @field_validator("metric")
    @classmethod
    def _metric_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("metric 不能为空")
        return value.strip()

    @model_validator(mode="after")
    def _check_type_consistency(self) -> Self:
        expected = ValueKind.BOOLEAN if self.metric_type is MetricType.BOOLEAN else ValueKind.NUMBER
        if self.target.value.kind is not expected:
            raise ValueError(
                f"关键结果 {self.id} 的 metric_type={self.metric_type.value} "
                f"与目标值类型 {self.target.value.kind.value} 不匹配"
            )
        return self

    def is_satisfied_by(self, actual: TargetValue) -> bool:
        """给定实际值，判断该关键结果是否达标。"""
        return self.target.evaluate(actual)

    def gap(self, actual: TargetValue) -> Decimal | None:
        """与目标的差距（正数表示未达标）。仅数值型指标可算。"""
        if self.metric_type is MetricType.BOOLEAN:
            return None
        current = actual.as_number()
        expected = self.target.value.as_number()
        if current is None or expected is None:
            return None
        if self.direction is Direction.HIGHER_IS_BETTER:
            return max(Decimal(0), expected - current)
        return max(Decimal(0), current - expected)


# 六要素：显示名 → 配置键
_SIX_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("Objective", "objective"),
    ("Key Results", "key_results"),
    ("Constraints", "constraints"),
    ("Non-goals", "non_goals"),
    ("Autonomy Policy", "autonomy"),
    ("Stop Conditions", "stop_conditions"),
)


def _has_content(value: object) -> bool:
    """判断某六要素是否"有内容"。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value) > 0
    if isinstance(value, Mapping):
        return len(value) > 0
    return True


class ProjectGoal(BaseModel):
    """目标六要素的聚合。

    六要素在 :meth:`_require_six_elements` 中强制齐全。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: tuple[Objective, ...] = ()
    key_results: tuple[KeyResult, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    non_goals: tuple[NonGoal, ...] = ()
    autonomy: AutonomyPolicy = Field(default_factory=default_policy)
    stop_conditions: tuple[StopCondition, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _require_six_elements(cls, data: object) -> object:
        """载入时检查六要素是否齐全，指出缺哪一项。

        放在 ``before`` 阶段是因为只有这里能看到"用户到底传了什么"——
        若字段带默认值，在 ``after`` 阶段已经分不清"没传"和"传了默认值"。
        """
        if not isinstance(data, Mapping):
            return data
        missing = [display for display, key in _SIX_ELEMENTS if not _has_content(data.get(key))]
        if missing:
            raise GoalValidationError(
                f"目标六要素不完整，缺少：{'、'.join(missing)}",
                missing_elements=missing,
            )
        return data

    def key_result(self, key_result_id: str) -> KeyResult | None:
        for key_result in self.key_results:
            if key_result.id == key_result_id:
                return key_result
        return None

    @classmethod
    def model_validate(
        cls,
        obj: object,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        from_attributes: bool | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """校验目标，并把结构化的目标错误还原后抛出。

        见 :func:`_rethrow_structured`。

        **为什么不用 ``**kwargs``**：Pydantic 的 ``model_validate`` 有六个
        关键字参数，每个都有确定的类型。把它们显式列出来，本方法的签名
        就是 Pydantic 签名的一个**具体化**版本 —— 类型检查器能验证两者
        兼容，而不是靠 ``**kwargs: Any`` 把一切吞掉。用 ``Any`` 意味着
        调用方传错参数类型也要等到运行时才发现。
        """
        try:
            return super().model_validate(
                obj,
                strict=strict,
                extra=extra,
                from_attributes=from_attributes,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except ValidationError as exc:
            _rethrow_structured(exc)


class RepositoryRef(BaseModel):
    """被管项目的仓库引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=1)
    default_branch: str = "main"
    local_path: Path | None = None


class Project(BaseModel):
    """项目。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    goal: ProjectGoal
    budget: Budget = Field(default_factory=Budget)
    repository: RepositoryRef
    tech_stack: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Project:
        """从字典构造。目标不合法时抛 :class:`GoalValidationError`。"""
        return cls.model_validate(raw)

    @classmethod
    def model_validate(
        cls,
        obj: object,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        from_attributes: bool | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """校验项目，并把结构化的目标错误还原后抛出。

        见 :func:`_rethrow_structured`。
        """
        try:
            return super().model_validate(
                obj,
                strict=strict,
                extra=extra,
                from_attributes=from_attributes,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except ValidationError as exc:
            _rethrow_structured(exc)

    @classmethod
    def from_yaml(cls, text: str) -> Project:
        """从 YAML 文本构造。

        兼容两种写法：

        - ``project:`` 包装（design.md §3.1 的示例形式）
        - 顶层直接写项目字段

        两种写法下六要素既可以内嵌在 ``goal:`` 下，也可以直接平铺。
        """
        raw = yaml.safe_load(text) or {}
        if not isinstance(raw, Mapping):
            raise GoalValidationError("项目配置必须是 YAML 映射")
        inner = raw.get("project", raw)
        if not isinstance(inner, Mapping):
            raise GoalValidationError("project 段必须是 YAML 映射")
        return cls.from_mapping(_split_goal(inner))


def _split_goal(raw: Mapping[str, object]) -> dict[str, object]:
    """把平铺的六要素归拢到 ``goal:`` 子键下。

    六要素平铺（design.md §3.1 的写法）与嵌套在 ``goal:`` 下是等价的 ——
    前者更贴近示例，后者更适合与其他项目级字段混排。
    """
    goal_keys = {key for _, key in _SIX_ELEMENTS}
    if "goal" in raw:
        return dict(raw)
    if not (goal_keys & set(raw)):
        return dict(raw)
    return {
        **{k: v for k, v in raw.items() if k not in goal_keys},
        "goal": {k: v for k, v in raw.items() if k in goal_keys},
    }


def load_project(path: Path) -> Project:
    """从 YAML 文件载入项目。"""
    return Project.from_yaml(path.read_text(encoding="utf-8"))


__all__ = [
    "ComparisonOperator",
    "Constraint",
    "ConstraintKind",
    "Direction",
    "EvalMethod",
    "GoalValidationError",
    "KeyResult",
    "MetricType",
    "NonGoal",
    "Objective",
    "Project",
    "ProjectGoal",
    "RepositoryRef",
    "StopCondition",
    "StopConditionKind",
    "TargetExpression",
    "TargetValue",
    "ValueKind",
    "load_project",
]
