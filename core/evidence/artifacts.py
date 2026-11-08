"""验收证据的词汇与端口 —— 归档这件事的"名词表"。

设计依据：docs/design.md §3.6（执行产物库保存不可丢失的证据）；docs/tasks.md
TASK-P1-048（验证结束后把三类产物上传对象存储并登记 ``Artifact``）、
TASK-P1-004（内容寻址 + 哈希校验）、TASK-P1-024（append-only、证据不可丢失）；
R-065 / R-071 / R-099。

**这个模块里没有一行 IO。** 它只定义三样东西：

1. **产物有哪几种**（:class:`ArtifactKind`）—— ``git_diff`` / ``test_report`` /
   ``execution_trace``。
2. **产物怎么寻址**（:func:`key_for` / :func:`digest_from_key`）—— 内容寻址
   的词汇：key 形如 ``sha256:<hex>``，**是内容的函数**。
3. **两个端口**（:class:`ArtifactStorage` / :class:`ArtifactRegistry`）——
   存东西的地方与登记东西的地方。

**为什么词表住在 ``core/``。** 三方都要说同一套话：验收契约说"要哪些"，
归档器说"存了哪些"，数据库 ``artifact.type`` 列存的也是它们。词表放在使用
方之一那儿（``harness/verification/contract.py`` 或
``knowledge/artifacts/archiver.py``），另外两方就得各自抄一份 —— 抄本迟早
对不上，而对不上的那一刻没人说得清哪一份是对的。所以它既不在契约那边
（那里只是**用**它），也不在归档器那边（那里同样只是**用**它），而在两边
都往下能看见的地方。

**为什么端口也住在 ``core/``。** 依赖方向必须是 ``infrastructure → core``：
实现方（对象存储后端、数据库仓储）在外面，归档器在里面。端口写在归档器
那儿，实现方就得反向依赖归档器 —— 那条依赖一旦长出来，"换个对象存储"就
变成了"改 core"。已有的两处先例同源：``core/policy/budget.py`` →
``infrastructure/database/budget.py``、``core/policy/approval.py`` →
``infrastructure/database/approval.py``。

**内容寻址这条性质被这里的三件事一起撑着**：key 由内容算出；同一份内容永远
落在同一个 key 上（幂等）；读的时候再算一次摘要，对不上当场报错。少了第三
条，"证据还在"与"证据没被换过"就是两回事 —— 而归档器要保证的恰恰是后者。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from core.evidence.artifact_hash import HASH_LENGTH

#: 内容寻址 key 的前缀。完整 key 形如 ``sha256:<hex>``。
DIGEST_PREFIX: Final[str] = "sha256:"


class ArtifactKind(StrEnum):
    """验收证据的种类。

    **这三种就是验收契约里能点名的那三种**（``AcceptanceContract.artifacts``），
    也正是 ``artifact.type`` 列里会出现的值：

    - ``git_diff`` —— 改了什么；
    - ``test_report`` —— 验得怎么样；
    - ``execution_trace`` —— 怎么验的（哪几项检查、退出码几）。

    选这三样是因为它们合起来能回答"这次验收到底证明了什么"：只看 diff 不知道
    验没验，只看报告不知道改了哪几行，只看轨迹不知道结论。
    """

    GIT_DIFF = "git_diff"
    TEST_REPORT = "test_report"
    EXECUTION_TRACE = "execution_trace"


def key_for(digest: str) -> str:
    """由内容摘要得到内容寻址 key。

    **key 是内容的函数** —— 这条性质是内容寻址的全部意义：同一份内容永远
    落在同一个 key 上（重复归档不会多出第二份），内容变了 key 就变了（旧记录
    指着的仍是旧内容）。
    """
    return f"{DIGEST_PREFIX}{digest}"


def digest_from_key(key: str) -> str:
    """从内容寻址 key 反解出内容摘要。

    只认合法的内容寻址 key —— 别的 key 反解出来的"摘要"是编的，拿它去核对
    内容会得到一个看似合理的假结论。
    """
    if not key.startswith(DIGEST_PREFIX):
        raise ValueError(f"不是内容寻址 key：{key!r}")
    digest = key[len(DIGEST_PREFIX) :]
    if len(digest) != HASH_LENGTH:
        raise ValueError(f"内容寻址 key 的摘要长度不对：{key!r}")
    return digest


@dataclass(frozen=True)
class StoredObject:
    """一次写入的结果。

    ``digest`` 是内容 sha256（十六进制，不带前缀），
    ``key`` 是内容寻址的完整 key（``sha256:<digest>``），
    ``uri`` 是这个对象在具体后端里的地址。

    ``skipped`` 表示"key 已存在，本次没有写入"。
    """

    key: str
    uri: str
    digest: str
    size_bytes: int
    #: 这次调用是否跳过了写入（同 key 的对象已存在）。
    skipped: bool = False


@runtime_checkable
class ArtifactStorage(Protocol):
    """内容寻址的产物存储。

    **只有存取与校验三件事。** 覆盖写、删除、列举桶都不在契约里 —— 证据是
    append-only 的（R-099 / I-05），提供一个能删对象的接口，早晚有人会去删。

    **``put_bytes`` 之后要自己调 :meth:`verify`。** 内容寻址的写是幂等的：
    key 已存在时**不重写**（包括内容已被损坏的对象 —— 篡改是**要保留的信号**，
    悄悄用一份"看起来对"的内容覆盖上去等于把篡改的痕迹抹掉）。所以 put 完
    不能假设"key 处的内容 == 刚写进去的内容"，要确认就得调 ``verify``。
    """

    async def put_bytes(self, data: bytes) -> StoredObject:
        """写入字节内容，返回写入结果（含 key / uri / 摘要）。"""
        ...

    async def get_bytes(self, key: str) -> bytes:
        """读出内容并**校验摘要**。对不上抛 :class:`ArtifactHashMismatchError`。"""
        ...

    async def verify(self, key: str) -> bool:
        """key 处的对象是否与它的摘要相符。对不上返回 ``False``。"""
        ...


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """一条产物登记。

    **这是"指针"，不是内容。** 内容在对象存储里，这里存它的地址（``uri``）
    与内容摘要（``hash``）。摘要是必填的：没有它，"证据还在"与"证据没被换
    过"就是两回事（R-024：产物以 hash 寻址）。

    ``type`` 用 :class:`ArtifactKind` 而不是裸字符串 —— 数据库那一列是
    ``String(64)``，但值域是**定死**的：合同、归档器、登记处三方得说同一套
    话。
    """

    artifact_id: str
    task_id: str
    verification_run_id: str
    uri: str
    hash: str
    type: ArtifactKind
    size_bytes: int | None = None
    project_id: str | None = None


#: ``verification_run.result`` 列的合法取值。
#:
#: 与数据库的 CHECK 约束一致。**``skipped`` 不在
#: :class:`~core.task.outcome.VerificationVerdict` 里** —— 那是"这次验收
#: 没跑"的记法，与"跑出了结论"是两回事；两者共用一列，但取值域是这四个。
VERIFICATION_RESULTS: Final[frozenset[str]] = frozenset({"passed", "failed", "error", "skipped"})


@dataclass(frozen=True, slots=True)
class VerificationRunRecord:
    """一条验收记录。

    **``result`` 记的是"这次验收完成没有"**，取值与 ``verification_run.result``
    列的约束一致（``passed`` / ``failed`` / ``error`` / ``skipped``）。

    ``checks`` 存这次验收实际跑了哪些检查、以及每项的结果 —— 不是只存一个
    "passed"。只存结论的验收无法被复核，也无法定位"哪一项回归了"。
    """

    run_id: str
    task_id: str
    checks: tuple[Mapping[str, object], ...] = ()
    result: str = "passed"
    verified_by: str = ""
    duration_ms: int | None = None
    cycle_id: str | None = None

    def __post_init__(self) -> None:
        if self.result not in VERIFICATION_RESULTS:
            raise ValueError(
                f"未知的验收结果 {self.result!r}，合法取值："
                f"{'、'.join(sorted(VERIFICATION_RESULTS))}"
            )
        if not self.checks:
            raise ValueError("验收记录必须带检查结果 —— 没跑检查的验收不算验收")


@runtime_checkable
class ArtifactRegistry(Protocol):
    """产物登记处 —— 存"哪份证据在哪"的指针。

    **写只有一个入口，而且是一整批。** :meth:`record_archive` 把一次验收与
    它的产物**一起**写：要么都进去，要么都没进去。分开写会留下一个中间态 ——
    "验收记录写着通过，产物只有两样" —— 而那个中间态在事后看与"证据丢了"
    完全一样。

    **登记是幂等的。** 同一份内容（同一个 ``verification_run_id`` + 摘要 +
    类型）重复登记不会多出一行 —— 重跑归档是常事（上一轮挂了要重来），而
    重复登记会把"有几份证据"这件事搅浑。
    """

    async def record_archive(
        self,
        run: VerificationRunRecord,
        artifacts: Sequence[ArtifactRecord],
    ) -> tuple[ArtifactRecord, ...]:
        """把一次验收与它的产物一起记下来。返回实际登记的产物。"""
        ...

    async def get(self, artifact_id: str) -> ArtifactRecord | None:
        """按 id 取一条登记。没有返回 ``None``。"""
        ...

    async def get_by_uri(self, uri: str) -> ArtifactRecord | None:
        """按地址取一条登记 —— "按 URI 取回证据"的第一步。"""
        ...

    async def list_for(self, verification_run_id: str) -> tuple[ArtifactRecord, ...]:
        """某次验收登记的全部产物。按登记顺序返回。"""
        ...


__all__ = [
    "DIGEST_PREFIX",
    "VERIFICATION_RESULTS",
    "ArtifactKind",
    "ArtifactRecord",
    "ArtifactRegistry",
    "ArtifactStorage",
    "StoredObject",
    "VerificationRunRecord",
    "digest_from_key",
    "key_for",
]
