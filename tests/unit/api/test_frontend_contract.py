"""前后端契约测试 —— 前端 TypeScript 契约 vs 后端真实响应。

**为什么需要这个测试。**

前后端分离之后，两边不再共享一份代码，靠一份"约定"对齐。这份约定就是
``apps/dashboard/src/types/control-plane.ts``。约定不会自己保持一致 ——
后端加一个字段没人知道，前端写错一个字段名也没人知道，直到界面上某个
数字莫名其妙变成 ``—``。

所以这里直接**把 TS 契约文件解析出来**，跟后端实际返回的 JSON 逐字段对账：

1. 后端返回了、TS 里没有的字段 → 前端会静默丢弃这份数据（契约漂移）；
2. TS 里声明了必填、后端没给的字段 → 前端会渲染成"缺失"（数据缺失）；
3. 类型对不上（TS 说 string，后端给 number）→ 前端算不出东西。

前两类是硬失败。第三类只对**明确知道是字符串的字段**断言 ——
后端 Decimal 序列化成字符串是 Pydantic v2 的既定行为，
`budget_spent_usd: "1.2628"` 而不是 ``1.2628``，前端必须按字符串处理。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from apps.api import control
from apps.api.control import AutonomyLevel, JsonObject
from apps.api.main import create_app

#: TS 契约文件。前后端分离的接缝就在这一行。
CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "apps"
    / "dashboard"
    / "src"
    / "types"
    / "control-plane.ts"
)


# ---------------------------------------------------------------------------
# TS 契约解析
# ---------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    """去掉 TS 注释 —— 注释里有分号，不能当字段分隔符。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _parse_members(body: str) -> list[str]:
    """把接口体切成成员 —— 只在括号深度 0 处认分号。"""
    members: list[str] = []
    current: list[str] = []
    depth = 0
    for char in body:
        if char in "{[(<":
            depth += 1
        elif char in "}])>":
            depth -= 1
        if char == ";" and depth == 0:
            members.append("".join(current))
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        members.append(tail)
    return members


def parse_ts_interfaces(source: str) -> dict[str, dict[str, bool]]:
    """解析 TS 接口声明。

    返回 ``{接口名: {字段名: 是否可选}}``。
    只取**顶层**字段 —— 嵌套结构由下面的 ``NESTED`` 映射显式描述，
    不做递归猜测（猜错了测试就成了摆设）。
    """
    interfaces: dict[str, dict[str, bool]] = {}
    for match in re.finditer(r"export interface (\w+)\s*\{", source):
        name = match.group(1)
        index = match.end()
        depth = 1
        body_start = index
        while index < len(source) and depth > 0:
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        body = _strip_comments(source[body_start : index - 1])

        fields: dict[str, bool] = {}
        for member in _parse_members(body):
            field = re.match(r"\s*(?:readonly\s+)?(\w+)\s*(\?)?\s*:", member)
            if field:
                fields[field.group(1)] = bool(field.group(2))
        interfaces[name] = fields
    return interfaces


@pytest.fixture(scope="module")
def contracts() -> dict[str, dict[str, bool]]:
    """TS 契约文件里的全部接口声明。"""
    assert CONTRACT_PATH.exists(), f"找不到 TS 契约文件：{CONTRACT_PATH}"
    return parse_ts_interfaces(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """测试用异步 HTTP 客户端。"""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@dataclass
class _StoreSnapshot:
    """控制台状态的一次快照。

    用 dataclass 而不是 dict —— dict 的取值类型会退化成 ``object``，
    后面 ``store.candidates[:] = snapshot["candidates"]`` 就变成了
    "往 list 里塞一个 object"，类型检查会拦下来（而且拦得对：
    快照里的列表和原列表必须是同一种类型）。
    """

    candidates: list[JsonObject]
    decisions: list[JsonObject]
    approvals: list[JsonObject]
    autonomy_level: AutonomyLevel
    is_paused: bool


@pytest.fixture
def store_guard() -> Iterator[Any]:
    """写操作前后把控制台状态还原。

    ``apps/api/control.py`` 里的 ``_STORE`` 是模块级单例，写端点直接改它。
    不还原的话，这个文件里的写测试会污染同进程里跑的其他用例
    （比如把审批签了、把候选推迟了）—— 那属于测试之间互相使坏。
    """
    store = control._STORE
    snapshot = _StoreSnapshot(
        candidates=copy.deepcopy(store.candidates),
        decisions=copy.deepcopy(store.decisions),
        approvals=copy.deepcopy(store.approvals),
        autonomy_level=store.autonomy_level,
        is_paused=store.is_paused,
    )
    yield store
    store.candidates[:] = snapshot.candidates
    store.decisions[:] = snapshot.decisions
    store.approvals[:] = snapshot.approvals
    store.autonomy_level = snapshot.autonomy_level
    store.is_paused = snapshot.is_paused


# ---------------------------------------------------------------------------
# 嵌套结构映射：字段名 → 它内部该用哪个 TS 接口
# ---------------------------------------------------------------------------

#: 一级嵌套。``字段名 → (接口名, 是数组吗)``
NESTED: dict[str, dict[str, tuple[str, bool]]] = {
    "TrendBoardResponse": {"metrics": ("TrendMetric", True)},
    "TrendMetric": {},
    "Candidate": {
        "priority_breakdown": ("PriorityBreakdown", False),
        "revisions": ("CandidateRevision", True),
        "pdf_anchor": ("PdfAnchor", False),
        "code_anchor": ("CodeAnchor", False),
    },
    "CandidateRevision": {},
    "Decision": {"critic_checks": ("CriticCheck", True)},
    "DagResponse": {
        "nodes": ("DagNode", True),
        "edges": ("DagEdge", True),
    },
    "VerificationRecord": {"layers": ("VerificationLayer", True)},
    "TraceabilityAnchor": {
        "pdf": ("PdfAnchor", False),
        "code": ("CodeAnchor", False),
    },
}

#: 明确是字符串的字段（后端 Decimal → Pydantic v2 序列化成字符串）。
#: 这些字段如果后端改成了数字，前端会算不出来 —— 必须挡住。
STRING_FIELDS: dict[str, set[str]] = {
    "SystemStatus": {"budget_spent_usd", "budget_daily_cap_usd"},
    "CycleSummary": {"cycle_id", "started_at", "closed_at"},
    "TrendMetric": {"metric_id", "layer", "layer_label", "label"},
    "Candidate": {"id", "title", "category", "evidence_ref", "status", "rationale"},
    "Decision": {"decision_id", "candidate_id", "action", "risk", "rationale"},
    "DagNode": {"task_id", "title", "type", "status", "executor"},
    "DagEdge": {"predecessor_id", "successor_id", "type"},
    "VerificationLayer": {"name", "label", "result", "details"},
    "VerificationRecord": {"task_id", "overall_result", "verified_at", "diff_summary"},
    "Approval": {
        "request_id",
        "task_id",
        "operation",
        "risk_level",
        "content_digest",
        "diff_summary",
        "status",
        "created_at",
    },
    "ExecutorProfile": {"name", "label"},
    "TelemetryEvent": {"timestamp", "task_id", "type", "message"},
    "PdfAnchor": {"section", "text"},
    "CodeAnchor": {"file", "snippet"},
    "TraceabilityAnchor": {"candidate_id", "title"},
}


def _check_object(
    payload: dict[str, Any],
    interface: str,
    contracts: dict[str, dict[str, bool]],
    path: str,
) -> None:
    """把一个 JSON 对象与一个 TS 接口逐字段对账。"""
    fields = contracts[interface]
    nested = NESTED.get(interface, {})
    strings = STRING_FIELDS.get(interface, set())

    # 1. 后端多给了字段 —— 前端会静默丢掉这份数据。
    for key in payload:
        assert key in fields, (
            f"{path}.{key} 不在 TS 契约 `{interface}` 里 —— "
            f"前端不会读它。请在 control-plane.ts 里补上，或者后端别发。"
        )

    # 2. TS 声明必填、后端没给 —— 前端会渲染成"缺失"。
    for key, optional in fields.items():
        if key not in payload:
            assert optional, f"{path} 缺少 TS 契约 `{interface}` 声明的必填字段 `{key}`。"
            continue

        value = payload[key]

        # 3. 类型对不上。
        if key in strings:
            assert isinstance(value, str), (
                f"{path}.{key} 后端返回了 {type(value).__name__}，"
                f"TS 契约声明的是 string（后端 Decimal 会序列化成字符串）。"
            )

        # 4. 嵌套结构继续往下对账。
        if key in nested:
            child_interface, is_array = nested[key]
            if value is None:
                continue
            if is_array:
                assert isinstance(value, list), f"{path}.{key} 应当是数组。"
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        _check_object(item, child_interface, contracts, f"{path}.{key}[{index}]")
            elif isinstance(value, dict):
                _check_object(value, child_interface, contracts, f"{path}.{key}")


# ---------------------------------------------------------------------------
# 逐端点对账
# ---------------------------------------------------------------------------

#: (端点, 响应类型, 顶层 TS 接口)。``list`` 表示响应是对象数组。
ENDPOINTS: list[tuple[str, str, str]] = [
    ("/api/v1/status", "object", "SystemStatus"),
    ("/api/v1/cycles", "list", "CycleSummary"),
    ("/api/v1/trends", "object", "TrendBoardResponse"),
    ("/api/v1/candidates", "list", "Candidate"),
    ("/api/v1/decisions", "list", "Decision"),
    ("/api/v1/traceability/anchors", "map", "TraceabilityAnchor"),
    ("/api/v1/tasks/dag", "object", "DagResponse"),
    ("/api/v1/verifications", "list", "VerificationRecord"),
    ("/api/v1/governance/approvals", "list", "Approval"),
    ("/api/v1/executors", "list", "ExecutorProfile"),
    ("/api/v1/telemetry/events", "list", "TelemetryEvent"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "kind", "interface"), ENDPOINTS)
async def test_api_response_matches_ts_contract(
    client: httpx.AsyncClient,
    contracts: dict[str, dict[str, bool]],
    path: str,
    kind: str,
    interface: str,
) -> None:
    """每个端点的真实响应必须与 TS 契约逐字段吻合。"""
    assert interface in contracts, f"TS 契约里没有接口 `{interface}`。"

    response = await client.get(path)
    assert response.status_code == 200, f"{path} 返回 {response.status_code}"
    payload = response.json()

    if kind == "object":
        assert isinstance(payload, dict)
        _check_object(payload, interface, contracts, path)
    elif kind == "list":
        assert isinstance(payload, list)
        assert payload, f"{path} 返回了空数组 —— 无法验证契约。"
        for index, item in enumerate(payload):
            assert isinstance(item, dict)
            _check_object(item, interface, contracts, f"{path}[{index}]")
    else:  # map
        assert isinstance(payload, dict)
        assert payload, f"{path} 返回了空对象 —— 无法验证契约。"
        for key, item in payload.items():
            assert isinstance(item, dict)
            _check_object(item, interface, contracts, f"{path}[{key}]")


@pytest.mark.asyncio
async def test_decision_ids_are_real_and_not_derivable(
    client: httpx.AsyncClient,
) -> None:
    """决策 id 必须与候选 id 一一对应 —— 前端不再靠猜。

    早期前端用 ``candId === 'CAND-001' ? 'DEC-001' : 'DEC-002'`` 推导决策
    id，结果第三个候选永远覆盖到错的决策上。这里锁死"后端给的 id 就是
    唯一的真相"，并且要求决策 id 与候选 id 的对应关系是**显式**的
    （``decision.candidate_id`` 指向真实候选）。
    """
    candidates = (await client.get("/api/v1/candidates")).json()
    decisions = (await client.get("/api/v1/decisions")).json()

    candidate_ids = {candidate["id"] for candidate in candidates}
    decision_ids = {decision["decision_id"] for decision in decisions}

    # 决策 id 不能由候选 id 机械推导（否则前端一旦沿用旧逻辑又会错位）。
    assert decision_ids - candidate_ids, "决策 id 与候选 id 完全同名 —— 无法区分两者。"

    for decision in decisions:
        assert decision["candidate_id"] in candidate_ids, (
            f"决策 {decision['decision_id']} 指向了不存在的候选 {decision['candidate_id']}。"
        )


@pytest.mark.asyncio
async def test_decimal_fields_are_serialized_as_strings(
    client: httpx.AsyncClient,
) -> None:
    """后端 Decimal 必须序列化成字符串。

    Pydantic v2 把 ``Decimal`` 序列化成字符串。前端如果按数字处理，
    ``Number.isFinite`` / ``toFixed`` 这类判断会静默失效 —— 预算条会变成
    ``NaN%``。这条一旦破了，前端得整体改一遍，所以在这儿钉死。
    """
    status = (await client.get("/api/v1/status")).json()

    assert isinstance(status["budget_spent_usd"], str)
    assert isinstance(status["budget_daily_cap_usd"], str)
    # 而且必须是能解析成数字的字符串，不是别的什么格式。
    assert float(status["budget_spent_usd"]) >= 0


# ---------------------------------------------------------------------------
# 写路径契约
#
# 读路径对了不代表写路径对 —— 早期前端把决策 id 猜错，就是写路径上的
# bug，而且读路径完全看不出来。所以写端点也要逐字段对账。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("store_guard")
async def test_autonomy_update_matches_contract(
    client: httpx.AsyncClient,
    contracts: dict[str, dict[str, bool]],
) -> None:
    """POST /autonomy 的响应要符合 ``AutonomyUpdateResponse``。"""
    response = await client.post("/api/v1/autonomy", json={"autonomy_level": "L3"})
    assert response.status_code == 200

    _check_object(response.json(), "AutonomyUpdateResponse", contracts, "/api/v1/autonomy")


@pytest.mark.asyncio
@pytest.mark.usefixtures("store_guard")
async def test_candidate_update_matches_contract(
    client: httpx.AsyncClient,
    contracts: dict[str, dict[str, bool]],
) -> None:
    """PATCH /candidates/{id} 的响应要符合 ``CandidateUpdateResponse``。"""
    response = await client.patch(
        "/api/v1/candidates/CAND-001",
        json={"title": "契约测试改过的标题", "reviewer": "contract-test"},
    )
    assert response.status_code == 200

    payload = response.json()
    _check_object(payload, "CandidateUpdateResponse", contracts, "/api/v1/candidates/CAND-001")
    # 内嵌的 candidate 也要符合 Candidate 契约。
    _check_object(
        payload["candidate"], "Candidate", contracts, "/api/v1/candidates/CAND-001.candidate"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("store_guard")
async def test_candidate_workflow_matches_contract(
    client: httpx.AsyncClient,
    contracts: dict[str, dict[str, bool]],
) -> None:
    """POST /candidates/{id}/workflow 的响应要符合 ``CandidateWorkflowResponse``。"""
    response = await client.post(
        "/api/v1/candidates/CAND-002/workflow",
        json={"action": "ignore", "reviewer": "contract-test", "reason": "契约测试"},
    )
    assert response.status_code == 200

    payload = response.json()
    _check_object(
        payload, "CandidateWorkflowResponse", contracts, "/api/v1/candidates/CAND-002/workflow"
    )
    _check_object(payload["candidate"], "Candidate", contracts, "…/workflow.candidate")


@pytest.mark.asyncio
@pytest.mark.usefixtures("store_guard")
async def test_decision_override_matches_contract(
    client: httpx.AsyncClient,
    contracts: dict[str, dict[str, bool]],
) -> None:
    """POST /decisions/{id}/override 的响应要符合 ``DecisionOverrideResponse``。

    覆盖之后后端会多写一个 ``overridden`` 字段 —— TS 契约里它是可选的，
    但**结构**必须对得上（``by`` / ``reason`` / ``previous_action`` /
    ``overridden_at``），否则前端渲染覆盖记录时会读到 undefined。
    """
    decisions = (await client.get("/api/v1/decisions")).json()
    decision_id = decisions[0]["decision_id"]

    response = await client.post(
        f"/api/v1/decisions/{decision_id}/override",
        json={
            "override_action": "record",
            "reviewer": "contract-test",
            "reason": "契约测试",
        },
    )
    assert response.status_code == 200

    payload = response.json()
    _check_object(payload, "DecisionOverrideResponse", contracts, "…/override")
    _check_object(payload["decision"], "Decision", contracts, "…/override.decision")

    overridden = payload["decision"]["overridden"]
    assert overridden["by"] == "contract-test"
    assert overridden["previous_action"] == decisions[0]["action"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("store_guard")
async def test_decision_override_404_for_unknown_id(
    client: httpx.AsyncClient,
) -> None:
    """覆盖不存在的决策必须 404，不能静默成功。

    前端靠状态码区分"覆盖成功"和"覆盖到了别的地方"。返回 200 的话，
    操作员会以为覆盖生效了，实际上什么都没发生。
    """
    response = await client.post(
        "/api/v1/decisions/DEC-999/override",
        json={"override_action": "record", "reviewer": "contract-test", "reason": "不存在"},
    )
    assert response.status_code == 404
    assert "detail" in response.json()


@pytest.mark.asyncio
@pytest.mark.usefixtures("store_guard")
async def test_approval_review_matches_contract(
    client: httpx.AsyncClient,
    contracts: dict[str, dict[str, bool]],
) -> None:
    """POST /governance/approvals/{id}/review 要符合 ``ApprovalReviewResponse``。"""
    approvals = (await client.get("/api/v1/governance/approvals")).json()
    approval = approvals[0]
    request_id = approval["request_id"]

    response = await client.post(
        f"/api/v1/governance/approvals/{request_id}/review",
        json={
            "action": "approve",
            "reviewer": "contract-test",
            "scopes": approval["required_scopes"],
            "comment": "契约测试",
            "content_digest": approval["content_digest"],
        },
    )
    assert response.status_code == 200

    payload = response.json()
    _check_object(payload, "ApprovalReviewResponse", contracts, "…/review")
    _check_object(payload["approval"], "Approval", contracts, "…/review.approval")


@pytest.mark.asyncio
@pytest.mark.usefixtures("store_guard")
async def test_repair_dispatch_matches_contract(
    client: httpx.AsyncClient,
    contracts: dict[str, dict[str, bool]],
) -> None:
    """POST /verifications/{task_id}/repair 要符合 ``RepairDispatchResponse``。"""
    verifications = (await client.get("/api/v1/verifications")).json()
    task_id = verifications[0]["task_id"]

    response = await client.post(
        f"/api/v1/verifications/{task_id}/repair",
        json={"reviewer": "contract-test", "repair_scope": "unit", "instruction": "契约测试"},
    )
    assert response.status_code == 200

    _check_object(response.json(), "RepairDispatchResponse", contracts, "…/repair")
