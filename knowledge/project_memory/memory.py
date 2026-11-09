"""项目记忆 —— 跨轮次留下来的事实。

设计依据：docs/tasks.md TASK-P2-016（项目知识库）、R-064（不每轮重读
仓库重建认知）、R-067（不全塞向量库，不用对话历史当事实）；
docs/design.md §3.6（项目事实归 Git + Markdown/YAML）。

**这一层解决的是"上一轮做过什么，这一轮别忘了"。** 试点仓库里有一条
名为 ``PILOT-001`` 的已知缺陷；一轮演进把它修好之后，下一轮的发现器
如果还把它当成"新发现"提出来，那就是系统在**重复劳动** —— 而更糟的
是它会再修一次，再验证一次，再花一次钱。记住"这条已经修好了"是
**自主演进能连续跑的前提**。

**为什么是文件而不是向量库。** "PILOT-001 已修复"是一条**结构化的事
实**：它有唯一的键、明确的取值、明确的记下时间。向量检索给的是"最像
的若干条"，而"最像"在这里完全用不上 —— 我们要的是精确命中。R-067 说
得直接：不全塞向量库，不用对话历史当事实。所以记忆是**Markdown + YAML
前言**：人能在 Git 里读，程序能解析。

**记住的事只能追加，不能改写。** 同一条事实再记一次，是**新的修订**
（写到 ``<key>.r2.md``），不是覆盖旧的。理由与别处的 append-only 一致：
"当时记的是什么"是历史的一部分，而覆盖等于让历史只剩最后一次。
当前生效的那一版由**修订号最大的那个文件**决定 —— 这条规则是推导出来
的，不是存出来的，所以不需要回头改旧文件。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

import yaml

#: YAML 前言的分隔线。
FRONTMATTER_FENCE: Final[str] = "---"

#: 一份事实文本按围栏切开之后应有的段数 —— 前言 + 正文。
FRONTMATTER_PARTS: Final[int] = 2

#: 事实的种类。
KIND_ISSUE_FIXED: Final[str] = "issue_fixed"
KIND_DECISION: Final[str] = "decision"
KIND_CONSTRAINT: Final[str] = "constraint"
KIND_OBSERVATION: Final[str] = "observation"

#: 文件名里允许出现的字符。
_SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")


class FactKind(StrEnum):
    """事实的种类。"""

    #: 一条缺陷已经修好。
    ISSUE_FIXED = KIND_ISSUE_FIXED
    #: 做过一个决定。
    DECISION = KIND_DECISION
    #: 一条约束（非目标、边界条件）。
    CONSTRAINT = KIND_CONSTRAINT
    #: 一条观察（测量结果、架构发现）。
    OBSERVATION = KIND_OBSERVATION


class FactStoreError(RuntimeError):
    """事实存取出错。"""


@dataclass(frozen=True, slots=True)
class Fact:
    """一条项目事实。

    :attr:`key` 是这条事实的**标识** —— 同一类事实里，键相同就指同一件
    事（``PILOT-001`` 就是那条缺陷）。键是"跨轮对照"时唯一能用的东西：
    摘要会改，键不会。
    """

    kind: FactKind
    key: str
    summary: str
    detail: str = ""
    recorded_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    cycle_ref: str = ""
    revision: int = 1
    tags: tuple[str, ...] = ()

    def to_document(self) -> Mapping[str, object]:
        """转成可 JSON/YAML 序列化的文档。"""
        return {
            "kind": str(self.kind),
            "key": self.key,
            "summary": self.summary,
            "recorded_at": self.recorded_at.isoformat(),
            "cycle_ref": self.cycle_ref,
            "revision": self.revision,
            "tags": list(self.tags),
        }


def render_fact(fact: Fact) -> str:
    """把一条事实渲染成 Markdown + YAML 前言。

    **摘要只写在前言里，正文只放详情。** 摘要已经在前言里，再抄一遍到
    正文就会出现"两处摘要对不上"的可能 —— 而解析时只能取其中一处。
    所以分工是死的：前言是**结构化的字段**（键、种类、摘要、时间），
    正文是**人读的详情**。
    """
    frontmatter = yaml.safe_dump(
        dict(fact.to_document()), allow_unicode=True, sort_keys=True
    ).strip()
    body = fact.detail.strip()
    lines = [
        FRONTMATTER_FENCE,
        frontmatter,
        FRONTMATTER_FENCE,
        "",
        f"# {fact.key}",
        "",
    ]
    if body:
        lines.extend([body, ""])
    return "\n".join(lines)


def parse_fact(text: str) -> Fact | None:
    """从 Markdown 文本解析出一条事实。解析不出来返回 ``None``。

    **"解析不出来"只有一种处理方式。** 前言缺字段、种类不认识、时间戳
    解析不出来 —— 都归成"这不是一条事实"。分开处理只会让调用方多写几个
    分支，而结果还是同一个。
    """
    try:
        fact = _fact_from_frontmatter(_load_frontmatter(text))
    except (ValueError, TypeError, yaml.YAMLError):
        return None
    body = text.split(f"\n{FRONTMATTER_FENCE}\n", 1)[1]
    return replace(fact, detail=_body_of(body))


def _load_frontmatter(text: str) -> Mapping[str, object]:
    """取出前言并解析成映射。不合法就抛 ``ValueError``。"""
    if not text.startswith(f"{FRONTMATTER_FENCE}\n"):
        raise ValueError("没有前言")
    parts = text.split(f"\n{FRONTMATTER_FENCE}\n", 1)
    if len(parts) != FRONTMATTER_PARTS:
        raise ValueError("前言没有闭合")
    raw = yaml.safe_load(parts[0][len(FRONTMATTER_FENCE) + 1 :])
    if not isinstance(raw, Mapping):
        raise ValueError("前言不是映射")
    return raw


def _fact_from_frontmatter(raw: Mapping[str, object]) -> Fact:
    """把前言映射变成一条事实（详情留空）。不合法就抛 ``ValueError``。"""
    kind = raw.get("kind")
    key = raw.get("key")
    summary = raw.get("summary")
    if not isinstance(kind, str) or not isinstance(key, str) or not isinstance(summary, str):
        raise ValueError("前言缺字段")
    try:
        kind_value = FactKind(kind)
    except ValueError as error:
        raise ValueError(f"未知的种类：{kind!r}") from error
    revision = raw.get("revision")
    cycle_ref = raw.get("cycle_ref")
    tags = raw.get("tags")
    return Fact(
        kind=kind_value,
        key=key,
        summary=summary,
        detail="",
        recorded_at=_moment_of(raw.get("recorded_at")),
        cycle_ref=cycle_ref if isinstance(cycle_ref, str) else "",
        revision=revision if isinstance(revision, int) else 1,
        tags=tuple(tag for tag in tags if isinstance(tag, str)) if isinstance(tags, list) else (),
    )


def _moment_of(raw: object) -> datetime:
    """解析时间戳 —— 认不出来就用"纪元之前"那个占位值。

    **时间戳坏了不影响这条事实本身。** 摘要、键、种类才是事实的内容；
    时间戳只是"什么时候记下的"，坏了就退回到最小值，而不是把整条
    事实判成坏的。
    """
    fallback = datetime.min.replace(tzinfo=UTC)
    if not isinstance(raw, str):
        return fallback
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return fallback


def _body_of(body: str) -> str:
    """取正文（去掉标题行）。

    **先把空行去掉再找标题。** 前言的围栏后面天然跟着一个空行，
    不先去掉的话标题就不是第一行，标题行会跟着混进详情里。
    """
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _safe_name(key: str) -> str:
    """把键变成安全的文件名。

    **键本身不变** —— 原键存在前言里，文件名只是索引。改文件名不改键，
    跨轮对照才不会因为一个斜杠就断掉。
    """
    cleaned = _SAFE_NAME.sub("_", key).strip("_")
    return cleaned or "fact"


class ProjectMemory:
    """项目记忆 —— 一份"跨轮事实"的存取。

    **记忆只增不减。** 记一条已经存在的事实（同键）不会覆盖旧的，而是
    写成新的修订；"当前生效的那一版"是修订号最大的那个。
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """记忆的根目录。"""
        return self._root

    def record(  # noqa: PLR0913 - 参数就是事实本身的字段，打包成 dict 只会把结构藏起来
        self,
        *,
        kind: FactKind,
        key: str,
        summary: str,
        detail: str = "",
        cycle_ref: str = "",
        tags: Sequence[str] = (),
        recorded_at: datetime | None = None,
    ) -> Fact:
        """记一条事实。

        **同键再记一次 = 新修订。** 旧的那一版留在原地（``<key>.md``），
        新的写到 ``<key>.r2.md``、``<key>.r3.md``……
        """
        if not key.strip():
            raise FactStoreError("事实的键不能为空")
        moment = recorded_at or datetime.now(UTC)
        revision = self._next_revision(kind, key)
        fact = Fact(
            kind=kind,
            key=key,
            summary=summary,
            detail=detail,
            recorded_at=moment,
            cycle_ref=cycle_ref,
            revision=revision,
            tags=tuple(tags),
        )
        directory = self._root / str(kind)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self._file_name(key, revision)
        path.write_text(render_fact(fact), encoding="utf-8")
        return fact

    def recall(self, kind: FactKind, key: str) -> Fact | None:
        """取某个键**当前生效**的那一版。没有返回 ``None``。"""
        versions = self.history(kind, key)
        return versions[-1] if versions else None

    def history(self, kind: FactKind, key: str) -> tuple[Fact, ...]:
        """某个键的全部修订（按修订号排序）。"""
        directory = self._root / str(kind)
        if not directory.is_dir():
            return ()
        found: list[Fact] = []
        for path in sorted(directory.glob(f"{_safe_name(key)}*.md")):
            parsed = parse_fact(path.read_text(encoding="utf-8"))
            if parsed is not None and parsed.key == key:
                found.append(parsed)
        found.sort(key=lambda item: item.revision)
        return tuple(found)

    def facts(self, kind: FactKind | None = None) -> tuple[Fact, ...]:
        """全部事实（每键只取当前生效的那一版），按 kind、key 排序。"""
        kinds = (kind,) if kind is not None else tuple(FactKind)
        found: list[Fact] = []
        for item in kinds:
            directory = self._root / str(item)
            if not directory.is_dir():
                continue
            seen: dict[str, Fact] = {}
            for path in sorted(directory.glob("*.md")):
                parsed = parse_fact(path.read_text(encoding="utf-8"))
                if parsed is None:
                    continue
                current = seen.get(parsed.key)
                if current is None or parsed.revision >= current.revision:
                    seen[parsed.key] = parsed
            found.extend(seen.values())
        found.sort(key=lambda item: (str(item.kind), item.key))
        return tuple(found)

    def keys(self, kind: FactKind) -> tuple[str, ...]:
        """某一类事实的全部键（按字典序）。"""
        return tuple(sorted({item.key for item in self.facts(kind)}))

    def resolved_issues(self) -> tuple[str, ...]:
        """已经修好的缺陷编号（按字典序）。

        **这是跨轮对照要用的那份清单。** 发现器拿到一份候选之后，先拿
        它的缺陷编号来这里对一下 —— 在这儿的就是修好的，不该再提。
        """
        return self.keys(FactKind.ISSUE_FIXED)

    def is_resolved(self, key: str) -> bool:
        """这条缺陷是不是已经修好了。"""
        return self.recall(FactKind.ISSUE_FIXED, key) is not None

    def _next_revision(self, kind: FactKind, key: str) -> int:
        """下一个修订号。"""
        versions = self.history(kind, key)
        if not versions:
            return 1
        return max(item.revision for item in versions) + 1

    def _file_name(self, key: str, revision: int) -> str:
        """这一版事实的文件名。"""
        stem = _safe_name(key)
        if revision <= 1:
            return f"{stem}.md"
        return f"{stem}.r{revision}.md"


__all__ = [
    "KIND_CONSTRAINT",
    "KIND_DECISION",
    "KIND_ISSUE_FIXED",
    "KIND_OBSERVATION",
    "Fact",
    "FactKind",
    "FactStoreError",
    "ProjectMemory",
    "parse_fact",
    "render_fact",
]
