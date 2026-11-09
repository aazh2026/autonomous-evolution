"""覆盖率基线 —— **阈值按模块各自的基线定，不许靠降基线过关**。

设计依据：docs/tasks.md TASK-P1-042（R-055 §3.5B 脚注、I-09）、TASK-P1-023
（快照里那份按模块的覆盖率是基线的输入）、R-056（覆盖率门槛）。

**这一层只回答两件事**：某个模块的覆盖率阈值是多少、一次基线变更能不能做。
存哪儿、谁来批、审计写哪儿，都是别人的事 —— 这一层只定口径与端口。

四条硬约束：

1. **阈值 = max(模块基线, 全局地板)。** 整体覆盖率会掩盖问题：新增一个
   没测的模块，总覆盖率只掉 0.3%，而那个模块其实是 0。所以阈值按模块算，
   而**模块基线压不过全局地板** —— 没有基线的模块拿到的也是地板。
2. **全局地板只往上调。** 地板是 0.85（:data:`GLOBAL_COVERAGE_FLOOR`），
   往上（更严）可以，往下（更松）不行 —— 地板往下调就是"把标准降下来换通过"。
3. **基线只往上调。** 覆盖率涨了，基线跟着涨；覆盖率跌了，基线**不动** ——
   基线是"已经做到过的高度"，不是"最近一次的高度"。否则测一次飘了就能把
   标准拉下来。
4. **下调要审批，被拒不留下审计记录。** 想下调就得有人批；没批就拒，**而且
   拒了要留痕** —— 否则"有人试过降基线"这件事没人知道，而它正是最该被
   看见的。

**"审批"批的是哪一次下调。** :class:`LoweringApproval` 里带着项目、模块与
**目标覆盖率**：批的是"把这个模块的基线降到 0.8"这一件事，不是"这个模块
以后随便降"。这条与 :mod:`core.policy.approval` 的老规矩一致 —— 批准的是
当时那一份，不是"以后这类都算"。

**审计写不下来，这次变更就不算数。** 与 :mod:`harness.security.audit` 的
fail-closed 同一条：写不下来的审计等于这件事没发生过。所以审计**先写**、
后改基线；写失败就抛错，基线保持原样。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

#: 全局地板 —— **任何模块的阈值都不低于它**。
#:
#: **为什么有地板。** 没有地板的话，一个从没测过的模块基线是 0，阈值也就是
#: 0 —— "这个模块一行都没测"于是合法了。而覆盖率门槛本来要防的正是这个。
#: 地板只能往上调（见 :class:`CoverageBaselineSet`）：往上调是收紧，往下调
#: 是放松，后者不在权限之内。
GLOBAL_COVERAGE_FLOOR: Final[Decimal] = Decimal("0.85")

#: 覆盖率的比例精度 —— 与 :mod:`evaluation.baselines` 那边的量化步长一致。
COVERAGE_QUANTUM: Final[Decimal] = Decimal("0.000001")


class CoverageBaselineError(ValueError):
    """覆盖率基线上的错。"""


@dataclass(frozen=True, slots=True)
class ModuleBaseline:
    """一个模块的覆盖率基线。

    **这是"已经做到过的高度"，不是"最近一次的高度"。** 它只会往上走
    （见 :class:`CoverageBaselineService`）—— 除非有人批了往下调。

    :attr:`sample_count` 是**这个数背后有多少个样本** —— 一个样本与十个
    样本的基线不是一回事，而基线表要能说清它是几个样本的中位数。
    """

    #: 模块名。与快照里那份按模块的覆盖率**用同一个名字**（否则对不上）。
    module: str
    #: 基线覆盖率 —— 0 到 1 之间。
    coverage: Decimal
    #: 样本数。
    sample_count: int = 1
    #: 这个数从哪儿来 —— 逐条留档，好让人复核。
    source: str = ""

    def __post_init__(self) -> None:
        if not self.module.strip():
            raise CoverageBaselineError("模块名不能是空白")
        if not Decimal(0) <= self.coverage <= Decimal(1):
            raise CoverageBaselineError(
                f"模块 {self.module} 的覆盖率必须在 0 与 1 之间：{self.coverage}"
            )
        if self.sample_count < 0:
            raise CoverageBaselineError(f"样本数不能为负：{self.sample_count}")

    def to_document(self) -> dict[str, object]:
        """落盘用的表示 —— **十进制量写成字符串**（读回来才不会改掉最后几位）。"""
        return {
            "module": self.module,
            "coverage": str(self.coverage),
            "sample_count": self.sample_count,
            "source": self.source,
        }

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> ModuleBaseline:
        """读回来 —— **读不懂就报错，不塞默认值**。"""
        module = raw.get("module")
        if not isinstance(module, str):
            raise CoverageBaselineError("模块基线缺 module")
        coverage = raw.get("coverage")
        if isinstance(coverage, bool) or not isinstance(coverage, (str, int, float, Decimal)):
            raise CoverageBaselineError(f"模块 {module} 的覆盖率不是一个数")
        try:
            ratio = Decimal(str(coverage))
        except ArithmeticError as error:
            raise CoverageBaselineError(f"模块 {module} 的覆盖率不是一个数") from error
        sample_count = raw.get("sample_count", 1)
        if isinstance(sample_count, bool) or not isinstance(sample_count, int):
            raise CoverageBaselineError(f"模块 {module} 的样本数不是一个整数")
        source = raw.get("source", "")
        if not isinstance(source, str):
            raise CoverageBaselineError(f"模块 {module} 的来源不是字符串")
        return cls(
            module=module,
            coverage=ratio,
            sample_count=sample_count,
            source=source,
        )


@dataclass(frozen=True, slots=True)
class CoverageBaselineSet:
    """一个项目的全部覆盖率基线 —— **地板、每模块的基线、下调次数**。

    **三者放在一份里，是因为它们必须一起变。** 地板与各模块的基线如果分两
    处存，一次写失败就会留下"地板是新的、模块是旧的"这种组合 —— 而那正是
    "标准被悄悄放松了"的样子。

    :attr:`lowering_count` 是**实际下调过几次**（被拒的不算）—— T2 要的
    "基线下调次数 = 0"就是看它。
    """

    #: 项目 id。
    project_id: str
    #: 全局地板 —— **不低于** :data:`GLOBAL_COVERAGE_FLOOR`。
    floor: Decimal = GLOBAL_COVERAGE_FLOOR
    #: 每模块的基线。
    modules: Mapping[str, ModuleBaseline] = field(default_factory=dict)
    #: 实际下调过几次。
    lowering_count: int = 0

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise CoverageBaselineError("项目 id 不能是空白")
        if self.floor < GLOBAL_COVERAGE_FLOOR:
            raise CoverageBaselineError(
                f"全局地板不能低于 {GLOBAL_COVERAGE_FLOOR}：{self.floor} —— "
                "往下调地板就是降低标准换通过"
            )
        if self.lowering_count < 0:
            raise CoverageBaselineError(f"下调次数不能为负：{self.lowering_count}")
        object.__setattr__(self, "floor", self.floor.quantize(COVERAGE_QUANTUM))
        object.__setattr__(self, "modules", MappingProxyType(dict(self.modules)))

    # ---------- 阈值 ----------

    def threshold_for(self, module: str) -> Decimal:
        """这个模块的覆盖率阈值 —— **max(模块基线, 全局地板)**。

        **没有基线的模块拿到的也是地板。** 一个从没量过的模块不是"要求 0"，
        而是"要求至少达到地板" —— 前者是放过，后者是守住。
        """
        baseline = self.modules.get(module)
        if baseline is None:
            return self.floor
        return max(baseline.coverage, self.floor)

    def thresholds(self) -> Mapping[str, Decimal]:
        """全部模块的阈值 —— **包括只有地板的那些**。"""
        names = set(self.modules)
        names.update(item.module for item in self.modules.values())
        return MappingProxyType({name: self.threshold_for(name) for name in sorted(names)})

    def module_baseline(self, module: str) -> ModuleBaseline | None:
        """这个模块的基线 —— **没有就是 ``None``**（不是 0）。"""
        return self.modules.get(module)

    # ---------- 变更 ----------

    def with_module(self, baseline: ModuleBaseline) -> CoverageBaselineSet:
        """换掉一个模块的基线 —— **返回新的一份，不改原来那份**。

        **基线是证据的一部分** —— 原地改掉它，事后就分不清"当时是按哪个标准
        判的"。
        """
        modules = dict(self.modules)
        modules[baseline.module] = baseline
        return replace(self, modules=modules)

    def with_floor(self, floor: Decimal) -> CoverageBaselineSet:
        """换掉全局地板 —— **只能往上调**。"""
        if floor < self.floor:
            raise CoverageBaselineError(
                f"全局地板不能往下调：{self.floor} → {floor} —— 降低标准不在权限之内"
            )
        return replace(self, floor=floor.quantize(COVERAGE_QUANTUM))

    def counted_lowering(self) -> CoverageBaselineSet:
        """记一次**实际发生的**下调。"""
        return replace(self, lowering_count=self.lowering_count + 1)

    # ---------- 落盘 ----------

    def to_document(self) -> dict[str, object]:
        """落盘用的表示 —— **键排好序**，两次落盘才能逐字节比较。"""
        return {
            "project_id": self.project_id,
            "floor": str(self.floor),
            "lowering_count": self.lowering_count,
            "modules": self.modules_document(),
        }

    def modules_document(self) -> dict[str, object]:
        """各模块的基线 —— **落库时写进 JSON 列的那一份**。

        **单独一个入口**，是为了落库那一层不必自己去拆整个文档 —— 拆文档的
        形状是这一层的事，不是存储的事。
        """
        return {name: self.modules[name].to_document() for name in sorted(self.modules)}

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> CoverageBaselineSet:
        """读回来 —— **不认识的键直接报错**。

        少写一个字段会让那一列静默消失，而消失的列看起来跟"没量过"一模一样。
        """
        known = {"project_id", "floor", "lowering_count", "modules"}
        unknown = set(raw) - known
        if unknown:
            raise CoverageBaselineError(f"基线文档里有不认识的键：{'、'.join(sorted(unknown))}")
        project_id = raw.get("project_id")
        if not isinstance(project_id, str):
            raise CoverageBaselineError("基线文档缺 project_id")
        floor_raw = raw.get("floor", GLOBAL_COVERAGE_FLOOR)
        if isinstance(floor_raw, bool) or not isinstance(floor_raw, (str, int, float, Decimal)):
            raise CoverageBaselineError("全局地板不是一个数")
        try:
            floor = Decimal(str(floor_raw))
        except ArithmeticError as error:
            raise CoverageBaselineError("全局地板不是一个数") from error
        lowering_count = raw.get("lowering_count", 0)
        if isinstance(lowering_count, bool) or not isinstance(lowering_count, int):
            raise CoverageBaselineError("下调次数不是一个整数")
        modules_raw = raw.get("modules", {})
        if not isinstance(modules_raw, Mapping):
            raise CoverageBaselineError("modules 必须是一个映射")
        modules: dict[str, ModuleBaseline] = {}
        for name, entry in modules_raw.items():
            if not isinstance(entry, Mapping):
                raise CoverageBaselineError(f"模块 {name} 的基线不是一个映射")
            baseline = ModuleBaseline.from_document(entry)
            if baseline.module != name:
                raise CoverageBaselineError(
                    f"模块键 {name} 与基线里的模块名 {baseline.module} 对不上"
                )
            modules[name] = baseline
        return cls(
            project_id=project_id,
            floor=floor,
            modules=modules,
            lowering_count=lowering_count,
        )


class BaselineUpdateKind(StrEnum):
    """一次基线更新的结果 —— **四种，缺一个都不对**。"""

    #: 基线被抬高。
    RAISED = "raised"
    #: 没有变化。
    UNCHANGED = "unchanged"
    #: 基线被下调 —— **凭审批**。
    LOWERED = "lowered"
    #: 下调被拒 —— **基线没动**。
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class LoweringApproval:
    """一次"允许下调"的批准。

    **它批的是"把某个模块的基线降到某个数"这一件事。** 项目、模块、目标
    覆盖率三样都对得上才算数 —— 批了 A 模块的 0.8，拿去降 B 模块的 0.5，
    不认。
    """

    #: 批的是哪个项目。
    project_id: str
    #: 批的是哪个模块。
    module: str
    #: 批的是降到多少。
    target_coverage: Decimal
    #: 谁批的。
    approver: str
    #: 什么时候批的。
    approved_at: datetime

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise CoverageBaselineError("审批缺 project_id")
        if not self.module.strip():
            raise CoverageBaselineError("审批缺 module")
        if not Decimal(0) <= self.target_coverage <= Decimal(1):
            raise CoverageBaselineError(f"批准的覆盖率必须在 0 与 1 之间：{self.target_coverage}")
        if not self.approver.strip():
            raise CoverageBaselineError("审批缺批准人")
        moment = self.approved_at
        object.__setattr__(
            self,
            "approved_at",
            moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC),
        )

    def covers(self, project_id: str, module: str, target: Decimal) -> bool:
        """这份批准覆盖这一次下调吗。"""
        return (
            self.project_id == project_id
            and self.module == module
            and self.target_coverage == target
        )


@dataclass(frozen=True, slots=True)
class BaselineUpdateRequest:
    """一次基线更新的请求。"""

    #: 哪个项目。
    project_id: str
    #: 哪个模块。
    module: str
    #: 想把基线设成多少。
    coverage: Decimal
    #: 谁提的。
    requested_by: str
    #: 哪个任务上提的 —— 写进审计，好追溯。
    task_id: str = ""
    #: 样本数 —— 新基线背后有多少个样本。
    sample_count: int = 1
    #: 来源 —— 逐条留档。
    source: str = ""
    #: 批准 —— **下调时必须带上**。
    approval: LoweringApproval | None = None

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise CoverageBaselineError("请求缺 project_id")
        if not self.module.strip():
            raise CoverageBaselineError("请求缺 module")
        if not Decimal(0) <= self.coverage <= Decimal(1):
            raise CoverageBaselineError(f"覆盖率必须在 0 与 1 之间：{self.coverage}")
        if not self.requested_by.strip():
            raise CoverageBaselineError("请求缺提名人")
        if self.sample_count < 0:
            raise CoverageBaselineError(f"样本数不能为负：{self.sample_count}")


@dataclass(frozen=True, slots=True)
class BaselineUpdateOutcome:
    """一次基线更新的结果。

    :attr:`effective` 是**落下去之后的基线** —— 被拒时是 ``None``，因为
    没有"落下去"这回事。
    """

    #: 结果。
    kind: BaselineUpdateKind
    #: 哪个项目。
    project_id: str
    #: 哪个模块。
    module: str
    #: 原来是多少 —— **没有基线时是 ``None``**。
    previous: Decimal | None
    #: 想设成多少。
    proposed: Decimal
    #: 落下去之后是多少 —— **被拒时是 ``None``**。
    effective: Decimal | None
    #: 为什么 —— 说给人听的一句。
    reason: str = ""

    @property
    def applied(self) -> bool:
        """基线动了吗。"""
        return self.kind in (BaselineUpdateKind.RAISED, BaselineUpdateKind.LOWERED)

    @property
    def refused(self) -> bool:
        """被拒了吗。"""
        return self.kind is BaselineUpdateKind.REFUSED


@runtime_checkable
class CoverageBaselineStore(Protocol):
    """基线的存取口。

    **读不到就是"还没有基线"，不是"基线是 0"。** 实现返回一份空集合
    （而不是 ``None``）—— 那份空集合的地板就是全局地板，阈值照常算得出来。
    """

    async def load(self, project_id: str) -> CoverageBaselineSet:
        """读这个项目的基线 —— **没有就返回一份空的**。"""
        ...

    async def save(self, baselines: CoverageBaselineSet) -> CoverageBaselineSet:
        """存一份基线 —— **存的是给出去的那一份**（落库后读回来应当一样）。"""
        ...


@dataclass(frozen=True, slots=True)
class LoweringAttempt:
    """一次基线下调尝试 —— **审计记的是它**。

    **成功还是被拒都记。** 被拒的更要记：拒绝只留下一句"没做"，而"为什么
    没做"往往正是后来要复盘的东西。
    """

    #: 哪个项目。
    project_id: str
    #: 哪个模块。
    module: str
    #: 现在是多少 —— **没有基线时是 ``None``**。
    current: Decimal | None
    #: 想降到多少。
    proposed: Decimal
    #: 谁提的。
    requested_by: str
    #: 哪个任务上提的。
    task_id: str = ""
    #: 有没有批准。
    approved: bool = False
    #: 为什么 —— 说给人听的一句。
    reason: str = ""


@runtime_checkable
class BaselineAuditSink(Protocol):
    """基线变更的审计口。

    **写失败必须抛异常。** 吞掉异常等于告诉调用方"记下来了"，而实际上没有
    —— 那正是"审计失败就放行"最隐蔽的一种写法。
    """

    def record_lowering_attempt(self, attempt: LoweringAttempt) -> None:
        """记下一次基线下调尝试。"""
        ...


@runtime_checkable
class BaselineClock(Protocol):
    """取时间的口 —— **时间从这儿来，不从别处偷**。"""

    def now(self) -> datetime:
        """现在几点。"""
        ...


class SystemBaselineClock:
    """系统时钟 —— **UTC**。"""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CoverageBaselineService:
    """覆盖率基线的服务 —— **口径在这里，存哪儿是别人的事**。

    **三条流程：**

    - :meth:`update` —— 一次显式的基线变更。抬高自动，**下调要审批**。
    - :meth:`sync_with_measurements` —— **随代码演进**的那条：把量到的
      覆盖率与基线对齐。**只往上调** —— 量得比基线低不是下调的理由，那是
      "这次测砸了"，而基线是"已经做到过的高度"。
    - :meth:`threshold` —— 某个模块现在的阈值是多少。

    **下调被拒不留下审计记录就不算数。** 所以顺序是**先写审计、后改基线**：
    审计写失败就抛错，基线保持原样 —— 与 :mod:`harness.security.audit` 的
    fail-closed 同一条道理。
    """

    #: 基线存哪儿。
    store: CoverageBaselineStore
    #: 审计写哪儿。
    audit: BaselineAuditSink
    #: 现在几点。
    clock: BaselineClock = field(default_factory=SystemBaselineClock)

    # ---------- 读 ----------

    async def current(self, project_id: str) -> CoverageBaselineSet:
        """这个项目现在的基线。"""
        return await self.store.load(project_id)

    async def threshold(self, project_id: str, module: str) -> Decimal:
        """这个模块现在的阈值 —— **max(模块基线, 全局地板)**。"""
        baselines = await self.store.load(project_id)
        return baselines.threshold_for(module)

    async def thresholds(self, project_id: str) -> Mapping[str, Decimal]:
        """全部模块的阈值。"""
        baselines = await self.store.load(project_id)
        return baselines.thresholds()

    # ---------- 写 ----------

    async def update(self, request: BaselineUpdateRequest) -> BaselineUpdateOutcome:
        """一次基线变更 —— **抬高自动，下调要审批**。

        **下调的顺序是先审计、后改基线。** 审计写不下来的话这次变更就不算
        数 —— 抛错，基线保持原样。
        """
        current = await self.store.load(request.project_id)
        existing = current.module_baseline(request.module)
        previous = None if existing is None else existing.coverage

        if previous is None:
            return await self._apply(
                current,
                request,
                kind=BaselineUpdateKind.RAISED,
                previous=None,
                reason="新模块，第一次建立基线",
            )
        if request.coverage == previous:
            return BaselineUpdateOutcome(
                kind=BaselineUpdateKind.UNCHANGED,
                project_id=request.project_id,
                module=request.module,
                previous=previous,
                proposed=request.coverage,
                effective=previous,
                reason="与现在的基线一样",
            )
        if request.coverage > previous:
            return await self._apply(
                current,
                request,
                kind=BaselineUpdateKind.RAISED,
                previous=previous,
                reason="覆盖率上去了，基线跟着上",
            )

        # 走到这儿就是"要往下调" —— 那是唯一需要审批的一条路。
        approval = request.approval
        approved = approval is not None and approval.covers(
            request.project_id, request.module, request.coverage
        )
        reason = (
            "有批准，下调"
            if approved
            else "没有对应的批准 —— 下调基线就是降低测试质量换通过，不予放行"
        )
        self.audit.record_lowering_attempt(
            LoweringAttempt(
                project_id=request.project_id,
                module=request.module,
                current=previous,
                proposed=request.coverage,
                requested_by=request.requested_by,
                task_id=request.task_id,
                approved=approved,
                reason=reason,
            )
        )
        if not approved:
            return BaselineUpdateOutcome(
                kind=BaselineUpdateKind.REFUSED,
                project_id=request.project_id,
                module=request.module,
                previous=previous,
                proposed=request.coverage,
                effective=None,
                reason=reason,
            )
        saved = await self._apply(
            current,
            request,
            kind=BaselineUpdateKind.LOWERED,
            previous=previous,
            reason=reason,
        )
        return saved

    async def sync_with_measurements(
        self,
        *,
        project_id: str,
        coverage_by_module: Mapping[str, Decimal],
        requested_by: str,
        task_id: str = "",
        source: str = "",
    ) -> tuple[BaselineUpdateOutcome, ...]:
        """随代码演进 —— **把量到的覆盖率与基线对齐**。

        **只往上调。** 量到的比基线低时**不动**：基线是"已经做到过的高度"，
        而一次测砸了不代表那个高度没达到过。每一次"没达标"都当成一次下调
        请求，审计表会被自己的噪声淹掉 —— 而真正要盯的那次下调就被埋了。

        **新模块会建立基线** —— 没有基线的模块阈值就是地板，而实测值高于
        地板时，把它记成基线才不浪费这次测量。
        """
        outcomes: list[BaselineUpdateOutcome] = []
        current = await self.store.load(project_id)
        for module in sorted(coverage_by_module):
            measured = coverage_by_module[module]
            if not Decimal(0) <= measured <= Decimal(1):
                raise CoverageBaselineError(
                    f"模块 {module} 的实测覆盖率必须在 0 与 1 之间：{measured}"
                )
            existing = current.module_baseline(module)
            if existing is not None and measured <= existing.coverage:
                outcomes.append(
                    BaselineUpdateOutcome(
                        kind=BaselineUpdateKind.UNCHANGED,
                        project_id=project_id,
                        module=module,
                        previous=existing.coverage,
                        proposed=measured,
                        effective=existing.coverage,
                        reason="没超过基线 —— 基线只往上调",
                    )
                )
                continue
            request = BaselineUpdateRequest(
                project_id=project_id,
                module=module,
                coverage=measured,
                requested_by=requested_by,
                task_id=task_id,
                source=source,
            )
            outcomes.append(await self.update(request))
            current = await self.store.load(project_id)
        return tuple(outcomes)

    async def _apply(
        self,
        current: CoverageBaselineSet,
        request: BaselineUpdateRequest,
        *,
        kind: BaselineUpdateKind,
        previous: Decimal | None,
        reason: str,
    ) -> BaselineUpdateOutcome:
        """把新基线落下去。"""
        baseline = ModuleBaseline(
            module=request.module,
            coverage=request.coverage.quantize(COVERAGE_QUANTUM),
            sample_count=request.sample_count,
            source=request.source,
        )
        updated = current.with_module(baseline)
        if kind is BaselineUpdateKind.LOWERED:
            updated = updated.counted_lowering()
        saved = await self.store.save(updated)
        stored = saved.module_baseline(request.module)
        return BaselineUpdateOutcome(
            kind=kind,
            project_id=request.project_id,
            module=request.module,
            previous=previous,
            proposed=request.coverage,
            effective=baseline.coverage if stored is None else stored.coverage,
            reason=reason,
        )


__all__ = [
    "COVERAGE_QUANTUM",
    "GLOBAL_COVERAGE_FLOOR",
    "BaselineAuditSink",
    "BaselineClock",
    "BaselineUpdateKind",
    "BaselineUpdateOutcome",
    "BaselineUpdateRequest",
    "CoverageBaselineError",
    "CoverageBaselineService",
    "CoverageBaselineSet",
    "CoverageBaselineStore",
    "LoweringApproval",
    "ModuleBaseline",
    "SystemBaselineClock",
]
