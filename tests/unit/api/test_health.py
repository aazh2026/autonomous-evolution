"""API 的健康检查与就绪探针。

设计依据：docs/tasks.md TASK-P1-007（``/healthz`` 返回 200 且 DB/MinIO 均连通）。

**这一组测试守的是"健康检查会不会说谎"**：

- 依赖全好 → 200
- 依赖有一个不通 → 503（不是 200，更不是 500）
- 响应里只有异常**类型名**，没有异常消息（消息里有连接串，连接串里有口令）
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from apps.api.health import CheckResult, check_database, check_object_store
from apps.api.main import create_app, lifespan
from infrastructure.config.settings import Environment, Settings
from infrastructure.storage.object_store import ObjectStore

#: 一段"绝不能出现在响应里"的敏感串。
SECRET = "postgresql+asyncpg://ases:sup3rs3cret@db:5432/ases"


class _FakeEngine:
    """够用的假引擎。

    ``check_database`` 只做两件事：建立连接、执行 ``SELECT 1``。
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self._error)


class _FakeConnection:
    def __init__(self, error: Exception | None) -> None:
        self._error = error

    async def __aenter__(self) -> _FakeConnection:
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeStore(ObjectStore):
    """够用的假对象存储。"""

    def __init__(self, *, reachable: bool = True, error: Exception | None = None) -> None:
        self._reachable = reachable
        self._error = error
        self.closed = False

    async def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> int:
        return len(data)

    async def put_file(self, key: str, path: Any, *, content_type: str | None = None) -> int:
        return 0

    async def get_bytes(self, key: str) -> bytes:
        return b""

    async def get_file(self, key: str, destination: Any) -> int:
        return 0

    async def exists(self, key: str) -> bool:
        return False

    async def list_keys(self, prefix: str = "") -> list[str]:
        return []

    def uri(self, key: str) -> str:
        return f"memory://{key}"

    async def ping(self) -> bool:
        if self._error is not None:
            raise self._error
        return self._reachable

    async def ensure_ready(self) -> bool:
        return False

    async def close(self) -> None:
        self.closed = True


class TestCheckDatabase:
    async def test_healthy_database(self) -> None:
        result = await check_database(_FakeEngine())  # type: ignore[arg-type]
        assert result.name == "database"
        assert result.ok is True
        assert result.error is None

    async def test_unreachable_database_reports_the_exception_type(self) -> None:
        result = await check_database(_FakeEngine(error=OSError("连不上")))  # type: ignore[arg-type]
        assert result.ok is False
        assert result.error == "OSError"

    async def test_error_message_is_not_carried_through(self) -> None:
        """异常消息里常有连接串 —— 它不能进响应。"""
        result = await check_database(_FakeEngine(error=OSError(SECRET)))  # type: ignore[arg-type]
        assert SECRET not in str(result.as_dict())
        assert "连不上" not in str(result.as_dict())


class TestCheckObjectStore:
    async def test_reachable_store(self) -> None:
        result = await check_object_store(_FakeStore())
        assert result.name == "object_store"
        assert result.ok is True

    async def test_unreachable_store(self) -> None:
        result = await check_object_store(_FakeStore(reachable=False))
        assert result.ok is False
        assert result.error == "Unreachable"

    async def test_raising_store_reports_the_exception_type(self) -> None:
        result = await check_object_store(_FakeStore(error=TimeoutError("超时")))
        assert result.ok is False
        assert result.error == "TimeoutError"


class TestCheckResultShape:
    def test_ok_result_has_no_error_field(self) -> None:
        assert CheckResult(name="database", ok=True).as_dict() == {"ok": True}

    def test_failed_result_carries_the_error_name(self) -> None:
        payload = CheckResult(name="database", ok=False, error="OSError").as_dict()
        assert payload == {"ok": False, "error": "OSError"}


def _app(engine: Any, store: ObjectStore) -> FastAPI:
    return create_app(engine=engine, store=store)


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


class TestHealthEndpoint:
    async def test_all_dependencies_up_returns_200(self) -> None:
        app = _app(_FakeEngine(), _FakeStore())
        response = await _get(app, "/healthz")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["checks"]["database"]["ok"] is True
        assert payload["checks"]["object_store"]["ok"] is True

    async def test_database_down_returns_503(self) -> None:
        app = _app(_FakeEngine(error=OSError("连不上")), _FakeStore())
        response = await _get(app, "/healthz")

        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "degraded"
        assert payload["checks"]["database"]["ok"] is False
        assert payload["checks"]["object_store"]["ok"] is True

    async def test_object_store_down_returns_503(self) -> None:
        app = _app(_FakeEngine(), _FakeStore(reachable=False))
        response = await _get(app, "/healthz")

        assert response.status_code == 503
        assert response.json()["checks"]["object_store"]["ok"] is False

    async def test_dependency_error_is_503_not_500(self) -> None:
        """依赖挂了是"暂时不可用"，不是"服务有 bug"。

        500 与 503 被负载均衡器和编排器区别对待：500 通常意味着
        请求本身有问题，503 意味着"稍后再来"。
        """
        app = _app(_FakeEngine(error=RuntimeError("炸了")), _FakeStore())
        response = await _get(app, "/healthz")
        assert response.status_code == 503

    async def test_response_does_not_leak_the_connection_string(self) -> None:
        app = _app(_FakeEngine(error=OSError(SECRET)), _FakeStore())
        response = await _get(app, "/healthz")

        assert SECRET not in response.text
        assert "连不上" not in response.text

    async def test_readyz_uses_the_same_probe(self) -> None:
        app = _app(_FakeEngine(), _FakeStore())
        response = await _get(app, "/readyz")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestStoreLifecycle:
    """应用自己建的后端，应用自己关。

    S3 后端持着一个 aiohttp 连接池 —— 进程退出时不还回去，
    aiohttp 会喊 "Unclosed client session"。
    """

    async def test_the_app_closes_the_store_it_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore()
        monkeypatch.setattr("apps.api.main.build_object_store", lambda _settings: store)
        await _quiet_lifespan(monkeypatch)

        engine: Any = _FakeEngine()
        app = create_app(Settings(environment=Environment.TEST), engine=engine)
        async with lifespan(app):
            await _get(app, "/healthz")

        assert store.closed is True

    async def test_an_injected_store_is_not_closed_by_the_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """注入的后端归注入者管 —— 应用不替别人关东西。

        测试里注入的后端可能还要在应用关掉之后继续用；
        谁建的谁关，这条界线不能糊。
        """
        store = _FakeStore()
        await _quiet_lifespan(monkeypatch)

        engine: Any = _FakeEngine()
        app = create_app(
            Settings(environment=Environment.TEST),
            engine=engine,
            store=store,
        )
        async with lifespan(app):
            await _get(app, "/healthz")

        assert store.closed is False


async def _quiet_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 lifespan 里两个有全局副作用的东西换成空实现。

    - ``configure_logging`` 调 ``logging.basicConfig(force=True)``，
      会**掀掉 pytest 装在 root 上的日志捕获器** —— 别的用例的
      日志断言会跟着遭殃。
    - ``dispose_engine`` 会去建一个真的数据库引擎，跟这条测试无关。
    """

    async def _noop() -> None:
        return None

    monkeypatch.setattr("apps.api.main.configure_logging", lambda _settings: None)
    monkeypatch.setattr("apps.api.main.dispose_engine", _noop)


__all__ = [
    "TestCheckDatabase",
    "TestCheckObjectStore",
    "TestCheckResultShape",
    "TestHealthEndpoint",
    "TestStoreLifecycle",
]
