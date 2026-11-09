"""S3 客户端必须复用 —— 每次操作现造一个是 12.55 ms 的纯浪费。

**这个门禁守的是一条性能契约。** botocore 每建一个客户端，都要把 S3 的
服务模型展开成上百个操作方法。实测（api 容器内跑 ``time.process_time``）：

===========================  ==========
做什么                        CPU
===========================  ==========
只建客户端、不发请求            12.55 ms
复用客户端发一次 head_bucket     0.35 ms
===========================  ==========

**97% 的开销花在"造客户端"上，而不是"问一句 MinIO 在不在"。** 而原实现
每次操作都现造一个 —— 健康的 api 每 5 秒问一次，一天 17280 次，
每次白烧 12.55 ms ≈ **217 CPU-秒/天**（一个核的 0.25% 常年挂着）。

**为什么这个门禁必须存在。** 这类退化没有任何报错：功能测试全绿，
只是每次操作慢 36 倍。写测试是唯一能把它钉住的办法。

**还有两条边界要一起守住：**

- **跨事件循环要重建。** aiohttp 的连接池绑在创建它的事件循环上。
  同一个 store 在两个 loop 里用（测试里 ``asyncio.run`` 和用例自己的
  loop 不是同一个），旧客户端就废了 —— 拿着它去请求会直接炸。
- **并发首次调用只造一个。** 造客户端要 await，中途让出控制权，
  两个协程可能同时看到"还没有客户端"而各造一个 —— 白造一个不说，
  还会漏掉一个没关的连接池。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from infrastructure.config.settings import Environment, Settings
from infrastructure.storage.object_store import ContentAddressedStore, S3ObjectStore


class _FakeClient:
    """够用的 S3 假客户端。

    只实现 ``ping`` 这条路径用到的调用 —— 这里的重点不在 S3 语义，
    在于"客户端被造了几次、关了几次"。

    **参数名照抄 boto3 的**（``Bucket`` / ``Key``）—— 生产代码是按关键字
    调用的，名字不一样就等于测了个假的东西。调了什么记下来，
    免得参数只是"摆设"。
    """

    def __init__(self) -> None:
        self.closed = False
        self.buckets: list[str] = []
        self.keys: list[str] = []

    async def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803 - boto3 的参数名
        self.buckets.append(Bucket)

    async def head_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803 - boto3 的参数名
        self.buckets.append(Bucket)
        self.keys.append(Key)

    async def create_bucket(self, *, Bucket: str) -> None:  # noqa: N803 - boto3 的参数名
        self.buckets.append(Bucket)


class _FakeClientContext:
    """模拟 aioboto3 ``session.client()`` 的返回物。

    真实的那个是个异步上下文管理器：进入得到客户端，退出关掉它。
    这里如实模拟 —— 因为**关客户端**正是被测行为的一半。
    """

    def __init__(self, client: _FakeClient, session: _FakeSession) -> None:
        self._client = client
        self._session = session

    async def __aenter__(self) -> _FakeClient:
        return self._client

    async def __aexit__(self, *exc: object) -> None:
        self._client.closed = True
        self._session.closed_clients.append(self._client)


class _FakeSession:
    """会记账的假 aioboto3 会话。"""

    def __init__(self) -> None:
        self.client_calls = 0
        self.clients: list[_FakeClient] = []
        self.closed_clients: list[_FakeClient] = []
        self.requested_services: list[str] = []
        self.client_kwargs: list[dict[str, Any]] = []

    def client(self, service_name: str, **kwargs: Any) -> _FakeClientContext:
        self.client_calls += 1
        self.requested_services.append(service_name)
        self.client_kwargs.append(kwargs)
        client = _FakeClient()
        self.clients.append(client)
        return _FakeClientContext(client, self)


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    """把 ``aioboto3.Session`` 换成会记账的假实现。"""
    pytest.importorskip("aioboto3")
    session = _FakeSession()
    monkeypatch.setattr("aioboto3.Session", lambda: session)
    return session


def _store(*, client: Any | None = None) -> S3ObjectStore:
    settings = Settings(
        environment=Environment.TEST,
        s3_endpoint_url="http://minio.invalid:9000",
        s3_bucket="ases-artifacts",
    )
    return S3ObjectStore(settings, client=client)


class TestClientIsBuiltOnce:
    async def test_repeated_pings_reuse_one_client(self, fake_session: _FakeSession) -> None:
        """连着问三次"MinIO 在不在"，只造一个客户端。"""
        store = _store()

        assert await store.ping() is True
        assert await store.ping() is True
        assert await store.ping() is True

        assert fake_session.client_calls == 1

    async def test_the_reused_client_still_carries_its_configuration(
        self, fake_session: _FakeSession
    ) -> None:
        """缓存的是**配好的**客户端 —— 不是把配置丢了换了个裸的。

        造客户端时传的那些参数（endpoint、region、签名方式）只传一次，
        但它们得**一直在**客户端身上。
        """
        store = _store()

        await store.ping()

        assert fake_session.requested_services == ["s3"]
        kwargs = fake_session.client_kwargs[0]
        assert kwargs["endpoint_url"] == "http://minio.invalid:9000"
        assert kwargs["region_name"] == "us-east-1"

    async def test_different_operations_share_one_client(self, fake_session: _FakeSession) -> None:
        """不是一个操作一个客户端 —— 是整个 store 一个。"""
        store = _store()

        await store.ping()
        await store.exists("sha256:nonexistent")
        await store.ensure_ready()

        assert fake_session.client_calls == 1

    async def test_concurrent_first_calls_build_only_one(self, fake_session: _FakeSession) -> None:
        """并发首次调用只造一个。

        造客户端要 await，中途会让出控制权 —— 没有锁的话两个协程
        会同时看到"还没有客户端"，各造一个，还漏掉一个没关的。
        """
        store = _store()

        results = await asyncio.gather(*(store.ping() for _ in range(8)))

        assert all(results)
        assert fake_session.client_calls == 1


class TestClientIsRebuiltPerEventLoop:
    def test_a_new_event_loop_rebuilds_the_client(self, fake_session: _FakeSession) -> None:
        """换了事件循环就重建 —— 旧的连接池绑在已经关掉的 loop 上。

        测试里 ``asyncio.run`` 和用例自己的 loop 不是同一个，
        这不是假设，是每天都在发生的事。
        """
        store = _store()

        asyncio.run(store.ping())
        assert fake_session.client_calls == 1

        asyncio.run(store.ping())
        assert fake_session.client_calls == 2

    def test_the_replaced_client_is_closed(self, fake_session: _FakeSession) -> None:
        """重建时旧的也要关 —— 不关就漏了一个连接池。"""
        store = _store()

        asyncio.run(store.ping())
        asyncio.run(store.ping())

        assert len(fake_session.closed_clients) == 1


class TestClientLifecycle:
    async def test_close_releases_the_client(self, fake_session: _FakeSession) -> None:
        store = _store()
        await store.ping()

        assert fake_session.closed_clients == []

        await store.close()

        assert len(fake_session.closed_clients) == 1

    async def test_operations_after_close_rebuild(self, fake_session: _FakeSession) -> None:
        """关掉之后还能再用 —— 关的是连接池，不是把后端作废。"""
        store = _store()
        await store.ping()
        await store.close()

        assert await store.ping() is True
        assert fake_session.client_calls == 2

    async def test_close_on_an_unused_store_is_harmless(self, fake_session: _FakeSession) -> None:
        """从没建过客户端的 store，close 不该出错。"""
        store = _store()

        await store.close()

        assert fake_session.client_calls == 0

    async def test_content_addressed_store_forwards_close(self, fake_session: _FakeSession) -> None:
        """内容寻址层包着后端 —— 不转发关闭就等于没人关。"""
        backend = _store()
        store = ContentAddressedStore(backend)
        await store.ping()

        await store.close()

        assert len(fake_session.closed_clients) == 1


class TestInjectedClient:
    async def test_an_injected_client_is_used_as_is(self, fake_session: _FakeSession) -> None:
        """注入的假客户端直接用 —— 不建会话，也不造客户端。"""
        injected = _FakeClient()
        store = _store(client=injected)

        assert await store.ping() is True
        assert fake_session.client_calls == 0
        assert injected.closed is False


__all__ = [
    "TestClientIsBuiltOnce",
    "TestClientIsRebuiltPerEventLoop",
    "TestClientLifecycle",
    "TestInjectedClient",
]
