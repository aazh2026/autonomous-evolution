"""仓库索引 —— 把"仓库长什么样"从每轮重读变成一次构建、多次查询。

设计依据：docs/tasks.md TASK-P2-016（交付物：架构知识图谱、ADR 存储、
模块依赖索引；验收条件：系统能回答"某模块被哪些模块依赖"且答案来自已构建
的索引而非每次重读仓库）、R-064（不每轮重读仓库重建认知）、R-067（不全塞
向量库，不用对话历史当事实）；docs/design.md §3.6（四类存储：项目事实归
Git + Markdown/YAML，语义检索才轮到向量库）。

**为什么是索引，而不是每轮现读。** 自主演进每一轮都要回答"这个模块被谁
依赖""这个模块对外暴露了什么"。现读一次要遍历整个仓库、解析每个文件、
重建依赖图 —— 一轮几万行代码就是几千次文件打开，而那一圈文件里九成以上
这一轮根本不会变。所以这里做的是**一次构建、多次查询**：索引是一次构建
出来的结构，查询只读它。

**为什么索引不落向量库。** "谁依赖谁"是一个**结构问题**，答案可以用图
精确回答；向量检索给的是"最像的若干条"，它会把 ``tinylib.stats`` 与
``tinylib.stringz`` 排在一起，而"相似"在这里是错的。R-067 说得很直接：
不全塞向量库，不用对话历史当事实。所以索引是**结构化的** —— 模块、符号、
导入边，每一项都能指名道姓。

**刷新不等于重建。** :func:`refresh_index` 只重新解析**变了**的文件 ——
判定"变了"用 ``(大小, 修改时间)``，命中就直接复用上一轮解析出来的记录，
**连文件都不读**。一个仓库里有三个文件被改过，这一轮就只读那三个。

**解析不出来的模块也要进索引。** 语法坏掉的文件是**事实**，不是噪声 ——
把它悄悄跳过，索引就会说"这个模块没有符号"，读索引的人会以为它是个空
模块。解析失败的文件带着 ``parse_error`` 进索引，谁都能看见它坏了。
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final

#: 索引时跳过的目录名。
SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".scratch",
        "build",
        "dist",
        ".tox",
        ".eggs",
    }
)

#: 源码根目录名。存在时按它切分模块名（``src/tinylib/cli.py`` → ``tinylib.cli``）。
SOURCE_ROOTS: Final[tuple[str, ...]] = ("src",)

#: 测试目录名。
TEST_DIRS: Final[tuple[str, ...]] = ("tests",)

#: 已知缺陷复现套件的目录名。
KNOWN_ISSUE_DIRS: Final[tuple[str, ...]] = ("known_issues",)

#: 脚本目录名。
SCRIPT_DIRS: Final[tuple[str, ...]] = ("scripts",)

#: 索引文档的格式版本。读到的版本不认识就当索引不存在 —— 宁可重建，
#: 也不要拿一份格式对不上的索引去回答问题。
INDEX_FORMAT_VERSION: Final[int] = 1

#: 符号种类。
KIND_MODULE: Final[str] = "module"
KIND_CLASS: Final[str] = "class"
KIND_FUNCTION: Final[str] = "function"
KIND_METHOD: Final[str] = "method"


class ModuleLayer(StrEnum):
    """模块所在的层。

    **分层不是装饰 —— 它决定依赖方向是否合法。** 源模块依赖测试模块，
    说明测试代码被编进了产品；源模块依赖已知缺陷套件，说明产品代码知道
    自己有哪个缺陷还没修。这两种依赖都是**架构问题**，而要发现它们，
    前提是先知道每个模块在哪一层。
    """

    SOURCE = "source"
    TEST = "test"
    KNOWN_ISSUE = "known_issue"
    SCRIPT = "script"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class FileStamp:
    """文件指纹 —— 判断"变没变"用的廉价依据。

    :attr:`size` 与 :attr:`mtime_ns` 是**快路径**：两者都与上一轮一致，
    就直接认定文件没动，连内容都不读。:attr:`sha256` 是**权威值** ——
    真要读文件时才算它，它进索引摘要，所以索引的摘要是内容决定的。
    """

    size: int
    mtime_ns: int
    sha256: str

    def unchanged_since(self, other: FileStamp) -> bool:
        """与另一份指纹相比是否"没动过"（只比快路径）。"""
        return self.size == other.size and self.mtime_ns == other.mtime_ns


@dataclass(frozen=True, slots=True)
class Symbol:
    """一个模块里的一个符号。

    :attr:`qualname` 是点分全名（``Money.total``）。只给名字是不够的 ——
    ``total`` 这个名字在试点仓库里有好几个含义不同的定义，光看名字分不清
    是哪一个。
    """

    name: str
    qualname: str
    kind: str
    lineno: int
    is_public: bool
    doc: str


@dataclass(frozen=True, slots=True)
class ImportRef:
    """一条 import。

    :attr:`base` 是**展开相对导入之后**的基模块名，:attr:`member` 是
    ``from X import Y`` 里的 ``Y``（``import a.b`` 时为空）。

    留着这两个字段是为了**重新解析**：模块集合变了（新增或删除了模块）时，
    "这条 import 指向谁"要重算一遍，而重算不需要再读文件。
    """

    base: str
    member: str
    target: str
    lineno: int
    level: int
    internal: bool
    raw: str


@dataclass(frozen=True, slots=True)
class ModuleRecord:
    """一个模块在索引里的全部记录。"""

    path: str
    module: str
    layer: ModuleLayer
    stamp: FileStamp
    symbols: tuple[Symbol, ...]
    imports: tuple[ImportRef, ...]
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """一条依赖边 —— "谁依赖谁"。

    :attr:`importer` 与 :attr:`imported` 都是**模块名**（点分），
    不是路径。问"谁依赖 ``tinylib.stats``"的人要的是模块名。
    """

    importer: str
    imported: str
    lineno: int


@dataclass(frozen=True, slots=True)
class RepositoryIndex:
    """一个仓库的索引。

    **索引是不可变的。** 刷新会造出一个**新的**索引对象，而不是就地改 ——
    上一轮的问题"谁依赖我"必须还能按上一轮的样子回答，否则"这一轮比上一轮
    多了什么依赖"这个问题就没法问了。
    """

    modules: tuple[ModuleRecord, ...] = ()
    format_version: int = INDEX_FORMAT_VERSION

    # ---------------------------------------------------------------- 查询

    def record(self, module: str) -> ModuleRecord | None:
        """按模块名取记录。取不到返回 ``None``。"""
        for item in self.modules:
            if item.module == module:
                return item
        return None

    def record_for_path(self, path: str) -> ModuleRecord | None:
        """按仓库相对路径取记录。取不到返回 ``None``。"""
        for item in self.modules:
            if item.path == path:
                return item
        return None

    def module_names(self) -> tuple[str, ...]:
        """索引里所有模块名，按字典序。"""
        return tuple(sorted(item.module for item in self.modules))

    def dependents(self, module: str) -> tuple[str, ...]:
        """**谁依赖这个模块**（直接依赖者，按字典序）。

        这是 TASK-P2-016 的验收问题。答案只来自索引 —— 不读仓库、不跑
        任何分析。
        """
        return tuple(
            sorted(
                {
                    edge.importer
                    for edge in self.edges()
                    if edge.imported == module and edge.importer != module
                }
            )
        )

    def dependencies(self, module: str) -> tuple[str, ...]:
        """**这个模块依赖谁**（内部依赖，按字典序）。"""
        return tuple(
            sorted(
                {
                    edge.imported
                    for edge in self.edges()
                    if edge.importer == module and edge.imported != module
                }
            )
        )

    def transitive_dependents(self, module: str) -> tuple[str, ...]:
        """**谁（直接或间接）依赖这个模块**。

        改一个模块之前要看的不是直接依赖者，而是所有会跟着受影响的人 ——
        直接依赖者只是第一层。
        """
        seen: set[str] = set()
        frontier = [module]
        while frontier:
            current = frontier.pop()
            for dependent in self.dependents(current):
                if dependent not in seen:
                    seen.add(dependent)
                    frontier.append(dependent)
        return tuple(sorted(seen))

    def edges(self) -> tuple[DependencyEdge, ...]:
        """全部**内部**依赖边（按导入方、导入项、行号排序）。"""
        found: list[DependencyEdge] = []
        for item in self.modules:
            for ref in item.imports:
                if ref.internal and ref.target != item.module:
                    found.append(
                        DependencyEdge(
                            importer=item.module,
                            imported=ref.target,
                            lineno=ref.lineno,
                        )
                    )
        found.sort(key=lambda edge: (edge.importer, edge.imported, edge.lineno))
        return tuple(found)

    def find_symbol(self, name: str) -> tuple[tuple[str, Symbol], ...]:
        """按名字找符号 —— 返回 ``(模块名, 符号)`` 的序列。

        同名符号可能有多个（不同模块里各有一个 ``total``），所以返回的是
        序列而不是单个 —— 返回单个会逼调用方自己挑，而挑的那份未必对。
        """
        found: list[tuple[str, Symbol]] = []
        for item in self.modules:
            for symbol in item.symbols:
                if symbol.name == name:
                    found.append((item.module, symbol))
        found.sort(key=lambda pair: (pair[0], pair[1].qualname))
        return tuple(found)

    def symbols_of(self, module: str) -> tuple[Symbol, ...]:
        """某个模块里的全部符号。模块不存在返回空元组。"""
        item = self.record(module)
        return item.symbols if item else ()

    # ---------------------------------------------------------------- 摘要

    @property
    def digest(self) -> str:
        """索引摘要 —— 内容变了就变。

        摘要由**每个模块的路径、层、符号、导入与文件哈希**算出来。文件
        哈希进摘要，是因为"索引一样但源码不一样"这种事必须能被摘要看出来 ——
        否则两份索引看起来一模一样，用起来却不是同一件事。
        """
        digest = hashlib.sha256()
        for item in self.modules:
            digest.update(item.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(item.module.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(item.layer).encode("utf-8"))
            digest.update(b"\0")
            digest.update(item.stamp.sha256.encode("utf-8"))
            digest.update(b"\0")
            for symbol in item.symbols:
                digest.update(
                    f"{symbol.qualname}:{symbol.kind}:{symbol.lineno}:{symbol.is_public}".encode()
                )
                digest.update(b"\0")
            for ref in item.imports:
                digest.update(f"{ref.target}:{ref.lineno}:{ref.internal}".encode())
                digest.update(b"\0")
        return digest.hexdigest()

    # ------------------------------------------------------------ 序列化

    def to_document(self) -> Mapping[str, object]:
        """转成可 JSON 序列化的文档。"""
        return {
            "format_version": self.format_version,
            "digest": self.digest,
            "modules": [
                {
                    "path": item.path,
                    "module": item.module,
                    "layer": str(item.layer),
                    "stamp": {
                        "size": item.stamp.size,
                        "mtime_ns": item.stamp.mtime_ns,
                        "sha256": item.stamp.sha256,
                    },
                    "parse_error": item.parse_error,
                    "symbols": [
                        {
                            "name": symbol.name,
                            "qualname": symbol.qualname,
                            "kind": symbol.kind,
                            "lineno": symbol.lineno,
                            "is_public": symbol.is_public,
                            "doc": symbol.doc,
                        }
                        for symbol in item.symbols
                    ],
                    "imports": [
                        {
                            "base": ref.base,
                            "member": ref.member,
                            "target": ref.target,
                            "lineno": ref.lineno,
                            "level": ref.level,
                            "internal": ref.internal,
                            "raw": ref.raw,
                        }
                        for ref in item.imports
                    ],
                }
                for item in self.modules
            ],
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> RepositoryIndex:
        """从文档还原索引。

        **格式版本对不上就报错**，不猜。猜错的后果是索引看起来能用、
        答出来的却是另一份仓库的事 —— 那比没有索引更坏。
        """
        version = document.get("format_version")
        if version != INDEX_FORMAT_VERSION:
            raise IndexFormatError(f"索引格式版本是 {version!r}，只认 {INDEX_FORMAT_VERSION}")
        raw_modules = document.get("modules")
        if not isinstance(raw_modules, list):
            raise IndexFormatError("索引文档里没有 modules 列表")
        modules = [_record_from_document(raw) for raw in raw_modules]
        return cls(modules=tuple(modules), format_version=INDEX_FORMAT_VERSION)


class IndexFormatError(ValueError):
    """索引文档的格式不对。"""


class IndexBuildError(RuntimeError):
    """索引构建失败。"""


def _record_from_document(raw: object) -> ModuleRecord:
    """从文档里还原一条模块记录。"""
    if not isinstance(raw, dict):
        raise IndexFormatError(f"模块记录不是对象：{type(raw).__name__}")
    path = raw.get("path")
    module = raw.get("module")
    layer = raw.get("layer")
    stamp = raw.get("stamp")
    if not isinstance(path, str) or not isinstance(module, str) or not isinstance(layer, str):
        raise IndexFormatError(f"模块记录缺字段：{raw!r}")
    if not isinstance(stamp, dict):
        raise IndexFormatError(f"模块 {module} 的指纹不是对象")
    size = stamp.get("size")
    mtime_ns = stamp.get("mtime_ns")
    sha256 = stamp.get("sha256")
    if not isinstance(size, int) or not isinstance(mtime_ns, int) or not isinstance(sha256, str):
        raise IndexFormatError(f"模块 {module} 的指纹字段不完整")
    parse_error = raw.get("parse_error")
    return ModuleRecord(
        path=path,
        module=module,
        layer=ModuleLayer(layer),
        stamp=FileStamp(size=size, mtime_ns=mtime_ns, sha256=sha256),
        symbols=_symbols_from_document(raw.get("symbols"), module),
        imports=_imports_from_document(raw.get("imports"), module),
        parse_error=parse_error if isinstance(parse_error, str) else None,
    )


def _symbols_from_document(raw: object, module: str) -> tuple[Symbol, ...]:
    """从文档里还原符号列表。"""
    if not isinstance(raw, list):
        raise IndexFormatError(f"模块 {module} 的符号列表不是数组")
    symbols: list[Symbol] = []
    for item in raw:
        if not isinstance(item, dict):
            raise IndexFormatError(f"模块 {module} 的符号不是对象")
        name = item.get("name")
        qualname = item.get("qualname")
        kind = item.get("kind")
        lineno = item.get("lineno")
        is_public = item.get("is_public")
        doc = item.get("doc")
        if (
            not isinstance(name, str)
            or not isinstance(qualname, str)
            or not isinstance(kind, str)
            or not isinstance(lineno, int)
            or not isinstance(is_public, bool)
            or not isinstance(doc, str)
        ):
            raise IndexFormatError(f"模块 {module} 的符号字段不完整：{item!r}")
        symbols.append(
            Symbol(
                name=name,
                qualname=qualname,
                kind=kind,
                lineno=lineno,
                is_public=is_public,
                doc=doc,
            )
        )
    return tuple(symbols)


def _imports_from_document(raw: object, module: str) -> tuple[ImportRef, ...]:
    """从文档里还原 import 列表。"""
    if not isinstance(raw, list):
        raise IndexFormatError(f"模块 {module} 的 import 列表不是数组")
    refs: list[ImportRef] = []
    for item in raw:
        if not isinstance(item, dict):
            raise IndexFormatError(f"模块 {module} 的 import 不是对象")
        base = item.get("base")
        member = item.get("member")
        target = item.get("target")
        lineno = item.get("lineno")
        level = item.get("level")
        internal = item.get("internal")
        raw_text = item.get("raw")
        if (
            not isinstance(base, str)
            or not isinstance(member, str)
            or not isinstance(target, str)
            or not isinstance(lineno, int)
            or not isinstance(level, int)
            or not isinstance(internal, bool)
            or not isinstance(raw_text, str)
        ):
            raise IndexFormatError(f"模块 {module} 的 import 字段不完整：{item!r}")
        refs.append(
            ImportRef(
                base=base,
                member=member,
                target=target,
                lineno=lineno,
                level=level,
                internal=internal,
                raw=raw_text,
            )
        )
    return tuple(refs)


# ---------------------------------------------------------------- 构建


@dataclass(frozen=True, slots=True)
class _SourceFile:
    """索引过程中用到的文件信息（还没读过内容）。"""

    path: Path
    relative: str
    module: str
    is_package: bool
    layer: ModuleLayer


def _layer_of(parts: tuple[str, ...]) -> ModuleLayer:
    """按目录判断模块在哪一层。"""
    if parts and parts[0] in KNOWN_ISSUE_DIRS:
        return ModuleLayer.KNOWN_ISSUE
    if parts and parts[0] in TEST_DIRS:
        return ModuleLayer.TEST
    if parts and parts[0] in SOURCE_ROOTS:
        return ModuleLayer.SOURCE
    if parts and parts[0] in SCRIPT_DIRS:
        return ModuleLayer.SCRIPT
    return ModuleLayer.OTHER


def _module_parts(relative: Path) -> tuple[tuple[str, ...], bool]:
    """算出模块的点分名与"是不是包"。

    ``src/tinylib/__init__.py`` → ``("tinylib",)``，是包；
    ``src/tinylib/cli.py`` → ``("tinylib", "cli")``，不是包；
    ``tests/test_config.py`` → ``("tests", "test_config")``。
    """
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__.py":
        parts = parts[:-1]
        is_package = True
    else:
        if parts and parts[-1].endswith(".py"):
            parts[-1] = parts[-1][: -len(".py")]
        is_package = False
    if parts and parts[0] in SOURCE_ROOTS:
        parts = parts[1:]
    return tuple(parts), is_package


def _source_files(root: Path) -> tuple[_SourceFile, ...]:
    """列出要索引的 Python 文件（**不读内容**）。"""
    if not root.is_dir():
        raise IndexBuildError(f"仓库根目录不存在：{root}")
    found: list[_SourceFile] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        parts, is_package = _module_parts(relative)
        if not parts:
            continue
        layer = _layer_of(relative.parts)
        found.append(
            _SourceFile(
                path=path,
                relative=relative.as_posix(),
                module=".".join(parts),
                is_package=is_package,
                layer=layer,
            )
        )
    return tuple(found)


def _read_bytes(path: Path) -> bytes:
    """读文件内容。单独一个函数 —— 测试要靠它证明"没变的文件没被读"。"""
    return path.read_bytes()


def _stamp_of(path: Path, content: bytes) -> FileStamp:
    """算文件指纹。"""
    stat_result = path.stat()
    return FileStamp(
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _doc_of(node: ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef) -> str:
    """取文档字符串的第一行。"""
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def _collect_symbols(tree: ast.Module) -> tuple[Symbol, ...]:
    """收集模块里的符号（函数、类、类里的方法）。"""
    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(
                Symbol(
                    name=node.name,
                    qualname=node.name,
                    kind=KIND_CLASS,
                    lineno=node.lineno,
                    is_public=not node.name.startswith("_"),
                    doc=_doc_of(node),
                )
            )
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    symbols.append(
                        Symbol(
                            name=child.name,
                            qualname=f"{node.name}.{child.name}",
                            kind=KIND_METHOD,
                            lineno=child.lineno,
                            is_public=not child.name.startswith("_"),
                            doc=_doc_of(child),
                        )
                    )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbols.append(
                Symbol(
                    name=node.name,
                    qualname=node.name,
                    kind=KIND_FUNCTION,
                    lineno=node.lineno,
                    is_public=not node.name.startswith("_"),
                    doc=_doc_of(node),
                )
            )
    symbols.sort(key=lambda symbol: (symbol.qualname, symbol.lineno))
    return tuple(symbols)


def _relative_base(module: str, is_package: bool, level: int) -> str | None:
    """把相对导入展开成基模块名。

    ``level`` 是 import 语句里的前导点数。``from . import x`` 在
    ``pkg/sub/mod.py`` 里指的是 ``pkg.sub.x``；``from .. import x`` 指的是
    ``pkg.x``。超出顶层（``from ... import x`` 在 ``pkg/mod.py`` 里）返回
    ``None`` —— 那不是合法导入，当成外部依赖处理。
    """
    package = module if is_package else module.rpartition(".")[0]
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None
    trimmed = parts[: len(parts) - (level - 1)] if level > 1 else parts
    return ".".join(trimmed)


def _import_refs(
    tree: ast.Module,
    module: str,
    is_package: bool,
    known: frozenset[str],
) -> tuple[ImportRef, ...]:
    """抽出模块里的全部 import，并解析它们指向哪个模块。"""
    refs: list[ImportRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                refs.append(
                    _make_ref(
                        base=alias.name,
                        member="",
                        lineno=node.lineno,
                        level=0,
                        raw=f"import {alias.name}",
                        known=known,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _relative_base(module, is_package, node.level)
                if base is None:
                    continue
            else:
                base = node.module or ""
            for alias in node.names:
                refs.append(
                    _make_ref(
                        base=base,
                        member=alias.name,
                        lineno=node.lineno,
                        level=node.level,
                        raw=f"from {base} import {alias.name}",
                        known=known,
                    )
                )
    refs.sort(key=lambda ref: (ref.lineno, ref.target))
    return tuple(refs)


def _make_ref(  # noqa: PLR0913 - 参数就是一条 import 记录本身的字段
    *,
    base: str,
    member: str,
    lineno: int,
    level: int,
    raw: str,
    known: frozenset[str],
) -> ImportRef:
    """造一条 import 记录，并解析它的目标。

    **解析规则**：`from a.b import c` 指的是 ``a.b.c``（如果 ``a.b.c`` 是
    仓库里的模块 —— 那就是导入了一个子模块）否则是 ``a.b``。`import a.b`
    指的是 ``a.b`` 本身。这条规则能把 ``from tinylib import cli`` 正确
    解析成 ``tinylib.cli`` —— 而那正是"谁依赖 cli"的答案所在。
    """
    candidate = f"{base}.{member}" if member else base
    if candidate in known:
        target = candidate
    elif base in known:
        target = base
    else:
        target = candidate or base
    return ImportRef(
        base=base,
        member=member,
        target=target,
        lineno=lineno,
        level=level,
        internal=target in known,
        raw=raw,
    )


def _parse_module(source: _SourceFile, known: frozenset[str]) -> ModuleRecord:
    """解析一个模块。"""
    content = _read_bytes(source.path)
    stamp = _stamp_of(source.path, content)
    try:
        tree = ast.parse(content, filename=source.relative)
    except SyntaxError as error:
        return ModuleRecord(
            path=source.relative,
            module=source.module,
            layer=source.layer,
            stamp=stamp,
            symbols=(),
            imports=(),
            parse_error=f"{type(error).__name__}: {error.msg}",
        )
    return ModuleRecord(
        path=source.relative,
        module=source.module,
        layer=source.layer,
        stamp=stamp,
        symbols=_collect_symbols(tree),
        imports=_import_refs(tree, source.module, source.is_package, known),
    )


def build_index(root: Path) -> RepositoryIndex:
    """构建仓库索引。

    **这一次调用是"贵"的** —— 它遍历仓库、读每个 Python 文件、解析 AST。
    之后每次查询都只是读内存里的结构。想省这一次，请用
    :func:`refresh_index` 复用上一轮的索引。
    """
    sources = _source_files(root)
    known = frozenset(item.module for item in sources)
    records = [_parse_module(source, known) for source in sources]
    records.sort(key=lambda record: record.path)
    return RepositoryIndex(modules=tuple(records))


def refresh_index(root: Path, previous: RepositoryIndex) -> RepositoryIndex:
    """在上一轮索引的基础上刷新。

    **没变的文件不读、不解析。** 判定依据是 ``(大小, 修改时间)`` —— 命中
    就直接复用上一轮解析出来的记录。这是 R-064"不每轮重读仓库重建认知"的
    具体落法：仓库里改了几个文件，这一轮就只碰那几个。

    模块集合变了（新增或删了模块）时，import 的指向要重算一遍 —— 但重算
    不需要读文件，:class:`ImportRef` 里留着基名与成员名就是为了这个。
    """
    sources = _source_files(root)
    known = frozenset(item.module for item in sources)
    previous_modules = frozenset(item.module for item in previous.modules)
    targets_changed = known != previous_modules

    records: list[ModuleRecord] = []
    for source in sources:
        old = previous.record_for_path(source.relative)
        if old is not None and old.stamp.unchanged_since(_stat_stamp(source.path)):
            records.append(old if not targets_changed else _reresolve(old, known))
            continue
        records.append(_parse_module(source, known))
    records.sort(key=lambda record: record.path)
    return RepositoryIndex(modules=tuple(records))


def _stat_stamp(path: Path) -> FileStamp:
    """只 stat 不读内容的指纹（哈希此时是空的）。"""
    stat_result = path.stat()
    return FileStamp(
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        sha256="",
    )


def _reresolve(record: ModuleRecord, known: frozenset[str]) -> ModuleRecord:
    """模块集合变了之后重算 import 的指向（不碰文件）。"""
    return replace(
        record,
        imports=tuple(
            _make_ref(
                base=ref.base,
                member=ref.member,
                lineno=ref.lineno,
                level=ref.level,
                raw=ref.raw,
                known=known,
            )
            for ref in record.imports
        ),
    )


def iter_modules(index: RepositoryIndex) -> Iterator[ModuleRecord]:
    """按路径顺序遍历索引里的模块。"""
    yield from index.modules


__all__ = [
    "INDEX_FORMAT_VERSION",
    "KIND_CLASS",
    "KIND_FUNCTION",
    "KIND_METHOD",
    "KIND_MODULE",
    "DependencyEdge",
    "FileStamp",
    "ImportRef",
    "IndexBuildError",
    "IndexFormatError",
    "ModuleLayer",
    "ModuleRecord",
    "RepositoryIndex",
    "Symbol",
    "build_index",
    "iter_modules",
    "refresh_index",
]
