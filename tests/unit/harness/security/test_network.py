"""TASK-P1-043 —— 网络出站白名单。

覆盖任务书的 T1："网络白名单外的域名不可达（**真实请求断言**）"。

**这一组测试的重点是"没有连接发生"这件事本身。** 一个只会检查 URL 字符串
的白名单，可以在"先连上再拒绝"的实现下依然通过绝大多数断言 —— 所以这里
真的起一台本地 HTTP 服务器，然后**数它收到了几个请求**：

- 被拒的地址 → 服务器的计数必须仍然是 0（不是 1，不是"连上之后断开"）；
- 被允许的地址 → 计数必须是 1，且响应内容要对。

**数请求比看异常更严。** "抛了个 ``NetworkPolicyError``"和"根本没连出去"
是两件事：前者可能在连出去**之后**才抛。前者能测出白名单，后者能测出
白名单真的在挡路 —— 而白名单的全部意义就在这里。

**默认策略是"什么都不允许"。** 忘配置的后果应该是吵闹的（连不上），不是
安静的（什么都连得上）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.security.network import (
    ALLOWED_SCHEMES,
    NetworkPolicy,
    NetworkPolicyError,
    check_url,
    default_network_policy,
    fetch_url,
)


@dataclass
class LocalServer:
    """一台本地 HTTP 服务器，顺带记录它收到了哪些请求。"""

    host: str
    port: int
    received: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def allow_self(self) -> NetworkPolicy:
        """一份把这台服务器放进白名单的策略。"""
        return NetworkPolicy(
            allowed_hosts=frozenset({self.host}),
            allowed_ports=frozenset({self.port}),
        )


@pytest.fixture
def server() -> Iterator[LocalServer]:
    """起一台本地服务器，用完关掉。"""
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            received.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://elsewhere.invalid/landing")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            """别把请求打进测试输出里。"""

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        sockname = httpd.socket.getsockname()
        host = str(sockname[0])
        port = int(sockname[1])
        yield LocalServer(host=host, port=port, received=received)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class TestTheDefaultPolicy:
    """默认什么都不允许 —— 忘配置时应该是吵闹的失败。"""

    def test_it_allows_nothing(self) -> None:
        policy = default_network_policy()
        assert not policy.allowed_hosts
        assert not policy.allows_host("example.com")

    def test_every_url_is_refused(self) -> None:
        with pytest.raises(NetworkPolicyError):
            check_url("https://example.com/", policy=default_network_policy())


class TestTheEgressIsBlockedBeforeConnecting:
    """T1 —— 白名单外的域名**不可达**，而且是真的没连出去。"""

    async def test_a_denied_host_never_reaches_the_network(self, server: LocalServer) -> None:
        """真实请求断言：被拒之后，对端收到的请求数必须仍然是 0。"""
        policy = NetworkPolicy(allowed_hosts=frozenset({"example.com"}))
        with pytest.raises(NetworkPolicyError):
            await fetch_url(server.url, policy=policy)
        assert server.received == [], "被拒的请求居然到达了服务器"

    async def test_an_allowed_host_is_reachable(self, server: LocalServer) -> None:
        """放行的那条路必须真的通 —— 否则白名单就是"全都拒绝"。"""
        response = await fetch_url(server.url, policy=server.allow_self())
        assert response.status_code == 200
        assert response.text == "ok"
        assert server.received == ["/"]

    async def test_a_port_outside_the_allowlist_is_refused(self, server: LocalServer) -> None:
        policy = NetworkPolicy(
            allowed_hosts=frozenset({server.host}),
            allowed_ports=frozenset({server.port + 1}),
        )
        with pytest.raises(NetworkPolicyError):
            await fetch_url(server.url, policy=policy)
        assert server.received == []

    async def test_the_environment_cannot_redirect_the_traffic(
        self, server: LocalServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """环境变量里的代理不能改道。

        白名单保证的是"只连到允许的主机"。如果客户端读 ``HTTP_PROXY``，
        实际连接就会落到**代理主机**上 —— 判定的是这个主机，流量去的是
        另一个。出站白名单一旦能被环境变量改道，它就不再是白名单。
        """
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        response = await fetch_url(server.url, policy=server.allow_self())
        assert response.status_code == 200
        assert server.received == ["/"]


class TestTheUrlIsJudged:
    """判定发生在连接之前 —— 这一组全是纯函数断言。"""

    @pytest.mark.parametrize("scheme", sorted(ALLOWED_SCHEMES))
    def test_http_and_https_are_allowed(self, scheme: str) -> None:
        policy = NetworkPolicy(allowed_hosts=frozenset({"example.com"}))
        host, port = check_url(f"{scheme}://example.com/path", policy=policy)
        assert host == "example.com"
        assert port == (443 if scheme == "https" else 80)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com/",
            "/no/scheme",
        ],
    )
    def test_other_schemes_are_refused(self, url: str) -> None:
        policy = NetworkPolicy(allowed_hosts=frozenset({"example.com"}))
        with pytest.raises(NetworkPolicyError):
            check_url(url, policy=policy)

    def test_a_non_http_scheme_is_refused_even_when_its_host_and_port_are_allowed(self) -> None:
        """协议是**第一道**判据 —— 主机与端口都在白名单里也不算数。

        少了这条，一条"主机对、端口对、协议错"的 URL 会因为别的检查碰巧
        拦住而被放过 —— 那是运气，不是规则。
        """
        policy = NetworkPolicy(
            allowed_hosts=frozenset({"example.com"}),
            allowed_ports=frozenset({21}),
        )
        with pytest.raises(NetworkPolicyError):
            check_url("ftp://example.com:21/x", policy=policy)

    def test_a_url_with_credentials_is_refused(self) -> None:
        """凭据进 URL 就等于进日志。"""
        policy = NetworkPolicy(allowed_hosts=frozenset({"example.com"}))
        with pytest.raises(NetworkPolicyError):
            check_url("https://user:pass@example.com/", policy=policy)

    def test_a_url_without_a_host_is_refused(self) -> None:
        policy = NetworkPolicy(allowed_hosts=frozenset({"example.com"}))
        with pytest.raises(NetworkPolicyError):
            check_url("http:///path", policy=policy)

    def test_a_malformed_host_is_refused(self) -> None:
        policy = NetworkPolicy(allowed_hosts=frozenset({"example.com"}))
        with pytest.raises(NetworkPolicyError):
            check_url("http://exa mple.com/", policy=policy)


class TestTheHostPatterns:
    """三种写法，语义各不相同 —— 混起来就会有人以为自己被放行了。"""

    def test_an_exact_host_matches_only_itself(self) -> None:
        policy = NetworkPolicy(allowed_hosts=frozenset({"example.com"}))
        assert policy.allows_host("example.com")
        assert not policy.allows_host("evil.example.com")
        assert not policy.allows_host("example.com.evil.net")

    def test_a_wildcard_matches_subdomains_only(self) -> None:
        policy = NetworkPolicy(allowed_hosts=frozenset({"*.corp.example"}))
        assert policy.allows_host("api.corp.example")
        assert not policy.allows_host("corp.example")

    def test_a_leading_dot_matches_the_domain_itself_too(self) -> None:
        policy = NetworkPolicy(allowed_hosts=frozenset({".corp.example"}))
        assert policy.allows_host("corp.example")
        assert policy.allows_host("api.corp.example")

    def test_the_case_and_the_trailing_dot_do_not_matter(self) -> None:
        policy = NetworkPolicy(allowed_hosts=frozenset({"Example.COM"}))
        assert policy.allows_host("example.com")
        assert policy.allows_host("example.com.")


class TestRedirectsAreNotFollowed:
    """白名单只对"你明确写出来的那个地址"生效。"""

    async def test_the_redirect_is_returned_to_the_caller(self, server: LocalServer) -> None:
        response = await fetch_url(f"{server.url}/redirect", policy=server.allow_self())
        assert response.status_code == 302
        assert response.headers["location"] == "http://elsewhere.invalid/landing"

    async def test_the_redirect_target_is_not_fetched(self, server: LocalServer) -> None:
        """跟着跳就等于让白名单形同虚设 —— 所以只跳一次请求。"""
        await fetch_url(f"{server.url}/redirect", policy=server.allow_self())
        assert server.received == ["/redirect"]
