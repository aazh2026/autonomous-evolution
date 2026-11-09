"""健康检查不能把 CPU 烧在"问一句还在不在"上。

这是一个**门禁**：它跑在 CI 里，防止有人把健康检查改回"起一个解释器"。

**为什么要有这条门禁。** 健康检查每 5 秒跑一次，一天 17280 次。api 那版
用 ``python -c "import urllib.request; ..."`` —— 实测每次 **119.0 ms CPU**，
其中 81% 是 ``import urllib.request`` 拉进来的 ``ssl`` / ``http.client`` /
``socket``（解释器启动本身只要 6 ms）。换算下来，**一个核的 2.38% 常年挂着，
什么活都不干**。换成 bash 的 ``/dev/tcp`` 是 12.7 ms —— 一个核的 0.25%。

**这些数是怎么来的。** 不是估的，是从容器自己的 cgroup 里读的：
``/sys/fs/cgroup/cpu.stat``，同一个容器里连跑 50 次取差值。这么量是因为
``docker stats`` 报的就是这本账 —— 量到的开销**实打实记在容器头上**，
不是被谁藏起来了。（12.7 ms 里真正的内核 socket 时间只有 0.45 ms，
其余是 bash 自己的启动。）

**这个门禁拦的是哪一条。** 它不是"不许用 Python"：worker 的心跳检查也用
Python，但它只读一个文件的 mtime，实测 6 ms，没有花钱。花钱的是**为了发一个
HTTP 请求而把 urllib 那一串模块拖进来**。所以规则是"不许 import urllib"，
而不是"不许用 python" —— 门禁拦的是**花钱的方式**，不是**用哪个语言**。

**为什么还要盯"必须真的检查 200"。** 一个把 ``exit 0`` 写死的健康检查，
比没有健康检查更坏：容器看起来永远就绪，而依赖其实早就不通了。所以除了
"不许花那笔钱"，还得同时盯住"这笔钱得花在真检查上"。

**还有一条更隐蔽的：Compose 会拿 ``$VAR`` 做变量替换。** 健康检查命令里
只要出现一个 ``$status``，Compose 就把它当成 compose 文件里的变量给替换掉
了（未定义就替换成空串）—— 到容器里剩下的会是 ``case "" in``：检查还在跑，
**但检查的东西已经没了**。这类 bug 极难查：命令照跑、退出码也像是对的，
只是判据没了。所以门禁把 Compose 的替换规矩实现了一份，把命令**渲染一遍**
再检查渲染之后还剩下什么。
"""

from __future__ import annotations

import re
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

#: Compose 的变量引用 —— ``$VAR`` 和 ``${VAR}`` 两种写法。
#:
#: ``${...}`` 里可以有默认值（``${VAR:-default}``、``${VAR:?必须给}``），所以
#: 花括号里不再细分 —— 只要不是嵌套的花括号，就当成一个变量引用。
VARIABLE = re.compile(r"\$\{[^{}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")

#: Compose 自己的写法 —— 带花括号。它是**明确的**：这个 ``$`` 就是给
#: Compose 用的，不是给 shell 用的。
BRACED = re.compile(r"\$\{[^{}]*\}")

#: Compose 的转义写法：``$$`` 渲染成一个字面的 ``$``。
ESCAPE = "$$"


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


def _rendered(command: str) -> str:
    """按 Compose 的规矩把命令渲染一遍。

    **只渲染这一件事**：Compose 拿 ``$VAR`` / ``${VAR}`` 做替换，没定义
    的变量替换成空串；``$$`` 是一个字面的 ``$``。真实 Compose 还会去读环境
    变量和 ``.env``，但"未定义的变量变空串"这一条已经足够让门禁看见问题
    —— 门禁要抓的是**命令自己把判据送掉了**。
    """
    placeholder = "\x00"
    return VARIABLE.sub("", command.replace(ESCAPE, placeholder)).replace(placeholder, "$")


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
            "实测每次 119 ms CPU，每 5 秒一次就是一个核的 2.38% 常年挂着"
        )


@pytest.mark.parametrize("name", sorted(_healthchecks()))
def test_no_healthcheck_uses_a_bare_dollar_variable(name: str) -> None:
    """健康检查里不许出现**不带花括号**的 ``$VAR``。

    **为什么单挑这种写法。** Compose 文件里的 ``$`` 头一个就是 Compose 的
    元字符 —— 它会先把 ``$VAR`` / ``${VAR}`` 拿去做变量替换，然后才轮到
    shell 看见命令。所以：

    - ``${VAR}`` 是 **Compose 自己的**写法，意图明确，放行（postgres 的
      ``${POSTGRES_USER:-ases}`` 就是这种）。
    - ``$VAR`` 这种**不带花括号**的写法，正是人写 *shell* 变量时的手笔 ——
      到了 Compose 手里它就成了"一个变量名恰好叫 VAR"，没定义就替换成空串。
      命令照跑，**判据没了**，而且从外面完全看不出来。

    要在健康检查里写字面的 ``$``，写成 ``$$``。
    """
    command = _healthchecks()[name]
    leftover = command.replace(ESCAPE, "")

    bare = re.findall(r"\$[A-Za-z_]", leftover)
    assert not bare, (
        f"{name} 的健康检查里有不带花括号的 $ 变量（{bare}）—— "
        "Compose 会把它当变量替换掉，到容器里判据就没了"
    )

    outside = BRACED.sub("", leftover)
    assert "$" not in outside, (
        f"{name} 的健康检查里有 $ 落在 ${{...}} 之外 —— "
        "Compose 的变量一律写 ${...}，要字面的 $ 就写 $$"
    )


def test_the_api_healthcheck_really_checks_the_status_code() -> None:
    """健康检查必须真的看状态码 —— 写死 exit 0 比没有检查更坏。

    **查的是 Compose 渲染之后的样子。** 写 ``$status`` 的时候，渲染前
    明明有 ``200``，渲染后就没了 —— 只看原文的门禁会漏掉它。
    """
    command = _rendered(_healthchecks()["api"])

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
