"""事件域 —— outbox 的契约、投递与幂等消费。

设计依据：docs/tasks.md TASK-P1-025、I-06、R-079。

这一层回答的是"事件怎么可靠地从一个地方到另一个地方"：

- :mod:`core.events.outbox` —— 至少一次投递 + 幂等消费的契约。

**"可靠"在这里不是"一定送到"，而是"送到了不会丢、送重了不会重复处理"。**
"""

from __future__ import annotations

from core.events.outbox import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_LEASE,
    DEFAULT_MAX_ATTEMPTS,
    ConsumeOutcome,
    EventConsumerMark,
    EventMessage,
    EventOutbox,
    EventPublisher,
    IdempotentConsumer,
    OutboxReader,
    OutboxRelay,
    RelayReport,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_LEASE",
    "DEFAULT_MAX_ATTEMPTS",
    "ConsumeOutcome",
    "EventConsumerMark",
    "EventMessage",
    "EventOutbox",
    "EventPublisher",
    "IdempotentConsumer",
    "OutboxReader",
    "OutboxRelay",
    "RelayReport",
]
