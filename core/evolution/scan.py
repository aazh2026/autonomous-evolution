"""定时项目扫描的领域契约 —— 扫出什么、怎么算"扫过了"。

设计依据：docs/tasks.md TASK-P3-012（交付物：跨模块、长期积累问题的定期
扫描；验收条件：定期扫描能发现事件触发发现不了的问题（跨模块耦合类））；
R-080（事件驱动与定期扫描结合）；docs/design.md §4（"事件驱动与定期扫描
结合，才能既不漏局部变化，也不漏跨模块的长期积累"）。

**为什么还要有定期扫描。** 事件驱动的那一半天生只看**局部**：一次提交、
一次流水线失败、一次用户反馈 —— 它们各自指向一个具体的地方。跨模块耦合
不是这种形状的问题。**它没有事件。** 一个模块被七个模块依赖、而它自己
没有任何用例：没有任何一条事件会宣告这件事发生 —— 一切都正常，只有把
整个仓库摊开看才看得见。design.md §4 说"事件驱动与定期扫描结合，才能既
不漏局部变化，也不漏跨模块的长期积累"，说的就是这件事。

**信号不是结论。** 一条信号说"这里有个耦合枢纽，fan_in 是 7"；它不说"该
重构"。后者是决策层的事（P1-054 的 record / defer / reject）—— 扫描器
替它做这个判断就是越权。

**事实不是描述。** "耦合度过高"没法被独立核验；"fan_in=7、fan_out=3"可以。
所以每条信号都带一组**有名有姓的事实**（:class:`ScanFact`），下游可以
拿着它自己去查一遍。这与候选的 evidence 要求是同一条纪律（R-029：证据
必须是引用，不是描述）。

**"扫过了"与"没扫"是两件事。** 报告里既有扫出来的问题，也有扫过但什么
都没发现的种类。两者必须能分开看：策略关掉某一种类，与"扫了但没发现"，
在读报告的人眼里是两件完全不同的事 —— 前者是"没看"，后者是"看了，没有"。
:attr:`ScanReport.examined_modules` 与 :attr:`ScanReport.policy` 一起
把这件事说清楚。

**这一层不知道仓库长什么样。** 它只定义"扫出来的东西长什么样"。至于
耦合环、耦合枢纽这些结论怎么从索引里推出来，那是编排层的事
（:mod:`orchestration.scan`）—— 因为那些推导依赖架构模型，而架构模型在
``knowledge/``，那是比本层高的一层。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.evolution.assessment import GapSeverity


class ScanKind(StrEnum):
    """定时扫描能发现的问题种类。

    **五种都是"没有对应事件"的问题。** 这是这一层的存在理由：事件驱动的
    那一半看不见它们，因为它们压根不会以事件的形式发生。

    - ``dependency_cycle`` —— 模块之间形成了环；
    - ``coupling_hub`` —— 某个模块被太多模块依赖，改一处动全身；
    - ``layer_violation`` —— 依赖方向错了（产品代码依赖测试代码）；
    - ``untested_module`` —— 源模块有对外接口，却没有任何自有用例碰过；
    - ``orphaned_public_api`` —— 公开符号没有任何用例引用过。

    **后两种说的是"覆盖缺口"，不是"死代码"。** 静态分析看不见运行时：``obj.
    method()`` 这种属性访问、插件式注册、动态 ``getattr`` —— 用例可能真的在
    用它，只是索引里看不出来。所以这两种的准确读法是"**没有任何用例 import
    过它**"，而不是"没有人用它"。这个区别决定了下一步该做什么：前者是补
    一条用例（或者加一条显式的豁免说明），后者才谈得上删。
    """

    DEPENDENCY_CYCLE = "dependency_cycle"
    COUPLING_HUB = "coupling_hub"
    LAYER_VIOLATION = "layer_violation"
    UNTESTED_MODULE = "untested_module"
    ORPHANED_PUBLIC_API = "orphaned_public_api"


#: 默认扫描的种类。见 :class:`ScanKind` 的说明 —— 五种都是事件看不见的。
DEFAULT_SCAN_KINDS: Final[tuple[ScanKind, ...]] = tuple(ScanKind)

#: **耦合枢纽的默认阈值**：被依赖的模块数达到这个数才算枢纽。
#:
#: 为什么是 5：它要过滤掉"正常的高复用"。一个工具函数被 3 个模块用是
#: 常识，被 7 个模块用才值得看一眼。这个数是**本仓库选的默认值**，不是
#: 量出来的 —— 换成具体项目时应当按模块总数调。
DEFAULT_COUPLING_HUB_THRESHOLD: Final[int] = 5


class ScanFact(BaseModel):
    """一条有名有姓的事实。

    **名字 + 值，没有第三样。** 事实要能被独立核验，所以它只能是"某个
    叫 ``fan_in`` 的量等于 7"这种形状 —— 不能是"耦合情况比较严重"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 事实的名字（``fan_in`` / ``fan_out`` / ``impact`` …）。
    name: str = Field(min_length=1)
    #: 事实的值。**字符串**而不是数字 —— 有的事实是数字（扇入数），有的
    #: 是名单（依赖者），统一成字符串才放得下同一个容器。
    value: str = Field(min_length=1)

    @field_validator("name", "value")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("事实的名字与值都不能是空白")
        return value.strip()


class ScanSignal(BaseModel):
    """扫描出的一条信号。

    **它说的是"这里有个东西值得看一眼"，不是"该怎么做"。** 处置是决策
    层的事（:mod:`core.evolution.decision`）。

    :attr:`fingerprint` 是这条信号的**身份**：同一条问题在下一轮扫描里
    还是同一个指纹。它由种类与涉及的模块决定 —— 不含时间、不含计数，
    所以"耦合环还在"这件事在两轮扫描之间是可比的。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 问题的种类。
    kind: ScanKind
    #: 主模块 —— 这条信号主要指向谁。
    module: str = Field(min_length=1)
    #: 涉及的**全部**模块（耦合环有好几个）。只有一个时就是它自己。
    modules: tuple[str, ...] = ()
    #: 严重度。**用差距的那一套**（:class:`~core.evolution.assessment.
    #: GapSeverity`）—— 同一个词表在两个地方各写一遍，迟早对不上。
    severity: GapSeverity
    #: 一句话说明这条信号。
    summary: str = Field(min_length=1)
    #: 支撑这条信号的事实。**至少一条** —— 没有事实的信号只是一句说法。
    facts: tuple[ScanFact, ...] = Field(min_length=1)

    @field_validator("module", "summary")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("模块名与说明都不能是空白")
        return value.strip()

    @model_validator(mode="after")
    def _modules_must_contain_the_module(self) -> ScanSignal:
        """涉及到的模块必须包含主模块。"""
        if self.modules and self.module not in self.modules:
            raise ValueError("涉及到的模块里必须包含主模块")
        return self

    @property
    def fingerprint(self) -> str:
        """这条信号的身份 —— 与时间无关，与计数无关。

        **只由种类与模块集合决定。** 耦合环这一轮有 3 个模块、下一轮还是
        3 个模块（只是多了一个新的），它就是同一个问题；被依赖数从 7 涨到
        9，它也还是同一个问题 —— 变了的是 :attr:`facts`，不是身份。
        """
        involved = ",".join(sorted(self.modules or (self.module,)))
        return f"{self.kind}:{involved}"

    def fact(self, name: str) -> str | None:
        """按名字取一条事实。取不到返回 ``None``。"""
        for item in self.facts:
            if item.name == name:
                return item.value
        return None


class ScanScope(BaseModel):
    """这次扫描看哪些东西。

    :attr:`module_prefixes` 为空表示**全扫**。不是"扫了但没扫到" ——
    是"没设限制"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 项目 id。
    project_id: str = Field(min_length=1)
    #: 只看这些前缀开头的模块。空 = 不限。
    module_prefixes: tuple[str, ...] = ()

    @field_validator("project_id")
    @classmethod
    def _project_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("项目 id 不能是空白")
        return value.strip()

    def covers(self, module: str) -> bool:
        """这个模块在这次扫描范围内吗。"""
        if not self.module_prefixes:
            return True
        return any(module.startswith(prefix) for prefix in self.module_prefixes)


class ScanPolicy(BaseModel):
    """扫描策略 —— 看哪些种类、阈值定在哪。

    **策略是配置，结论是事实。** 阈值写着"fan_in 到 5 才算枢纽"，那是
    **本仓库选的默认值**；而"这个模块的 fan_in 是 7"是**从索引里读出来
    的事实**。两者混在一起说，就分不清"我们认为它高"与"它确实高"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 扫哪些种类。不在列表里的种类**不扫** —— 报告里也不会有它。
    kinds: tuple[ScanKind, ...] = DEFAULT_SCAN_KINDS
    #: 耦合枢纽的阈值：被依赖的模块数达到它才算枢纽。
    coupling_hub_threshold: int = Field(default=DEFAULT_COUPLING_HUB_THRESHOLD, ge=1)

    def scans(self, kind: ScanKind) -> bool:
        """这一次要不要扫这一种。"""
        return kind in self.kinds


class ScanReport(BaseModel):
    """一次扫描的结果。

    **空报告有三种，不能混。**

    1. 扫了，没发现问题 —— :attr:`examined_modules` 大于 0；
    2. 没扫这一种 —— 种类不在 :attr:`policy.kinds` 里；
    3. 压根没有数据可扫 —— :attr:`examined_modules` 是 0。

    第 3 种最容易被当成第 1 种："扫完了，什么都没有"和"没有东西可扫"在
    只看信号列表时长得一模一样。所以被扫过的模块数放在报告里 —— 它是
    三种情况里唯一能一眼分辨的那个。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 这次扫描的标识。
    scan_id: str = Field(min_length=1)
    #: 这次扫描的范围。
    scope: ScanScope
    #: 这次扫描用的策略。
    policy: ScanPolicy
    #: 扫出来的信号。
    signals: tuple[ScanSignal, ...] = ()
    #: **被扫过的模块数**。0 表示没有数据可扫，不是"扫了但没问题"。
    examined_modules: int = Field(ge=0)

    @field_validator("scan_id")
    @classmethod
    def _scan_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("扫描 id 不能是空白")
        return value.strip()

    def of_kind(self, kind: ScanKind) -> tuple[ScanSignal, ...]:
        """某一类别的信号。"""
        return tuple(item for item in self.signals if item.kind is kind)

    def for_module(self, module: str) -> tuple[ScanSignal, ...]:
        """与某个模块有关的信号（主模块或涉及模块）。"""
        return tuple(
            item for item in self.signals if item.module == module or module in item.modules
        )

    @property
    def fingerprints(self) -> frozenset[str]:
        """这次扫出来的所有信号的身份。

        **两轮扫描之间比这个集合，才知道"哪些问题是新的"。** 定时扫描
        天天跑同一个仓库，同一个问题天天报一遍没有意义 —— 但"今天多了一个
        新的耦合环"是要紧的。
        """
        return frozenset(item.fingerprint for item in self.signals)


__all__ = [
    "DEFAULT_COUPLING_HUB_THRESHOLD",
    "DEFAULT_SCAN_KINDS",
    "ScanFact",
    "ScanKind",
    "ScanPolicy",
    "ScanReport",
    "ScanScope",
    "ScanSignal",
]
