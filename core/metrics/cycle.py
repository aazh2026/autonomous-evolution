"""演进周期记录。

设计依据：docs/design.md §3.6（``EvolutionCycle``：cycle_id、outcome）、§12
（"每一轮演进都应该让项目状态、目标达成度或工程质量得到可验证的改善"）；
docs/tasks.md TASK-P1-070。

一个演进周期 = 一次循环 B：观察 → 发现 → 决策 → 规划（design.md §4）。
周期记录回答两个问题：

1. **这一轮做了什么？** —— 候选数 / 决策数 / 任务数 / 完成数 / 成本 / 证据引用。
2. **这一轮有没有变好？** —— ``outcome`` ∈ {improved, no_change, regressed, stalled}。

第二条才是重点。design.md §12 要求每一轮演进都产生**可验证的改善**；既然要
"可验证"，就得有一个能被机器判定的结论，而不是一句"这轮还行"。

**为什么 outcome 有四个取值而不是两个。** 只留 improved / regressed 时，
"这轮啥也没干成"就只能被硬塞进 improved 或 regressed —— 记录于是开始撒谎，
而撒谎的方向恰好是"看起来系统很能干"。所以：

- ``improved`` —— 达成度上升了；
- ``regressed`` —— 达成度下降了；
- ``no_change`` —— 没有**可验证的**改善（达成度确实没动，或缺可比的前值而无从判定）；
- ``stalled`` —— 这一轮什么也没落地（周期没跑完，或一个任务都没完成）。

**"不可判定"不能被当成"没变"。** 缺"之前"的达成度快照时算不出差值；这时
不能说"达成度没变"（那是在编），只能说"没有可验证的改善" —— 所以归到
``no_change``，并把前值留空，让读记录的人看得出这一条是缺基线的。

**这一模块不碰数据库。** 它只描述"一个周期记录长什么样"以及"结局怎么判"。
计数从哪来、记录怎么落库，是仓储层的事（``infrastructure/database/repositories.py``
的 ``CycleRepository``）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CycleRecordError(ValueError):
    """周期记录不合法。"""


class CycleNotClosedError(CycleRecordError):
    """周期还没结束。

    进行中的周期没有结局、也没有达成度差值 —— 把它还原成完整记录等于凭空
    造出一个结论。所以这里报错，而不是给出一个"暂时空着"的记录。
    """

    def __init__(self, cycle_id: str) -> None:
        super().__init__(f"周期 {cycle_id!r} 尚未结束，无法还原为完整记录")
        self.cycle_id = cycle_id


class CycleOutcome(StrEnum):
    """一轮演进的结局 —— "有没有变好"。

    四个取值缺一不可，理由见模块文档。
    """

    IMPROVED = "improved"
    NO_CHANGE = "no_change"
    REGRESSED = "regressed"
    STALLED = "stalled"


class CycleStatus(StrEnum):
    """周期**怎么**结束的。

    与 :class:`CycleOutcome` 是两个维度：一个是"跑完了吗"，一个是"变好了吗"。
    一轮被预算掐断的周期，跑完了但没变好 —— 它的 status 是
    ``budget_exhausted``，outcome 是 ``stalled``。
    """

    COMPLETED = "completed"
    ABORTED = "aborted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BLOCKED = "blocked"


class CycleCounts(BaseModel):
    """周期内各环节的产出计数。

    四个数字必须与数据库里的实际行数一致 —— 记录里写"发现了 5 个候选"而
    库里躺着 3 个，是最容易被忽略也最伤信任的一种偏差。所以这些数字由仓储层
    从数据库数出来，不接受调用方口头声明。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: int = Field(default=0, ge=0)
    decisions: int = Field(default=0, ge=0)
    tasks: int = Field(default=0, ge=0)
    completed_tasks: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_completed_subset(self) -> Self:
        if self.completed_tasks > self.tasks:
            raise ValueError(f"已完成任务数 {self.completed_tasks} 不能多于任务总数 {self.tasks}")
        return self


class CycleVerdict(BaseModel):
    """结局判定结果 —— 结论 + 理由。

    带理由是因为结论要能被审。一个只有 ``outcome`` 字段的记录，读者无从判断
    "improved" 是怎么来的；理由让判定本身也可复核。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: CycleOutcome
    reason: str = Field(min_length=1)


def classify_cycle(
    status: CycleStatus,
    counts: CycleCounts,
    *,
    attainment_before: Decimal | None,
    attainment_after: Decimal | None,
) -> CycleVerdict:
    """判定一轮演进的结局。

    判定顺序（先命中先返回）：

    1. 周期没有正常结束 → ``stalled``。被预算掐断的周期不能报"有改善"——
       它没跑完，"这一轮有没有变好"这个问题就没有答案。
    2. 没有任何任务完成 → ``stalled``。一个任务都没落地，这一轮就没有产出。
    3. 有可比的前后达成度 → 按差值方向给 ``improved`` / ``regressed`` /
       ``no_change``。
    4. 没有可比的前后达成度 → ``no_change``。缺基线时无法计算差值；说成
       "达成度没变"是编的，只能说"没有可验证的改善"。
    """
    if status is not CycleStatus.COMPLETED:
        return CycleVerdict(
            outcome=CycleOutcome.STALLED,
            reason=f"周期未正常结束（status={status.value}），本轮没有可交付的结论",
        )

    if counts.completed_tasks == 0:
        return CycleVerdict(
            outcome=CycleOutcome.STALLED,
            reason="本轮没有任何任务完成，没有落地任何产出",
        )

    if attainment_before is None or attainment_after is None:
        return CycleVerdict(
            outcome=CycleOutcome.NO_CHANGE,
            reason="缺少可比的前后目标达成度，本轮没有可验证的改善",
        )

    delta = attainment_after - attainment_before
    if delta > 0:
        return CycleVerdict(
            outcome=CycleOutcome.IMPROVED,
            reason=(f"目标达成度从 {attainment_before} 提升到 {attainment_after}（差值 +{delta}）"),
        )
    if delta < 0:
        return CycleVerdict(
            outcome=CycleOutcome.REGRESSED,
            reason=(f"目标达成度从 {attainment_before} 降到 {attainment_after}（差值 {delta}）"),
        )
    return CycleVerdict(
        outcome=CycleOutcome.NO_CHANGE,
        reason=f"目标达成度前后均为 {attainment_after}，没有变化",
    )


class EvolutionCycle(BaseModel):
    """一个演进周期的完整记录。

    **同名不同层**：``core.entities.EvolutionCycle`` 是**持久化实体**
    （SQLAlchemy ORM）；这里是**领域模型**（纯 Python，可独立校验与比较）。
    两者描述的是同一个东西的两种形态。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    #: 人类可读的周期编号，如 ``cycle-0007``。
    cycle_id: str = Field(min_length=1)
    #: 触发来源（commit / test_failure / defect / schedule / …）。
    trigger: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    #: 周期怎么结束的。
    status: CycleStatus
    #: 这一轮有没有变好。
    outcome: CycleOutcome
    #: 结局判定的理由 —— 让结论可复核。
    outcome_reason: str = Field(min_length=1)
    #: 各环节的产出计数。
    counts: CycleCounts
    #: 周期开始前的目标达成度。``None`` 表示没有可比的前值。
    attainment_before: Decimal | None = None
    #: 周期结束后的目标达成度。``None`` 表示没有可比的后值。
    attainment_after: Decimal | None = None
    #: 本轮花费。
    cost: Decimal = Field(default=Decimal(0), ge=0)
    #: 花费的币种。金额没有单位就不代表任何东西 —— 与
    #: ``infrastructure/database/budget.py::CurrencyConflictError`` 同一个道理。
    currency: str = ""
    #: 本轮产出的证据引用（artifact:// / issue: / 报告路径）。
    evidence_refs: tuple[str, ...] = ()

    @field_validator("attainment_before", "attainment_after")
    @classmethod
    def _attainment_in_range(cls, value: Decimal | None) -> Decimal | None:
        """达成度是比例，取值必然在 [0, 1]。"""
        if value is not None and not (Decimal(0) <= value <= Decimal(1)):
            raise ValueError(f"目标达成度必须是 [0, 1] 之间的比例，收到 {value}")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _refs_not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for ref in value:
            if not ref.strip():
                raise ValueError("证据引用不能是空串 —— 空引用指不到任何东西")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.ended_at < self.started_at:
            raise ValueError(f"周期结束时间 {self.ended_at} 不能早于开始时间 {self.started_at}")
        if self.cost > 0 and not self.currency.strip():
            raise ValueError("有花费就必须给出币种 —— 没有单位的数字不代表任何东西")
        return self

    @property
    def attainment_delta(self) -> Decimal | None:
        """前后达成度的差值。差值 > 0 表示"变好了"。

        缺"之前"或"之后"任一快照就返回 ``None`` —— 缺数据不是"差值为 0"。
        """
        if self.attainment_before is None or self.attainment_after is None:
            return None
        return self.attainment_after - self.attainment_before

    @property
    def duration_seconds(self) -> float:
        """周期时长（秒）。"""
        return (self.ended_at - self.started_at).total_seconds()

    @classmethod
    def build(
        cls,
        *,
        project_id: str,
        cycle_id: str,
        trigger: str,
        started_at: datetime,
        ended_at: datetime,
        status: CycleStatus,
        counts: CycleCounts,
        attainment_before: Decimal | None = None,
        attainment_after: Decimal | None = None,
        cost: Decimal = Decimal(0),
        currency: str = "",
        evidence_refs: tuple[str, ...] = (),
    ) -> EvolutionCycle:
        """由原始数据构造记录，结局与理由由判定器给出。

        结局**不接受外部指定** —— 给了也能被改写成别的，那这份记录就不再是
        判定结果，而是"某个人声称的结果"。想改结局，得改判定规则。
        """
        verdict = classify_cycle(
            status,
            counts,
            attainment_before=attainment_before,
            attainment_after=attainment_after,
        )
        return cls(
            project_id=project_id,
            cycle_id=cycle_id,
            trigger=trigger,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
            outcome=verdict.outcome,
            outcome_reason=verdict.reason,
            counts=counts,
            attainment_before=attainment_before,
            attainment_after=attainment_after,
            cost=cost,
            currency=currency,
            evidence_refs=evidence_refs,
        )


__all__ = [
    "CycleCounts",
    "CycleNotClosedError",
    "CycleOutcome",
    "CycleRecordError",
    "CycleStatus",
    "CycleVerdict",
    "EvolutionCycle",
    "classify_cycle",
]
