"""索引的落盘 —— 一份 JSON，随时可取。

设计依据：docs/tasks.md TASK-P2-016、R-064（不每轮重读仓库重建认知）。

**为什么索引要落盘，而不只是活在内存里。** 内存里的索引活不过一次进程。
自主演进是**长跑**的 —— 每一轮开始时，系统需要回答"这个模块被谁依赖"，
而回答这个问题不该以"把整个仓库再读一遍"为前提。落盘之后，新一轮的
第一件事是**把上一轮的索引读回来**，只在确实变了的地方补一次。

**为什么落 JSON 而不是落数据库。** 索引是**派生物** —— 它可以从仓库
重建出来，而且是**确定性地**重建（同样的源码 → 同样的索引）。数据库是
**事实**的存处，而索引不是事实，是事实的投影。派生数据落数据库的坏处
很具体：它会和源码不同步，而不同步的派生数据比没有更危险 —— 因为人
会信它。落成一份 JSON 文件，"它过期了"这件事一眼可见（对着源码一比
就知道），而且它可以跟着仓库一起进 Git。

**写的时候不覆盖别人的东西。** 索引文件写的是**整个文档**，不是增量 ——
所以它天生是"最后一次写入者赢"的那种文件。这没关系：索引没有并发写者
（一个仓库一个索引），而真要有并发，那是另一个问题（该上数据库）。
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge.repository_index.index import (
    IndexFormatError,
    RepositoryIndex,
    build_index,
    refresh_index,
)

#: 索引文件的格式版本。写进文件里，读的时候先对版本 —— 版本对不上的
#: 索引当成"没有"，重建一份，而不是硬读。
STORE_FORMAT_VERSION: int = 1


class RepositoryIndexStore:
    """仓库索引的落盘位置。

    **它只管存取，不管构建。** 构建是 :func:`build_index` 与
    :func:`refresh_index` 的事 —— 存文件的模块不该顺手把仓库读一遍，
    那样"读索引"和"重建索引"就分不开了，而分不开的代价是：调用方
    以为自己在读现成的索引，其实在偷偷重建。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        """索引文件的位置。"""
        return self._path

    def exists(self) -> bool:
        """索引文件在不在。"""
        return self._path.is_file()

    def load(self) -> RepositoryIndex | None:
        """读回索引。**文件不在返回 ``None``** —— "没有索引"与"有索引但
        是空的"是两回事，调用方要能区分：前者该去建一份，后者说明这个
        仓库真的一行代码都没有。
        """
        if not self._path.is_file():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise IndexFormatError(f"索引文件读不出来：{self._path}（{error}）") from error
        if not isinstance(raw, dict):
            raise IndexFormatError(f"索引文件不是对象：{self._path}")
        document = raw.get("index")
        if not isinstance(document, dict):
            raise IndexFormatError(f"索引文件里没有 index 段：{self._path}")
        return RepositoryIndex.from_document(document)

    def save(self, index: RepositoryIndex) -> None:
        """把索引写下去。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "store_version": STORE_FORMAT_VERSION,
            "index": dict(index.to_document()),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        self._path.write_text(text + "\n", encoding="utf-8")

    def load_or_refresh(self, root: Path) -> RepositoryIndex:
        """读回索引；没有就**建一份**，有就**刷新一次**。

        **有索引不等于索引是新的。** 仓库里的文件可能在上次建索引之后
        又被改过 —— 所以这里走的是刷新而不是直接用。刷新的代价与改动
        的大小成正比，而不是与仓库的大小成正比。
        """
        previous = self.load()
        index = build_index(root) if previous is None else refresh_index(root, previous)
        self.save(index)
        return index


__all__ = ["STORE_FORMAT_VERSION", "RepositoryIndexStore"]
