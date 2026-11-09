"""架构决策的存取 —— 人读的那一份（Git + Markdown/YAML）。

设计依据：docs/tasks.md TASK-P2-016（交付物：ADR 存储）、R-067；
core/architecture/decisions.py（端口与数据类型的定义）；core/entities/
architecture.py（``ArchitectureDecision`` 实体的设计说明）。

**端口与数据类型在 ``core/``，因为它有两头。** 架构决策要存两处：Git
里的 Markdown（给人读）与 PostgreSQL 里的行（给系统读）。两个实现分处
两层，如果端口跟着其中一个走，另一个就得反过来依赖它 —— 而
``infrastructure`` 依赖 ``knowledge`` 是反的。所以这里只实现"文件"这一
半，端口从 ``core`` 取。

**YAML 前言 + Markdown 正文。** 前言放**结构化的字段**（状态、被谁取
代、时间），正文放**人读的内容**（背景、决策、代价）。这样既能在 Git
里读（``git diff`` 看得出改了哪条决策），又能被程序读（解析前言拿到
结构化字段）。

**决策不可变，只能被取代。** 新决策用 ``supersedes`` 指向旧的，旧的
**不删不改** —— 历史不能被抹掉。"已被取代"这件事是**推导**出来的
（谁被指向了谁就是被取代了），不需要回头改旧文件。

**编号只进不退。** 编号被发出去过就作废了 —— 哪怕那个文件后来被人手工
删掉，下一个编号也不会去捡它。因为**重号会毁掉取代链**：有一条决策写
着"取代 adr-0002"，而 adr-0002 后来被删了、又发了一个同号的决策，那么
"谁取代了谁"就会指向一条完全不相干的决策，而且**看不出来**。所以这里
宁可跳号（留个空洞）也不重号 —— 空洞看得见，重号看不见。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import yaml

from core.architecture.decisions import (
    DECISION_STATUSES,
    STATUS_ACCEPTED,
    STATUS_SUPERSEDED,
    ArchitectureDecisionError,
    DecisionRecord,
)

#: ADR 文件名前缀。
ADR_PREFIX: Final[str] = "adr-"

#: ADR 文件名后缀。
ADR_SUFFIX: Final[str] = ".md"

#: YAML 前言的分隔线。
FRONTMATTER_FENCE: Final[str] = "---"

#: 编号水位线文件 —— 记住"已经发出过的最大编号"。
SEQUENCE_FILE: Final[str] = ".sequence"

#: 撞号时的重试次数。
MAX_ID_ATTEMPTS: Final[int] = 16

#: 一份 ADR 文本按围栏切开之后应有的段数 —— 前言 + 正文。
FRONTMATTER_PARTS: Final[int] = 2

#: ADR 正文里三节的小标题。
SECTION_CONTEXT: Final[str] = "背景"
SECTION_DECISION: Final[str] = "决策"
SECTION_TRADEOFF: Final[str] = "代价"


class DecisionError(ArchitectureDecisionError):
    """架构决策存取出错。"""


@dataclass(frozen=True, slots=True)
class DecisionSet:
    """一组决策 —— 以及"哪条还生效"这个推导。

    **"生效"是推导出来的，不是存出来的。** 谁被别的决策指向，谁就是被
    取代了。这样"取代"这个动作**不需要回头改旧决策** —— 而回头改旧
    决策正是那种会在并发或中断下留下半截状态的操作。
    """

    decisions: tuple[DecisionRecord, ...]

    def effective(self) -> tuple[DecisionRecord, ...]:
        """当前生效的决策（没有被别的决策取代的那些）。"""
        superseded = {
            item.supersedes_id for item in self.decisions if item.supersedes_id is not None
        }
        return tuple(item for item in self.decisions if item.id not in superseded)

    def chain(self, decision_id: str) -> tuple[DecisionRecord, ...]:
        """一条决策的取代链（从它自己开始，往后串）。

        链的方向是"新 → 旧"：先是被这条取代的那条，再是被那条取代的。
        走到底就是最初的决策 —— 那一条通常回答了"当初为什么这么定"。
        """
        by_id = {item.id: item for item in self.decisions}
        chain: list[DecisionRecord] = []
        seen: set[str] = set()
        current = by_id.get(decision_id)
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            if current.supersedes_id is None:
                break
            current = by_id.get(current.supersedes_id)
        return tuple(chain)


class FileDecisionStore:
    """把 ADR 写成 Markdown + YAML 前言（Git + Markdown/YAML 那一类）。

    **同一条决策不会被覆盖。** 每次记录都写新的文件，文件名按序号递。
    历史不能被抹掉。
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """ADR 目录。"""
        return self._root

    async def record(  # noqa: PLR0913 - 参数就是决策本身的字段，打包成 dict 只会把结构藏起来
        self,
        *,
        project_id: str,
        context: str,
        decision: str,
        tradeoff: str | None = None,
        status: str = STATUS_ACCEPTED,
        supersedes_id: str | None = None,
    ) -> DecisionRecord:
        """记一条决策 —— 写一个新文件。"""
        return await asyncio.to_thread(
            self._record,
            project_id=project_id,
            context=context,
            decision=decision,
            tradeoff=tradeoff,
            status=status,
            supersedes_id=supersedes_id,
        )

    def _record(  # noqa: PLR0913 - 与异步那一半签名一致
        self,
        *,
        project_id: str,
        context: str,
        decision: str,
        tradeoff: str | None = None,
        status: str = STATUS_ACCEPTED,
        supersedes_id: str | None = None,
    ) -> DecisionRecord:
        """记一条决策 —— 同步实现。"""
        if status not in DECISION_STATUSES:
            raise DecisionError(f"决策状态不合法：{status!r}")
        self._root.mkdir(parents=True, exist_ok=True)
        for _ in range(MAX_ID_ATTEMPTS):
            record = DecisionRecord(
                id=self._next_id(),
                project_id=project_id,
                context=context,
                decision=decision,
                tradeoff=tradeoff,
                status=status,
                supersedes_id=supersedes_id,
                created_at=datetime.now(UTC),
            )
            path = self._root / f"{record.id}{ADR_SUFFIX}"
            try:
                # **独占创建。** 并发的两个进程可能算出同一个编号 ——
                # 撞上了就换下一个，而不是把对方写的内容覆盖掉。
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(render_decision(record))
            except FileExistsError:
                continue
            return record
        raise DecisionError(f"连续 {MAX_ID_ATTEMPTS} 次撞号，这条决策没写下来")

    async def recall(self, project_id: str) -> tuple[DecisionRecord, ...]:
        """把这个项目的决策全取出来（按时间排序）。"""
        return await asyncio.to_thread(self._recall, project_id)

    def _recall(self, project_id: str) -> tuple[DecisionRecord, ...]:
        """取一个项目的全部决策 —— 同步实现。"""
        found = [item for item in self._read_all() if item.project_id == project_id]
        found.sort(key=lambda item: (item.created_at, item.id))
        return tuple(found)

    async def get(self, decision_id: str) -> DecisionRecord | None:
        """按 id 取一条决策。取不到返回 ``None``。"""
        return await asyncio.to_thread(self._get, decision_id)

    def _get(self, decision_id: str) -> DecisionRecord | None:
        """按 id 取一条决策 —— 同步实现。"""
        for item in self._read_all():
            if item.id == decision_id:
                return item
        return None

    def _next_id(self) -> str:
        """下一个 ADR 编号 —— **只进不退**。

        **编号一旦发出去就作废了。** 哪怕那个文件后来被人手工删掉，下一
        个编号也不会去捡它 —— 重号会让取代链指向不相干的决策，而且
        看不出来。所以宁可跳号（留个空洞）也不重号。

        编号来自两处，取大的那个：目录里现存的编号，以及 ``.sequence``
        里记着的水位线。水位线负责"删掉的文件不算数"，目录负责"目录
        里现存的也不算数"。
        """
        number = max(self._used_numbers(), default=0) + 1
        self._write_watermark(number)
        return f"{ADR_PREFIX}{number:04d}"

    def _used_numbers(self) -> set[int]:
        """已经用掉的编号（目录里现存的 + 水位线）。"""
        used: set[int] = set()
        if self._root.is_dir():
            for path in self._root.glob(f"{ADR_PREFIX}*{ADR_SUFFIX}"):
                stem = path.stem.removeprefix(ADR_PREFIX)
                if stem.isdigit():
                    used.add(int(stem))
        watermark = self._read_watermark()
        if watermark is not None:
            used.add(watermark)
        return used

    def _read_watermark(self) -> int | None:
        """读水位线 —— 没有或者坏了就当没有。"""
        path = self._root / SEQUENCE_FILE
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

    def _write_watermark(self, number: int) -> None:
        """写下水位线 —— 记下"这个编号发出去了"。

        **写在文件之前。** 中途崩溃最多留一个空洞（那个编号没用上），
        而空洞无害；反过来先写文件后记水位线，崩溃就可能重号。
        """
        current = self._read_watermark()
        if current is not None and current >= number:
            return
        path = self._root / SEQUENCE_FILE
        path.write_text(f"{number}\n", encoding="utf-8")

    def _read_all(self) -> tuple[DecisionRecord, ...]:
        """读目录里全部 ADR。"""
        if not self._root.is_dir():
            return ()
        found: list[DecisionRecord] = []
        for path in sorted(self._root.glob(f"{ADR_PREFIX}*{ADR_SUFFIX}")):
            parsed = parse_decision(path.read_text(encoding="utf-8"))
            if parsed is not None:
                found.append(parsed)
        return tuple(found)


def render_decision(record: DecisionRecord) -> str:
    """把一条决策渲染成 Markdown + YAML 前言。"""
    frontmatter: dict[str, object] = {
        "id": record.id,
        "project_id": record.project_id,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
    }
    if record.supersedes_id is not None:
        frontmatter["supersedes"] = record.supersedes_id
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=True).strip()
    lines = [
        FRONTMATTER_FENCE,
        header,
        FRONTMATTER_FENCE,
        "",
        f"# {record.id}",
        "",
        f"## {SECTION_CONTEXT}",
        "",
        record.context.strip(),
        "",
        f"## {SECTION_DECISION}",
        "",
        record.decision.strip(),
        "",
        f"## {SECTION_TRADEOFF}",
        "",
        (record.tradeoff or "（无）").strip(),
        "",
    ]
    return "\n".join(lines)


def parse_decision(text: str) -> DecisionRecord | None:
    """从 Markdown 文本解析出一条决策。

    **解析不出来就返回 ``None``**，不抛异常 —— 一个坏掉的 ADR 文件不该
    让整个项目的事实库读不出来。但"读不出来"和"没有"要分得开，所以返回
    ``None`` 而不是空列表。
    """
    fields = _frontmatter_fields(text)
    if fields is None:
        return None
    decision_id, project_id, status, moment, supersedes = fields
    body = text.split(f"\n{FRONTMATTER_FENCE}\n", 1)[1]
    return DecisionRecord(
        id=decision_id,
        project_id=project_id,
        context=_section(body, SECTION_CONTEXT),
        decision=_section(body, SECTION_DECISION),
        tradeoff=_section(body, SECTION_TRADEOFF) or None,
        status=status,
        supersedes_id=supersedes,
        created_at=moment,
    )


def _frontmatter_fields(
    text: str,
) -> tuple[str, str, str, datetime, str | None] | None:
    """把前言解析成五个字段 —— 任何一处不合法就整体返回 ``None``。

    **"不合法"只有一种处理方式。** 前言缺字段、类型不对、时间戳解析
    不出来 —— 都归成"这不是一条决策"。分开处理只会让调用方多写几个
    分支，而结果还是同一个。
    """
    try:
        raw = _load_frontmatter(text)
        return _fields_of(raw)
    except (ValueError, TypeError, yaml.YAMLError):
        return None


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


def _fields_of(
    raw: Mapping[str, object],
) -> tuple[str, str, str, datetime, str | None]:
    """把前言映射变成五个字段。任何一处不合法就抛 ``ValueError``。"""
    decision_id = raw.get("id")
    project_id = raw.get("project_id")
    status = raw.get("status")
    created_at = raw.get("created_at")
    if (
        not isinstance(decision_id, str)
        or not isinstance(project_id, str)
        or not isinstance(status, str)
        or not isinstance(created_at, str)
    ):
        raise ValueError("前言缺字段")
    try:
        moment = datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ValueError(f"时间戳不合法：{created_at!r}") from error
    supersedes = raw.get("supersedes")
    return (
        decision_id,
        project_id,
        status,
        moment,
        supersedes if isinstance(supersedes, str) else None,
    )


def _section(body: str, title: str) -> str:
    """取 Markdown 正文里某一节的内容。"""
    lines = body.splitlines()
    try:
        start = lines.index(f"## {title}")
    except ValueError:
        return ""
    collected: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        collected.append(line)
    return "\n".join(collected).strip()


def decisions_as_set(decisions: Sequence[DecisionRecord]) -> DecisionSet:
    """把一个决策序列包成 :class:`DecisionSet`。"""
    return DecisionSet(decisions=tuple(decisions))


__all__ = [
    "ADR_PREFIX",
    "ADR_SUFFIX",
    "MAX_ID_ATTEMPTS",
    "SEQUENCE_FILE",
    "STATUS_ACCEPTED",
    "STATUS_SUPERSEDED",
    "DecisionError",
    "DecisionSet",
    "FileDecisionStore",
    "decisions_as_set",
    "parse_decision",
    "render_decision",
]
