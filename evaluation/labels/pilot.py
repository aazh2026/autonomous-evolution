"""试点仓库的人工标注 —— 误报率的分母。

设计依据：docs/tasks.md TASK-P1-083（"对 20 个历史问题/缺陷做'是否为有效
发现'的人工标注，作为误报率的分母"）；docs/design.md §10 决策质量层。

**标注是人工给的，系统不能自己给自己打分。** 这份文件里每一条都是人看过
之后写下的结论 —— 谁标的、什么时候标的、属于哪一类，都记在里面。

**为什么条数被钉死。** 误报率是一个比例，比例的分母越小越不稳 —— 3 条
样本里的"1 条误报"是 33%，10 条里的"1 条误报"是 10%，而这两件事的可信度
完全不同。所以 ``EXPECTED_LABEL_COUNT`` 是个常量：条数不对，分母就不成立。

**"读不到"与"读到但是空的"是两件事。** 前者是**基线未建立**（报
``baseline_missing``），后者是"确实一条误报都没有"。所以文件不在时抛
:class:`LabelSetMissingError`，与"文件在但内容坏了"（:class:`LabelSetError`）
分开 —— 前者要补标注，后者要修文件。

**为什么解析得这么严。** 一个读坏了的标注集如果被当成"没有标注"，误报率
就会报"基线未建立"；如果被当成"空的标注集"，误报率就会报 0 —— 而 0 是
最容易被当成好消息的那个数。所以多一个键、少一个键、``is_valid_finding``
写成 ``1`` 而不是 ``true``、时间戳不带时区，一律报错。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from evaluation.common import Verdict

#: 试点仓库的标注集 —— **任务书指定的交付物路径**。
PILOT_LABELS_PATH: Final[Path] = Path(__file__).resolve().parent / "pilot_labels.jsonl"

#: 标注的条数 —— **不多不少**。
#:
#: 少一条，分母就不成立；多一条，说明有人事后往里塞了不该塞的东西。
EXPECTED_LABEL_COUNT: Final[int] = 20

#: 一条标注必须有的字段 —— **一个都不能少，也一个都不许多**。
_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "annotated_at",
    "annotated_by",
    "category",
    "is_valid_finding",
    "issue_ref",
)


class LabelSetError(ValueError):
    """标注集的内容有问题 —— 读不懂、字段不对、条数不对。"""


class LabelSetMissingError(LabelSetError):
    """标注集不在 —— **基线未建立**，不是"标注集是空的"。"""


@dataclass(frozen=True, slots=True)
class PilotLabel:
    """一条人工标注 —— 某个历史问题"是不是有效发现"的结论。

    **五个字段缺一不可。** ``issue_ref`` 是它标的那个问题，``is_valid_finding``
    是结论，``category`` 说明这是哪一类问题，``annotated_by`` 与
    ``annotated_at`` 说明**谁在什么时候这么标的** —— 没有后两个，这条标注
    就没法被复核，也没法在标注人换人时重新审视。
    """

    issue_ref: str
    is_valid_finding: bool
    category: str
    annotated_by: str
    annotated_at: datetime

    @property
    def verdict(self) -> Verdict:
        """这条标注对应的结论 —— 决策质量层认的就是它。"""
        return Verdict.VALID if self.is_valid_finding else Verdict.INVALID

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object], *, where: str) -> PilotLabel:
        """从一条 JSON 记录构造 —— **读不懂就报错**。

        :param raw: 一条记录。
        :param where: 出错时指明是哪儿 —— 标注集是逐行读的，只说"格式不对"
            的话，标注人得自己一行行找。
        """
        missing = [field for field in _REQUIRED_FIELDS if field not in raw]
        if missing:
            raise LabelSetError(f"{where} 缺少字段：{'、'.join(missing)}")
        extra = [key for key in raw if key not in _REQUIRED_FIELDS]
        if extra:
            raise LabelSetError(f"{where} 多了字段：{'、'.join(sorted(extra))}")

        finding = raw["is_valid_finding"]
        if not isinstance(finding, bool):
            raise LabelSetError(f"{where} 的 is_valid_finding 必须是布尔值：{finding!r}")

        return cls(
            issue_ref=_text(raw, "issue_ref", where),
            is_valid_finding=finding,
            category=_text(raw, "category", where),
            annotated_by=_text(raw, "annotated_by", where),
            annotated_at=_timestamp(raw, where),
        )

    def to_mapping(self) -> dict[str, object]:
        """写回一条 JSON 记录 —— **键排好序**，两次写出来能逐字节比较。"""
        return {
            "annotated_at": self.annotated_at.isoformat(),
            "annotated_by": self.annotated_by,
            "category": self.category,
            "is_valid_finding": self.is_valid_finding,
            "issue_ref": self.issue_ref,
        }


@dataclass(frozen=True, slots=True)
class LabelSet:
    """一份标注集。"""

    labels: tuple[PilotLabel, ...]

    def __len__(self) -> int:
        return len(self.labels)

    @property
    def refs(self) -> tuple[str, ...]:
        """所有被标注的问题编号 —— 顺序与文件里一致。"""
        return tuple(label.issue_ref for label in self.labels)

    @property
    def valid_count(self) -> int:
        """有效发现的条数。"""
        return sum(1 for label in self.labels if label.is_valid_finding)

    @property
    def invalid_count(self) -> int:
        """无效发现（误报）的条数。"""
        return len(self.labels) - self.valid_count

    def verdict_of(self, issue_ref: str) -> Verdict | None:
        """某个问题被标成了什么 —— **没标过就是 ``None``**。

        ``None`` 与 :attr:`Verdict.INVALID` 必须分开：前者是"这条没被审过"，
        后者是"这条被审过，结论是无效"。把它俩混成一个，误报率的分母就会
        把没审过的也算进去。
        """
        for label in self.labels:
            if label.issue_ref == issue_ref:
                return label.verdict
        return None

    def label_of(self, issue_ref: str) -> PilotLabel | None:
        """某个问题的完整标注 —— 没标过就是 ``None``。"""
        for label in self.labels:
            if label.issue_ref == issue_ref:
                return label
        return None

    @classmethod
    def from_jsonl(cls, text: str) -> LabelSet:
        """从 JSONL 文本读出标注集 —— **一行一条**。

        空行跳过；重复的 ``issue_ref`` 报错 —— 同一个问题标两次，第二次
        盖掉第一次的话，标注就变成"最后写的人赢"。
        """
        labels: list[PilotLabel] = []
        seen: set[str] = set()
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            where = f"标注集第 {number} 行"
            raw = _json_object(stripped, where)
            label = PilotLabel.from_mapping(raw, where=where)
            if label.issue_ref in seen:
                raise LabelSetError(f"{where} 的 issue_ref 重复：{label.issue_ref}")
            seen.add(label.issue_ref)
            labels.append(label)
        return cls(labels=tuple(labels))

    def to_jsonl(self) -> str:
        """写成 JSONL —— **键排好序**，两次写出来能逐字节比较。"""
        return "".join(
            f"{json.dumps(label.to_mapping(), ensure_ascii=False, sort_keys=True)}\n"
            for label in self.labels
        )


@runtime_checkable
class LabelSetStore(Protocol):
    """标注集的来源。

    **取不到就抛，不返回空集。** 返回空集的话，"标注集不见了"和"标注集是
    空的"在调用方眼里长得一模一样 —— 而后者会让误报率报 0。
    """

    def load(self) -> LabelSet:
        """读出标注集。"""


class JsonlLabelSetStore:
    """从 JSONL 文件读标注集。

    :param path: 标注集文件。缺省是试点仓库那一份。
    :param expected_count: 条数下限要求 —— 不为 ``None`` 时，条数不对就报错。
    """

    __slots__ = ("_expected_count", "_path")

    def __init__(self, path: Path | None = None, *, expected_count: int | None = None) -> None:
        self._path = PILOT_LABELS_PATH if path is None else path
        self._expected_count = expected_count

    @property
    def path(self) -> Path:
        """标注集文件在哪 —— 出错时要能指出来。"""
        return self._path

    def load(self) -> LabelSet:
        """读标注集。

        文件不在 → :class:`LabelSetMissingError`（**基线未建立**）。
        文件在但内容坏了 → :class:`LabelSetError`（**要修文件**）。
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise LabelSetMissingError(f"标注集不在：{self._path}") from exc
        except UnicodeDecodeError as exc:
            raise LabelSetError(f"标注集不是 UTF-8 文本：{self._path}") from exc

        label_set = LabelSet.from_jsonl(text)
        if self._expected_count is not None and len(label_set) != self._expected_count:
            raise LabelSetError(
                f"标注集条数必须是 {self._expected_count} 条，"
                f"实际 {len(label_set)} 条：{self._path}"
            )
        return label_set


def pilot_label_store() -> LabelSetStore:
    """试点仓库的标注集来源 —— **条数必须是 20**。"""
    return JsonlLabelSetStore(PILOT_LABELS_PATH, expected_count=EXPECTED_LABEL_COUNT)


def _text(raw: Mapping[str, object], field: str, where: str) -> str:
    """取一个非空字符串字段 —— 空串与空白都不算数。"""
    value = raw[field]
    if not isinstance(value, str) or not value.strip():
        raise LabelSetError(f"{where} 的 {field} 必须是非空字符串：{value!r}")
    return value.strip()


def _timestamp(raw: Mapping[str, object], where: str) -> datetime:
    """取一个带时区的时间戳 —— **不带时区的时间戳没有意义**。"""
    text = _text(raw, "annotated_at", where)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LabelSetError(f"{where} 的 annotated_at 不是 ISO 时间戳：{text!r}") from exc
    if moment.tzinfo is None:
        raise LabelSetError(f"{where} 的 annotated_at 必须带时区：{text!r}")
    return moment.astimezone(UTC)


def _json_object(text: str, where: str) -> dict[str, object]:
    """把一行 JSON 读成一个对象 —— **不是对象就报错**。"""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LabelSetError(f"{where} 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(raw, dict):
        raise LabelSetError(f"{where} 不是一个 JSON 对象")
    return {str(key): value for key, value in raw.items()}


__all__ = [
    "EXPECTED_LABEL_COUNT",
    "PILOT_LABELS_PATH",
    "JsonlLabelSetStore",
    "LabelSet",
    "LabelSetError",
    "LabelSetMissingError",
    "LabelSetStore",
    "PilotLabel",
    "pilot_label_store",
]
