"""网络出站白名单 —— 默认禁止访问生产环境，网络访问采用白名单。

设计依据：docs/design.md §3.5D（"默认禁止访问生产环境"、"网络访问采用白名单"）、
§3.5A（沙箱不借道宿主机出网）、docs/tasks.md TASK-P1-043、R-060。

**白名单是"先拒绝、再放行"，不是"先放行、再拒绝"。** 默认策略的允许清单是
**空的** —— 也就是说，不显式配置就什么都连不了。这条默认值是刻意的：
一个"默认放行内网、默认拒绝其他"的策略，在有人忘了配置时会变成一把
没上锁的锁；而"默认全拒"在有人忘了配置时的后果是"连不上"，那是吵闹的
失败，不是安静的失败。

**检查发生在连接之前。** 请求地址先被解析、归一化、按白名单判定，**然后**
才交给 HTTP 客户端。没通过判定就**根本没有连接发生** —— 不是"连上了再
断开"。这一点可测：被拒之后，对端服务器收到的请求数必须仍然是 0。

**只放行 http/https。** ``file://`` 不是"网络"，但它是读本地文件的另一条
路；URL 里带 ``user:password@`` 的那种则是把凭据塞进日志的路。两者都在
这里被拒掉。

**不跟随重定向。** 白名单只对"你明确写出来的那个地址"生效。一个允许的
地址回一个 302 到别处，跟着跳就等于把白名单绕过去了。所以客户端
``follow_redirects=False``：重定向**返回给调用方**，由调用方决定要不要
重新走一次白名单判定。

**也不读环境里的代理设置**（``trust_env=False``）。理由同上：环境变量里的
``HTTP_PROXY`` 会让实际连接落到代理主机上 —— 白名单判定的是这个主机，
流量去的是另一个。出站白名单一旦能被环境变量改道，它就不再是白名单。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx

#: 允许的协议。只有这两种 —— ``file://`` 读本地文件，``ftp://``/``gopher://``
#: 是绕过白名单的老路。
ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: 各协议的默认端口。
DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

#: 主机名的合法构成。
_HOST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*$"
)


class NetworkPolicyError(RuntimeError):
    """出站请求被白名单拒绝。

    这条异常**只**表达"这个地址不在白名单里"。它不是"网络请求失败" ——
    后者是运行问题（超时、连接被拒），由调用方按重试处理。两者必须分开：
    "这个地址不许去"是策略结论，要被记录；"这次没连上"是偶发故障。
    """


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """出站白名单。

    :param allowed_hosts: 允许的主机名。三种写法：

        - ``"pypi.org"`` —— 精确匹配；
        - ``"*.corp.example"`` —— 匹配它的子域，**不**匹配 ``corp.example``
          本身；
        - ``".corp.example"`` —— 匹配 ``corp.example`` 本身**以及**它的
          所有子域。

    :param allowed_ports: 允许的端口。URL 里没写端口时用协议的默认端口
        来判。
    """

    allowed_hosts: frozenset[str] = frozenset()
    allowed_ports: frozenset[int] = frozenset({80, 443})

    def allows_host(self, host: str) -> bool:
        """这个主机名在白名单里吗。"""
        normalized = host.strip().lower().rstrip(".")
        if not normalized:
            return False
        for entry in self.allowed_hosts:
            candidate = entry.strip().lower().rstrip(".")
            if not candidate:
                continue
            if candidate.startswith("*."):
                suffix = candidate[2:]
                if normalized.endswith("." + suffix):
                    return True
                continue
            if candidate.startswith("."):
                suffix = candidate[1:]
                if normalized == suffix or normalized.endswith("." + suffix):
                    return True
                continue
            if normalized == candidate:
                return True
        return False

    def allows_port(self, port: int) -> bool:
        """这个端口在白名单里吗。"""
        return port in self.allowed_ports


def default_network_policy() -> NetworkPolicy:
    """默认策略 —— **什么都不允许**。

    刻意不给"内网默认放行"之类的便利默认值：忘配置的后果应该是吵闹的
    （连不上），而不是安静的（什么都连得上）。
    """
    return NetworkPolicy()


def check_url(url: str, *, policy: NetworkPolicy) -> tuple[str, int]:
    """检查一个 URL 能不能访问。

    返回 ``(主机名, 端口)`` —— 端口是判定之后真正要用的那个（URL 里没写
    就取协议的默认端口）。

    :raises NetworkPolicyError: 任何一条不满足。
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise NetworkPolicyError(
            f"协议 {scheme or '(空)'} 不在允许范围 {sorted(ALLOWED_SCHEMES)} 内"
        )
    if parts.username or parts.password:
        raise NetworkPolicyError("URL 里带着凭据 —— 凭据不进 URL（它会进日志）")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise NetworkPolicyError("URL 里没有主机名")
    if not _HOST_PATTERN.match(host):
        raise NetworkPolicyError(f"主机名 {host!r} 不是合法的域名")
    port = parts.port if parts.port is not None else DEFAULT_PORTS.get(scheme, 0)
    if not policy.allows_port(port):
        raise NetworkPolicyError(f"端口 {port} 不在允许清单 {sorted(policy.allowed_ports)} 里")
    if not policy.allows_host(host):
        raise NetworkPolicyError(f"主机名 {host!r} 不在允许清单里")
    return host, port


async def fetch_url(
    url: str,
    *,
    policy: NetworkPolicy,
    timeout_seconds: float = 10.0,
) -> httpx.Response:
    """按白名单取一个 URL。

    **先判定，再连接。** 判定没过就抛异常，**一次连接都不会发生**。

    不跟随重定向：重定向返回给调用方，由调用方决定要不要重新走一次判定 ——
    直接跟着跳等于让白名单形同虚设。

    **不读环境里的代理设置**（``trust_env=False``）。这条不是洁癖：白名单
    保证的是"只连到允许的主机"，而环境变量里的 ``HTTP_PROXY`` 会让实际
    连接落到**代理主机**上 —— 白名单判定的是一个主机，流量去的是另一个。
    出站白名单一旦能被环境变量改道，它就不再是白名单。
    """
    check_url(url, policy=policy)
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        return await client.get(url)


def allowed_hosts_of(policy: NetworkPolicy) -> Iterable[str]:
    """白名单里的主机名（排好序，便于展示）。"""
    return sorted(policy.allowed_hosts)


__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_PORTS",
    "NetworkPolicy",
    "NetworkPolicyError",
    "allowed_hosts_of",
    "check_url",
    "default_network_policy",
    "fetch_url",
]
