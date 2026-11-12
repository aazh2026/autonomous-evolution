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
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.api.control import router as control_router
from apps.api.health import CheckResult, check_database, check_object_store
from apps.api.static_frontend import mount_frontend
from infrastructure.config.settings import DEFAULT_DASHBOARD_DIST_DIR, Settings, get_settings
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


def _resolve_settings(settings: Settings | None) -> Settings | None:
    """尽最大努力拿到配置，但**不让配置问题把导入带崩**。

    CORS 中间件必须在应用创建时装上（Starlette 不允许应用启动后再加中间件），
    所以这里不得不提前读一次配置。但这个模块的导入期约定是"**不读配置**" ——
    ``from apps.api.main import create_app`` 出现在测试收集阶段，
    配置有问题的话应该由 ``lifespan`` 在启动时报错，而不是让收集阶段整个炸掉。

    读不到就返回 ``None``：没有来源 = 不装 CORS = 同源部署，
    这是最安全的默认。配置真有问题的话，``lifespan`` 里的
    ``get_settings()`` 照样会抛 —— 该报的错不会少报，只是晚一步。
    """
    if settings is not None:
        return settings
    try:
        return get_settings()
    except Exception as exc:  # 配置异常种类多（pydantic 校验错误等），统一降级
        logger.warning("settings_unavailable", error=str(exc))
        return None


def _apply_cors(app: FastAPI, settings: Settings) -> None:
    """按配置装 CORS 中间件。

    **前后端分离之后 CORS 是部署配置，不是代码常量。**

    之前这里写死了 ``allow_origins=["*"]`` 配 ``allow_credentials=True`` ——
    这两个不能一起用。规范明确禁止 ``Access-Control-Allow-Origin: *``
    与 ``Access-Control-Allow-Credentials: true`` 同时出现：浏览器会拒掉
    带凭证的通配符响应，于是这个组合要么被浏览器拒绝，要么逼着运维去
    加各种 workaround。

    现在：
    - 没配来源 → **不装中间件**。同源部署压根不需要 CORS，
      这是最安全也最省事的默认；
    - 配了具体来源 → 只放行这些来源，可以带凭证；
    - 配了 ``*`` → 放行所有来源，但**强制关掉凭证**（规范不许）。
    """
    origins = [origin.strip() for origin in settings.cors_allowed_origins if origin.strip()]
    if not origins:
        logger.info("cors_disabled", reason="no allowed origins configured")
        return

    allow_credentials = settings.cors_allow_credentials
    if "*" in origins:
        if allow_credentials:
            logger.warning(
                "cors_credentials_disabled",
                reason="wildcard origin cannot be combined with credentials",
            )
        allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("cors_configured", origins=origins, allow_credentials=allow_credentials)


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    store: ObjectStore | None = None,
    dashboard_dist: Path | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    做成工厂而不是模块级单例：测试要能塞进不同的 ``Settings``
    （换数据库、换对象存储后端）而不必改环境变量。

    配置与对象存储后端都**延迟**到第一次需要时才解析 —— 导入这个模块
    不该触发配置读取。模块级的 ``app`` 是给 uvicorn 用的，
    导入它的是容器启动命令，不是测试。

    ``engine`` 与 ``store`` 是给测试用的注入口。生产路径上两者都是
    ``None``，由下面的惰性单例接管。

    ``dashboard_dist`` 指定前端构建产物目录。传 ``None`` 时按
    ``Settings.dashboard_dist_dir`` 解析；测试可以指着一条不存在的路径，
    强制走"内嵌旧控制舱"的降级分支。
    """
    app = FastAPI(
        title="ASES API",
        version="0.1.0",
        lifespan=lifespan,
    )

    resolved_settings = _resolve_settings(settings)
    if resolved_settings is not None:
        _apply_cors(app, resolved_settings)

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

    # 前端静态资源挂载**放在最后**。``app.mount("/")`` 匹配一切路径 ——
    # 注册在前面的话，它会盖住上面所有的 API 路由与健康检查。
    dist_dir = dashboard_dist
    if dist_dir is None:
        dist_dir = (
            resolved_settings.dashboard_dist_dir
            if resolved_settings is not None
            else DEFAULT_DASHBOARD_DIST_DIR
        )
    frontend_mode = mount_frontend(app, dist_dir)
    logger.info("frontend_mounted", mode=frontend_mode, dist_dir=str(dist_dir))

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
