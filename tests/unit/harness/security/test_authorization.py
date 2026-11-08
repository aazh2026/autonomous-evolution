"""TASK-P1-044 —— 任务级授权上下文（Agent 身份 ≠ 任务身份）。

覆盖任务书的 T1：

① **同一 Agent 在 task A 中持有仓库读权限，无法在 task B 中执行合并操作**
   （断言拒绝**且审计留痕**）；
② 授权过期后操作被拒；
③ 授权 workspace 外的路径访问被拒；
④ 高风险操作缺少独立授权记录时被拒。

**这一组测试盯的是"没有被授权"这件事。** 一个只会检查"该放行的放行了"的
实现，能通过绝大多数正向断言 —— 所以这里几乎每条用例都在问"这条本该被
拦下来的请求，真的被拦下来了吗"，而且拦下来的理由要对（不是碰巧被别的
检查挡住）。

**留痕是被断言的，不是被假定的。** 判定结果写进哪儿由调用方决定，所以
测试用一个会记账的假去处，然后**数它记了几条**、**看每条里有没有主体、
任务、资源、结果四要素** —— 只断言"被拒绝了"而不断言"留下了痕迹"的测试，
留痕这一半就没被验证过。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.policy.autonomy import Operation
from harness.security.authorization import (
    HIGH_RISK_CAPABILITIES,
    AuthorizationDecision,
    AuthorizationDeniedError,
    AuthorizationIdentity,
    AuthorizationKey,
    AuthorizationRegistry,
    AuthorizationRequest,
    AuthorizationService,
    Capability,
    DecisionSink,
    TaskAuthorization,
    path_scope,
)

AGENT = "agent-1"
OTHER_AGENT = "agent-2"
TASK_A = "task-a"
TASK_B = "task-b"
PROJECT = "project-1"
OTHER_PROJECT = "project-2"
WORKSPACE = "ws-1"
OTHER_WORKSPACE = "ws-2"

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


class Clock:
    """可拨的时钟 —— 用来把时间拨到授权到期之后。"""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class RecordingSink:
    """会记账的假留痕去处。"""

    decisions: list[AuthorizationDecision] = field(default_factory=list)

    def record(self, decision: AuthorizationDecision) -> None:
        self.decisions.append(decision)


class BrokenSink:
    """写不下来的留痕去处。"""

    def record(self, decision: AuthorizationDecision) -> None:
        raise RuntimeError("审计库不可写")


@dataclass
class Harness:
    """一次判定所需的全部零件。"""

    service: AuthorizationService
    sink: RecordingSink
    registry: AuthorizationRegistry
    independent: AuthorizationRegistry
    clock: Clock
    workspace: Path


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    """一套判定器：工作空间根目录是 ``tmp_path``。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = AuthorizationRegistry()
    independent = AuthorizationRegistry()
    sink = RecordingSink()
    clock = Clock()
    service = AuthorizationService(
        registry,
        sink=sink,
        workspace_roots={WORKSPACE: workspace, OTHER_WORKSPACE: tmp_path / "other"},
        independent_registry=independent,
        clock=clock,
    )
    yield Harness(
        service=service,
        sink=sink,
        registry=registry,
        independent=independent,
        clock=clock,
        workspace=workspace,
    )


def _grant(
    *,
    agent_id: str = AGENT,
    task_id: str = TASK_A,
    project_id: str = PROJECT,
    workspace_id: str = WORKSPACE,
    expiry: datetime | None = None,
    operations: tuple[Operation, ...] = (),
    scopes: tuple[str, ...] = (),
    approval_ref: str | None = None,
) -> TaskAuthorization:
    return TaskAuthorization(
        agent_id=agent_id,
        task_id=task_id,
        project_id=project_id,
        workspace_id=workspace_id,
        expiry=expiry,
        allowed_operations=frozenset(operations),
        granted_scopes=frozenset(scopes),
        approval_ref=approval_ref,
    )


def _request(
    *,
    agent_id: str = AGENT,
    task_id: str = TASK_A,
    project_id: str = PROJECT,
    workspace_id: str = WORKSPACE,
    operation: Operation | None = None,
    capability: Capability | None = None,
    resource: str | None = None,
) -> AuthorizationRequest:
    return AuthorizationRequest(
        agent_id=agent_id,
        task_id=task_id,
        project_id=project_id,
        workspace_id=workspace_id,
        operation=operation,
        capability=capability,
        resource=resource,
    )


def _read_grant(**kwargs: Any) -> TaskAuthorization:
    """一条只读授权。"""
    return _grant(
        operations=(Operation.ANALYZE_PROJECT,),
        scopes=(Capability.REPO_READ.value,),
        **kwargs,
    )


class TestTheIdentityIsNotMixable:
    """T1 ① —— Agent 身份不能替代任务身份。"""

    def test_the_read_grant_of_task_a_does_not_authorize_a_merge_in_task_b(
        self, harness: Harness
    ) -> None:
        """任务 A 的仓库读权限，在任务 B 里换不来一次合并。"""
        harness.registry.register(_read_grant(task_id=TASK_A))
        decision = harness.service.authorize(
            _request(task_id=TASK_B, operation=Operation.MERGE_TO_MAIN)
        )
        assert not decision.allowed
        assert "没有针对" in decision.reason

    def test_a_grant_of_another_task_is_not_used(self, harness: Harness) -> None:
        """任务 B 自己有授权，但里面没有合并 —— 照样拒绝。"""
        harness.registry.register(_read_grant(task_id=TASK_A))
        harness.registry.register(
            _grant(
                task_id=TASK_B,
                operations=(Operation.CODE_CHANGES,),
                scopes=(Capability.REPO_WRITE.value,),
            )
        )
        decision = harness.service.authorize(
            _request(task_id=TASK_B, operation=Operation.MERGE_TO_MAIN)
        )
        assert not decision.allowed
        assert "merge_to_main" in decision.reason

    def test_the_write_grant_of_task_a_does_not_authorize_task_b(self, harness: Harness) -> None:
        """**这条才是"任务身份"的正面考法。**

        任务 A 里有一份能改代码的授权 —— 如果查找时忽略任务，任务 B 就会
        顺手拿到它。只测"读权限不能换来合并"是不够的：那条即使查错了
        任务也仍然会被拒（因为读权限本来就不含合并），于是它**测不出**
        "身份被混用"这件事。
        """
        harness.registry.register(
            _grant(
                task_id=TASK_A,
                operations=(Operation.CODE_CHANGES,),
                scopes=(Capability.REPO_WRITE.value,),
            )
        )
        decision = harness.service.authorize(
            _request(task_id=TASK_B, operation=Operation.CODE_CHANGES)
        )
        assert not decision.allowed
        assert "没有针对" in decision.reason

    def test_the_refusal_is_recorded(self, harness: Harness) -> None:
        """拒绝要留痕 —— 而且留痕里有主体、任务、资源、结果四要素。"""
        harness.registry.register(_read_grant(task_id=TASK_A))
        harness.service.authorize(
            _request(task_id=TASK_B, operation=Operation.MERGE_TO_MAIN, resource="src/main.py")
        )
        assert len(harness.sink.decisions) == 1
        recorded = harness.sink.decisions[0]
        assert recorded.agent_id == AGENT
        assert recorded.task_id == TASK_B
        assert recorded.resource == "src/main.py"
        assert recorded.allowed is False
        assert recorded.operation == Operation.MERGE_TO_MAIN
        assert recorded.reason

    def test_another_project_does_not_borrow_the_grant(self, harness: Harness) -> None:
        harness.registry.register(_read_grant())
        decision = harness.service.authorize(
            _request(project_id=OTHER_PROJECT, operation=Operation.ANALYZE_PROJECT)
        )
        assert not decision.allowed

    def test_another_workspace_does_not_borrow_the_grant(self, harness: Harness) -> None:
        harness.registry.register(_read_grant())
        decision = harness.service.authorize(
            _request(workspace_id=OTHER_WORKSPACE, operation=Operation.ANALYZE_PROJECT)
        )
        assert not decision.allowed

    def test_another_agent_does_not_borrow_the_grant(self, harness: Harness) -> None:
        harness.registry.register(_read_grant())
        decision = harness.service.authorize(
            _request(agent_id=OTHER_AGENT, operation=Operation.ANALYZE_PROJECT)
        )
        assert not decision.allowed

    def test_the_same_agent_in_the_same_task_is_allowed(self, harness: Harness) -> None:
        """反面也要钉住：同一身份下该放行的要放行。"""
        harness.registry.register(_read_grant(task_id=TASK_A))
        decision = harness.service.authorize(
            _request(task_id=TASK_A, operation=Operation.ANALYZE_PROJECT)
        )
        assert decision.allowed
        assert harness.sink.decisions[0].allowed is True


class TestTheExpiry:
    """T1 ② —— 授权过期后操作被拒。"""

    def test_an_expired_grant_is_refused(self, harness: Harness) -> None:
        harness.registry.register(
            _read_grant(expiry=NOW - timedelta(minutes=1)),
        )
        decision = harness.service.authorize(_request(operation=Operation.ANALYZE_PROJECT))
        assert not decision.allowed
        assert "过期" in decision.reason

    def test_a_grant_that_has_not_expired_yet_is_accepted(self, harness: Harness) -> None:
        harness.registry.register(_read_grant(expiry=NOW + timedelta(minutes=1)))
        decision = harness.service.authorize(_request(operation=Operation.ANALYZE_PROJECT))
        assert decision.allowed

    def test_the_grant_expires_when_the_clock_moves(self, harness: Harness) -> None:
        """把时间拨过到期点，同一条授权立刻失效。"""
        harness.registry.register(_read_grant(expiry=NOW + timedelta(minutes=1)))
        assert harness.service.authorize(_request(operation=Operation.ANALYZE_PROJECT)).allowed
        harness.clock.now = NOW + timedelta(minutes=2)
        decision = harness.service.authorize(_request(operation=Operation.ANALYZE_PROJECT))
        assert not decision.allowed

    def test_the_expiry_boundary_is_inclusive(self, harness: Harness) -> None:
        """到期时刻本身就算过期 —— 边界含糊等于给了一分钟窗口。"""
        harness.registry.register(_read_grant(expiry=NOW))
        decision = harness.service.authorize(_request(operation=Operation.ANALYZE_PROJECT))
        assert not decision.allowed

    def test_a_permanent_grant_never_expires(self, harness: Harness) -> None:
        harness.registry.register(_read_grant(expiry=None))
        harness.clock.now = NOW + timedelta(days=365)
        decision = harness.service.authorize(_request(operation=Operation.ANALYZE_PROJECT))
        assert decision.allowed

    def test_an_expired_grant_is_still_recorded(self, harness: Harness) -> None:
        harness.registry.register(_read_grant(expiry=NOW - timedelta(minutes=1)))
        harness.service.authorize(_request(operation=Operation.ANALYZE_PROJECT))
        assert len(harness.sink.decisions) == 1
        assert harness.sink.decisions[0].allowed is False


class TestTheWorkspaceBoundary:
    """T1 ③ —— 授权 workspace 外的路径访问被拒。"""

    def test_a_path_outside_the_workspace_is_refused(self, harness: Harness) -> None:
        harness.registry.register(_read_grant())
        outside = harness.workspace.parent / "outside.txt"
        decision = harness.service.authorize(
            _request(operation=Operation.ANALYZE_PROJECT, resource=str(outside))
        )
        assert not decision.allowed
        assert "不在工作空间" in decision.reason

    def test_a_path_inside_the_workspace_is_accepted(self, harness: Harness) -> None:
        harness.registry.register(_read_grant())
        inside = harness.workspace / "src" / "main.py"
        decision = harness.service.authorize(
            _request(operation=Operation.ANALYZE_PROJECT, resource=str(inside))
        )
        assert decision.allowed

    def test_a_relative_path_is_resolved_inside_the_workspace(self, harness: Harness) -> None:
        """相对路径按工作空间根目录解析，不是按进程当前目录。"""
        harness.registry.register(_read_grant())
        decision = harness.service.authorize(
            _request(operation=Operation.ANALYZE_PROJECT, resource="src/main.py")
        )
        assert decision.allowed

    def test_a_dotdot_path_that_climbs_out_is_refused(self, harness: Harness) -> None:
        """``..`` 走出去的路径必须被拒 —— 边界检查的是解析之后的位置。"""
        harness.registry.register(_read_grant())
        escaping = harness.workspace / ".." / "outside.txt"
        decision = harness.service.authorize(
            _request(operation=Operation.ANALYZE_PROJECT, resource=str(escaping))
        )
        assert not decision.allowed

    def test_a_symlink_pointing_outside_is_refused(self, harness: Harness) -> None:
        """符号链接指向工作空间外时也必须被拒。"""
        harness.registry.register(_read_grant())
        outside = harness.workspace.parent / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        link = harness.workspace / "link.txt"
        link.symlink_to(outside)
        decision = harness.service.authorize(
            _request(operation=Operation.ANALYZE_PROJECT, resource=str(link))
        )
        assert not decision.allowed

    def test_a_sibling_directory_with_the_same_prefix_is_refused(self, harness: Harness) -> None:
        """``/ws`` 与 ``/ws-other`` —— 前缀相同不等于在边界内。

        边界判断若用字符串前缀而不是路径关系，``/ws-other`` 会被当成
        ``/ws`` 的一部分放过去。
        """
        harness.registry.register(_read_grant())
        sibling = harness.workspace.parent / f"{harness.workspace.name}-other"
        sibling.mkdir()
        decision = harness.service.authorize(
            _request(operation=Operation.ANALYZE_PROJECT, resource=str(sibling / "file.txt"))
        )
        assert not decision.allowed

    def test_an_unknown_workspace_is_refused(self, harness: Harness) -> None:
        """工作空间没有登记目录边界 —— 不知道边界在哪就不许动文件。"""
        harness.registry.register(_read_grant(workspace_id="ws-unknown"))
        decision = harness.service.authorize(
            _request(
                workspace_id="ws-unknown",
                operation=Operation.ANALYZE_PROJECT,
                resource="src/main.py",
            )
        )
        assert not decision.allowed
        assert "没有登记目录边界" in decision.reason

    def test_a_path_scope_narrows_the_workspace_further(self, harness: Harness) -> None:
        """授权的路径范围比工作空间更窄时，以更窄的为准。"""
        harness.registry.register(
            _grant(
                operations=(Operation.ANALYZE_PROJECT,),
                scopes=(Capability.REPO_READ.value, path_scope(harness.workspace / "src")),
            )
        )
        inside_scope = harness.service.authorize(
            _request(
                operation=Operation.ANALYZE_PROJECT,
                resource=str(harness.workspace / "src" / "main.py"),
            )
        )
        outside_scope = harness.service.authorize(
            _request(
                operation=Operation.ANALYZE_PROJECT,
                resource=str(harness.workspace / "docs" / "readme.md"),
            )
        )
        assert inside_scope.allowed
        assert not outside_scope.allowed
        assert "不在授权的路径范围内" in outside_scope.reason


class TestTheHighRiskOperations:
    """T1 ④ —— 高风险操作缺少独立授权记录时被拒。"""

    def _merge_grant(self) -> TaskAuthorization:
        return _grant(
            operations=(Operation.MERGE_TO_MAIN,),
            scopes=(Capability.REPO_MERGE.value,),
        )

    def test_a_merge_without_an_independent_record_is_refused(self, harness: Harness) -> None:
        """一般授权里写着合并也不行 —— 高风险要有**另一本册子**里的记录。"""
        harness.registry.register(self._merge_grant())
        decision = harness.service.authorize(_request(operation=Operation.MERGE_TO_MAIN))
        assert not decision.allowed
        assert "独立授权记录" in decision.reason

    def test_an_independent_record_with_an_approval_allows_the_merge(
        self, harness: Harness
    ) -> None:
        harness.registry.register(self._merge_grant())
        independent = _grant(
            operations=(Operation.MERGE_TO_MAIN,),
            scopes=(Capability.REPO_MERGE.value,),
            approval_ref="approval-1",
        )
        harness.independent.register(independent)
        decision = harness.service.authorize(_request(operation=Operation.MERGE_TO_MAIN))
        assert decision.allowed
        assert decision.independent_key == independent.key

    def test_an_independent_record_without_an_approval_is_refused(self, harness: Harness) -> None:
        """没有审批编号的独立记录不算独立授权 —— 自己签发自己不算。"""
        harness.registry.register(self._merge_grant())
        harness.independent.register(
            _grant(
                operations=(Operation.MERGE_TO_MAIN,),
                scopes=(Capability.REPO_MERGE.value,),
            )
        )
        decision = harness.service.authorize(_request(operation=Operation.MERGE_TO_MAIN))
        assert not decision.allowed
        assert "没有引用审批" in decision.reason

    def test_an_expired_independent_record_is_refused(self, harness: Harness) -> None:
        harness.registry.register(self._merge_grant())
        harness.independent.register(
            _grant(
                expiry=NOW - timedelta(minutes=1),
                operations=(Operation.MERGE_TO_MAIN,),
                scopes=(Capability.REPO_MERGE.value,),
                approval_ref="approval-1",
            )
        )
        decision = harness.service.authorize(_request(operation=Operation.MERGE_TO_MAIN))
        assert not decision.allowed
        assert "过期" in decision.reason

    def test_an_independent_record_that_does_not_cover_the_capability_is_refused(
        self, harness: Harness
    ) -> None:
        harness.registry.register(self._merge_grant())
        harness.independent.register(
            _grant(
                operations=(Operation.MERGE_TO_MAIN,),
                scopes=(Capability.REPO_WRITE.value,),
                approval_ref="approval-1",
            )
        )
        decision = harness.service.authorize(_request(operation=Operation.MERGE_TO_MAIN))
        assert not decision.allowed
        assert "未授予高风险能力" in decision.reason

    def test_an_independent_record_that_does_not_cover_the_operation_is_refused(
        self, harness: Harness
    ) -> None:
        """给了能力却没覆盖这个操作 —— 独立授权也得说清"允许什么操作"。"""
        harness.registry.register(self._merge_grant())
        harness.independent.register(
            _grant(
                scopes=(Capability.REPO_MERGE.value,),
                approval_ref="approval-1",
            )
        )
        decision = harness.service.authorize(_request(operation=Operation.MERGE_TO_MAIN))
        assert not decision.allowed
        assert "未覆盖操作" in decision.reason

    def test_a_service_without_an_independent_registry_refuses_high_risk(
        self, tmp_path: Path
    ) -> None:
        """没有独立册子时，高风险一律拒绝 —— 默认姿势是关着的。"""
        registry = AuthorizationRegistry()
        registry.register(
            _grant(
                operations=(Operation.MERGE_TO_MAIN,),
                scopes=(Capability.REPO_MERGE.value,),
            )
        )
        service = AuthorizationService(
            registry,
            sink=RecordingSink(),
            workspace_roots={WORKSPACE: tmp_path},
            clock=Clock(),
        )
        decision = service.authorize(_request(operation=Operation.MERGE_TO_MAIN))
        assert not decision.allowed
        assert "独立授权记录" in decision.reason

    def test_an_independent_record_of_another_task_is_not_used(self, harness: Harness) -> None:
        harness.registry.register(self._merge_grant())
        harness.independent.register(
            _grant(
                task_id=TASK_B,
                operations=(Operation.MERGE_TO_MAIN,),
                scopes=(Capability.REPO_MERGE.value,),
                approval_ref="approval-1",
            )
        )
        decision = harness.service.authorize(_request(operation=Operation.MERGE_TO_MAIN))
        assert not decision.allowed

    @pytest.mark.parametrize("capability", sorted(HIGH_RISK_CAPABILITIES))
    def test_every_high_risk_capability_needs_an_independent_record(
        self, harness: Harness, capability: Capability
    ) -> None:
        """删除、发布、合并、部署（以及改治理）一个都不能少。"""
        harness.registry.register(
            _grant(operations=(Operation.CODE_CHANGES,), scopes=(capability.value,))
        )
        decision = harness.service.authorize(_request(capability=capability))
        assert not decision.allowed
        assert "独立授权记录" in decision.reason

    def test_a_low_risk_capability_needs_no_independent_record(self, harness: Harness) -> None:
        harness.registry.register(
            _grant(
                operations=(Operation.CODE_CHANGES,),
                scopes=(Capability.REPO_WRITE.value,),
            )
        )
        decision = harness.service.authorize(_request(operation=Operation.CODE_CHANGES))
        assert decision.allowed


class TestTheTwoGates:
    """操作与能力是两道闸，都要过。"""

    def test_the_operation_must_be_in_the_allowed_operations(self, harness: Harness) -> None:
        harness.registry.register(
            _grant(
                operations=(Operation.CODE_CHANGES,),
                scopes=(Capability.REPO_WRITE.value,),
            )
        )
        decision = harness.service.authorize(_request(operation=Operation.TEST_EXECUTION))
        assert not decision.allowed
        assert "不在" in decision.reason

    def test_the_capability_must_be_in_the_granted_scopes(self, harness: Harness) -> None:
        """写了操作但没给能力 —— 不行。"""
        harness.registry.register(_grant(operations=(Operation.CODE_CHANGES,), scopes=()))
        decision = harness.service.authorize(_request(operation=Operation.CODE_CHANGES))
        assert not decision.allowed
        assert "未授予能力" in decision.reason

    def test_an_empty_grant_authorizes_nothing(self, harness: Harness) -> None:
        """范围是空的授权 = 什么都没授权（不是"什么都授权"）。"""
        harness.registry.register(_grant())
        decision = harness.service.authorize(_request(operation=Operation.CODE_CHANGES))
        assert not decision.allowed

    def test_a_capability_without_an_operation_is_enough(self, harness: Harness) -> None:
        harness.registry.register(
            _grant(scopes=(Capability.REPO_READ.value,)),
        )
        decision = harness.service.authorize(_request(capability=Capability.REPO_READ))
        assert decision.allowed

    def test_every_required_capability_must_be_granted(self, harness: Harness) -> None:
        """一次请求要行使两项能力时，两项都得有 —— 只查第一项等于没查。

        授权里给的是 ``repo:read``（按值排序时排在 ``repo:write`` 前面），
        缺的是 ``repo:write``。**只查第一项的实现会在这里放行** ——
        所以这条用例的排列是有意的：先给的那项必须恰好是排在前面的。
        """
        harness.registry.register(
            _grant(
                operations=(Operation.ANALYZE_PROJECT,),
                scopes=(Capability.REPO_READ.value,),
            )
        )
        decision = harness.service.authorize(
            _request(operation=Operation.ANALYZE_PROJECT, capability=Capability.REPO_WRITE)
        )
        assert not decision.allowed
        assert "未授予能力" in decision.reason

    def test_a_missing_capability_is_refused_whichever_way_around(self, harness: Harness) -> None:
        """缺的是排在前面的那项时，同样要拒绝。"""
        harness.registry.register(
            _grant(
                operations=(Operation.CODE_CHANGES,),
                scopes=(Capability.REPO_WRITE.value,),
            )
        )
        decision = harness.service.authorize(
            _request(operation=Operation.CODE_CHANGES, capability=Capability.REPO_DELETE)
        )
        assert not decision.allowed
        assert "未授予能力" in decision.reason


class TestTheAuditTrail:
    """每一次判定都留痕；留痕失败即阻断。"""

    def test_every_decision_is_recorded(self, harness: Harness) -> None:
        harness.registry.register(_read_grant())
        harness.service.authorize(_request(operation=Operation.ANALYZE_PROJECT))
        harness.service.authorize(_request(operation=Operation.MERGE_TO_MAIN))
        assert len(harness.sink.decisions) == 2
        assert [decision.allowed for decision in harness.sink.decisions] == [True, False]

    def test_a_failing_sink_blocks_the_operation(self, tmp_path: Path) -> None:
        """留痕写不下来 → 拒绝。审计失败就放行，等于没有审计。"""
        registry = AuthorizationRegistry()
        registry.register(_read_grant())
        service = AuthorizationService(
            registry,
            sink=BrokenSink(),
            workspace_roots={WORKSPACE: tmp_path},
            independent_registry=AuthorizationRegistry(),
            clock=Clock(),
        )
        decision = service.authorize(_request(operation=Operation.ANALYZE_PROJECT))
        assert not decision.allowed
        assert decision.audit_recorded is False
        assert "留痕失败" in decision.reason

    def test_the_record_carries_the_four_elements(self, harness: Harness) -> None:
        """主体、任务、资源、结果 —— 四个都要在。"""
        harness.registry.register(_read_grant())
        harness.service.authorize(
            _request(operation=Operation.ANALYZE_PROJECT, resource="src/main.py")
        )
        recorded = harness.sink.decisions[0]
        assert recorded.agent_id
        assert recorded.task_id
        assert recorded.resource == "src/main.py"
        assert recorded.allowed is True
        assert recorded.decided_at == NOW

    def test_require_raises_when_the_operation_is_refused(self, harness: Harness) -> None:
        harness.registry.register(_read_grant())
        with pytest.raises(AuthorizationDeniedError) as caught:
            harness.service.require(_request(operation=Operation.MERGE_TO_MAIN))
        assert caught.value.decision.allowed is False

    def test_require_returns_the_decision_when_allowed(self, harness: Harness) -> None:
        harness.registry.register(_read_grant())
        decision = harness.service.require(_request(operation=Operation.ANALYZE_PROJECT))
        assert decision.allowed


class TestTheRegistry:
    """登记表：按五元组 key 存，后发接管。"""

    def test_the_key_is_the_five_tuple(self) -> None:
        grant = _grant(expiry=NOW)
        assert grant.key == AuthorizationKey(AGENT, TASK_A, PROJECT, WORKSPACE, NOW)
        assert grant.identity == AuthorizationIdentity(AGENT, TASK_A, PROJECT, WORKSPACE)

    def test_a_record_can_be_fetched_by_its_key(self) -> None:
        registry = AuthorizationRegistry()
        grant = _grant(expiry=NOW)
        registry.register(grant)
        assert registry.get(grant.key) is grant
        assert registry.get(AuthorizationKey(AGENT, TASK_A, PROJECT, WORKSPACE, None)) is None

    def test_a_duplicate_key_is_refused(self) -> None:
        registry = AuthorizationRegistry()
        registry.register(_grant(expiry=NOW))
        with pytest.raises(ValueError, match="已存在"):
            registry.register(_grant(expiry=NOW))

    def test_the_latest_registration_governs(self) -> None:
        """后登记的接管 —— 收紧授权时旧的那条必须让位。"""
        registry = AuthorizationRegistry()
        broad = _grant(
            expiry=NOW + timedelta(hours=2),
            operations=(Operation.CODE_CHANGES,),
            scopes=(Capability.REPO_WRITE.value,),
        )
        narrow = _grant(
            expiry=NOW + timedelta(hours=1),
            operations=(Operation.CODE_CHANGES,),
            scopes=(Capability.REPO_READ.value,),
        )
        registry.register(broad)
        registry.register(narrow)
        effective = registry.effective(broad.identity)
        assert effective is narrow
        assert len(registry) == 2

    def test_an_unknown_identity_has_no_grant(self) -> None:
        registry = AuthorizationRegistry()
        assert registry.effective(AuthorizationIdentity(AGENT, TASK_A, PROJECT, WORKSPACE)) is None

    def test_the_grants_of_an_identity_are_kept_in_order(self) -> None:
        registry = AuthorizationRegistry()
        first = _grant(expiry=NOW)
        second = _grant(expiry=NOW + timedelta(hours=1))
        registry.register(first)
        registry.register(second)
        assert registry.grants_for(first.identity) == (first, second)


class TestTheModelGuardsItself:
    """模型自己的边界：空身份、没说要干什么，都不成立。"""

    @pytest.mark.parametrize("field", ["agent_id", "task_id", "project_id", "workspace_id"])
    def test_an_empty_identity_field_is_refused(self, field: str) -> None:
        values: dict[str, Any] = {
            "agent_id": AGENT,
            "task_id": TASK_A,
            "project_id": PROJECT,
            "workspace_id": WORKSPACE,
        }
        values[field] = "  "
        with pytest.raises(ValueError, match=field):
            TaskAuthorization(**values)

    def test_a_request_without_an_operation_or_capability_is_refused(self) -> None:
        with pytest.raises(ValueError, match="操作或能力"):
            _request()

    def test_a_naive_expiry_is_read_as_utc(self) -> None:
        """没带时区的到期时间按 UTC 解释 —— 换台机器结果必须一样。"""
        grant = _grant(expiry=datetime(2026, 11, 8, 12, 0))
        assert grant.expiry == NOW

    def test_the_sink_is_a_structural_protocol(self) -> None:
        """留痕去处只要有 ``record`` 就行 —— 不必继承任何基类。"""
        assert isinstance(RecordingSink(), DecisionSink)
