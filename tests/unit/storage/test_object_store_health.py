"""对象存储的可达性与就绪语义。

设计依据：docs/tasks.md TASK-P1-007（``/healthz`` 返回 200 且 DB/MinIO 均连通）。

这一组测试守着一条容易被搞混的界线：

    ``ping()``         —— 后端**在不在**（服务有响应）
    ``ensure_ready()`` —— 后端**能不能直接写**（桶建好了没有）

把两者混为一处，健康检查就会在"桶还没建"时报"MinIO 挂了" ——
而这两件事的处置完全不同：前者要去看网络，后者只要建个桶。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from infrastructure.config.settings import Environment, Settings
from infrastructure.storage.object_store import (
    ContentAddressedStore,
    LocalObjectStore,
    S3ObjectStore,
)


def _client_error(code: str, status: int) -> ClientError:
    """构造一个 botocore 的 ClientError。"""
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "HeadBucket",
    )


class _FakeS3Client:
    """够用的 S3 假客户端。

    只实现 ``ping`` / ``ensure_ready`` 这条路径用到的两个调用。
    """

    def __init__(self, *, head_error: Exception | None = None) -> None:
        self._head_error = head_error
        self.created_buckets: list[str] = []

    async def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803, ARG002 - boto3 的参数名
        if self._head_error is not None:
            raise self._head_error

    async def create_bucket(self, *, Bucket: str) -> None:  # noqa: N803 - boto3 的参数名
        self.created_buckets.append(Bucket)


def _s3_store(client: Any) -> S3ObjectStore:
    settings = Settings(
        environment=Environment.TEST,
        s3_endpoint_url="http://minio.invalid:9000",
        s3_bucket="ases-artifacts",
    )
    return S3ObjectStore(settings, client=client)


class TestLocalBackendHealth:
    async def test_ping_is_true_for_an_existing_root(self, tmp_path: Path) -> None:
        store = LocalObjectStore(tmp_path / "artifacts")
        assert await store.ping() is True

    async def test_ensure_ready_creates_nothing(self, tmp_path: Path) -> None:
        """根目录在构造时就建好了 —— 没有"待建"的时刻。"""
        store = LocalObjectStore(tmp_path / "artifacts")
        assert await store.ensure_ready() is False


class TestS3BackendReachability:
    async def test_reachable_when_the_bucket_exists(self) -> None:
        store = _s3_store(_FakeS3Client())
        assert await store.ping() is True

    async def test_missing_bucket_still_counts_as_reachable(self) -> None:
        """桶不存在 ≠ 服务挂了。

        404 说明服务**明确回答了**"这个桶没有" —— 它在响应。
        只有连不上（连接被拒、超时）才是不可达。
        """
        store = _s3_store(_FakeS3Client(head_error=_client_error("404", 404)))
        assert await store.ping() is True

    async def test_connection_failure_counts_as_unreachable(self) -> None:
        store = _s3_store(_FakeS3Client(head_error=ConnectionError("连不上")))
        assert await store.ping() is False


class TestS3BackendReadiness:
    async def test_ensure_ready_creates_the_bucket(self) -> None:
        client = _FakeS3Client(head_error=_client_error("NoSuchBucket", 404))
        store = _s3_store(client)

        assert await store.ensure_ready() is True
        assert client.created_buckets == ["ases-artifacts"]

    async def test_ensure_ready_is_idempotent(self) -> None:
        client = _FakeS3Client()
        store = _s3_store(client)

        assert await store.ensure_ready() is False
        assert client.created_buckets == []


class TestContentAddressedDelegation:
    async def test_ping_is_delegated(self, tmp_path: Path) -> None:
        store = ContentAddressedStore(LocalObjectStore(tmp_path / "artifacts"))
        assert await store.ping() is True

    async def test_ensure_ready_is_delegated(self, tmp_path: Path) -> None:
        store = ContentAddressedStore(LocalObjectStore(tmp_path / "artifacts"))
        assert await store.ensure_ready() is False


__all__ = [
    "TestContentAddressedDelegation",
    "TestLocalBackendHealth",
    "TestS3BackendReachability",
    "TestS3BackendReadiness",
]
