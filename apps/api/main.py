"""ASES API 服务。

设计依据：docs/tasks.md TASK-P1-007（``make up`` 后 ``/healthz`` 返回 200
且 DB/MinIO 均连通）。

阶段一的 API 面很窄 —— 只有一个健康检查端点。业务接口属于后面的任务
（TASK-P1-060 编排、TASK-P1-062 准入 gate 等）。先有"能不能起来"这个
可验证的事实，再谈业务。

**这个进程不持有长期任务**：所有耗时工作由 worker 承担（``apps/worker``）。
API 只做"接请求 → 返回结果"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.api.control import router as control_router
from apps.api.health import CheckResult, check_database, check_object_store
from infrastructure.config.settings import Settings, get_settings
from infrastructure.database.engine import dispose_engine, get_engine
from infrastructure.observability.logging import configure_logging, get_logger
from infrastructure.storage.object_store import ObjectStore, build_object_store

#: 服务的对外标识。出现在日志与健康检查响应里。
SERVICE_NAME = "ases-api"

logger = get_logger(__name__)


async def probe_dependencies(
    store: ObjectStore,
    engine: AsyncEngine | None = None,
) -> list[CheckResult]:
    """检查全部依赖。返回逐项结果。

    抽成独立函数而不是内联在路由里：``/healthz`` 与 ``/readyz``
    探的是同一批依赖，探针逻辑只有一份。

    ``engine`` 默认取进程级单例；测试可以塞一个假的进来，
    不必去改全局状态。
    """
    target = engine if engine is not None else get_engine()
    return [
        await check_database(target),
        await check_object_store(store),
    ]


def _render(results: list[CheckResult]) -> tuple[int, dict[str, object]]:
    """把检查结果渲染成 (状态码, 响应体)。"""
    healthy = all(r.ok for r in results)
    payload: dict[str, object] = {
        "status": "ok" if healthy else "degraded",
        "checks": {r.name: r.as_dict() for r in results},
    }
    return (200 if healthy else 503), payload


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    store: ObjectStore | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    做成工厂而不是模块级单例：测试要能塞进不同的 ``Settings``
    （换数据库、换对象存储后端）而不必改环境变量。

    配置与对象存储后端都**延迟**到第一次需要时才解析 —— 导入这个模块
    不该触发配置读取。模块级的 ``app`` 是给 uvicorn 用的，
    导入它的是容器启动命令，不是测试。

    ``engine`` 与 ``store`` 是给测试用的注入口。生产路径上两者都是
    ``None``，由下面的惰性单例接管。
    """
    app = FastAPI(
        title="ASES API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(control_router)

    #: 进程级单例。健康检查每次都新建一个 aioboto3 会话是纯浪费 ——
    #: 后端本来就该被复用。
    resolved_store: ObjectStore | None = store

    def get_store() -> ObjectStore:
        nonlocal resolved_store
        if resolved_store is None:
            resolved_store = build_object_store(settings)
            # **自己建的后端自己负责关。** 注入进来的（测试用的假后端）
            # 不归这个应用管 —— 应用不替别人关东西。
            app.state.owned_store = resolved_store
        return resolved_store

    @app.get("/healthz")
    async def healthz() -> Response:
        """健康检查。

        返回 200 当且仅当数据库与对象存储**都**可达；任一不可达返回 503。

        503 而不是 500：依赖挂了是"暂时不可用"，不是"服务有 bug"。
        两种状态码被负载均衡器区别对待。
        """
        status_code, payload = _render(await probe_dependencies(get_store(), engine))
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/readyz")
    async def readyz() -> Response:
        """就绪探针。

        与 ``/healthz`` 相同，但语义是"可以接收流量"而非"进程活着"。
        阶段一两者等价 —— 拆分是为了将来"进程活着但暂时不接流量"
        （比如正在跑迁移）时不必改调用方。
        """
        status_code, payload = _render(await probe_dependencies(get_store(), engine))
        return JSONResponse(status_code=status_code, content=payload)

    return app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动与关闭。

    启动时配置日志 —— 日志配置放在这里而不是模块导入时，
    是为了让"导入这个模块"（比如测试收集阶段）不产生副作用。
    """
    settings = get_settings()
    configure_logging(settings)
    logger.info(
        "api_starting",
        service=SERVICE_NAME,
        environment=settings.environment.value,
    )
    try:
        yield
    finally:
        await dispose_engine()
        # S3 后端持着一个 aiohttp 连接池 —— 不还回去，进程退出时
        # aiohttp 会喊 "Unclosed client session"。
        owned: ObjectStore | None = getattr(app.state, "owned_store", None)
        if owned is not None:
            await owned.close()
        logger.info("api_stopped", service=SERVICE_NAME)


#: uvicorn 的入口点：``uvicorn apps.api.main:app``。
app = create_app()


__all__ = ["SERVICE_NAME", "app", "create_app", "probe_dependencies"]
