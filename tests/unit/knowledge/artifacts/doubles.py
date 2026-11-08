"""归档器的测试替身。

**替身必须 faithful —— 否则测试会骗人。** 这里的假存储照抄了真存储的
三条关键性质：

1. **key 是内容的函数**（``sha256:<hex>``）；
2. **重复写不重写**（幂等）—— 同 key 已存在时**不覆盖**，因为"被篡改的
   对象"是要保留的信号；
3. **verify 重新算摘要**，对不上返回 ``False``。

第 2 条尤其重要：如果替身每次写都覆盖，归档器里"写完必须自己核对一遍"
那条逻辑就永远走不到真问题（真问题只在"对象已存在且已被损坏"时出现）。

另外，替身**记事件**（``events`` 列表）—— 归档器的次序、幂等这类性质
靠断言"调用了几次、什么顺序"才能钉住，光断言最终状态会漏掉一大类错误
（"写了两遍"、"登记了两次"、"写完直接登记没核对"）。
"""

from __future__ import annotations

from collections.abc import Sequence

from core.evidence.artifact_hash import ArtifactHashMismatchError, compute_hash
from core.evidence.artifacts import (
    ArtifactKind,
    ArtifactRecord,
    StoredObject,
    VerificationRunRecord,
    digest_from_key,
    key_for,
)
from knowledge.artifacts.archiver import EvidenceSource


class FakeArtifactStorage:
    """内存里的内容寻址存储。

    实现 :class:`~core.evidence.artifacts.ArtifactStorage` 端口。
    """

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        #: 事件流水。``("put", key)`` / ``("verify", key)`` / ``("get", key)``。
        self.events: list[tuple[str, str]] = []

    # ---------- 读与写 ----------

    async def put_bytes(self, data: bytes) -> StoredObject:
        digest = compute_hash(data)
        key = key_for(digest)
        self.events.append(("put", key))
        skipped = key in self._objects
        if not skipped:
            self._objects[key] = data
        return StoredObject(
            key=key,
            uri=f"memory://{key}",
            digest=digest,
            size_bytes=len(data),
            skipped=skipped,
        )

    async def get_bytes(self, key: str) -> bytes:
        self.events.append(("get", key))
        try:
            data = self._objects[key]
        except KeyError:
            raise KeyError(f"没有这个对象：{key}") from None
        expected = digest_from_key(key)
        actual = compute_hash(data)
        if actual != expected:
            raise ArtifactHashMismatchError(expected, actual, uri=f"memory://{key}")
        return data

    async def verify(self, key: str) -> bool:
        self.events.append(("verify", key))
        try:
            data = self._objects[key]
        except KeyError:
            return False
        return compute_hash(data) == digest_from_key(key)

    # ---------- 给测试用的把手 ----------

    def object_count(self) -> int:
        return len(self._objects)

    def tamper(self, key: str, data: bytes) -> None:
        """绕过内容寻址层直接改内容 —— 模拟"对象被覆盖"。"""
        self._objects[key] = data


class FailingStorage:
    """每一次写入都炸的存储。"""

    def __init__(self, message: str = "对象存储不可用") -> None:
        self._message = message
        self.events: list[tuple[str, str]] = []

    async def put_bytes(self, data: bytes) -> StoredObject:
        self.events.append(("put", ""))
        raise RuntimeError(self._message)

    async def get_bytes(self, key: str) -> bytes:
        raise RuntimeError(self._message)

    async def verify(self, key: str) -> bool:
        raise RuntimeError(self._message)


class FakeArtifactRegistry:
    """内存里的产物登记处。

    实现 :class:`~core.evidence.artifacts.ArtifactRegistry` 端口。登记是
    **幂等**的：同一份内容（同一个 ``verification_run_id`` + 摘要 + 类型）
    重复登记不会多出一行 —— 真实现（Postgres）也是这么做的，替身不这么做
    的话，两边会得出不同的结论。
    """

    def __init__(self) -> None:
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._runs: dict[str, VerificationRunRecord] = {}
        #: 事件流水。``("record", run_id)`` / ``("get", uri)``。
        self.events: list[tuple[str, str]] = []

    async def record_archive(
        self,
        run: VerificationRunRecord,
        artifacts: Sequence[ArtifactRecord],
    ) -> tuple[ArtifactRecord, ...]:
        self.events.append(("record", run.run_id))
        self._runs.setdefault(run.run_id, run)
        registered: list[ArtifactRecord] = []
        for record in artifacts:
            existing = self._find_by_content(record)
            if existing is not None:
                registered.append(existing)
                continue
            self._artifacts[record.artifact_id] = record
            registered.append(record)
        return tuple(registered)

    async def get(self, artifact_id: str) -> ArtifactRecord | None:
        return self._artifacts.get(artifact_id)

    async def get_by_uri(self, uri: str) -> ArtifactRecord | None:
        self.events.append(("get", uri))
        for record in self._artifacts.values():
            if record.uri == uri:
                return record
        return None

    async def list_for(self, verification_run_id: str) -> tuple[ArtifactRecord, ...]:
        return tuple(
            record
            for record in self._artifacts.values()
            if record.verification_run_id == verification_run_id
        )

    # ---------- 给测试用的把手 ----------

    def run(self, run_id: str) -> VerificationRunRecord | None:
        return self._runs.get(run_id)

    def artifact_count(self) -> int:
        return len(self._artifacts)

    def _find_by_content(self, record: ArtifactRecord) -> ArtifactRecord | None:
        for existing in self._artifacts.values():
            if (
                existing.verification_run_id == record.verification_run_id
                and existing.hash == record.hash
                and existing.type == record.type
            ):
                return existing
        return None


class UnreadableStorage:
    """写得进去、读不出来的存储。

    与 :class:`FailingStorage` 相反：写入正常，**读**炸掉 —— 归档看起来
    成功，取回时才发现东西拿不到。
    """

    def __init__(self, inner: FakeArtifactStorage) -> None:
        self._inner = inner

    async def put_bytes(self, data: bytes) -> StoredObject:
        return await self._inner.put_bytes(data)

    async def get_bytes(self, key: str) -> bytes:
        raise RuntimeError("存储读不出来")

    async def verify(self, key: str) -> bool:
        return await self._inner.verify(key)


class LyingStorage:
    """读出来的东西与登记的不符，而且**不报错**的存储。

    真存储在读的时候会自己核对摘要，对不上就抛错 —— 那样的话归档器自己
    那次核对就永远走不到真问题：这个替身专门演"存储撒谎"的情况：它把
    **别的内容**交出来，还一脸没事。

    留着它的理由是 :meth:`EvidenceArchiver.retrieve` 里那道核对 —— 归档器
    不能把"存储说没事"当成"没事"。只有会撒谎的存储才逼出那道核对。
    """

    def __init__(self, inner: FakeArtifactStorage, *, content: bytes) -> None:
        self._inner = inner
        self._content = content

    async def put_bytes(self, data: bytes) -> StoredObject:
        return await self._inner.put_bytes(data)

    async def get_bytes(self, key: str) -> bytes:
        return self._content

    async def verify(self, key: str) -> bool:
        return await self._inner.verify(key)


class FailingRegistry:
    """登记时炸掉的登记处。"""

    def __init__(self, message: str = "数据库不可用") -> None:
        self._message = message
        self.events: list[tuple[str, str]] = []

    async def record_archive(
        self,
        run: VerificationRunRecord,
        artifacts: Sequence[ArtifactRecord],
    ) -> tuple[ArtifactRecord, ...]:
        self.events.append(("record", run.run_id))
        raise RuntimeError(self._message)

    async def get(self, artifact_id: str) -> ArtifactRecord | None:
        return None

    async def get_by_uri(self, uri: str) -> ArtifactRecord | None:
        return None

    async def list_for(self, verification_run_id: str) -> tuple[ArtifactRecord, ...]:
        return ()


class StubCollector:
    """按种类给内容的取证据器。

    ``contents`` 里没写的种类取不到（返回 ``None``）；``errors`` 里的种类
    取证据时抛错 —— 这两件事在归档器那里是**不同的结论**（"没产出"是
    ``FAILED``，"过程出错"是 ``ERROR``）。
    """

    def __init__(
        self,
        contents: dict[ArtifactKind, bytes] | None = None,
        errors: dict[ArtifactKind, str] | None = None,
    ) -> None:
        self._contents = dict(contents or {})
        self._errors = dict(errors or {})
        self.events: list[tuple[str, ArtifactKind]] = []

    async def collect(self, kind: ArtifactKind, *, source: EvidenceSource) -> bytes | None:
        self.events.append(("collect", kind))
        if kind in self._errors:
            raise RuntimeError(self._errors[kind])
        return self._contents.get(kind)


__all__ = [
    "FailingRegistry",
    "FailingStorage",
    "FakeArtifactRegistry",
    "FakeArtifactStorage",
    "LyingStorage",
    "StubCollector",
    "UnreadableStorage",
]
