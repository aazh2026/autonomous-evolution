"""静态前端托管。

前后端分离之后，控制舱是一个**独立构建**的 SPA（``apps/dashboard/dist``），
后端只提供 JSON 接口。这个模块负责一件事：**要不要顺手把这个 SPA 也端上来**。

两种部署形态：

1. **后端托管 SPA**（单机跑起来最省事）：``dist`` 存在时挂到 ``/``，
   ``/ui`` 重定向到 ``/``。前后端同源，不需要 CORS。
2. **前端独立部署**：``dist`` 不存在，或者前端由 nginx 之类的静态服务器
   提供，后端只提供 API，两者靠 CORS 沟通。

**为什么还留着内嵌的 control.html。**

老版本把整个控制舱塞在一个 74KB 的 HTML 里由 FastAPI 直接下发。
新前端还没构建出来时（第一次 ``make up``），控制舱不能整个消失 ——
"启动就白屏"会变成常态。所以留了一条降级路径：构建产物不在，就回落到
内嵌界面。

这不是"舍不得删老代码"，是**降级**：新前端构建失败或没构建时，老界面
还能用。控制舱是值班界面，不能"构建一次才能看"。

**降级路径也是要测试的。** 所以 ``create_app`` 接受 ``dashboard_dist``
参数，测试可以指着一条不存在的路径，强制走降级分支。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

#: 内嵌旧控制舱的 HTML 文件。
LEGACY_HTML_FILE = Path(__file__).parent / "control.html"

#: 挂载方式。
FRONTEND_SPA = "spa"
FRONTEND_LEGACY = "legacy"


def get_legacy_html() -> str:
    """读取内嵌旧控制舱的 HTML。"""
    if LEGACY_HTML_FILE.exists():
        return LEGACY_HTML_FILE.read_text(encoding="utf-8")
    return "<html><body><h3>ASES Control UI Not Found</h3></body></html>"


def is_built(dist_dir: Path) -> bool:
    """构建产物是否可用。

    判据是 ``index.html`` 存在 —— 只有它才说明 Vite 真的构建过。
    光有个空目录不算。
    """
    return (dist_dir / "index.html").is_file()


def mount_frontend(app: FastAPI, dist_dir: Path) -> str:
    """把前端挂到应用上，返回实际生效的挂载方式。

    **挂载顺序很要紧。** ``app.mount("/")`` 匹配一切路径，所以它必须
    **最后**注册 —— 否则它会盖住 ``/api/v1/*`` 与 ``/healthz``。
    调用方负责保证这一点（本仓库在 ``create_app`` 的末尾调用）。

    ``dist_dir`` 指向一个不存在的目录时，走降级分支：``/`` 与 ``/ui``
    继续下发内嵌的旧控制舱。
    """
    if not is_built(dist_dir):
        _mount_legacy(app)
        return FRONTEND_LEGACY

    # **顺序要紧。** Starlette 按注册顺序匹配路由，而 ``app.mount("/")``
    # 匹配一切路径 —— ``/ui`` 的重定向必须**先**注册，否则它会被挂载
    # 吃掉，拿到一个 404 而不是重定向。
    @app.get("/ui", include_in_schema=False)
    async def redirect_ui_to_spa() -> RedirectResponse:
        """``/ui`` 曾经是旧控制舱的地址 —— 现在指到新 SPA。

        书签不失效是小事，但比"书签打不开"重要得多。
        """
        return RedirectResponse("/", status_code=307)

    app.mount(
        "/",
        StaticFiles(directory=str(dist_dir), html=True),
        name="dashboard",
    )

    return FRONTEND_SPA


def _mount_legacy(app: FastAPI) -> None:
    """降级路径：下发内嵌的旧控制舱。

    ``/`` 与 ``/ui`` 都指向同一份 HTML —— 这两个地址在分离之前就是同义的。
    """
    html = get_legacy_html()

    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def serve_legacy_ui() -> HTMLResponse:
        return HTMLResponse(content=html, status_code=200)


__all__ = [
    "FRONTEND_LEGACY",
    "FRONTEND_SPA",
    "LEGACY_HTML_FILE",
    "get_legacy_html",
    "is_built",
    "mount_frontend",
]
