"""健康检查不能把 CPU 烧在"问一句还在不在"上。

这是一个**门禁**：它跑在 CI 里，防止有人把健康检查改回"起一个解释器"。

**为什么要有这条门禁。** 健康检查每 5 秒跑一次，一天 17280 次。api 那版
用 ``python -c "import urllib.request; ..."`` —— 实测每次 **100.7 ms CPU**，
其中 81% 是 ``import urllib.request`` 拉进来的 ``ssl`` / ``http.client`` /
``socket``（解释器启动本身只要 6 ms）。换算下来，**一个核的 2% 常年挂着，
什么活都不干**。换成 bash 的 ``/dev/tcp`` 是 0.45 ms —— 差 224 倍。

**这个门禁拦的是哪一条。** 它不是"不许用 Python"：worker 的心跳检查也用
Python，但它只读一个文件的 mtime，实测 6 ms，没有花钱。花钱的是**为了发一个
HTTP 请求而把 urllib 那一串模块拖进来**。所以规则是"不许 import urllib"，
而不是"不许用 python" —— 门禁拦的是**花钱的方式**，不是**用哪个语言**。

**为什么还要盯"必须真的检查 200"。** 一个把 ``exit 0`` 写死的健康检查，
比没有健康检查更坏：容器看起来永远就绪，而依赖其实早就不通了。所以除了
"不许花那笔钱"，还得同时盯住"这笔钱得花在真检查上"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

#: 贵的模块 —— 为了发一个 HTTP 请求而拖进来的那一串。
#:
#: ``urllib.request`` 会连带拉进 ``ssl`` / ``http.client`` / ``socket`` /
#: ``email``，实测就是 81% 的开销在这儿。只列 ``urllib`` 一个就够：它是
#: 这条路上唯一"必须"的名字，其余的都是被它拖进来的。
EXPENSIVE_IMPORTS: tuple[str, ...] = ("urllib",)


def _compose() -> dict[str, Any]:
    """读 compose 文件。"""
    loaded = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _healthchecks() -> dict[str, str]:
    """每个服务的健康检查命令，摊平成一段字符串。

    命令既可能是列表（``CMD`` 形式），也可能是字符串（``CMD-SHELL`` 形式）。
    两种都摊成一段字符串，才能用同一条规则去查。
    """
    services = _compose().get("services", {})
    assert isinstance(services, dict)
    checks: dict[str, str] = {}
    for name, service in services.items():
        assert isinstance(service, dict)
        healthcheck = service.get("healthcheck")
        if not isinstance(healthcheck, dict):
            continue
        test = healthcheck.get("test")
        if isinstance(test, list):
            checks[str(name)] = " ".join(str(part) for part in test)
        elif isinstance(test, str):
            checks[str(name)] = test
    return checks


def test_the_compose_file_has_healthchecks_to_check() -> None:
    """先确认这个门禁看得见东西 —— 不然"一条都没拦"看起来像通过。"""
    checks = _healthchecks()

    assert checks, "compose 里一个健康检查都没有 —— 门禁成了空转"


@pytest.mark.parametrize("name", sorted(_healthchecks()))
def test_no_healthcheck_drags_in_the_expensive_http_modules(name: str) -> None:
    """不许为了发一个请求把 urllib 那一串拖进来。"""
    command = _healthchecks()[name]

    for module in EXPENSIVE_IMPORTS:
        assert module not in command, (
            f"{name} 的健康检查 import 了 {module} —— "
            "实测每次 100 ms CPU，每 5 秒一次就是一个核的 2% 常年挂着"
        )


def test_the_api_healthcheck_really_checks_the_status_code() -> None:
    """健康检查必须真的看状态码 —— 写死 exit 0 比没有检查更坏。"""
    command = _healthchecks()["api"]

    assert "200" in command, "api 的健康检查不检查状态码 —— 那它检查了什么？"
    assert "/healthz" in command, "api 的健康检查不打 /healthz —— 那它问的是谁？"


def test_the_api_healthcheck_does_not_reach_out_to_the_network() -> None:
    """检查的是**本机**的服务，不是外面某个地址。

    连本机都要走网络栈是对的（那就是在测 socket），但目标必须是
    ``127.0.0.1`` —— 打成公网地址会让"服务活着吗"这个问题变成
    "互联网通吗"，那不是这个检查该回答的。
    """
    command = _healthchecks()["api"]

    assert "127.0.0.1" in command
