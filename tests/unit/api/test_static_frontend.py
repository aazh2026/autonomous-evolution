"""静态前端托管与 CORS 配置的测试。

**这两件事是前后端分离的最后一公里**，也是最容易出"看起来没问题"的
地方：

- 静态挂载注册在前面，会**盖住**所有 API 路由 —— 界面白屏，接口全 404，
  而且报错信息完全指不到真正的原因上；
- CORS 配错，浏览器会拦掉响应，但 **curl 完全正常** ——
  于是"后端没问题"这句话会被反复重复，直到有人去翻浏览器控制台。

所以两条都要测。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from apps.api.main import create_app
from apps.api.static_frontend import (
    FRONTEND_LEGACY,
    FRONTEND_SPA,
    is_built,
    mount_frontend,
)
from infrastructure.config.settings import REPO_ROOT, Settings

#: 一条不存在的目录 —— 用来强制走"内嵌旧控制舱"的降级分支。
MISSING_DIST = Path("/tmp/ases-no-such-dashboard-dist")


def _middleware_name(middleware: object) -> str:
    """取中间件类的名字。

    ``app.user_middleware`` 里的 ``cls`` 在类型上是"类工厂"，
    运行时却是真正的中间件类 —— 所以这里用 ``getattr`` 拿名字，
    拿不到就退回到类型名（而不是让整条测试挂在 AttributeError 上）。
    """
    cls = getattr(middleware, "cls", None)
    name = getattr(cls, "__name__", None)
    if isinstance(name, str):
        return name
    return type(cls).__name__


@pytest.fixture
async def legacy_client() -> httpx.AsyncClient:
    """强制走降级分支的客户端（前端没构建出来的情形）。"""
    app = create_app(dashboard_dist=MISSING_DIST)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def spa_client() -> httpx.AsyncClient:
    """强制走 SPA 分支的客户端。

    用真实的构建产物目录 —— 仓库里 ``apps/dashboard/dist`` 可能不存在
    （CI 里不构建前端），所以这里现场造一个最小的构建产物。
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="ases-dist-"))
    (tmpdir / "index.html").write_text(
        "<!DOCTYPE html><html><head><title>ASES 控制舱</title></head>"
        "<body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    assets = tmpdir / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log('dashboard');", encoding="utf-8")

    app = create_app(dashboard_dist=tmpdir)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestStaticMount:
    """静态挂载。"""

    async def test_spa_is_served_at_root(self, spa_client: httpx.AsyncClient) -> None:
        """构建产物存在时，根路径下发 SPA。"""
        response = await spa_client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "id='root'" in response.text or 'id="root"' in response.text

    async def test_spa_assets_are_served(self, spa_client: httpx.AsyncClient) -> None:
        """SPA 引用的静态资源也要能取到。"""
        response = await spa_client.get("/assets/index-abc123.js")
        assert response.status_code == 200
        assert "dashboard" in response.text

    async def test_ui_redirects_to_spa(self, spa_client: httpx.AsyncClient) -> None:
        """``/ui`` 指向新 SPA —— 旧书签不能失效。"""
        response = await spa_client.get("/ui", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/"

    async def test_api_routes_survive_the_mount(self, spa_client: httpx.AsyncClient) -> None:
        """**关键**：静态挂载不能盖住 API 路由。

        ``app.mount("/")`` 匹配一切路径 —— 注册在 API 路由前面的话，
        整个控制舱会变成"接口全 404，页面正常显示"的怪状态。
        """
        status = await spa_client.get("/api/v1/status")
        assert status.status_code == 200
        assert "project_id" in status.json()

        candidates = await spa_client.get("/api/v1/candidates")
        assert candidates.status_code == 200

    async def test_legacy_fallback_when_not_built(self, legacy_client: httpx.AsyncClient) -> None:
        """前端没构建时，根路径回落到内嵌的旧控制舱。"""
        response = await legacy_client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "ASES 控制舱" in response.text

    async def test_legacy_fallback_at_ui_path(self, legacy_client: httpx.AsyncClient) -> None:
        """降级时 ``/ui`` 同样下发内嵌界面。"""
        response = await legacy_client.get("/ui")
        assert response.status_code == 200
        assert "ASES 控制舱" in response.text

    async def test_api_routes_work_in_legacy_mode(self, legacy_client: httpx.AsyncClient) -> None:
        """降级模式下 API 一样可用 —— 前端和内嵌界面都靠它。"""
        response = await legacy_client.get("/api/v1/status")
        assert response.status_code == 200

    def test_is_built_requires_index_html(self, tmp_path: Path) -> None:
        """空目录不算构建完成 —— 光有目录说明不了什么。"""
        assert is_built(tmp_path) is False
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        assert is_built(tmp_path) is True

    def test_mount_frontend_reports_the_mode(self, tmp_path: Path) -> None:
        """挂载方式要能报出来 —— 启动日志里得看得出走的是哪条路。

        走错分支时（构建产物没出来却以为挂上了），
        界面上只会表现为"白屏"，没有别的线索。
        """
        app = FastAPI()
        assert mount_frontend(app, MISSING_DIST) == FRONTEND_LEGACY

        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        app2 = FastAPI()
        assert mount_frontend(app2, tmp_path) == FRONTEND_SPA


class TestCORS:
    """CORS 配置。"""

    async def test_no_cors_middleware_without_origins(self) -> None:
        """没配来源 = 不装 CORS 中间件 = 同源部署。

        这是最安全的默认：同源部署压根不需要 CORS。
        """
        app = create_app(
            Settings(cors_allowed_origins=[]),
            dashboard_dist=MISSING_DIST,
        )
        middleware = [_middleware_name(m) for m in app.user_middleware]
        assert "CORSMiddleware" not in middleware

    async def test_cors_middleware_installed_with_origins(self) -> None:
        """配了来源就装中间件。"""
        app = create_app(
            Settings(cors_allowed_origins=["https://cockpit.example.com"]),
            dashboard_dist=MISSING_DIST,
        )
        middleware = [_middleware_name(m) for m in app.user_middleware]
        assert "CORSMiddleware" in middleware

    async def test_allow_origin_header_for_listed_origin(self) -> None:
        """列出来的来源要拿到 CORS 响应头。"""
        app = create_app(
            Settings(cors_allowed_origins=["https://cockpit.example.com"]),
            dashboard_dist=MISSING_DIST,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/status",
                headers={"Origin": "https://cockpit.example.com"},
            )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://cockpit.example.com"

    async def test_unlisted_origin_gets_no_cors_header(self) -> None:
        """没列出来的来源**不能**拿到 CORS 响应头。

        这正是把 ``["*"]`` 换成显式列表的意义 ——
        之前是"谁都行"，现在是"只有说好的才行"。
        """
        app = create_app(
            Settings(cors_allowed_origins=["https://cockpit.example.com"]),
            dashboard_dist=MISSING_DIST,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/status",
                headers={"Origin": "https://evil.example.com"},
            )
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers

    async def test_wildcard_origin_disables_credentials(self) -> None:
        """``*`` 与凭证不能共存 —— 规范不让，实现里强制关掉。

        ``Access-Control-Allow-Origin: *`` 配
        ``Access-Control-Allow-Credentials: true`` 会被浏览器直接拒绝，
        于是"配了凭证的通配符"是个看着能跑、实际全线报错的组合。
        """
        app = create_app(
            Settings(cors_allowed_origins=["*"], cors_allow_credentials=True),
            dashboard_dist=MISSING_DIST,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/status",
                headers={"Origin": "https://cockpit.example.com"},
            )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers

    async def test_preflight_for_listed_origin(self) -> None:
        """预检请求要通过 —— 否则浏览器的实际请求根本发不出去。"""
        app = create_app(
            Settings(cors_allowed_origins=["https://cockpit.example.com"]),
            dashboard_dist=MISSING_DIST,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                "/api/v1/status",
                headers={
                    "Origin": "https://cockpit.example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://cockpit.example.com"


class TestCorsSettingsParsing:
    """环境变量里的来源列表怎么解析。"""

    # 这几条走 ``model_validate`` 而不是构造函数：这里的输入**就是**
    # 一个字符串，而字段声明的类型是 ``list[str]``。构造函数的签名会说谎
    # ——"这不该传"—— 但环境变量里传的就是字符串，那正是要验的东西。
    # ``model_validate`` 走的是同一条校验管线（before-validator 照样跑），
    # 只是不拿类型签名当门槛。

    def test_comma_separated_string(self) -> None:
        """运维写逗号分隔的字符串就认 —— 不该要求人理解 JSON。"""
        settings = Settings.model_validate({"cors_allowed_origins": "https://a.com, https://b.com"})
        assert settings.cors_allowed_origins == ["https://a.com", "https://b.com"]

    def test_empty_string_means_no_origins(self) -> None:
        """空串 = 不放行任何来源。"""
        settings = Settings.model_validate({"cors_allowed_origins": ""})
        assert settings.cors_allowed_origins == []

    def test_json_array_still_works(self) -> None:
        """JSON 数组也认 —— pydantic-settings 的原生格式不能退化。"""
        settings = Settings.model_validate({"cors_allowed_origins": '["https://a.com"]'})
        assert settings.cors_allowed_origins == ["https://a.com"]


class TestDashboardDistSetting:
    """前端构建产物目录的配置。"""

    def test_defaults_to_the_repo_dashboard_dist(self) -> None:
        """默认指向仓库里的 ``apps/dashboard/dist``。"""
        settings = Settings()
        assert settings.dashboard_dist_dir == REPO_ROOT / "apps" / "dashboard" / "dist"

    def test_is_overridable_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """容器里可以指到别处 —— 部署形态不该写死在代码里。"""
        monkeypatch.setenv("ASES_DASHBOARD_DIST_DIR", "/srv/cockpit")
        settings = Settings()
        assert settings.dashboard_dist_dir == Path("/srv/cockpit")
