"""依赖健康检查。

设计依据：docs/tasks.md TASK-P1-007（``/healthz`` 返回 200 且 DB/MinIO 均连通）。

**检查什么**：进程能不能连上它依赖的两样东西 —— 数据库、对象存储。
**不检查什么**：业务数据对不对、迁移是否最新、桶里有没有对象。
那些是别的问题；把它们塞进健康检查，会让"服务挂了"和"数据不对"
变成同一个信号，排障时反而更难。

两样依赖的检查方式刻意不同：

- **数据库**用 ``SELECT 1`` —— 真的发一条查询过去。``SELECT 1`` 不会碰业务表，
  所以它不受 append-only 触发器、锁、慢查询的影响，报的是"连得上"。
- **对象存储**用后端的 ``ping()`` —— 判定"服务在响应"。
  桶是否存在不归它管（那是 :meth:`ensure_ready` 的职责）。

异常信息一律**不回传**给调用方，只回报异常类型名。数据库连接串里有口令，
而连接异常的消息里常常带着连接串 —— 直接回传等于把凭证发给了任何能访问
这个端点的人。完整细节留在服务端日志里。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from infrastructure.storage.object_store import ObjectStore

#: 健康检查各依赖的名称。写死在这里而不是动态生成，
#: 是为了让"检查了哪几项"在代码里一目了然 —— 少一项就是少一项。
DEPENDENCIES = ("database", "object_store")


@dataclass(frozen=True)
class CheckResult:
    """单项依赖的检查结果。"""

    name: str
    ok: bool
    #: 失败时的异常类型名。不含消息 —— 消息里可能有凭证。
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"ok": self.ok}
        if self.error is not None:
            payload["error"] = self.error
        return payload


async def check_database(engine: AsyncEngine) -> CheckResult:
    """数据库是否可查询。

    用 ``SELECT 1`` 而不是查业务表：业务表可能有 append-only 触发器、
    外键约束、甚至被别的测试占着锁 —— 那些都不该让健康检查变红。
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        # 任何失败都只是"不健康" —— 具体是什么原因，留在日志里。
        return CheckResult(name="database", ok=False, error=type(exc).__name__)
    return CheckResult(name="database", ok=True)


async def check_object_store(store: ObjectStore) -> CheckResult:
    """对象存储是否可达。

    注意这里用的是 ``ping()`` 而不是 ``ensure_ready()``：健康检查是
    **只读**的。让它顺手建桶会让"读端点"变成"写端点"，也就无法用它
    判断"这个后端现在能不能用"。
    """
    try:
        reachable = await store.ping()
    except Exception as exc:
        return CheckResult(name="object_store", ok=False, error=type(exc).__name__)
    if not reachable:
        return CheckResult(name="object_store", ok=False, error="Unreachable")
    return CheckResult(name="object_store", ok=True)


__all__ = [
    "DEPENDENCIES",
    "CheckResult",
    "check_database",
    "check_object_store",
]
