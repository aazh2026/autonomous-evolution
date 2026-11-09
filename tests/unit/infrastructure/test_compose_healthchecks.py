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

**最后两条是后来加的，起因是一次真实的翻车。** 优化完 api 之后，本想把
同样的写法推广到 minio 和 worker，结果 minio 那个镜像里**根本没有 grep** ——
``head | grep`` 写出来的检查在那边 30 次全部失败，而命令看起来"在跑"。
于是多了两条门禁：

- **不许起解释器或重型二进制。** 健康检查一天跑几千次，每一次起进程都是
  固定开销。实测（同一容器内连跑 30 次取 cgroup 的 ``usage_usec`` 差值，
  新旧用同一把尺子）：``python -c`` 是 6.59 ms、``mc`` 是 10.71 ms，而纯 bash
  的 ``/dev/tcp`` 是 0.265 ms、纯 shell 的 ``stat``/``date`` 是 0.808 ms。
- **不许依赖基础镜像里不一定有的文本工具。** 读状态行用 bash 内建的
  ``read`` + ``case`` 就够，一个外部进程都不用起，也就不存在"这个镜像有没有
  grep"的问题。

外加一条**语法门禁**：健康检查现在是多行 bash 脚本了，一个括号打错就是
"容器永远不健康"，而症状（在 compose 文件里）离现场很远。所以用 ``bash -n``
把渲染之后的脚本过一遍 —— CI 上是 ubuntu，bash 一定有。
"""

from __future__ import annotations

import re
import shutil
import subprocess
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

#: 健康检查里不许起的**解释器和重型二进制**。
#:
#: 每个外部程序都是一次进程启动，而健康检查一天要跑几千次。实测：
#:
#: - ``python -c`` —— 解释器冷启动 **6.59 ms**。worker 那版只为了读一个
#:   文件的 mtime，却要起一次解释器；纯 shell 的 ``stat``/``date`` 只要
#:   **0.808 ms**，快 8 倍。
#: - ``mc`` —— Go 二进制冷启动 **10.71 ms**。minio 那版起一次 ``mc`` 就为了
#:   问一句"准备好了吗"；bash 的 ``/dev/tcp`` 只要 **0.265 ms**，快 40 倍。
INTERPRETERS_AND_HEAVY: tuple[str, ...] = ("python", "python3", "mc")

#: 健康检查里不许依赖的**文本处理工具**。
#:
#: 理由不是慢，是**基础镜像里有什么不能假设**：minio 那个镜像里没有 grep，
#: ``head | grep`` 写出来的检查在那边会静悄悄地全部失败 —— 命令在跑、
#: 退出码非 0、容器永远不健康，而从外面看不出哪儿错了。
#: 读状态行用 bash 内建的 ``read`` + ``case`` 就够，一个外部进程都不用起。
TEXT_TOOLS: tuple[str, ...] = ("grep", "head")

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


def _word_present(command: str, word: str) -> bool:
    """命令里有没有出现过这个词。

    **按 shell 的词边界匹配**，不把 ``-`` 和 ``.`` 当分隔符 —— 否则
    ``localhost`` 会被当成含有 ``mc``，``pg_isready`` 会被当成含有 ``python``。
    """
    return re.search(rf"(?<![\w.-]){re.escape(word)}(?![\w.-])", command) is not None


@pytest.mark.parametrize("name", sorted(_healthchecks()))
def test_no_healthcheck_starts_an_interpreter_or_a_heavy_binary(name: str) -> None:
    """健康检查不许起解释器或重型二进制。

    **为什么。** 健康检查一天要跑几千次，每一次起进程都是一笔固定开销 ——
    这笔钱不换来任何判断力，只是让"你还活着吗"这句话变得更贵。
    实测（从容器自己的 cgroup 里读的，新旧用同一把尺子）：

    ====================  ==========  ==========================
    实现                   CPU/次       换算成常驻占用
    ====================  ==========  ==========================
    ``python -c``           6.59 ms    每 10 秒一次 = 0.07% 的一个核
    ``mc ready local``     10.71 ms    每 5 秒一次 = 0.21% 的一个核
    纯 shell（stat/date）    0.808 ms   每 10 秒一次 = 0.009%
    bash ``/dev/tcp``        0.265 ms   每 5 秒一次 = 0.005%
    ====================  ==========  ==========================

    这些检查要做的事只有两件：发一个 HTTP GET、读一个文件的 mtime。
    **两件都不需要解释器，也不需要一个 100 MB 的 Go 二进制。**
    """
    command = _healthchecks()[name]

    for program in INTERPRETERS_AND_HEAVY:
        assert not _word_present(command, program), (
            f"{name} 的健康检查起了 {program} —— "
            "解释器冷启动 6 ms、`mc` 是 Go 二进制冷启动 9 ms，"
            "一天几千次就是常驻占用；纯 bash 就能做同样的事"
        )


@pytest.mark.parametrize("name", sorted(_healthchecks()))
def test_no_healthcheck_depends_on_a_text_tool_the_image_may_not_have(name: str) -> None:
    """健康检查不许依赖 ``grep`` / ``head`` 这类文本处理工具。

    **理由不是慢，是"基础镜像里有什么不能假设"。** minio 那个镜像里
    **没有 grep** —— ``head | grep`` 写出来的检查在那边 30 次全部失败，
    而命令看起来在跑、退出码非 0、容器永远不健康，从外面完全看不出
    哪儿错了（实测踩过）。

    读状态行用 bash 内建的 ``read`` + ``case`` 就够：一个外部进程都不用起，
    也就不存在"这个镜像有没有 grep"的问题。
    """
    command = _healthchecks()[name]

    for tool in TEXT_TOOLS:
        assert not _word_present(command, tool), (
            f"{name} 的健康检查用了 {tool} —— "
            "基础镜像里不一定有（minio 那个镜像就没有 grep），"
            "缺了会静悄悄地失败；用 bash 内建的 read + case 代替"
        )


def _scripts() -> dict[str, str]:
    """每个健康检查里那段**真正会被 shell 执行**的脚本。

    只有两种形态带脚本：

    - ``["CMD", "bash", "-c", "<脚本>"]`` —— 脚本在 ``-c`` 后面；
    - ``["CMD-SHELL", "<脚本>"]`` —— 脚本就是第二个元素。

    ``["CMD", "redis-cli", ...]`` 这种直接跑一个二进制的形态没有脚本，
    不参与语法检查（上面那几条规则照样查它的命令本身）。
    """
    scripts: dict[str, str] = {}
    for name, service in _compose().get("services", {}).items():
        if not isinstance(service, dict):
            continue
        healthcheck = service.get("healthcheck")
        if not isinstance(healthcheck, dict):
            continue
        test = healthcheck.get("test")
        if not isinstance(test, list) or not test:
            continue
        parts = [str(part) for part in test]
        if parts[0] == "CMD-SHELL" and len(parts) >= 2:
            scripts[str(name)] = parts[1]
        elif parts[0] == "CMD" and "bash" in parts:
            index = parts.index("bash")
            if index + 2 < len(parts) and parts[index + 1] == "-c":
                scripts[str(name)] = parts[index + 2]
    return scripts


@pytest.mark.skipif(shutil.which("bash") is None, reason="这台机器上没有 bash")
@pytest.mark.parametrize("name", sorted(_scripts()))
def test_every_healthcheck_script_is_valid_bash(name: str) -> None:
    """健康检查的脚本必须是合法的 bash。

    **查的是渲染之后的样子** —— `$$` 要变成 `$`，`${...}` 要变成它的值，
    否则量的不是容器里真正跑的那段东西。

    **为什么值得单独一条门禁。** 健康检查现在是多行 bash 脚本了，一个括号
    打错的后果是"容器永远不健康" —— 而症状（在 compose 文件里）离现场很远，
    排查起来比写这条门禁贵得多。
    """
    script = _rendered(_scripts()[name])

    done = subprocess.run(
        ["bash", "-n"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, f"{name} 的健康检查脚本语法不对：\n{script}\n{done.stderr}"
