"""Langfuse 客户端封装。

设计依据：docs/design.md §10（可观测性）、docs/tasks.md TASK-P1-005。

核心约束：**观测不可用不得阻塞主流程**。Langfuse 没配凭证、网络不通、
服务端返回 5xx —— 演进循环都要照常跑。所以这个客户端**永不抛异常**：
失败时把状态标成 ``DEGRADED``、在日志里留下降级标记，然后继续。

为什么值得单独写一层而不是直接用 SDK：

1. SDK 的异常语义是"抛出去"，调用方要么到处 try/except，要么一个漏掉
   就把观测故障升级成业务故障 —— 后者正是设计文档要防的"自我修改失控"的近亲。
2. 降级状态需要**可见**：日志里要能看出"这一段时间的观测是缺的"，
   否则"观测没数据"和"这段时间没发生事"分不开。
3. 传输层做成可注入的 —— 没有 Langfuse 也能测降级路径。
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum

import httpx

from infrastructure.config.settings import Settings, get_settings
from infrastructure.observability.logging import get_logger

#: 一次上报的传输函数。注入它就能在没有 Langfuse 的环境里测试降级路径。
Transport = Callable[[str, Mapping[str, object]], Awaitable[None]]

#: Langfuse 公共 trace 接口路径（相对 ``langfuse_host``）。
TRACES_PATH = "/api/public/traces"

#: 上报超时。观测不值得让主流程等 —— 宁可丢一条 trace。
REQUEST_TIMEOUT_SECONDS = 2.0


class LangfuseState(StrEnum):
    """客户端状态。"""

    #: 没启用 Langfuse —— 所有调用都是 no-op。
    DISABLED = "disabled"
    #: 正常工作中。
    ACTIVE = "active"
    #: 启用过但当前不可达 —— 已降级为 no-op。
    DEGRADED = "degraded"


class LangfuseClient:
    """Langfuse 客户端。

    线程/任务安全性：状态读取是同步的，不加锁 —— 降级标记只是个提示，
    偶尔读到旧值不影响正确性（主流程无论如何都不会因此中断）。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport or self._http_transport()
        self._state = (
            LangfuseState.ACTIVE if self._settings.langfuse_enabled else LangfuseState.DISABLED
        )
        self._degraded_reason: str | None = None
        self._warned = False

    # ---------- 状态 ----------

    @property
    def state(self) -> LangfuseState:
        return self._state

    @property
    def degraded(self) -> bool:
        """当前是否处于降级状态。"""
        return self._state is LangfuseState.DEGRADED

    @property
    def degraded_reason(self) -> str | None:
        """降级原因。降级后第一次失败时写入。"""
        return self._degraded_reason

    @property
    def enabled(self) -> bool:
        return self._settings.langfuse_enabled

    # ---------- 上报 ----------

    async def record_trace(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        input_data: Mapping[str, object] | None = None,
        output_data: Mapping[str, object] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> bool:
        """记录一条 trace。**永不抛异常。**

        返回值表示"这次真的送出去了"：降级或未启用时返回 ``False``。
        调用方一般不需要看返回值 —— 它主要是给测试和诊断用的。
        """
        if self._state is LangfuseState.DISABLED:
            return False

        body: dict[str, object] = {"name": name}
        if trace_id is not None:
            body["traceId"] = trace_id
        if session_id is not None:
            body["sessionId"] = session_id
        if input_data is not None:
            body["input"] = dict(input_data)
        if output_data is not None:
            body["output"] = dict(output_data)
        if metadata is not None:
            body["metadata"] = dict(metadata)

        try:
            await self._transport(name, body)
        except Exception as exc:
            self._degrade(exc)
            return False

        self._recover()
        return True

    async def flush(self) -> bool:
        """把待发送的数据冲出去。**永不抛异常。**

        阶段一没有真正的缓冲 —— Langfuse 是 HTTP 直发 —— 所以这里只是
        一个语义占位：真正的批量缓冲在阶段二引入 SDK 时才有。
        """
        return self._state is LangfuseState.ACTIVE

    async def aclose(self) -> None:
        """释放底层资源。"""
        transport = self._transport
        aclose = getattr(transport, "aclose", None)
        if aclose is not None:
            await aclose()

    # ---------- 内部 ----------

    def _degrade(self, exc: Exception) -> None:
        """转入降级状态，并在日志里留下可见标记。"""
        already_degraded = self._state is LangfuseState.DEGRADED
        self._state = LangfuseState.DEGRADED
        if not already_degraded:
            self._degraded_reason = f"{type(exc).__name__}: {exc}"
        # 只在**进入**降级时告警一次。持续故障每次都刷屏会把真正的日志淹掉。
        if not self._warned:
            self._warned = True
            get_logger(__name__).warning(
                "langfuse_unavailable",
                degraded=True,
                langfuse_state=self._state.value,
                reason=f"{type(exc).__name__}",
            )

    def _recover(self) -> None:
        """一次成功即视为恢复。"""
        if self._state is LangfuseState.DEGRADED:
            self._state = LangfuseState.ACTIVE
            self._degraded_reason = None
            self._warned = False
            get_logger(__name__).info(
                "langfuse_recovered",
                degraded=False,
                langfuse_state=self._state.value,
            )

    def _http_transport(self) -> Transport:
        """默认传输：Langfuse 公共 trace 接口。

        用 Basic 认证（public key 作用户名、secret key 作口令）。
        凭证缺失时不构造客户端 —— 没有凭证的请求只会被服务端拒绝，
        徒增日志噪音。
        """
        settings = self._settings
        public_key = settings.langfuse_public_key
        secret_key = settings.langfuse_secret_key
        if public_key is None or secret_key is None:
            return _unconfigured_transport

        url = f"{settings.langfuse_host.rstrip('/')}{TRACES_PATH}"
        token = base64.b64encode(
            f"{public_key.get_secret_value()}:{secret_key.get_secret_value()}".encode()
        ).decode()

        async def _send(_name: str, body: Mapping[str, object]) -> None:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    url,
                    json=dict(body),
                    headers={
                        "Authorization": f"Basic {token}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()

        return _send


async def _unconfigured_transport(_name: str, _body: Mapping[str, object]) -> None:
    """没配凭证时的传输：直接失败，让调用方走降级路径。"""
    raise RuntimeError("Langfuse 未配置凭证（ASES_LANGFUSE_PUBLIC_KEY / ASES_LANGFUSE_SECRET_KEY）")


__all__ = [
    "REQUEST_TIMEOUT_SECONDS",
    "TRACES_PATH",
    "LangfuseClient",
    "LangfuseState",
    "Transport",
]
