"""TASK-P1-004 —— 对象存储在真实 MinIO 上的行为。

单元测试（``tests/unit/storage/test_object_store.py``）覆盖的是
``LocalObjectStore``；这里验证 **S3 后端**的语义与本地后端一致 ——
两条路径必须给出同样的保证，否则"本地跑得通"就是假信号。

需要 MinIO。不可用时跳过（见 ``tests/integration/conftest.py``）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.evidence.artifact_hash import ArtifactHashMismatchError
from infrastructure.storage.object_store import (
    DIGEST_PREFIX,
    ContentAddressedStore,
    S3ObjectStore,
)

pytestmark = pytest.mark.integration

SAMPLE = b"ases-s3-artifact-content-\x00\x01\x02"


async def test_bucket_is_created_on_demand(s3_backend: S3ObjectStore) -> None:
    assert await s3_backend.exists("sha256:nonexistent") is False


class TestRoundTrip:
    """T1 ① —— 上传后下载字节完全一致。"""

    async def test_bytes_round_trip(self, s3_store: ContentAddressedStore) -> None:
        stored = await s3_store.put_bytes(SAMPLE)
        assert await s3_store.get_bytes(stored.key) == SAMPLE

    async def test_file_round_trip(self, s3_store: ContentAddressedStore, tmp_path: Path) -> None:
        source = tmp_path / "report.json"
        source.write_bytes(SAMPLE)
        stored = await s3_store.put_file(source)
        assert await s3_store.get_bytes(stored.key) == SAMPLE

    async def test_download_to_file(self, s3_store: ContentAddressedStore, tmp_path: Path) -> None:
        stored = await s3_store.put_bytes(SAMPLE)
        destination = tmp_path / "out" / "report.json"
        await s3_store.get_file(stored.key, destination)
        assert destination.read_bytes() == SAMPLE

    async def test_key_is_the_content_digest(self, s3_store: ContentAddressedStore) -> None:
        stored = await s3_store.put_bytes(SAMPLE)
        assert stored.key == f"{DIGEST_PREFIX}{hashlib.sha256(SAMPLE).hexdigest()}"


class TestTamperDetection:
    """T1 ② —— 篡改存储中的对象后 get 抛 hash 不匹配。"""

    async def test_overwritten_object_is_rejected(
        self, s3_store: ContentAddressedStore, s3_backend: S3ObjectStore
    ) -> None:
        stored = await s3_store.put_bytes(SAMPLE)
        # 绕过内容寻址层直接覆盖 —— 模拟"对象被换掉"。
        await s3_backend.put_bytes(stored.key, b"tampered")

        with pytest.raises(ArtifactHashMismatchError) as excinfo:
            await s3_store.get_bytes(stored.key)
        assert excinfo.value.expected == stored.digest


class TestIdempotence:
    """T1 ③ —— 同一内容重复上传不产生第二个对象。"""

    async def test_only_one_object_is_created(self, s3_store: ContentAddressedStore) -> None:
        await s3_store.put_bytes(SAMPLE)
        await s3_store.put_bytes(SAMPLE)
        assert len(await s3_store.backend.list_keys()) == 1

    async def test_second_upload_is_reported_as_skipped(
        self, s3_store: ContentAddressedStore
    ) -> None:
        first = await s3_store.put_bytes(SAMPLE)
        second = await s3_store.put_bytes(SAMPLE)

        assert first.skipped is False
        assert second.skipped is True
        assert second.key == first.key


class TestTamperIsNotSilentlyRepaired:
    """已损坏的对象不会被悄悄覆盖。

    这是刻意的设计：证据被换掉是**要保留的信号**。写的时候顺手用一份
    "看起来对"的内容覆盖上去，等于把篡改的痕迹抹掉；损坏应该在读的时候
    暴露出来，而不是在写的时候被修好。
    """

    async def test_reupload_does_not_hide_the_tampering(
        self, s3_store: ContentAddressedStore, s3_backend: S3ObjectStore
    ) -> None:
        stored = await s3_store.put_bytes(SAMPLE)
        await s3_backend.put_bytes(stored.key, b"something-else")

        again = await s3_store.put_bytes(SAMPLE)
        assert again.skipped is True
        assert await s3_store.verify(stored.key) is False
        with pytest.raises(ArtifactHashMismatchError):
            await s3_store.get_bytes(stored.key)
