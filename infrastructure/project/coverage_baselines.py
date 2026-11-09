"""覆盖率基线落库 —— **一个项目一行，地板与各模块的基线一起走**。

设计依据：docs/tasks.md TASK-P1-042（"每模块基线存储"、"阈值 =
max(模块基线, 全局地板)"、"基线下调需审批"）。

**这一层只做翻译。** 口径在 :mod:`core.task.coverage_baseline`：那里决定阈值
怎么算、下调要不要审批、审计什么时候写。这里只负责把那份集合存进
``coverage_baseline`` 表、再原样取回来。

**为什么不是每次读都现算。** 阈值是"当时按哪个标准判的"的一部分 —— 它必须
是**一个数**，而不是"看仓库现在的覆盖率算出来是多少"。后者每时每刻都在变，
于是"当初为什么过了"再也说不清。

**地板与模块基线在同一次写里落库。** 分两次写，中间失败就会留下"地板是新的、
模块是旧的"这种组合 —— 而那正是"标准被悄悄放松了"的样子。

**读不到就是一份空的集合，不是 ``None``。** 空集合的地板就是全局地板，
阈值照常算得出来；返回 ``None`` 的话，每个调用方都得自己处理"没有基线"
这一种情况，而漏掉一个就等于"阈值算不出来"。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities.project import CoverageBaseline
from core.task.coverage_baseline import CoverageBaselineSet, ModuleBaseline


class PostgresCoverageBaselineStore:
    """把覆盖率基线存进 ``coverage_baseline`` 表。

    :param session: 数据库会话。**不替调用方提交** —— 事务边界由调用方决定
        （与 :mod:`infrastructure.database.outbox` 同一条规矩）。
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, project_id: str) -> CoverageBaselineSet:
        """读这个项目的基线 —— **没有就返回一份空的**。

        **空集合 != 缺失。** 空集合的地板是全局地板，阈值照样算得出来；
        而"没有基线"这句话会被读成"这个项目什么都不要求"。
        """
        result = await self._session.scalars(
            select(CoverageBaseline).where(CoverageBaseline.project_id == project_id)
        )
        row = result.first()
        if row is None:
            return CoverageBaselineSet(project_id=project_id)
        return CoverageBaselineSet(
            project_id=row.project_id,
            floor=Decimal(row.floor),
            modules=_modules_of(row.modules),
            lowering_count=row.lowering_count,
        )

    async def save(self, baselines: CoverageBaselineSet) -> CoverageBaselineSet:
        """存一份基线 —— **存的是给出去的那一份**。

        **整个集合一次写完。** 地板与各模块的基线写在同一条 UPDATE 里，
        中间没有"写了一半"的状态。
        """
        row = await self._find(baselines.project_id)
        if row is None:
            row = CoverageBaseline(project_id=baselines.project_id)
            self._session.add(row)
        row.floor = baselines.floor
        row.modules = baselines.modules_document()
        row.lowering_count = baselines.lowering_count
        await self._session.flush()
        return baselines

    async def _find(self, project_id: str) -> CoverageBaseline | None:
        """这一行在不在 —— **没有就是 ``None``**。"""
        result = await self._session.scalars(
            select(CoverageBaseline).where(CoverageBaseline.project_id == project_id)
        )
        return result.first()


def _modules_of(raw: object) -> Mapping[str, ModuleBaseline]:
    """把库里那份 JSON 读成模块基线 —— **读不懂就报错**。

    **一个读坏了的基线不能被当成"没有基线"。** 那会让阈值悄悄退化成地板，
    而没有人知道标准曾经是别的样子。
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"coverage_baseline.modules 不是一个映射：{raw!r}")
    modules: dict[str, ModuleBaseline] = {}
    for name, entry in raw.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"coverage_baseline.modules[{name}] 不是一个映射：{entry!r}")
        modules[str(name)] = ModuleBaseline.from_document(entry)
    return modules


__all__ = ["PostgresCoverageBaselineStore"]
