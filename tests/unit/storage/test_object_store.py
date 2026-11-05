"""TASK-P1-004 —— 对象存储与产物哈希校验。

三条硬断言（对应任务书 T1 ①②③）：

1. 上传后下载字节完全一致；
2. 篡改存储中的对象后 ``get`` 抛 hash 不匹配；
3. 同一内容重复上传不产生第二个对象（幂等）。

第 2 条与第 3 条是这个模块存在的全部理由 —— 没有它们，
"证据不可丢失"就只是一句口号。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.evidence.artifact_hash import ArtifactHashMismatchError
from infrastructure.storage.object_store import (
    DIGEST_PREFIX,
    ContentAddressedStore,
    LocalObjectStore,
    ObjectNotFoundError,
    ObjectStore,
    StoredObject,
    key_to_relative_path,
    relative_path_to_key,
)

SAMPLE = b"ases-artifact-content-\x00\x01\x02"


@pytest.fixture
def backend(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "artifacts")


@pytest.fixture
def store(backend: LocalObjectStore) -> ContentAddressedStore:
    return ContentAddressedStore(backend)


class _CountingStore(ObjectStore):
    """包一层计数，用来断言"第二次写入根本没发生"。

    只断言"库里只有一个对象"是不够的 —— 那证明不了幂等路径被走到了，
    也可能只是因为两次写到了同一个 key 上。
    """

    def __init__(self, inner: ObjectStore) -> None:
        self._inner = inner
        self.put_bytes_calls = 0
        self.put_file_calls = 0

    async def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> int:
        self.put_bytes_calls += 1
        return await self._inner.put_bytes(key, data, content_type=content_type)

    async def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> int:
        self.put_file_calls += 1
        return await self._inner.put_file(key, path, content_type=content_type)

    async def get_bytes(self, key: str) -> bytes:
        return await self._inner.get_bytes(key)

    async def get_file(self, key: str, destination: Path) -> int:
        return await self._inner.get_file(key, destination)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)

    async def list_keys(self, prefix: str = "") -> list[str]:
        return await self._inner.list_keys(prefix)

    def uri(self, key: str) -> str:
        return self._inner.uri(key)

    async def ping(self) -> bool:
        return await self._inner.ping()

    async def ensure_ready(self) -> bool:
        return await self._inner.ensure_ready()


class TestLocalObjectStore:
    """本地文件系统后端。"""

    async def test_bytes_round_trip_is_identical(self, backend: LocalObjectStore) -> None:
        await backend.put_bytes("runs:1:report.json", SAMPLE)
        assert await backend.get_bytes("runs:1:report.json") == SAMPLE

    async def test_file_round_trip_is_identical(
        self, backend: LocalObjectStore, tmp_path: Path
    ) -> None:
        source = tmp_path / "input.bin"
        source.write_bytes(SAMPLE)
        await backend.put_file("runs:1:input.bin", source)
        assert await backend.get_bytes("runs:1:input.bin") == SAMPLE

    async def test_get_file_writes_the_same_bytes(
        self, backend: LocalObjectStore, tmp_path: Path
    ) -> None:
        source = tmp_path / "input.bin"
        source.write_bytes(SAMPLE)
        await backend.put_file("runs:1:input.bin", source)
        destination = tmp_path / "out" / "input.bin"
        await backend.get_file("runs:1:input.bin", destination)
        assert destination.read_bytes() == SAMPLE

    async def test_exists_reports_presence(self, backend: LocalObjectStore) -> None:
        assert await backend.exists("runs:1:report.json") is False
        await backend.put_bytes("runs:1:report.json", SAMPLE)
        assert await backend.exists("runs:1:report.json") is True

    async def test_missing_object_raises(self, backend: LocalObjectStore) -> None:
        with pytest.raises(ObjectNotFoundError):
            await backend.get_bytes("runs:1:absent.json")

    async def test_list_keys_returns_logical_keys(self, backend: LocalObjectStore) -> None:
        await backend.put_bytes("sha256:aa", SAMPLE)
        await backend.put_bytes("sha256:bb", SAMPLE)
        assert await backend.list_keys() == ["sha256:aa", "sha256:bb"]

    async def test_list_keys_filters_by_prefix(self, backend: LocalObjectStore) -> None:
        await backend.put_bytes("sha256:aa", SAMPLE)
        await backend.put_bytes("runs:1:x", SAMPLE)
        assert await backend.list_keys("sha256:") == ["sha256:aa"]

    def test_uri_is_a_file_uri(self, backend: LocalObjectStore) -> None:
        assert backend.uri("sha256:aa").startswith("file://")


class TestKeyMapping:
    """key ↔ 本地路径的映射。"""

    def test_colon_becomes_a_directory_level(self) -> None:
        assert key_to_relative_path("sha256:abcd") == Path("sha256/abcd")

    def test_mapping_is_reversible(self) -> None:
        for key in ("sha256:abcd", "runs:2026-11-06:report.json"):
            assert relative_path_to_key(key_to_relative_path(key)) == key

    @pytest.mark.parametrize("key", ["", "..", "a:..", "a:.", ":", "a:b:c:"])
    def test_illegal_keys_are_rejected(self, key: str) -> None:
        with pytest.raises(ValueError):
            key_to_relative_path(key)

    def test_path_separator_in_key_is_rejected(self) -> None:
        """key 用 ``:`` 分段；混进 ``/`` 会让映射不可逆。"""
        with pytest.raises(ValueError):
            key_to_relative_path("sha256/abcd")


class TestContentAddressing:
    """内容寻址：key 由内容决定。"""

    async def test_key_is_the_content_digest(self, store: ContentAddressedStore) -> None:
        stored = await store.put_bytes(SAMPLE)
        expected = hashlib.sha256(SAMPLE).hexdigest()
        assert stored.key == f"{DIGEST_PREFIX}{expected}"
        assert stored.digest == expected
        assert stored.size_bytes == len(SAMPLE)

    async def test_same_content_yields_same_key(self, store: ContentAddressedStore) -> None:
        first = await store.put_bytes(SAMPLE)
        second = await store.put_bytes(SAMPLE)
        assert first.key == second.key

    async def test_different_content_yields_different_key(
        self, store: ContentAddressedStore
    ) -> None:
        first = await store.put_bytes(SAMPLE)
        second = await store.put_bytes(SAMPLE + b"!")
        assert first.key != second.key

    async def test_put_file_digest_matches_the_file(
        self, store: ContentAddressedStore, tmp_path: Path
    ) -> None:
        source = tmp_path / "report.json"
        source.write_bytes(SAMPLE)
        stored = await store.put_file(source)
        assert stored.digest == hashlib.sha256(SAMPLE).hexdigest()
        assert await store.get_bytes(stored.key) == SAMPLE


class TestRoundTrip:
    """T1 ① —— 上传后下载字节完全一致。"""

    async def test_bytes_survive_the_round_trip(self, store: ContentAddressedStore) -> None:
        stored = await store.put_bytes(SAMPLE)
        assert await store.get_bytes(stored.key) == SAMPLE

    async def test_file_survives_the_round_trip(
        self, store: ContentAddressedStore, tmp_path: Path
    ) -> None:
        source = tmp_path / "report.json"
        source.write_bytes(SAMPLE)
        stored = await store.put_file(source)
        assert await store.get_bytes(stored.key) == SAMPLE

    async def test_large_file_survives_the_round_trip(
        self, store: ContentAddressedStore, tmp_path: Path
    ) -> None:
        """大于一个块的内容 —— 确认分块读写没有丢字节。"""
        payload = bytes(range(256)) * 8192  # 2 MiB
        source = tmp_path / "big.bin"
        source.write_bytes(payload)
        stored = await store.put_file(source)
        assert await store.get_bytes(stored.key) == payload


class TestTamperDetection:
    """T1 ② —— 篡改存储中的对象后 get 抛 hash 不匹配。"""

    async def test_tampered_object_is_rejected(
        self, store: ContentAddressedStore, backend: LocalObjectStore
    ) -> None:
        stored = await store.put_bytes(SAMPLE)
        # 绕过内容寻址层，直接改后端里的对象 —— 模拟"对象被覆盖"。
        await backend.put_bytes(stored.key, b"tampered")

        with pytest.raises(ArtifactHashMismatchError) as excinfo:
            await store.get_bytes(stored.key)
        assert excinfo.value.expected == stored.digest
        assert excinfo.value.actual != stored.digest

    async def test_tampered_file_download_is_rejected(
        self, store: ContentAddressedStore, backend: LocalObjectStore, tmp_path: Path
    ) -> None:
        stored = await store.put_bytes(SAMPLE)
        await backend.put_bytes(stored.key, b"tampered")
        with pytest.raises(ArtifactHashMismatchError):
            await store.get_file(stored.key, tmp_path / "out.bin")

    async def test_tampered_file_is_rejected_on_download(
        self, store: ContentAddressedStore, backend: LocalObjectStore, tmp_path: Path
    ) -> None:
        source = tmp_path / "report.json"
        source.write_bytes(SAMPLE)
        stored = await store.put_file(source)
        await backend.put_bytes(stored.key, b"tampered")
        with pytest.raises(ArtifactHashMismatchError):
            await store.get_file(stored.key, tmp_path / "out.bin")


class TestUploadIdempotence:
    """T1 ③ —— 同一内容重复上传不产生第二个对象。"""

    async def test_only_one_object_is_created(self, store: ContentAddressedStore) -> None:
        await store.put_bytes(SAMPLE)
        await store.put_bytes(SAMPLE)
        assert len(await store.backend.list_keys()) == 1

    async def test_second_upload_does_not_write(self, backend: LocalObjectStore) -> None:
        counting = _CountingStore(backend)
        store = ContentAddressedStore(counting)

        await store.put_bytes(SAMPLE)
        await store.put_bytes(SAMPLE)

        assert counting.put_bytes_calls == 1, "第二次上传又写了一遍 —— 幂等路径没走到"

    async def test_second_file_upload_does_not_write(
        self, backend: LocalObjectStore, tmp_path: Path
    ) -> None:
        counting = _CountingStore(backend)
        store = ContentAddressedStore(counting)
        source = tmp_path / "report.json"
        source.write_bytes(SAMPLE)

        await store.put_file(source)
        await store.put_file(source)

        assert counting.put_file_calls == 1

    async def test_skip_is_reported(self, store: ContentAddressedStore) -> None:
        first = await store.put_bytes(SAMPLE)
        second = await store.put_bytes(SAMPLE)
        assert first.skipped is False
        assert second.skipped is True


class TestTamperIsNotSilentlyRepaired:
    """已损坏的对象不会被悄悄覆盖。

    证据被换掉是**要保留的信号**。写的时候顺手用一份"看起来对"的内容
    覆盖上去，等于把篡改的痕迹抹掉；损坏应该在读的时候暴露出来，
    而不是被写路径顺手修好。
    """

    async def test_reupload_does_not_hide_the_tampering(
        self, store: ContentAddressedStore, backend: LocalObjectStore
    ) -> None:
        stored = await store.put_bytes(SAMPLE)
        await backend.put_bytes(stored.key, b"tampered")

        again = await store.put_bytes(SAMPLE)
        assert again.skipped is True
        assert await store.verify(stored.key) is False
        with pytest.raises(ArtifactHashMismatchError):
            await store.get_bytes(stored.key)


class TestVerify:
    """完整性检查。"""

    async def test_intact_object_verifies(self, store: ContentAddressedStore) -> None:
        stored = await store.put_bytes(SAMPLE)
        assert await store.verify(stored.key) is True

    async def test_tampered_object_does_not_verify(
        self, store: ContentAddressedStore, backend: LocalObjectStore
    ) -> None:
        stored = await store.put_bytes(SAMPLE)
        await backend.put_bytes(stored.key, b"tampered")
        assert await store.verify(stored.key) is False


class TestStoredObject:
    """写入结果的元信息。"""

    async def test_uri_points_at_the_backend(self, store: ContentAddressedStore) -> None:
        stored = await store.put_bytes(SAMPLE)
        assert isinstance(stored, StoredObject)
        assert stored.uri == store.backend.uri(stored.key)

    def test_digest_from_key_round_trips(self) -> None:
        digest = "a" * 64
        assert ContentAddressedStore.digest_from_key(f"{DIGEST_PREFIX}{digest}") == digest

    @pytest.mark.parametrize("key", ["sha256:short", "md5:" + "a" * 64, "nope"])
    def test_digest_from_key_rejects_non_content_addressed_keys(self, key: str) -> None:
        with pytest.raises(ValueError):
            ContentAddressedStore.digest_from_key(key)
