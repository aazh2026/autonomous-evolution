"""仓库索引 —— 一次构建、多次查询的"仓库长什么样"。

设计依据：docs/tasks.md TASK-P2-016（模块依赖索引）、R-064。

对外只暴露三个入口：:func:`build_index`（建一份）、
:func:`refresh_index`（在上一轮基础上刷新）与 :class:`RepositoryIndexStore`
（存取）。查询方法都挂在 :class:`RepositoryIndex` 上 —— "谁依赖谁"这类
问题不需要调用方自己遍历模块表。
"""

from knowledge.repository_index.index import (
    KIND_CLASS,
    KIND_FUNCTION,
    KIND_METHOD,
    KIND_MODULE,
    DependencyEdge,
    FileStamp,
    ImportRef,
    IndexBuildError,
    IndexFormatError,
    ModuleLayer,
    ModuleRecord,
    RepositoryIndex,
    Symbol,
    build_index,
    refresh_index,
)
from knowledge.repository_index.store import RepositoryIndexStore

__all__ = [
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
    "RepositoryIndexStore",
    "Symbol",
    "build_index",
    "refresh_index",
]
