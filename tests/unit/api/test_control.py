"""ASES 自主演进控制台 API 与 UI 单元测试。

验证范围：
- /ui 与 / 页面 HTML 正常下发（包含控制舱核心组件与 6 项 UX 增强特性）；
- /api/v1/status 态势模型与 /api/v1/autonomy 自主权切换/急停；
- /api/v1/artifacts/spec.pdf PDF 1.4 规范生成与 /api/v1/traceability/anchors 溯源坐标；
- /api/v1/candidates 候选集置信度三级分级（verified / review / speculative）与 PATCH 行内编辑留痕；
- /api/v1/candidates/{id}/workflow 可执行风险工作流（Accept / Fix / Ignore）；
- /api/v1/decisions 决策沙盘与人工覆盖（override）审计；
- /api/v1/tasks/dag 任务依赖拓扑、波次与阻断；
- /api/v1/verifications 六层独立验证矩阵与定向修复派发（/repair）；
- /api/v1/trends 跨周期 7 天四层趋势看板、诚实缺失态（None）与 R-117 禁用量结构性拦截；
- /api/v1/governance/approvals 高风险审批法庭 SHA-256 防篡改哈希核验与签名放行；
- /api/v1/executors 执行器集群画像与并发负载；
- /api/v1/telemetry/events 实时 NDJSON 遥测流。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from apps.api.main import create_app

#: 指一条不存在的目录 —— 强制走"内嵌旧控制舱"的降级分支。
#:
#: 前端现在是独立构建的 SPA（``apps/dashboard/dist``）。本地跑过
#: ``make dashboard-build`` 之后那个目录就存在了，根路径会变成新 SPA ——
#: 那会让"内嵌界面长什么样"的断言变得**取决于谁最后构建过**。
#: 所以老界面的测试显式指着一条不存在的路径，两边互不干扰。
MISSING_DIST = Path("/tmp/ases-no-such-dashboard-dist")


@pytest.fixture
def client() -> httpx.AsyncClient:
    """测试用异步 HTTP 客户端。"""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def legacy_client() -> httpx.AsyncClient:
    """强制走降级分支（前端没构建）的客户端。"""
    app = create_app(dashboard_dist=MISSING_DIST)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_ui_page_rendering_and_ux_features(legacy_client: httpx.AsyncClient) -> None:
    """验证 /ui 与 / 渲染 HTML 页面且包含全部 6 项 UX 优化特性与核心看板。"""
    res_ui = await legacy_client.get("/ui")
    assert res_ui.status_code == 200
    assert "text/html" in res_ui.headers["content-type"]
    html = res_ui.text

    # 基础模块
    assert "ASES 控制舱" in html
    assert "双环态势与目标达成" in html
    assert "DAG 多任务调度流" in html

    # 1. PDF.js 高保真文档渲染
    assert "pdf.min.js" in html
    assert "pdfCanvas" in html
    assert "pdfBoundingBox" in html

    # 2. 可拖拽分屏面板与焦点模式
    assert "dragGutterCol" in html
    assert "dragGutterRow" in html
    assert "toggleFocusMode" in html

    # 3. 视觉溯源连线 (SVG Overlay)
    assert "traceabilityOverlay" in html
    assert "traceLinesContainer" in html
    assert "triggerTraceability" in html

    # 4. 行内编辑校准 (Inline Editing)
    assert "enableInlineEdit" in html
    assert "saveInlineEdit" in html

    # 5. 置信度分级视觉系统
    assert "Verified" in html
    assert "Review" in html
    assert "Speculative" in html

    # 6. 可执行风险工作流 (Accept / Fix / Ignore)
    assert "handleWorkflow" in html
    assert "采纳 (Accept)" in html
    assert "修订 (Fix)" in html
    assert "推迟 (Ignore)" in html

    # 跨周期 7 天四层累计趋势看板
    assert "Four-Layer Cumulative Trend Board" in html
    assert "R-117" in html

    res_root = await legacy_client.get("/")
    assert res_root.status_code == 200
    assert "ASES 控制舱" in res_root.text


@pytest.mark.asyncio
async def test_system_status_and_autonomy(client: httpx.AsyncClient) -> None:
    """验证系统态势查询与自主权调整。"""
    res = await client.get("/api/v1/status")
    assert res.status_code == 200
    data = res.json()
    assert data["project_id"] == "pilot-tinylib"
    assert data["autonomy_level"] in ["L0", "L1", "L2", "L3"]
    assert "budget_spent_usd" in data

    # 切换至 L3 全自主
    res_update = await client.post(
        "/api/v1/autonomy",
        json={"autonomy_level": "L3", "is_paused": False},
    )
    assert res_update.status_code == 200
    assert res_update.json()["autonomy_level"] == "L3"

    # 验证态势已更新
    res_after = await client.get("/api/v1/status")
    assert res_after.json()["autonomy_level"] == "L3"

    # 复原为 L2
    await client.post("/api/v1/autonomy", json={"autonomy_level": "L2"})


@pytest.mark.asyncio
async def test_pdf_artifact_and_traceability_anchors(client: httpx.AsyncClient) -> None:
    """验证 PDF 规范文档下载与视觉溯源坐标映射。"""
    res_pdf = await client.get("/api/v1/artifacts/spec.pdf")
    assert res_pdf.status_code == 200
    assert res_pdf.headers["content-type"] == "application/pdf"
    assert res_pdf.content.startswith(b"%PDF-1.4")
    assert b"RFC-014" in res_pdf.content

    res_anchors = await client.get("/api/v1/traceability/anchors")
    assert res_anchors.status_code == 200
    anchors = res_anchors.json()
    assert "CAND-001" in anchors
    assert "CAND-002" in anchors
    assert "CAND-003" in anchors
    assert anchors["CAND-001"]["pdf"]["page"] == 1
    assert len(anchors["CAND-001"]["pdf"]["bbox"]) == 4
    assert anchors["CAND-001"]["code"]["file"] == "src/tinylib/cli.py"


@pytest.mark.asyncio
async def test_candidates_confidence_grading_and_inline_editing(
    client: httpx.AsyncClient,
) -> None:
    """验证候选集三级置信度分级与 PATCH 行内编辑修订留痕。"""
    res_cand = await client.get("/api/v1/candidates")
    assert res_cand.status_code == 200
    candidates = res_cand.json()
    assert len(candidates) >= 3

    grades = {c["id"]: c["confidence_grade"] for c in candidates}
    assert grades["CAND-001"] == "verified"
    assert grades["CAND-002"] == "speculative"
    assert grades["CAND-003"] == "review"

    # 行内编辑 CAND-003 的标题、假设、预期收益与置信度
    res_patch = await client.patch(
        "/api/v1/candidates/CAND-003",
        json={
            "title": "配置加载器空键与未定义变量 Fail-Closed 强校验",
            "hypothesis": "空键直接抛出 ConfigValidationError",
            "expected_outcome": "100% 阻断空键配置注入",
            "confidence": 0.85,
            "reviewer": "chief-architect",
        },
    )
    assert res_patch.status_code == 200
    updated = res_patch.json()["candidate"]
    assert updated["title"] == "配置加载器空键与未定义变量 Fail-Closed 强校验"
    assert updated["confidence"] == 0.85
    assert updated["confidence_grade"] == "verified"
    assert len(updated["revisions"]) >= 1
    assert updated["revisions"][-1]["reviewer"] == "chief-architect"


@pytest.mark.asyncio
async def test_actionable_risk_workflows_and_decisions_override(
    client: httpx.AsyncClient,
) -> None:
    """验证 Accept / Fix / Ignore 风险工作流与决策沙盘人工覆盖。"""
    # 1. 对 CAND-003 执行 Fix 修订工作流
    res_fix = await client.post(
        "/api/v1/candidates/CAND-003/workflow",
        json={
            "action": "fix",
            "reviewer": "chief-architect",
            "fix_instruction": "补充环境变量白名单断言",
        },
    )
    assert res_fix.status_code == 200
    assert res_fix.json()["candidate"]["status"] == "fixing"
    assert res_fix.json()["candidate"]["fix_instruction"] == "补充环境变量白名单断言"

    # 2. 对 CAND-003 执行 Accept 采纳立项工作流
    res_acc = await client.post(
        "/api/v1/candidates/CAND-003/workflow",
        json={"action": "accept", "reviewer": "chief-architect"},
    )
    assert res_acc.status_code == 200
    assert res_acc.json()["candidate"]["status"] == "recorded"

    # 3. 对 CAND-002 执行 Ignore 推迟工作流
    res_ign = await client.post(
        "/api/v1/candidates/CAND-002/workflow",
        json={
            "action": "ignore",
            "reviewer": "chief-architect",
            "reason": "纯 CPU 短文本无需异步化",
        },
    )
    assert res_ign.status_code == 200
    assert res_ign.json()["candidate"]["status"] == "deferred"

    # 4. 人工指挥官覆盖 DEC-002
    res_over = await client.post(
        "/api/v1/decisions/DEC-002/override",
        json={
            "override_action": "record",
            "reviewer": "chief-architect",
            "reason": "强制探索异步重构分支",
        },
    )
    assert res_over.status_code == 200
    over_data = res_over.json()
    assert over_data["ok"] is True
    assert over_data["decision"]["action"] == "record"
    assert over_data["decision"]["overridden"]["by"] == "chief-architect"


@pytest.mark.asyncio
async def test_task_dag_verifications_and_repair(client: httpx.AsyncClient) -> None:
    """验证任务 DAG 拓扑、独立六层验证矩阵与定向修复派发。"""
    res_dag = await client.get("/api/v1/tasks/dag")
    assert res_dag.status_code == 200
    dag_data = res_dag.json()
    assert len(dag_data["waves"]) == 3

    res_veri = await client.get("/api/v1/verifications")
    assert res_veri.status_code == 200
    verifications = res_veri.json()
    assert len(verifications) >= 1
    v1 = verifications[0]
    assert v1["overall_result"] == "passed"
    assert len(v1["layers"]) == 6

    # 派发定向修复
    res_rep = await client.post(
        "/api/v1/verifications/TASK-001/repair",
        json={
            "reviewer": "chief-architect",
            "repair_scope": "architecture",
            "instruction": "加固 CLI 边界检查",
        },
    )
    assert res_rep.status_code == 200
    assert res_rep.json()["repair_task_id"] == "REPAIR-TASK-001"


@pytest.mark.asyncio
async def test_four_layer_trends_and_forbidden_metrics(
    client: httpx.AsyncClient,
) -> None:
    """验证跨周期 7 天四层趋势看板、诚实缺失态（None）与 R-117 禁用量拦截。"""
    res_trends = await client.get("/api/v1/trends")
    assert res_trends.status_code == 200
    data = res_trends.json()
    assert data["window_days"] == 7
    assert len(data["days"]) == 7
    assert len(data["metrics"]) == 10  # 8 核心指标 + 2 诊断量

    by_id = {m["metric_id"]: m for m in data["metrics"]}
    # 诚实缺失态：算不出来 ≠ 0
    assert by_id["effective_discovery_rate"]["status"] == "baseline_missing"
    assert all(pt is None for pt in by_id["effective_discovery_rate"]["points"])
    assert by_id["regression_defect_rate"]["status"] == "no_samples"
    assert all(pt is None for pt in by_id["regression_defect_rate"]["points"])

    # 窗口少于 7 天拒绝
    res_short = await client.get("/api/v1/trends?days=5")
    assert res_short.status_code == 400

    # R-117 禁用的虚荣指标结构性拒绝
    for forbidden in ("task_count", "token_count", "commit_count"):
        res_forbid = await client.get(f"/api/v1/trends?metric_id={forbidden}")
        assert res_forbid.status_code == 400
        assert "ForbiddenMetricError" in res_forbid.json()["detail"]


@pytest.mark.asyncio
async def test_governance_approvals_review_and_tamper_protection(
    client: httpx.AsyncClient,
) -> None:
    """验证高风险审批法庭 SHA-256 防篡改核验与签名放行。"""
    res_apps = await client.get("/api/v1/governance/approvals")
    assert res_apps.status_code == 200
    approvals = res_apps.json()
    assert len(approvals) >= 1
    appr = approvals[0]
    req_id = appr["request_id"]
    valid_digest = appr["content_digest"]

    # 1. 伪造或篡改后的哈希应返回 409 Conflict
    res_tampered = await client.post(
        f"/api/v1/governance/approvals/{req_id}/review",
        json={
            "action": "approve",
            "reviewer": "security-officer",
            "scopes": ["architecture", "governance"],
            "content_digest": "sha256:00000000000000000000000000000000",
        },
    )
    assert res_tampered.status_code == 409

    # 2. 正确哈希与双轴资格签名放行
    res_rev = await client.post(
        f"/api/v1/governance/approvals/{req_id}/review",
        json={
            "action": "approve",
            "reviewer": "security-officer",
            "scopes": ["architecture", "governance"],
            "comment": "架构师及合规员联签同意",
            "content_digest": valid_digest,
        },
    )
    assert res_rev.status_code == 200
    reviewed = res_rev.json()["approval"]
    assert reviewed["status"] == "approved"
    assert reviewed["reviewed_by"] == "security-officer"
    assert "governance" in reviewed["covered_scopes"]


@pytest.mark.asyncio
async def test_executors_and_telemetry(client: httpx.AsyncClient) -> None:
    """验证多执行器集群画像与遥测事件流。"""
    res_exec = await client.get("/api/v1/executors")
    assert res_exec.status_code == 200
    executors = res_exec.json()
    assert len(executors) >= 4
    names = {e["name"] for e in executors}
    assert {"claude-code", "codex", "gemini-cli", "opencode"}.issubset(names)

    res_events = await client.get("/api/v1/telemetry/events?limit=5")
    assert res_events.status_code == 200
    events = res_events.json()
    assert len(events) <= 5
    assert all("task_id" in ev for ev in events)
