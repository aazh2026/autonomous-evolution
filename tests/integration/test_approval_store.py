"""TASK-P1-046 —— 审批门在真实 PostgreSQL 上的行为。

**内存实现证明不了审批门。** 审批门恰恰是多进程的地方：编排器等审批、API
让人批、CLI 让人批，三者看到的必须是**同一条记录**。内存实现下，编排器看到
"还没批"、审批服务看到"已经批了"，两边都觉得自己是对的 —— 而这正是审批门
最不能出的错。

所以这里用真正的 PostgreSQL 验三件在内存里证明不了的事：

1. **一条请求只能被表态一次**，且这件事在**并发**下也成立 —— 两个人同时点
   "批准"和"拒绝"，后到的那次必须失败，而不是覆盖掉先到的结论。
2. **过期在读取时落成 expired** —— 领域层按 ``expires_at`` 现算结论，存储层
   在读的时候顺手把状态改掉。让"表里长什么样"与"系统怎么看待它"保持一致，
   且**不依赖有人记得跑那个定时任务**。
3. **审批请求不建外键也能存** —— 审批可以在任务还没落库时被发起，一条悬空
   引用不该让整张表写不进去。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.policy.approval import (
    ApprovalAlreadyDecidedError,
    ApprovalError,
    ApprovalNotFoundError,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStatus,
)
from infrastructure.database.approval import PostgresApprovalStore

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def store(engine: AsyncEngine, session: AsyncSession) -> AsyncIterator[PostgresApprovalStore]:
    """干净数据库上的审批存储。

    ``session`` 夹具在这里是为它的副作用：每个测试前把全部表清空。
    """
    rows = await session.execute(text("SELECT count(*) FROM approval_request"))
    assert rows.scalar_one() == 0, "审批请求表不是干净的 —— 前置条件不满足"
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield PostgresApprovalStore(factory, clock=lambda: _NOW)


def _request(
    *,
    request_id: str | None = None,
    task_id: str = "TASK-1",
    operation: str = "merge_to_main",
    expires_at: datetime | None = _NOW + timedelta(hours=24),
    requested_at: datetime = _NOW,
) -> ApprovalRequest:
    """造一条待审批的请求。"""
    return ApprovalRequest(
        request_id=request_id or str(uuid.uuid4()),
        task_id=task_id,
        operation=operation,
        diff_summary="README.md: 改一行",
        evidence_ref="artifact://sha256:abc",
        status=ApprovalStatus.PENDING,
        requested_by="agent-1",
        requested_at=requested_at,
        expires_at=expires_at,
    )


class TestTheRoundTrip:
    """存进去读出来是同一条。"""

    async def test_a_request_can_be_read_back(self, store: PostgresApprovalStore) -> None:
        created = await store.create(_request())
        loaded = await store.get(created.request_id)
        assert loaded.request_id == created.request_id
        assert loaded.task_id == created.task_id
        assert loaded.operation == created.operation
        assert loaded.diff_summary == created.diff_summary
        assert loaded.evidence_ref == created.evidence_ref
        assert loaded.status is ApprovalStatus.PENDING
        assert loaded.requested_by == "agent-1"

    async def test_an_unknown_id_is_refused(self, store: PostgresApprovalStore) -> None:
        with pytest.raises(ApprovalNotFoundError):
            await store.get(str(uuid.uuid4()))

    async def test_a_malformed_id_is_refused(self, store: PostgresApprovalStore) -> None:
        """**不是一个 id 的东西，就不能当 id 用。**

        转换失败时直接报错，不给"当成别的东西"的机会 —— 比如悄悄返回一个
        随机 UUID，那会让一次拼错的查询变成一次"查无此记录"，把真正的错误
        盖成一个无辜的答案。

        所以这里断言的是**具体那类错**："格式不对"与"没有这条记录"是两回事，
        前者是调用方的错，后者是事实。混成一句，运维就得靠猜来分辨。
        """
        with pytest.raises(ApprovalError) as caught:
            await store.get("这不是一个 UUID")
        assert not isinstance(caught.value, ApprovalNotFoundError)

    async def test_a_request_without_a_task_row_can_still_be_stored(
        self, store: PostgresApprovalStore
    ) -> None:
        """**审批可以先于任务存在。**

        策略检查发生在编排早期，那时任务未必已经落库。审批请求不建外键 ——
        一条悬空引用不该让整张表写不进去。
        """
        created = await store.create(_request(task_id="还没有落库的任务"))
        assert (await store.get(created.request_id)).task_id == "还没有落库的任务"


class TestOneDecisionOnly:
    """一条请求只能被表态一次。"""

    async def test_a_decided_request_cannot_be_decided_again(
        self, store: PostgresApprovalStore
    ) -> None:
        created = await store.create(_request())
        await store.decide(
            request_id=created.request_id,
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=_NOW,
        )
        with pytest.raises(ApprovalAlreadyDecidedError):
            await store.decide(
                request_id=created.request_id,
                status=ApprovalStatus.REJECTED,
                decided_by="bob",
                decided_at=_NOW,
            )

    async def test_the_first_conclusion_survives(self, store: PostgresApprovalStore) -> None:
        """**后来的表态不能改写先来的结论。**

        覆盖掉前一个结论，等于让后一次表态假装是第一次 —— 而人的意图是有
        先后的。
        """
        created = await store.create(_request())
        await store.decide(
            request_id=created.request_id,
            status=ApprovalStatus.REJECTED,
            decided_by="alice",
            decided_at=_NOW,
            comment="改得太糙",
        )
        stored = await store.get(created.request_id)
        assert stored.status is ApprovalStatus.REJECTED
        assert stored.decided_by == "alice"
        assert stored.comment == "改得太糙"

    async def test_two_concurrent_decisions_leave_exactly_one(
        self, engine: AsyncEngine, session: AsyncSession
    ) -> None:
        """**两个人同时点，后到的那次必须失败。**

        两个人同时点"批准"和"拒绝"时，谁先落地是不确定的 —— 但**必须有且只
        有一条结论**。两条都成功，等于同一个请求有两个互相矛盾的结论，
        而"哪个算数"这个问题没有好答案。

        用 ``SELECT … FOR UPDATE`` 锁住那一行来保证：锁在检查之前，否则两个
        事务都能读到 pending，然后各写各的结论。
        """
        rows = await session.execute(text("SELECT count(*) FROM approval_request"))
        assert rows.scalar_one() == 0, "审批请求表不是干净的 —— 前置条件不满足"

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        store = PostgresApprovalStore(factory, clock=lambda: _NOW)
        created = await store.create(_request())

        async def decide(status: ApprovalStatus, who: str) -> str:
            try:
                await store.decide(
                    request_id=created.request_id,
                    status=status,
                    decided_by=who,
                    decided_at=_NOW,
                )
            except ApprovalAlreadyDecidedError:
                return "失败"
            return "成功"

        results = await asyncio.gather(
            decide(ApprovalStatus.APPROVED, "alice"),
            decide(ApprovalStatus.REJECTED, "bob"),
        )
        assert sorted(results) == ["失败", "成功"]

        stored = await store.get(created.request_id)
        assert stored.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
        assert stored.decided_by in ("alice", "bob")


class TestExpiryIsComputedOnRead:
    """过期在读取时落成 expired。"""

    async def test_an_expired_pending_request_reads_as_expired(
        self, store: PostgresApprovalStore
    ) -> None:
        await store.create(_request(expires_at=_NOW - timedelta(minutes=1)))
        found = await store.find(task_id="TASK-1", operation="merge_to_main")
        assert found is not None
        assert found.status is ApprovalStatus.EXPIRED

    async def test_the_expiry_is_written_back(self, store: PostgresApprovalStore) -> None:
        """状态**顺手改掉**，让审批列表上不会一直挂着僵尸请求。

        注意结论本来就不依赖这一行改没改 —— 领域层按 ``expires_at`` 现算。
        这一行改成 expired 是为了让"表里现在长什么样"与"系统怎么看待它"一致。
        """
        created = await store.create(_request(expires_at=_NOW - timedelta(minutes=1)))
        await store.find(task_id="TASK-1", operation="merge_to_main")
        assert (await store.get(created.request_id)).status is ApprovalStatus.EXPIRED

    async def test_a_fresh_request_is_left_alone(self, store: PostgresApprovalStore) -> None:
        await store.create(_request())
        found = await store.find(task_id="TASK-1", operation="merge_to_main")
        assert found is not None
        assert found.status is ApprovalStatus.PENDING


class TestFindingTheLatest:
    """``find`` 找的是最近那一条。"""

    async def test_it_finds_the_newest_request(self, store: PostgresApprovalStore) -> None:
        await store.create(_request(request_id=str(uuid.uuid4()), requested_at=_NOW))
        await store.create(
            _request(request_id=str(uuid.uuid4()), requested_at=_NOW + timedelta(minutes=1))
        )
        found = await store.find(task_id="TASK-1", operation="merge_to_main")
        assert found is not None
        assert found.requested_at == _NOW + timedelta(minutes=1)

    async def test_it_does_not_look_at_other_tasks(self, store: PostgresApprovalStore) -> None:
        await store.create(_request(task_id="TASK-OTHER"))
        assert await store.find(task_id="TASK-1", operation="merge_to_main") is None

    async def test_it_does_not_look_at_other_operations(self, store: PostgresApprovalStore) -> None:
        await store.create(_request(operation="branch_creation"))
        assert await store.find(task_id="TASK-1", operation="merge_to_main") is None

    async def test_it_lists_every_request_of_a_task(self, store: PostgresApprovalStore) -> None:
        await store.create(_request(request_id=str(uuid.uuid4()), requested_at=_NOW))
        await store.create(
            _request(request_id=str(uuid.uuid4()), requested_at=_NOW + timedelta(minutes=1))
        )
        listed = await store.list_for_task("TASK-1")
        assert len(listed) == 2
        assert [item.requested_at for item in listed] == sorted(
            item.requested_at for item in listed
        )


class TestTheStatusIsConstrained:
    """状态只能是那五个值。"""

    async def test_an_illegal_status_is_refused_by_the_database(
        self, session: AsyncSession
    ) -> None:
        """**绕过应用层也必须落在合法状态集合里。**

        CHECK 约束是最后一道防线：脚本、手工 SQL 直插也要过它。否则"这条审批
        到底处于什么状态"这个问题就没有唯一答案。
        """
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

        rows = await session.execute(text("SELECT count(*) FROM approval_request"))
        assert rows.scalar_one() == 0, "审批请求表不是干净的 —— 前置条件不满足"

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO approval_request "
                    "(id, task_id, operation, diff_summary, evidence_ref, status, "
                    "requested_by, requested_at, comment, created_at, updated_at) "
                    "VALUES (:id, 'TASK-1', 'merge_to_main', 'd', 'e', '随便写的', "
                    "'agent-1', now(), '', now(), now())"
                ),
                {"id": uuid.uuid4()},
            )
        await session.rollback()

    async def test_a_withdrawal_is_a_legal_status(self, store: PostgresApprovalStore) -> None:
        """撤回是合法状态 —— 不是把字段删掉或改成空串。"""
        created = await store.create(_request())
        withdrawn = await store.decide(
            request_id=created.request_id,
            status=ApprovalStatus.WITHDRAWN,
            decided_by="agent-1",
            decided_at=_NOW,
            comment="diff 改了",
        )
        assert withdrawn.status is ApprovalStatus.WITHDRAWN
        assert withdrawn.outcome(_NOW) is ApprovalOutcome.WITHDRAWN
