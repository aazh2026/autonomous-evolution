"""验收契约 —— 每个任务在开始执行前，必须先定义怎样才算完成。

设计依据：docs/design.md §3.5B（Acceptance Contract 的字段与"这里的覆盖率
阈值只是示意，实际应根据模块基线确定，不能通过降低测试质量来达到指标"）、
§6（``READY → RUNNING`` 的准入闸门）；docs/tasks.md TASK-P1-040、
R-051~R-054。

**契约是准入闸门，不是事后报告。** 这是本模块存在的全部理由。design.md
§3.5B 说"每个任务在开始执行前，必须先定义怎样才算完成" —— 如果契约可以在
执行完之后补上，那它就退化成一份事后报告，而事后报告是**没有约束力**的：
执行完之后再写"覆盖率 >= 0.85"和写"覆盖率 >= 0.01"，效果完全一样。

所以契约与任务绑定（:attr:`AcceptanceContract.task_id`），且必须**先于**执行
存在。状态机层面的强制在 :mod:`core.task.state_machine`（进入 ``RUNNING``
的准入闸门），编排层面的强制在 TASK-P1-062。

**三组内容，三种失效方式**：

``required``
    必须通过的检查（单元测试、集成测试、lint、架构规则）。失效方式是
    **没过**。

``thresholds``
    数值阈值（覆盖率等）。失效方式是**没达标**。阈值必须按模块基线确定
    （R-055 / TASK-P1-042）—— 契约里的阈值是**上限**，把上限设得比现状还低，
    等于用降低标准换取"通过"。

``forbidden``
    一旦命中即整体失败（diff 里有密钥、依赖被擅改、公共接口被破坏）。
    **这一组是一票否决** —— 别的都过了也不能放行。

**判定规则里的三条"不可判定 ≠ 未通过"**：

1. **没产出结果的必检项算失败**（不是"算过"）。缺一项结果和那一项明确
   失败，区别在于可追查性 —— 后者能去看日志，前者只能重跑。
2. **缺实测值的阈值算失败**（不是"没达标"）。少了指标值就无法判断有没有
   达标，而"无法判断"绝不能被当成"没问题"。
3. **类型不匹配的比较算失败**（不是"没达标"）。拿字符串去比覆盖率大小，
   比不出结果 —— 与其假装比了，不如老实说这条没验成。

**为什么复用 :mod:`core.project.models` 的比较词汇**：``ComparisonOperator`` /
``TargetValue`` / ``TargetExpression`` 已经是仓库里被钉住的"可比较取值"模型，
自带类型标记（number / boolean / text）与"类型不匹配就报错"的语义。
再定义一套只会让两份比较语义分叉 —— 哪天一边改了另一边没改，比对就会给出
两种不同的答案。代价是模块名不贴切（比较词汇住在 project models 里），
那是一个已知的小别扭，记在这里备查。

**为什么产出物词汇（``ArtifactKind``）住在 :mod:`core.evidence.artifacts`**：
三方都要说同一套话 —— 契约说"要哪些"，归档器说"存了哪些"，数据库
``artifact.type`` 列存的也是它们。词表放在使用方之一（这里），另外两方就
得各自抄一份；抄本迟早对不上，而对不上的那一刻没人说得清哪一份是对的。
所以它搬到了三方都往下能看见的地方（``core/``）。这里仍然可以直接
``from harness.verification.contract import ArtifactKind`` —— 转出只是为了让
老调用点不必改，不是因为它该住在这一层。

**这个模块只定义契约与判定规则，不执行任何检查。** 执行是验证引擎的事
（TASK-P1-041）。分开的原因是方向：契约说"什么算完成"，引擎说"这次够不够"；
反过来就等于让执行者自己定义验收标准。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.evidence.artifacts import ArtifactKind
from core.immutable import FrozenDict
from core.project.models import ComparisonOperator, TargetExpression, TargetValue, ValueKind

#: 排序类运算符 —— 只能与数值配对。
#: 布尔与文本没有大小可言，拿它们比大小比不出结果。
_ORDERING_OPERATORS: Final[frozenset[ComparisonOperator]] = frozenset(
    {
        ComparisonOperator.GT,
        ComparisonOperator.GTE,
        ComparisonOperator.LT,
        ComparisonOperator.LTE,
    }
)


class RequiredCheck(StrEnum):
    """必须通过的检查（design.md §3.5B 的 ``required``）。

    这四项是**阶段一**的最小集合。design.md §3.5B 把验证分了六层（功能 /
    代码 / 架构 / 安全 / 性能 / 产品），阶段一只做功能、代码与架构三层；
    安全层由 :mod:`harness.security.gate` 独立把关（它是 ``forbidden`` 那一
    组，不是 ``required`` 那一组），性能与产品层依赖基线，推迟到阶段二
    （TASK-P2-014）。
    """

    UNIT_TESTS_PASS = "unit_tests_pass"
    INTEGRATION_TESTS_PASS = "integration_tests_pass"
    LINT_PASS = "lint_pass"
    ARCHITECTURE_RULES_PASS = "architecture_rules_pass"


class ForbiddenRule(StrEnum):
    """命中即整体失败的禁止项（design.md §3.5B 的 ``forbidden``）。"""

    SECRETS_IN_DIFF = "secrets_in_diff"
    UNAUTHORIZED_DEPENDENCY_CHANGE = "unauthorized_dependency_change"
    PUBLIC_API_BREAKING_CHANGE = "public_api_breaking_change"


class CheckOutcome(StrEnum):
    """一项检查的结果。

    三态而不是布尔值：**"没跑成"与"跑了没过"必须分开**。前者是环境问题
    （缺工具、装不上依赖），后者是产出问题 —— 两者的处置完全不同：前者
    该重试或换环境，后者该返工。把两者压成一个布尔值，"依赖装不上"会被
    记成"测试没过"，然后派去修一个根本没坏的东西。
    """

    PASSED = "passed"
    FAILED = "failed"
    #: 检查没能跑完 —— 引擎报错了、工具缺失、超时。
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """单项必检检查的结果。

    :attr:`detail` 是给人看的线索：``FAILED`` 与 ``ERROR`` 在下游看起来
    一样，没带 detail 的话，人只能靠猜来判断该不该返工。
    """

    check: RequiredCheck
    outcome: CheckOutcome
    detail: str = ""


class Threshold(BaseModel):
    """一条数值阈值。

    形如 ``{"operator": ">=", "value": 0.85}``。``value`` 用带类型标记的
    :class:`~core.project.models.TargetValue` 承载，而不是裸的
    ``Decimal | str`` 联合 —— 后者无法区分"字符串 ``0.85``"与"数字 0.85"，
    而这个区分决定了阈值能不能比。

    **运算符与值类型必须匹配**：``>=`` 配字符串直接拒绝（比不出结果，
    而"比不出结果"绝不能被当成"达标"）。这条在构造时就拦下 —— 契约是
    执行前定下的，等执行完才发现阈值不可比，那时候已经白跑了一轮。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator: ComparisonOperator
    value: TargetValue

    @field_validator("value", mode="before")
    @classmethod
    def _accept_raw_scalar(cls, value: object) -> object:
        """允许直接写 ``value: 0.85``。

        契约是从 YAML / JSON 里读进来的，写成嵌套的
        ``{kind: number, number: 0.85}`` 太啰嗦。标量在这里被解析成带
        类型标记的 :class:`TargetValue`。
        """
        if isinstance(value, TargetValue):
            return value
        if isinstance(value, bool):
            return TargetValue(kind=ValueKind.BOOLEAN, flag=value)
        if isinstance(value, int | float | str):
            return TargetValue.parse(str(value))
        return value

    @model_validator(mode="after")
    def _check_operator_against_value(self) -> Threshold:
        if self.operator in _ORDERING_OPERATORS and self.value.kind is not ValueKind.NUMBER:
            raise ValueError(
                f"运算符 {self.operator.value!r} 只能与数值阈值配对，"
                f"收到的是 {self.value.kind.value} 型取值"
            )
        return self

    def evaluate(self, actual: TargetValue) -> bool:
        """实际值是否满足这条阈值。

        类型不匹配时抛 :class:`ValueError` —— 由调用方（契约判定）转成
        失败，而不是在这里悄悄返回 ``False``。理由同上："比不出结果"与
        "没达标"必须能被区分。
        """
        expression = TargetExpression(operator=self.operator, value=self.value)
        try:
            return expression.evaluate(actual)
        except ValueError as exc:
            raise ValueError(
                f"阈值 {self.operator.value} {self.value} 无法与 "
                f"{actual.kind.value} 型实际值比较：{exc}"
            ) from exc

    def __str__(self) -> str:
        return f"{self.operator.value} {self.value}"


class AcceptanceContract(BaseModel):
    """验收契约。

    **必须绑定一个任务**（:attr:`task_id`）。没有绑定就没有归属：契约说
    "覆盖率要 >= 0.85"，而"谁的覆盖率"必须能回答，否则这份契约可以被拿到
    任何一个任务上去放行。

    ``required`` 不得为空 —— 一份什么都不要求的契约等于没有契约，而它会
    换来一个"通过了"的结论。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    required: tuple[RequiredCheck, ...]
    thresholds: dict[str, Threshold] = Field(default_factory=dict)
    forbidden: tuple[ForbiddenRule, ...] = ()
    artifacts: tuple[ArtifactKind, ...] = ()

    @field_validator("required")
    @classmethod
    def _required_is_not_empty(cls, value: tuple[RequiredCheck, ...]) -> tuple[RequiredCheck, ...]:
        if not value:
            raise ValueError("required 不得为空 —— 一份什么都不要求的契约等于没有契约")
        return value

    @model_validator(mode="after")
    def _no_duplicate_entries(self) -> AcceptanceContract:
        _reject_duplicates(self.required, "required")
        _reject_duplicates(self.forbidden, "forbidden")
        _reject_duplicates(self.artifacts, "artifacts")
        return self

    @field_validator("thresholds", mode="after")
    @classmethod
    def _freeze_thresholds(cls, value: dict[str, Threshold]) -> dict[str, Threshold]:
        """阈值只读 —— 它是验收口径，不是运行时可改的临时状态。

        ``frozen=True`` 只挡住属性赋值，挡不住 ``contract.thresholds["x"] = ...``
        —— 而那正是"把验收标准改松"最短的一条路（core/immutable.py 记着
        同一类洞）。
        """
        return FrozenDict(value)

    def to_document(self) -> dict[str, object]:
        """落库形态 —— 存进 ``task.acceptance_contract``（JSON 列）。

        刻意用**可读的字典**而不是 ``model_dump()`` 的内部形态：这一列
        会被人直接查（"这个任务的验收标准是什么"），读不懂的形态等于没有。
        """
        return {
            "task_id": self.task_id,
            "required": [check.value for check in self.required],
            "thresholds": {
                name: {"operator": item.operator.value, "value": str(item.value)}
                for name, item in self.thresholds.items()
            },
            "forbidden": [rule.value for rule in self.forbidden],
            "artifacts": [kind.value for kind in self.artifacts],
        }

    @classmethod
    def from_document(cls, raw: object) -> AcceptanceContract:
        """从落库形态还原。

        还原失败要**当场抛错**，不能默默补默认值 —— 一份还原不出来的契约
        是坏数据，坏数据被当成"没有契约"处理，就等于绕过了准入闸门。
        """
        if not isinstance(raw, dict):
            raise ValueError(f"验收契约的落库形态必须是对象，收到 {type(raw).__name__}")
        thresholds = raw.get("thresholds") or {}
        if not isinstance(thresholds, dict):
            raise ValueError("验收契约的 thresholds 必须是对象")
        parsed_thresholds: dict[str, Threshold] = {}
        for name, item in thresholds.items():
            if not isinstance(item, dict):
                raise ValueError(f"阈值 {name!r} 必须是对象")
            parsed_thresholds[str(name)] = Threshold.model_validate(item)
        return cls.model_validate(
            {
                "task_id": raw.get("task_id", ""),
                "required": raw.get("required", ()),
                "thresholds": parsed_thresholds,
                "forbidden": raw.get("forbidden", ()),
                "artifacts": raw.get("artifacts", ()),
            }
        )


def _reject_duplicates(values: tuple[object, ...], field: str) -> None:
    """拒绝重复声明。

    重复项不是无害的：它会让契约看起来比实际要求更多，读的人会按重复的
    条数去理解门槛，而门槛其实只有一个。
    """
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"{field} 中重复声明了 {value}")
        seen.add(value)


@dataclass(frozen=True, slots=True)
class ContractEvidence:
    """一次执行产出的证据 —— 判定契约的全部输入。

    五项输入打包成一个对象，是为了让"判定需要什么"这件事**一眼可见**。
    散成五个关键字参数时，多传一个、少传一个都不会有人发现；而漏掉
    ``violations`` 意味着禁止项被静默跳过 —— 那正是设计里最不能漏的一项。
    """

    #: 这次执行属于哪个任务。
    task_id: str
    #: 必检检查的结果。没出现的必检项算"没产出结果"。
    checks: Mapping[RequiredCheck, CheckResult] = field(default_factory=dict)
    #: 阈值的实际值。缺项的阈值算"没验成"。
    metrics: Mapping[str, TargetValue] = field(default_factory=dict)
    #: 命中的禁止项。
    violations: frozenset[ForbiddenRule] = frozenset()
    #: 实际产出的产物。
    produced_artifacts: frozenset[ArtifactKind] = frozenset()


@dataclass(frozen=True, slots=True)
class ContractEvaluation:
    """契约判定结果。

    :attr:`failures` 记下每一条不通过的理由。**不带理由的结论没有用** ——
    "验收未通过"进到看板之后就再也说不清是哪一条、为什么。

    :attr:`missing` 与 :attr:`failures` 分开记：前者是"压根没拿到结果"，
    后者是"拿到结果了但没过"。两者的处置不同（重跑 vs 返工），混在一起
    就只能一律返工 —— 而对着一份从没跑过的检查返工，是修一个不存在的问题。
    """

    passed: bool
    failures: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


def evaluate_contract(
    contract: AcceptanceContract,
    evidence: ContractEvidence,
) -> ContractEvaluation:
    """按契约判定这次执行够不够格。

    **判定是"与"逻辑**：任何一条不满足，整体就不通过。这让"forbidden
    一票否决"成为规则的**推论**而不是特例分支 —— 不会出现"别的都过了所以
    密钥那项被放行"这种事。

    契约绑定的任务与 :attr:`ContractEvidence.task_id` 不符时**直接失败** ——
    拿 A 任务的契约去放行 B 任务，等于没有契约。
    """
    failures: list[str] = []
    missing: list[str] = []

    if contract.task_id != evidence.task_id:
        failures.append(
            f"验收契约绑定的是任务 {contract.task_id}，与本次执行的任务 {evidence.task_id} 不符"
        )

    failures.extend(_missing_required(contract, evidence, missing))
    failures.extend(_unsatisfied_thresholds(contract, evidence, missing))
    failures.extend(_missing_artifacts(contract, evidence, missing))
    failures.extend(_hit_forbidden(contract, evidence))

    return ContractEvaluation(
        passed=not failures,
        failures=tuple(failures),
        missing=tuple(missing),
    )


def _missing_required(
    contract: AcceptanceContract,
    evidence: ContractEvidence,
    missing: list[str],
) -> list[str]:
    """必检项的结果。**没出现在结果里的必检项算"没产出结果"**，不是"算过"。"""
    failures: list[str] = []
    for check in contract.required:
        result = evidence.checks.get(check)
        if result is None:
            missing.append(check.value)
            failures.append(f"必检项 {check.value} 没有产出结果")
        elif result.outcome is CheckOutcome.ERROR:
            failures.append(f"必检项 {check.value} 执行出错：{result.detail or '无详情'}")
        elif result.outcome is CheckOutcome.FAILED:
            failures.append(f"必检项 {check.value} 未通过：{result.detail or '无详情'}")
    return failures


def _unsatisfied_thresholds(
    contract: AcceptanceContract,
    evidence: ContractEvidence,
    missing: list[str],
) -> list[str]:
    """阈值的判定。缺实测值算失败 —— "无法判断"不能被当成"没问题"。"""
    failures: list[str] = []
    for name, threshold in contract.thresholds.items():
        actual = evidence.metrics.get(name)
        if actual is None:
            missing.append(name)
            failures.append(f"阈值 {name} 没有实测值")
            continue
        try:
            satisfied = threshold.evaluate(actual)
        except ValueError as exc:
            failures.append(f"阈值 {name} 未能判定：{exc}")
            continue
        if not satisfied:
            failures.append(f"阈值 {name} 未达标（要求 {threshold}，实测 {actual}）")
    return failures


def _missing_artifacts(
    contract: AcceptanceContract,
    evidence: ContractEvidence,
    missing: list[str],
) -> list[str]:
    """要求的产物缺任何一项都不算通过 —— 证据缺失时，"没证据"不等于"没问题"。"""
    failures: list[str] = []
    for kind in contract.artifacts:
        if kind not in evidence.produced_artifacts:
            missing.append(kind.value)
            failures.append(f"要求的产物 {kind.value} 未产出")
    return failures


def _hit_forbidden(
    contract: AcceptanceContract,
    evidence: ContractEvidence,
) -> list[str]:
    """命中的禁止项。一票否决。"""
    hit = sorted(rule.value for rule in contract.forbidden if rule in evidence.violations)
    if not hit:
        return []
    return [f"命中禁止项：{'、'.join(hit)}"]


__all__ = [
    "AcceptanceContract",
    "ArtifactKind",
    "CheckOutcome",
    "CheckResult",
    "ContractEvaluation",
    "ContractEvidence",
    "ForbiddenRule",
    "RequiredCheck",
    "Threshold",
    "evaluate_contract",
]
