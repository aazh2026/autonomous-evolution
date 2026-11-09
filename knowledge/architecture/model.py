"""架构模型 —— 从索引里读出来的"这个仓库长成什么样"。

设计依据：docs/tasks.md TASK-P2-016（交付物：架构知识图谱、ADR 存储、
模块依赖索引）、R-064（不每轮重读仓库重建认知）、R-067（不用对话历史
当事实）；docs/design.md §3.6（架构知识图谱）。

**这个模块不读仓库。** 它的输入只有一份 :class:`RepositoryIndex` ——
所有结论都是从索引里**推导**出来的。这不是风格偏好，是 TASK-P2-016 的
验收条件："系统能回答'某模块被哪些模块依赖'且答案来自已构建的索引而非
每次重读仓库"。要是这里偷偷去读文件，那条验收就形同虚设。

**架构不是"画一张图"。** 图是给人看的，模型是给系统用的。这里推导出来
的三类东西各有用处：

1. **分层与依赖方向** —— 产品代码依赖测试代码，是架构问题；
2. **依赖环** —— 环会让"改哪个模块"这个问题没有确定的起点；
3. **没人碰的公开接口** —— 源模块里有公开符号，却没有任何一条自有用例
   引用它。它是覆盖率缺口的**结构性证据**：不是"覆盖率恰好低"，而是
   "这个对外接口根本没有测试在用它"。

**为什么第三条不能用覆盖率代替。** 覆盖率是**执行**出来的数据 —— 要跑
一遍测试才知道。而"没有任何测试文件 import 过这个模块"是**静态**事实，
索引里就有。跑测试要几十秒，读索引要几毫秒，而这条结论在两种情况下
指向同一件事。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from knowledge.repository_index.index import (
    KIND_CLASS,
    KIND_FUNCTION,
    KIND_METHOD,
    ModuleLayer,
    ModuleRecord,
    RepositoryIndex,
)

#: 入口函数名。带这些名字的公开函数视为模块的入口。
ENTRY_POINT_NAMES: Final[tuple[str, ...]] = ("main", "cli", "run")

#: 一个环至少要有几个模块 —— 一个模块自己依赖自己是另一条规则（自环）。
MIN_CYCLE_SIZE: Final[int] = 2


class ViolationKind(StrEnum):
    """架构问题的种类。"""

    #: 产品代码依赖测试代码。
    SOURCE_IMPORTS_TEST = "source_imports_test"
    #: 产品代码依赖缺陷复现套件。
    SOURCE_IMPORTS_KNOWN_ISSUE = "source_imports_known_issue"
    #: 产品代码依赖脚本。
    SOURCE_IMPORTS_SCRIPT = "source_imports_script"
    #: 模块之间形成了循环依赖。
    CYCLIC_DEPENDENCY = "cyclic_dependency"


@dataclass(frozen=True, slots=True)
class LayerViolation:
    """一条分层违规 —— "这条依赖不该存在"。"""

    kind: ViolationKind
    importer: str
    imported: str
    message: str

    def describe(self) -> str:
        """一句话说明这条违规。"""
        return f"[{self.kind}] {self.message}"


@dataclass(frozen=True, slots=True)
class DependencyCycle:
    """一个循环依赖 —— 环上的模块按依赖顺序排列。

    环上的模块**用一条边就能首尾相接**（最后一个依赖第一个）。
    """

    modules: tuple[str, ...]

    def describe(self) -> str:
        """一句话说明这个环。"""
        return " → ".join((*self.modules, self.modules[0]))


@dataclass(frozen=True, slots=True)
class ArchitectureModel:
    """从索引推导出来的架构模型。

    **模型是不可变的。** 索引变了就重新推导一个 —— 而不是就地改这个。
    理由与索引一样：上一轮的结构要还能按上一轮的样子被问。
    """

    index: RepositoryIndex

    # ------------------------------------------------------------ 分层

    def modules_in_layer(self, layer: ModuleLayer) -> tuple[str, ...]:
        """某一层里的模块名（按字典序）。"""
        return tuple(sorted(item.module for item in self.index.modules if item.layer == layer))

    def layers(self) -> Mapping[ModuleLayer, tuple[str, ...]]:
        """全部分层。"""
        return {layer: self.modules_in_layer(layer) for layer in ModuleLayer}

    def layer_of(self, module: str) -> ModuleLayer | None:
        """模块在哪一层。不在索引里返回 ``None``。"""
        record = self.index.record(module)
        return record.layer if record else None

    # ------------------------------------------------------------ 依赖

    def dependents(self, module: str) -> tuple[str, ...]:
        """谁依赖这个模块（直接）。答案来自索引，不读仓库。"""
        return self.index.dependents(module)

    def dependencies(self, module: str) -> tuple[str, ...]:
        """这个模块依赖谁。"""
        return self.index.dependencies(module)

    def impact_of(self, module: str) -> tuple[str, ...]:
        """改这个模块会影响谁（直接 + 间接）。

        这是**动手之前**要看的那张名单。直接依赖者只是第一层 ——
        真正会被改坏的是所有传递依赖者。
        """
        return self.index.transitive_dependents(module)

    def cycles(self) -> tuple[DependencyCycle, ...]:
        """全部循环依赖（按第一个模块的名字排序）。

        **用强连通分量找，不做枚举。** 找出"哪些模块在同一个环里"用 SCC
        就够了；而把环**逐条枚举**出来是指数级的 —— 一个模块多的仓库上
        那不是慢，那是做不完。系统要的是"有没有环、环上是谁"，不是
        "一共有几条不同的环路径"。
        """
        adjacency = self._adjacency()
        components = _strongly_connected_components(adjacency)
        cycles: list[DependencyCycle] = []
        for component in components:
            if len(component) < MIN_CYCLE_SIZE:
                continue
            if self._has_self_loop(component[0]):
                continue
            cycles.append(DependencyCycle(modules=tuple(sorted(component))))
        cycles.sort(key=lambda cycle: cycle.modules)
        return tuple(cycles)

    def _has_self_loop(self, module: str) -> bool:
        """模块是不是依赖自己。"""
        return module in self.index.dependencies(module)

    def _adjacency(self) -> Mapping[str, tuple[str, ...]]:
        """依赖邻接表（导入方 → 被导入方）。"""
        table: dict[str, set[str]] = {}
        for edge in self.index.edges():
            table.setdefault(edge.importer, set()).add(edge.imported)
            table.setdefault(edge.imported, set())
        return {node: tuple(sorted(children)) for node, children in table.items()}

    # ------------------------------------------------------------ 分层违规

    def violations(self) -> tuple[LayerViolation, ...]:
        """全部分层违规（按导入方、导入项排序）。

        **规则只有三条，且都只针对产品代码。** 测试依赖产品是对的（测试
        就是用来依赖产品的）；缺陷复现套件依赖产品也是对的（它要复现
        产品的问题）。反过来 —— 产品依赖测试 —— 意味着测试代码被编进了
        产品，或者产品在运行时去找测试。两种都不是好事。
        """
        found: list[LayerViolation] = []
        for edge in self.index.edges():
            importer_layer = self.layer_of(edge.importer)
            imported_layer = self.layer_of(edge.imported)
            if importer_layer is None or imported_layer is None:
                continue
            if importer_layer is not ModuleLayer.SOURCE:
                continue
            if imported_layer is ModuleLayer.TEST:
                found.append(
                    LayerViolation(
                        kind=ViolationKind.SOURCE_IMPORTS_TEST,
                        importer=edge.importer,
                        imported=edge.imported,
                        message=(f"产品模块 {edge.importer} 依赖了测试模块 {edge.imported}"),
                    )
                )
            elif imported_layer is ModuleLayer.KNOWN_ISSUE:
                found.append(
                    LayerViolation(
                        kind=ViolationKind.SOURCE_IMPORTS_KNOWN_ISSUE,
                        importer=edge.importer,
                        imported=edge.imported,
                        message=(f"产品模块 {edge.importer} 依赖了缺陷复现套件 {edge.imported}"),
                    )
                )
            elif imported_layer is ModuleLayer.SCRIPT:
                found.append(
                    LayerViolation(
                        kind=ViolationKind.SOURCE_IMPORTS_SCRIPT,
                        importer=edge.importer,
                        imported=edge.imported,
                        message=f"产品模块 {edge.importer} 依赖了脚本 {edge.imported}",
                    )
                )
        for cycle in self.cycles():
            found.append(
                LayerViolation(
                    kind=ViolationKind.CYCLIC_DEPENDENCY,
                    importer=cycle.modules[0],
                    imported=cycle.modules[-1],
                    message=f"存在循环依赖：{cycle.describe()}",
                )
            )
        found.sort(key=lambda item: (item.importer, item.imported, item.kind))
        return tuple(found)

    # ------------------------------------------------------------ 公开接口

    def public_surface(self, module: str) -> tuple[str, ...]:
        """模块对外暴露的符号名（不含下划线开头的私有符号）。"""
        return tuple(
            symbol.qualname
            for symbol in self.index.symbols_of(module)
            if symbol.is_public and symbol.kind in (KIND_FUNCTION, KIND_METHOD)
        )

    def entry_points(self) -> tuple[str, ...]:
        """入口模块 —— 有公开的 ``main`` / ``cli`` / ``run`` 函数。

        入口模块是**改坏了影响最大**的那种模块：所有用户都走它。所以
        它值得单独列出来，而不是混在几十个模块里。
        """
        found: list[str] = []
        for item in self.index.modules:
            if item.layer is not ModuleLayer.SOURCE:
                continue
            names = {symbol.name for symbol in item.symbols if symbol.is_public}
            if names.intersection(ENTRY_POINT_NAMES):
                found.append(item.module)
        return tuple(sorted(found))

    def referenced_symbols(self) -> Mapping[str, frozenset[str]]:
        """**测试引用了哪些模块的哪些符号**。

        判据是**引用**而不是**调用**：一个测试文件只要 import 了某个
        符号，它就在覆盖那条依赖路径上。调用点在哪不重要 —— 没有任何
        用例碰过的东西，覆盖率必然是 0，而这条事实从索引里就看得出来，
        不用跑测试。

        **粒度到符号，不到模块。** ``from api import used`` 只说明
        ``used`` 被用过 —— ``api`` 里的其它公开符号仍然可能没人碰。
        只看模块的话，"这个模块有人 import"会把整个模块的缺口都盖住。

        ``import api`` 与 ``from api import *`` 记成 ``*`` —— 整个模块
        都算被引用过。

        **类的方法跟着类走。** ``from api import Money`` 拿到的类，
        它的方法不能算"没人碰" —— 那条用例完全可能正在调
        ``Money.total``。所以类被引用时，它的方法也算被引用。
        """
        referenced: dict[str, set[str]] = {}
        for item in self.index.modules:
            if item.layer is not ModuleLayer.TEST:
                continue
            for ref in item.imports:
                if not ref.internal:
                    continue
                names = referenced.setdefault(ref.target, set())
                if not ref.member or ref.member == "*":
                    names.add("*")
                    continue
                names.add(ref.member)
                for symbol in self.index.symbols_of(ref.target):
                    if symbol.kind == KIND_CLASS and symbol.name == ref.member:
                        prefix = f"{ref.member}."
                        names.update(
                            other.name
                            for other in self.index.symbols_of(ref.target)
                            if other.kind == KIND_METHOD and other.qualname.startswith(prefix)
                        )
        return {module: frozenset(names) for module, names in referenced.items()}

    def unreferenced_public_symbols(self) -> Mapping[str, tuple[str, ...]]:
        """**没有任何自有用例引用过的公开符号**，按模块分组。

        **这是覆盖率缺口的结构性证据。** 覆盖率低可能是"用例跑了但没
        覆盖到"（那是补用例的事）；而"没有任何一条用例 import 过这个
        符号"说明的是另一件事 —— **这个对外接口压根没有测试在用它**。
        """
        referenced = self.referenced_symbols()
        result: dict[str, tuple[str, ...]] = {}
        for item in self.index.modules:
            if item.layer is not ModuleLayer.SOURCE:
                continue
            used = referenced.get(item.module, frozenset())
            names = tuple(
                symbol.qualname
                for symbol in item.symbols
                if symbol.is_public
                and symbol.kind in (KIND_FUNCTION, KIND_METHOD)
                and "*" not in used
                and symbol.name not in used
            )
            if names:
                result[item.module] = names
        return dict(sorted(result.items()))

    def untested_modules(self) -> tuple[str, ...]:
        """没有任何自有用例引用过的源模块（按字典序）。

        与 :meth:`unreferenced_public_symbols` 的差别：这条只问"模块有
        没有被引用过"，不问"它里面有没有没人碰的公开符号"。一个空的
        ``__init__.py`` 没被引用不构成问题 —— 它压根没有对外接口。
        """
        referenced = {module for module, names in self.referenced_symbols().items() if names}
        found = [
            item.module
            for item in self.index.modules
            if item.layer is ModuleLayer.SOURCE
            and item.module not in referenced
            and self.public_surface(item.module)
        ]
        return tuple(sorted(found))

    # ------------------------------------------------------------ 概览

    def summary(self) -> Mapping[str, object]:
        """一份可序列化的概览 —— 给文档、给日志、给下一轮当输入。"""
        return {
            "modules": len(self.index.modules),
            "layers": {str(layer): list(names) for layer, names in self.layers().items()},
            "edges": len(self.index.edges()),
            "cycles": [cycle.describe() for cycle in self.cycles()],
            "violations": [item.describe() for item in self.violations()],
            "entry_points": list(self.entry_points()),
            "untested_modules": list(self.untested_modules()),
        }


def build_architecture(index: RepositoryIndex) -> ArchitectureModel:
    """从索引推导架构模型。"""
    return ArchitectureModel(index=index)


def _strongly_connected_components(
    adjacency: Mapping[str, Sequence[str]],
) -> list[list[str]]:
    """求强连通分量（迭代版 Kosaraju）。

    **迭代而不是递归。** 仓库里模块一多，递归版会在几百层上撞到
    Python 的递归上限 —— 而那是个**运行时才炸**的失败，且规模越大越
    容易炸。迭代版没有这个问题。

    **两个 pass 的顺序不能反。** 第一个 pass 按结束时间排序，第二个
    pass 在**反图**上按这个顺序找连通块 —— 顺序反了就会把不相邻的
    模块归到同一个块里。
    """
    order = _finish_order(adjacency)
    reverse = _reverse_graph(adjacency)
    return _components_in_reverse(reverse, order)


def _finish_order(adjacency: Mapping[str, Sequence[str]]) -> list[str]:
    """第一个 pass —— 按 DFS 结束时间排序。

    **显式栈。** 递归版在几百层模块上会撞 Python 的递归上限，而那是
    **运行时才炸**的失败，且规模越大越容易炸。
    """
    visited: set[str] = set()
    order: list[str] = []
    for start in sorted(adjacency):
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(adjacency.get(start, ())))]
        while stack:
            node, children = stack[-1]
            pushed = False
            for child in children:
                if child not in visited:
                    visited.add(child)
                    stack.append((child, iter(adjacency.get(child, ()))))
                    pushed = True
                    break
            if not pushed:
                order.append(node)
                stack.pop()
    return order


def _reverse_graph(adjacency: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """反图 —— 边全都掉头。"""
    reverse: dict[str, list[str]] = {node: [] for node in adjacency}
    for node, children in adjacency.items():
        for child in children:
            reverse.setdefault(child, []).append(node)
    return reverse


def _components_in_reverse(
    reverse: Mapping[str, Sequence[str]],
    order: Sequence[str],
) -> list[list[str]]:
    """第二个 pass —— 在反图上按结束时间逆序找连通块。"""
    assigned: set[str] = set()
    components: list[list[str]] = []
    for node in reversed(order):
        if node in assigned:
            continue
        assigned.add(node)
        stack = [node]
        component: list[str] = []
        while stack:
            current = stack.pop()
            component.append(current)
            for child in reverse.get(current, ()):
                if child not in assigned:
                    assigned.add(child)
                    stack.append(child)
        components.append(component)
    return components


__all__ = [
    "ENTRY_POINT_NAMES",
    "ArchitectureModel",
    "DependencyCycle",
    "LayerViolation",
    "ModuleRecord",
    "ViolationKind",
    "build_architecture",
]
