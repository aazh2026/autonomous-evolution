"""持久化基类与共享列类型。

设计依据：docs/design.md §3.6「核心数据模型」、docs/tasks.md TASK-P1-020。

这一层刻意放在 ``core/`` 而不是 ``infrastructure/database/``：依赖方向必须是
``infrastructure → core``（基础设施依赖领域）。反过来会让数据库结构反向绑架
领域逻辑 —— 换库、换方言时先要改领域代码，那不是分层，是耦合。

引擎、会话工厂、迁移工具留在 ``infrastructure/database/``；仓储层留在那里，
因为"用哪个会话"是基础设施问题。

两条硬约束：

1. **约束必须具名**。命名约定（``NAMING_CONVENTION``）让 Alembic 的 downgrade
   有确定的目标；没有名字，回滚就只能靠顺序碰运气。
2. **JSON 列在 PostgreSQL 上用 JSONB**。项目目标、策略、证据载荷都要被查询和
   索引 —— JSONB 能建索引，JSON 不能。
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import JSON, DateTime, MetaData, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: 约束命名约定。``ck`` 使用 ``%(constraint_name)s`` —— 因此所有 CheckConstraint
#: 都必须显式给 name，否则这里会抛错（这正是我们想要的：漏命名当场暴露）。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSON 文档列。PostgreSQL 上用 JSONB，其它方言退回 JSON —— 保证单元测试
#: 可以脱离 PostgreSQL 运行，而生产库拿到可索引的列类型。
JSONDocument = JSON().with_variant(JSONB, "postgresql")


def utcnow() -> datetime:
    """当前 UTC 时间。

    全库统一走这一个函数：混用 ``datetime.now()``（本地时区）与
    ``datetime.now(UTC)`` 会让跨时区的快照排序错乱。
    """
    return datetime.now(UTC)


def enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """枚举的**值**列表。

    SQLAlchemy 的 Enum 默认持久化成员的 *名字*。对 StrEnum 而言两者通常相同，
    但依赖这一点等于把"重命名成员会静默改数据"的风险留在了代码里。
    显式用值，数据与领域模型始终一致。
    """
    return [str(member.value) for member in enum_cls]


class Base(DeclarativeBase):
    """所有持久化实体的声明式基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # JSON 列用 ``object`` 而不是 ``Any``：``Any`` 是双向兼容的，任何类型检查
    # 到这里都会静默通过 —— 那正是"类型逃逸"。``object`` 仍然允许任意 JSON 值，
    # 但取值后必须先收窄类型才能用，读代码的人能看见边界在哪。
    type_annotation_map: ClassVar[dict[object, object]] = {
        dict[str, object]: JSONDocument,
        list[str]: JSONDocument,
    }


class UUIDPrimaryKeyMixin:
    """UUID 主键。"""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """创建/更新时间戳。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "JSONDocument",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "enum_values",
    "utcnow",
]
