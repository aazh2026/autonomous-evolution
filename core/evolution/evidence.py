"""证据引用 —— "凭什么这么说"。

设计依据：docs/design.md §3.3（Candidate 的 evidence 必须能被独立核验，
"证据必须能被独立核验，否则'有证据'只是说法"）、§5（``project_assessment``
的 evidence 写法）、docs/tasks.md TASK-P1-051（R-082 每阶段结构化输出且下游
可独立验证；R-083 project_assessment 结构）。

**为什么词表住在 ``core/evolution/``。** 认知阶段的 ``goal_gap[].evidence``
与探索阶段的 ``candidate.evidence`` 说的是同一件事 —— "凭什么这么说"。两处
各写一套解析规则，早晚会对不上；对不上时，"这算不算有证据"就变成了一道
两边口径不同的判断题。所以词表只有一份，两边都往下能看见它。这与
:mod:`core.evidence.artifacts` 是同一个道理：验收契约说"要哪些"、归档器说
"存了哪些"、数据库列里存的也是它们 —— 抄本迟早对不上。

**能当证据的只有三种形态。** 一句"这里很慢"不是证据：它没法被独立核验。
能被核验的引用长这样：

- ``artifact://<id>`` —— 对象存储里那一份不可丢失的产物；
- ``issue:<id>`` —— 问题单（裸 id ``ISSUE-102`` 也认 —— 形态已经由
  ``kind`` 声明过了，前缀是重复的）；
- 文件路径 —— 仓库里的某个文件。

**形态由 ``kind`` 声明，不由字符串猜。** 猜的话，"带扩展名的是路径"这种规则
总会在某个边界上判错 —— 更要紧的是猜出来的规则**会误收**：一句中文描述
``"文件耦合度过高"`` 里没有点号，恰好被某条规则挡住，可规则稍一放宽它就
混进来了，而且混进来之后没人看得出来。声明式没有这个口子：调用方写
``kind="file"`` 就是**断言**那是一个文件路径，解析器只需要检查这个断言
成不成立 —— 断言错了会报错，断言对了就是对的。

**"能解析"是硬要求，不是建议。** 引用写错（scheme 拼错、路径写成一句话、
id 为空）必须在**构造模型时**就报错，而不是等到下游去取的时候才发现取不到
—— 那时候这条结论已经流到决策层了。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 产物引用的 scheme。指向对象存储里那一份不可丢失的证据（TASK-P1-048）。
ARTIFACT_SCHEME: Final[str] = "artifact://"

#: 问题单引用的 scheme。
ISSUE_SCHEME: Final[str] = "issue:"

#: 标识符里不允许出现空白 —— ``artifact://a b`` 不是一个 id，是两个词。
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^\S+$")

#: 控制字符的边界。低于它的字符不可见，不该出现在路径里。
_CONTROL_CHARACTER_LIMIT: Final[int] = 32


class EvidenceKind(StrEnum):
    """证据的形态。

    四个取值覆盖三种引用形态 —— ``artifact`` 与 ``test_report`` 指的是同一种
    引用（对象存储里的产物），但**说明的不是一回事**："这是一份产物"与
    "这是一份测试报告"对下游的判断完全不同：前者要自己去打开看，后者说明
    验收已经跑过一轮。分开记，下游才知道能不能直接引用它的结论。
    """

    ARTIFACT = "artifact"
    TEST_REPORT = "test_report"
    ISSUE = "issue"
    FILE = "file"


def _looks_like_path(ref: str) -> bool:
    """这个字符串像不像一个文件路径。

    **判据是"能不能指向一个文件"，不是"里面有没有点"。** 至少满足一条：

    1. 带路径分隔符（``/`` 或 ``\\``）—— ``docs/架构`` 也算路径；
    2. 是个点开头的文件名 —— ``.gitignore``、``.env``；
    3. 带扩展名 —— ``README.md``。

    裸词（``Makefile``）不算：它既不能被独立核验，也和一句自然语言描述
    无法区分 —— 而"无法区分"正是要挡住的。带空格的裸词更不算。

    **这是语法判断，不是 IO。** 文件在不在，由取证据的那一端去查；这里的
    模型只保证"这串东西**形状上**是个可解析的引用"。
    """
    if not ref.strip():
        return False
    if "://" in ref:
        return False
    if any(ord(char) < _CONTROL_CHARACTER_LIMIT for char in ref):
        return False
    # 带分隔符 —— 目录里的东西一定是路径。
    if "/" in ref or "\\" in ref:
        return True
    # 裸文件名：不含空白、以点开头、或者带扩展名。
    if any(char.isspace() for char in ref):
        return False
    return ref.startswith(".") or "." in ref


class Evidence(BaseModel):
    """一条证据引用。

    ``kind`` 声明形态，``ref`` 指向具体的东西。两者必须对得上 ——
    ``kind="issue"`` 配 ``ref="artifact://x"`` 是在说"这个问题有产物作证"，
    这话本身没错，但写错了地方：它会让"取证据"的那一端按 issue 去取，
    取不到。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvidenceKind
    ref: str = Field(min_length=1)
    #: 这一条证据说明了什么。自由文本，但**不能替代 ref** ——
    #: 光有 note 没有 ref 的证据不成立，所以 note 永远是可选的。
    note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_single_key_mapping(cls, data: object) -> object:
        """接受 design.md §5 的写法：``{"test_report": "artifact://report-001"}``。

        那是文档里的原样形状 —— 键是形态，值是引用。照着写的人不该为了
        迁就模型去改文档，所以模型认这份写法。

        只认**单键**且键名是合法形态的映射：``{"kind": ..., "ref": ...}``
        有两个键，不动；``{"note": "..."}`` 的键不是形态，也不动（它该在
        缺字段时报错，而不是被悄悄改写）。
        """
        if not isinstance(data, Mapping):
            return data
        if len(data) != 1:
            return data
        ((key, value),) = data.items()
        if not isinstance(value, str):
            return data
        try:
            kind = EvidenceKind(key)
        except ValueError:
            return data
        return {"kind": kind, "ref": value}

    @model_validator(mode="after")
    def _check_ref_matches_kind(self) -> Self:
        """引用必须与声明的形态对得上，且真的能解析。"""
        match self.kind:
            case EvidenceKind.ARTIFACT | EvidenceKind.TEST_REPORT:
                _require_scheme(self.ref, ARTIFACT_SCHEME, self.kind.value)
            case EvidenceKind.ISSUE:
                _require_issue_id(self.ref)
            case EvidenceKind.FILE:
                if not _looks_like_path(self.ref):
                    raise ValueError(
                        f"证据引用 {self.ref!r} 不是可解析的文件路径"
                        "（要带路径分隔符、点开头的文件名或扩展名）"
                    )
        return self

    @property
    def identifier(self) -> str:
        """引用指向的那个东西的标识。

        产物与问题单给出裸 id（``report-001``），文件给出路径本身。
        """
        if self.kind in {EvidenceKind.ARTIFACT, EvidenceKind.TEST_REPORT}:
            return self.ref[len(ARTIFACT_SCHEME) :]
        if self.kind is EvidenceKind.ISSUE:
            if self.ref.startswith(ISSUE_SCHEME):
                return self.ref[len(ISSUE_SCHEME) :]
            return self.ref
        return self.ref


def _require_scheme(ref: str, scheme: str, kind: str) -> None:
    """要求引用以指定 scheme 开头，且后面跟一个非空的 id。"""
    if not ref.startswith(scheme):
        raise ValueError(f"{kind} 类型的证据引用必须以 {scheme!r} 开头，收到 {ref!r}")
    identifier = ref[len(scheme) :]
    if not identifier or not _IDENTIFIER.match(identifier):
        raise ValueError(f"证据引用 {ref!r} 的标识符为空或含空白")


def _require_issue_id(ref: str) -> None:
    """要求引用是一个问题单标识。

    **两种写法都认**：``issue:ISSUE-102`` 与裸的 ``ISSUE-102``。design.md §5
    用的是后者 —— 形态已经由 ``kind`` 声明过了，再把 ``issue:`` 前缀写一遍
    是重复。这里只把前缀**剥掉**，不要求它存在。

    但含 ``://`` 的一律不认：那是别的 scheme 的引用，形态与声明对不上。
    """
    identifier = ref[len(ISSUE_SCHEME) :] if ref.startswith(ISSUE_SCHEME) else ref
    if not identifier:
        raise ValueError(f"证据引用 {ref!r} 的标识符为空")
    if "://" in identifier:
        raise ValueError(f"issue 类型的证据引用不能指向其它 scheme：{ref!r}")
    if not _IDENTIFIER.match(identifier):
        raise ValueError(f"证据引用 {ref!r} 的标识符为空或含空白")


__all__ = [
    "ARTIFACT_SCHEME",
    "ISSUE_SCHEME",
    "Evidence",
    "EvidenceKind",
]
