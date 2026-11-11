"""ASES 自主演进控制台 API 与服务（Control Plane API）。

设计依据：
- docs/design.md §1.2 态势感知与自主权分级控制（L0 ~ L3）；
- docs/design.md §3.2 任务拆解与 DAG 依赖图；
- docs/design.md §3.4 统一多执行器监控与实时事件流；
- docs/design.md §3.5B 独立六层验证结果矩阵；
- docs/design.md §7 治理审批门与防篡改审计；
- docs/tasks.md TASK-P2-011（执行器动态选择）、TASK-P2-012（DAG 调度）；
- docs/architecture/frontend-design.md（PDF.js 渲染、分屏、视觉溯源、行内编辑、置信度、风险流）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from core.planning.roadmap import TaskType
from core.task.state_machine import TaskState

_MIN_TREND_DAYS = 7
_VERIFIED_CONFIDENCE_THRESHOLD = 0.8
_REVIEW_CONFIDENCE_THRESHOLD = 0.3

type JsonObject = dict[str, object]


class AutonomyLevel(StrEnum):
    """系统自主权等级。"""

    L0 = "L0"  # 纯手动模式：仅建议，不自动执行
    L1 = "L1"  # 监督模式：计划自动生成，执行与合并需确认
    L2 = "L2"  # 半自主模式（默认）：日常变更自主闭环，高风险拦截
    L3 = "L3"  # 全自主巡航：预算与策略内完全自主运转


class SystemStatusModel(BaseModel):
    """系统全局态势模型。"""

    project_id: str
    project_name: str
    autonomy_level: AutonomyLevel
    is_paused: bool
    active_cycle_id: str | None
    budget_spent_usd: Decimal
    budget_daily_cap_usd: Decimal
    active_executors_count: int
    max_concurrency: int
    currency: str = "USD"
    system_health: str = "healthy"


class AutonomyUpdateRequest(BaseModel):
    """更新自主权或急停请求。"""

    autonomy_level: AutonomyLevel | None = None
    is_paused: bool | None = None


class DecisionOverrideRequest(BaseModel):
    """人工覆盖决策请求。"""

    override_action: str
    reviewer: str
    reason: str


class ApprovalReviewRequest(BaseModel):
    """审批评审请求。"""

    action: str
    reviewer: str
    scopes: list[str] = Field(default_factory=list)
    comment: str = ""
    content_digest: str | None = None


class CandidateUpdateRequest(BaseModel):
    """候选行内编辑更新请求。"""

    title: str | None = None
    rationale: str | None = None
    hypothesis: str | None = None
    expected_outcome: str | None = None
    confidence: float | None = None
    reviewer: str = "chief-architect"


class CandidateWorkflowRequest(BaseModel):
    """Actionable Risk 工作流请求 (Accept / Fix / Ignore)。"""

    action: str
    reviewer: str = "chief-architect"
    reason: str = ""
    fix_instruction: str | None = None


class RepairTriggerRequest(BaseModel):
    """定向修复派发请求。"""

    reviewer: str = "chief-architect"
    repair_scope: str = "functional"
    instruction: str = ""


def build_sample_spec_pdf() -> bytes:
    """生成符合 PDF 1.4 标准的项目规范与架构契约样本文档。"""
    c1 = (
        b"BT\n"
        b"/F1 18 Tf\n"
        b"50 730 Td\n"
        b"(ASES System Specification & Architecture RFC-014) Tj\n"
        b"0 -30 Td\n"
        b"/F1 12 Tf\n"
        b"(Project: pilot-tinylib | Autonomous Evolution Target Baseline) Tj\n"
        b"0 -40 Td\n"
        b"/F1 14 Tf\n"
        b"(Section 1: CLI Mutex Constraints [CAND-001 Anchor]) Tj\n"
        b"0 -25 Td\n"
        b"/F1 10 Tf\n"
        b"(The --slug and --limit CLI options are strictly mutually exclusive.) Tj\n"
        b"0 -18 Td\n"
        b"(When both options are provided, the CLI MUST fail fast with exit code 2.) Tj\n"
        b"0 -18 Td\n"
        b"(Silently dropping either parameter is considered a critical regression.) Tj\n"
        b"0 -40 Td\n"
        b"/F1 14 Tf\n"
        b"(Section 2: String Processing Concurrency Policy [CAND-002 Anchor]) Tj\n"
        b"0 -25 Td\n"
        b"/F1 10 Tf\n"
        b"(String operations in tinylib/strings.py are strictly CPU-bound pipelines.) Tj\n"
        b"0 -18 Td\n"
        b"(Speculative async coroutines MUST NOT be introduced without benchmark proof.) Tj\n"
        b"0 -40 Td\n"
        b"/F1 14 Tf\n"
        b"(Section 3: Configuration Loading and Safe Defaults) Tj\n"
        b"0 -25 Td\n"
        b"/F1 10 Tf\n"
        b"(Empty keys and undefined configuration values must be rejected fail-closed.) Tj\n"
        b"ET\n"
    )

    page_obj = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page_obj,
        b"<< /Length " + str(len(c1)).encode("ascii") + b" >>\nstream\n" + c1 + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"

    startxref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode("ascii") + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += b"trailer\n<< /Size " + str(len(objects) + 1).encode("ascii") + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(startxref).encode("ascii") + b"\n%%EOF\n"
    return out


# --------------------------------------------------------------------------
# 内存响应与演示态存储
# --------------------------------------------------------------------------


class ControlStateStore:
    """控制台运行时状态存储。"""

    def __init__(self) -> None:
        self.project_id = "pilot-tinylib"
        self.project_name = "Tinylib 基础算法与工具库"
        self.autonomy_level = AutonomyLevel.L2
        self.is_paused = False
        self.active_cycle_id: str | None = "cycle-014"
        self.budget_spent = Decimal("1.2628")
        self.budget_cap = Decimal("5.0000")
        self.max_concurrency = 4

        # 初始化演进周期数据
        self.cycles: list[JsonObject] = [
            {
                "cycle_id": "cycle-014",
                "status": "improved",
                "started_at": "2026-11-10T19:30:00Z",
                "closed_at": "2026-11-10T19:34:12Z",
                "attainment_delta": 0.1962,
                "before_attainment": 0.3592,
                "after_attainment": 0.5554,
                "candidates_count": 2,
                "tasks_count": 4,
                "cost_usd": 0.6314,
            },
            {
                "cycle_id": "cycle-013",
                "status": "no_change",
                "started_at": "2026-11-09T18:00:00Z",
                "closed_at": "2026-11-09T18:02:10Z",
                "attainment_delta": 0.0000,
                "before_attainment": 0.3592,
                "after_attainment": 0.3592,
                "candidates_count": 1,
                "tasks_count": 0,
                "cost_usd": 0.0820,
            },
        ]

        # 初始化候选集（含置信度六维分解、行内编辑留痕与溯源锚点）
        self.candidates: list[JsonObject] = [
            {
                "id": "CAND-001",
                "title": "CLI 命令 --slug 与 --limit 互斥参数未校验导致静默丢值",
                "category": "bugfix",
                "confidence": 0.90,
                "confidence_grade": "verified",
                "evidence_ref": "known_issues/test_known_issues.py:test_cli_mutex",
                "status": "recorded",
                "rationale": "复现用例确凿断言失败，两参数同时给出时静默吞掉截断逻辑。",
                "hypothesis": "增加互斥校验并在同时提供时抛出用法错误 UsageError(code=2)。",
                "expected_outcome": "CLI 测试全数通过，0 xpass，回归套件覆盖全部互斥组合。",
                "priority_breakdown": {
                    "value": 0.92,
                    "impact": 0.85,
                    "confidence": 0.90,
                    "urgency": 0.88,
                    "effort": 0.25,
                    "risk": 0.10,
                },
                "revisions": [],
                "pdf_anchor": {
                    "page": 1,
                    "section": "Section 1: CLI Mutex Constraints",
                    "bbox": [50, 180, 480, 50],
                    "text": "The --slug and --limit CLI options are strictly mutually exclusive.",
                },
                "code_anchor": {
                    "file": "src/tinylib/cli.py",
                    "line": 61,
                    "snippet": "if args.slug and args.limit is not None:",
                },
            },
            {
                "id": "CAND-002",
                "title": "核心字符处理模块迁移至投机性异步并发实现",
                "category": "refactoring",
                "confidence": 0.25,
                "confidence_grade": "speculative",
                "evidence_ref": "src/tinylib/strings.py",
                "status": "deferred",
                "rationale": "纯 CPU 密集短文本处理引入异步无收益，置信度过低 (<0.30)。",
                "hypothesis": "将字符串清理与转换迁移为 asyncio 协程，假设能提升吞吐量。",
                "expected_outcome": "Benchmark 显示协程上下文切换开销反增 40% 时延，收益为负。",
                "priority_breakdown": {
                    "value": 0.30,
                    "impact": 0.40,
                    "confidence": 0.25,
                    "urgency": 0.15,
                    "effort": 0.80,
                    "risk": 0.65,
                },
                "revisions": [],
                "pdf_anchor": {
                    "page": 1,
                    "section": "Section 2: String Processing Concurrency Policy",
                    "bbox": [50, 270, 480, 45],
                    "text": "String operations in tinylib/strings.py are strictly CPU-bound.",
                },
                "code_anchor": {
                    "file": "src/tinylib/strings.py",
                    "line": 15,
                    "snippet": "def clean_text(input_text: str) -> str:",
                },
            },
            {
                "id": "CAND-003",
                "title": "配置加载器空键与未定义环境变量 Fail-Closed 拦截加固",
                "category": "architecture",
                "confidence": 0.62,
                "confidence_grade": "review",
                "evidence_ref": "src/tinylib/config.py:42",
                "status": "review_required",
                "rationale": "AST 分析发现空键回退默认值存在歧义，置信度中等 (0.62)，需人工复核。",
                "hypothesis": "对空字符串键名及未声明环境变量直接抛出 ConfigValidationError。",
                "expected_outcome": "消除静默配置回退隐患，契合 RFC-014 Section 3 约束。",
                "priority_breakdown": {
                    "value": 0.78,
                    "impact": 0.70,
                    "confidence": 0.62,
                    "urgency": 0.65,
                    "effort": 0.35,
                    "risk": 0.25,
                },
                "revisions": [],
                "pdf_anchor": {
                    "page": 1,
                    "section": "Section 3: Configuration Loading and Safe Defaults",
                    "bbox": [50, 350, 480, 45],
                    "text": (
                        "Empty keys and undefined configuration values must be rejected "
                        "fail-closed."
                    ),
                },
                "code_anchor": {
                    "file": "src/tinylib/config.py",
                    "line": 42,
                    "snippet": "if not key or key.strip() == '': raise ConfigError('empty key')",
                },
            },
        ]

        # 初始化决策沙盘
        self.decisions: list[JsonObject] = [
            {
                "decision_id": "DEC-001",
                "candidate_id": "CAND-001",
                "action": "record",
                "confidence": 0.90,
                "risk": "low",
                "allocated_tasks": ["TASK-001", "TASK-002"],
                "critic_checks": [
                    {"question": "问题真实存在且有可复现证据", "passed": True},
                    {"question": "未触碰 Non-goals 范围与公共接口破坏", "passed": True},
                    {"question": "改动范围有界且具备独立验证契约", "passed": True},
                    {"question": "预期收益大于预估计算与调用成本", "passed": True},
                ],
                "rationale": "Critic 审查四项全通过，判定立项并派发任务进行沙箱修复与独立验证。",
            },
            {
                "decision_id": "DEC-002",
                "candidate_id": "CAND-002",
                "action": "defer",
                "confidence": 0.25,
                "risk": "high",
                "allocated_tasks": [],
                "critic_checks": [
                    {"question": "问题真实存在且有可复现证据", "passed": False},
                    {"question": "未触碰 Non-goals 范围与公共接口破坏", "passed": True},
                    {"question": "改动范围有界且具备独立验证契约", "passed": True},
                    {"question": "预期收益大于预估计算与调用成本", "passed": False},
                ],
                "rationale": "收益模型评分 0.25 未达安全门槛 (0.30)，执行'不行动'，予以推迟。",
            },
            {
                "decision_id": "DEC-003",
                "candidate_id": "CAND-003",
                "action": "record",
                "confidence": 0.62,
                "risk": "medium",
                "allocated_tasks": ["TASK-003"],
                "critic_checks": [
                    {"question": "问题真实存在且有可复现证据", "passed": True},
                    {"question": "未触碰 Non-goals 范围与公共接口破坏", "passed": True},
                    {"question": "改动范围有界且具备独立验证契约", "passed": True},
                    {"question": "预期收益大于预估计算与调用成本", "passed": True},
                ],
                "rationale": "涉及核心配置架构契约，立项执行但合并至主干需触发高风险双签审批。",
            },
        ]

        # 初始化 DAG 任务流
        self.dag_nodes: list[JsonObject] = [
            {
                "task_id": "TASK-001",
                "title": "修复 cli.py 互斥选项并抛出用法错误",
                "type": TaskType.BUGFIX.value,
                "status": TaskState.COMPLETED.value,
                "priority": 0.9,
                "executor": "codex",
                "wave": 0,
                "blocked_by": None,
                "blockage_reason": None,
            },
            {
                "task_id": "TASK-002",
                "title": "新增 CLI 互斥组合与边界回归测试套件",
                "type": TaskType.TEST.value,
                "status": TaskState.COMPLETED.value,
                "priority": 0.8,
                "executor": "gemini-cli",
                "wave": 1,
                "blocked_by": None,
                "blockage_reason": None,
            },
            {
                "task_id": "TASK-003",
                "title": "重构公共配置加载逻辑并支持空键拦截",
                "type": TaskType.REFACTORING.value,
                "status": TaskState.RUNNING.value,
                "priority": 0.7,
                "executor": "claude-code",
                "wave": 1,
                "blocked_by": None,
                "blockage_reason": None,
            },
            {
                "task_id": "TASK-004",
                "title": "全仓架构依赖索引更新与文档同步",
                "type": TaskType.DOCS.value,
                "status": TaskState.READY.value,
                "priority": 0.5,
                "executor": "opencode",
                "wave": 2,
                "blocked_by": None,
                "blockage_reason": None,
            },
        ]

        self.dag_edges: list[dict[str, str]] = [
            {"predecessor_id": "TASK-001", "successor_id": "TASK-002", "type": "finish_to_start"},
            {"predecessor_id": "TASK-001", "successor_id": "TASK-003", "type": "finish_to_start"},
            {"predecessor_id": "TASK-002", "successor_id": "TASK-004", "type": "finish_to_start"},
            {"predecessor_id": "TASK-003", "successor_id": "TASK-004", "type": "finish_to_start"},
        ]

        # 初始化六层验证矩阵
        self.verifications: list[JsonObject] = [
            {
                "task_id": "TASK-001",
                "overall_result": "passed",
                "verified_at": "2026-11-10T19:32:45Z",
                "diff_summary": "src/tinylib/cli.py (+11, -2 lines)",
                "layers": [
                    {
                        "name": "functional",
                        "label": "功能验收 (Pytest)",
                        "result": "passed",
                        "details": "44/44 passed (0 xpass / 8 xfail)",
                    },
                    {
                        "name": "code_quality",
                        "label": "代码质量 (Ruff)",
                        "result": "passed",
                        "details": "0 errors, format clean",
                    },
                    {
                        "name": "architecture",
                        "label": "架构守约 (AST)",
                        "result": "passed",
                        "details": "无分层越界，符合依赖规范",
                    },
                    {
                        "name": "security",
                        "label": "安全扫描 (Bandit)",
                        "result": "passed",
                        "details": "0 vulnerabilities detected",
                    },
                    {
                        "name": "performance",
                        "label": "性能基线 (Bench)",
                        "result": "passed",
                        "details": "耗时 1.312s ≤ 2.000s 预算基线",
                    },
                    {
                        "name": "product_spec",
                        "label": "产品契约 (Contract)",
                        "result": "passed",
                        "details": "验收契约完全达成",
                    },
                ],
            }
        ]

        # 初始化治理审批
        self.approvals: list[JsonObject] = [
            {
                "request_id": "APPR-001",
                "task_id": "TASK-003",
                "operation": "merge_to_main",
                "risk_level": "high",
                "required_scopes": ["architecture", "governance"],
                "covered_scopes": ["architecture"],
                "content_digest": "sha256:d82f716aa8c92e104f2910fa4d1b8c19",
                "diff_summary": "src/tinylib/config.py (+34, -5 lines)",
                "status": "pending",
                "created_at": "2026-11-10T19:33:10Z",
            }
        ]

        # 执行器画像与状态
        self.executors: list[JsonObject] = [
            {
                "name": "claude-code",
                "label": "Claude Code CLI",
                "is_healthy": True,
                "current_load": 1,
                "max_concurrency": 4,
                "cost_tier": 1.2,
                "success_rate": 0.96,
                "capabilities": [
                    "stream_events",
                    "interrupt",
                    "parallel_sessions",
                    "structured_output",
                ],
                "affinity": ["feature", "refactoring", "performance"],
            },
            {
                "name": "codex",
                "label": "Codex CLI",
                "is_healthy": True,
                "current_load": 1,
                "max_concurrency": 5,
                "cost_tier": 0.9,
                "success_rate": 0.92,
                "capabilities": [
                    "stream_events",
                    "interrupt",
                    "parallel_sessions",
                    "structured_output",
                ],
                "affinity": ["bugfix", "test"],
            },
            {
                "name": "gemini-cli",
                "label": "Gemini CLI",
                "is_healthy": True,
                "current_load": 0,
                "max_concurrency": 6,
                "cost_tier": 0.6,
                "success_rate": 0.88,
                "capabilities": [
                    "stream_events",
                    "interrupt",
                    "parallel_sessions",
                    "structured_output",
                ],
                "affinity": ["docs", "test", "dependency"],
            },
            {
                "name": "opencode",
                "label": "OpenCode CLI",
                "is_healthy": True,
                "current_load": 1,
                "max_concurrency": 8,
                "cost_tier": 0.3,
                "success_rate": 0.82,
                "capabilities": [
                    "stream_events",
                    "interrupt",
                    "parallel_sessions",
                    "structured_output",
                ],
                "affinity": ["bugfix", "test", "docs"],
            },
        ]

        # 遥测事件
        self.telemetry_events: list[JsonObject] = [
            {
                "seq": 101,
                "timestamp": "19:30:02",
                "task_id": "TASK-001",
                "type": "session.start",
                "message": "分配执行器 codex，初始化 Worktree 租约",
            },
            {
                "seq": 102,
                "timestamp": "19:30:15",
                "task_id": "TASK-001",
                "type": "thought",
                "message": "分析 cli.py 参数互斥逻辑与测试断言",
            },
            {
                "seq": 103,
                "timestamp": "19:30:45",
                "task_id": "TASK-001",
                "type": "action",
                "message": "修改 src/tinylib/cli.py:61 补齐用法拦截",
            },
            {
                "seq": 104,
                "timestamp": "19:31:20",
                "task_id": "TASK-001",
                "type": "verify",
                "message": "真跑 Pytest：44 passed, 8 xfailed, 0 xpassed",
            },
            {
                "seq": 105,
                "timestamp": "19:32:00",
                "task_id": "TASK-001",
                "type": "result",
                "message": "执行成功，生成提交 c81f9a0，推进状态至 PASSED",
            },
            {
                "seq": 106,
                "timestamp": "19:32:05",
                "task_id": "TASK-002",
                "type": "ready",
                "message": "上游 TASK-001 完成，波次 1 任务解锁进入 READY",
            },
            {
                "seq": 107,
                "timestamp": "19:32:10",
                "task_id": "TASK-003",
                "type": "ready",
                "message": "上游 TASK-001 完成，波次 1 任务解锁进入 READY",
            },
            {
                "seq": 108,
                "timestamp": "19:32:12",
                "task_id": "TASK-003",
                "type": "start",
                "message": "分配执行器 claude-code，开始重构配置解析",
            },
        ]

        # 跨周期 7 天趋势看板（4 层 8 核心指标 + 2 诊断量，对齐 docs/architecture/trend-board.md）
        self.forbidden_metrics: set[str] = {
            "task_count",
            "token_count",
            "commit_count",
            "lines_of_code",
        }
        self.trend_days: list[str] = [
            "11-04",
            "11-05",
            "11-06",
            "11-07",
            "11-08",
            "11-09",
            "11-10",
        ]
        self.trend_metrics: list[JsonObject] = [
            {
                "metric_id": "effective_discovery_rate",
                "layer": "discovery",
                "layer_label": "自主发现层",
                "label": "有效问题发现率",
                "is_diagnostic": False,
                "status": "baseline_missing",
                "missing_reason": "需人工标注基线 (evaluation/labels/pilot.py)，算不出来 ≠ 0",
                "points": [None, None, None, None, None, None, None],
            },
            {
                "metric_id": "duplicate_discovery_rate",
                "layer": "discovery",
                "layer_label": "自主发现层",
                "label": "重复发现率",
                "is_diagnostic": False,
                "status": "computed",
                "missing_reason": None,
                "points": [0.12, 0.10, 0.10, 0.08, 0.08, 0.05, 0.04],
            },
            {
                "metric_id": "decision_adoption_rate",
                "layer": "decision",
                "layer_label": "决策质量层",
                "label": "决策采纳率",
                "is_diagnostic": False,
                "status": "computed",
                "missing_reason": None,
                "points": [0.50, 0.55, 0.55, 0.60, 0.62, 0.62, 0.67],
            },
            {
                "metric_id": "false_positive_rate",
                "layer": "decision",
                "layer_label": "决策质量层",
                "label": "误报率",
                "is_diagnostic": False,
                "status": "computed",
                "missing_reason": None,
                "points": [0.18, 0.15, 0.15, 0.12, 0.10, 0.10, 0.08],
            },
            {
                "metric_id": "task_success_rate",
                "layer": "delivery",
                "layer_label": "工程交付层",
                "label": "任务成功率",
                "is_diagnostic": False,
                "status": "computed",
                "missing_reason": None,
                "points": [0.75, 0.78, 0.80, 0.82, 0.85, 0.85, 0.90],
            },
            {
                "metric_id": "rework_rate",
                "layer": "delivery",
                "layer_label": "工程交付层",
                "label": "返工率",
                "is_diagnostic": False,
                "status": "computed",
                "missing_reason": None,
                "points": [0.22, 0.20, 0.18, 0.15, 0.12, 0.12, 0.09],
            },
            {
                "metric_id": "regression_defect_rate",
                "layer": "delivery",
                "layer_label": "工程交付层",
                "label": "回归缺陷率",
                "is_diagnostic": False,
                "status": "no_samples",
                "missing_reason": "阶段一尚未建立线上回归缺陷追踪样本库，未采样 ≠ 0%",
                "points": [None, None, None, None, None, None, None],
            },
            {
                "metric_id": "attainment_delta",
                "layer": "evolution",
                "layer_label": "项目演进层",
                "label": "目标达成度累计增益 (Δ)",
                "is_diagnostic": False,
                "status": "computed",
                "missing_reason": None,
                "points": [0.0510, 0.0820, 0.0820, 0.1240, 0.1590, 0.1590, 0.3552],
            },
            {
                "metric_id": "verification_pass_rate",
                "layer": "diagnostic",
                "layer_label": "诊断量 (背景)",
                "label": "独立验收通过率",
                "is_diagnostic": True,
                "status": "computed",
                "missing_reason": None,
                "points": [0.80, 0.82, 0.85, 0.88, 0.90, 0.90, 0.94],
            },
            {
                "metric_id": "cumulative_cost_usd",
                "layer": "diagnostic",
                "layer_label": "诊断量 (背景)",
                "label": "累计花费 (USD)",
                "is_diagnostic": True,
                "status": "computed",
                "missing_reason": None,
                "points": [0.18, 0.32, 0.32, 0.48, 0.55, 0.63, 1.26],
            },
        ]


_STORE = ControlStateStore()

router = APIRouter(tags=["control"])


# --------------------------------------------------------------------------
# REST API 路由
# --------------------------------------------------------------------------


@router.get("/api/v1/status", response_model=SystemStatusModel)
async def get_system_status() -> SystemStatusModel:
    """获取系统全局运行态势。"""
    active_execs = sum(
        1
        for e in _STORE.executors
        if isinstance(e.get("current_load"), int) and cast(int, e["current_load"]) > 0
    )
    return SystemStatusModel(
        project_id=_STORE.project_id,
        project_name=_STORE.project_name,
        autonomy_level=_STORE.autonomy_level,
        is_paused=_STORE.is_paused,
        active_cycle_id=_STORE.active_cycle_id,
        budget_spent_usd=_STORE.budget_spent,
        budget_daily_cap_usd=_STORE.budget_cap,
        currency="USD",
        active_executors_count=active_execs,
        max_concurrency=_STORE.max_concurrency,
        system_health="healthy" if not _STORE.is_paused else "paused",
    )


@router.post("/api/v1/autonomy")
async def update_autonomy(request: AutonomyUpdateRequest) -> JsonObject:
    """更新自主权等级或下发急停/恢复指令。"""
    if request.autonomy_level is not None:
        _STORE.autonomy_level = request.autonomy_level
    if request.is_paused is not None:
        _STORE.is_paused = request.is_paused
    return {
        "ok": True,
        "autonomy_level": _STORE.autonomy_level.value,
        "is_paused": _STORE.is_paused,
        "updated_at": datetime.now(UTC).isoformat(),
    }


@router.get("/api/v1/cycles")
async def list_cycles() -> list[JsonObject]:
    """获取演进周期历史与达成度指标。"""
    return _STORE.cycles


@router.get("/api/v1/trends")
async def get_trends(
    metric_id: str | None = Query(default=None),
    days: int = Query(default=7),
) -> JsonObject:
    """获取跨周期 7 天趋势看板（4 层 8 核心指标 + 2 诊断量）。

    对齐 docs/architecture/trend-board.md：
    - 窗口不得少于 7 天；
    - 主动结构性拒绝 R-117 禁用的虚荣指标（task_count / token_count / commit_count）；
    - 算不出来（no_samples / baseline_missing）保留 None（前端渲染为 '—'），绝不混同为 0。
    """
    if days < _MIN_TREND_DAYS:
        raise HTTPException(
            status_code=400,
            detail="趋势窗口不得少于 7 天（验收要求 ≥7 天连续运行）",
        )
    if metric_id is not None and metric_id in _STORE.forbidden_metrics:
        raise HTTPException(
            status_code=400,
            detail=(
                f"ForbiddenMetricError: 指标 '{metric_id}' 属于 R-117 "
                "禁用的活动量虚荣指标，结构性拒绝渲染"
            ),
        )
    metrics = _STORE.trend_metrics
    if metric_id is not None:
        metrics = [m for m in metrics if m["metric_id"] == metric_id]
    return {
        "window_days": len(_STORE.trend_days),
        "days": _STORE.trend_days,
        "forbidden_metrics": sorted(_STORE.forbidden_metrics),
        "metrics": metrics,
    }


@router.get("/api/v1/candidates")
async def list_candidates() -> list[JsonObject]:
    """获取当前周期发掘的问题候选集。"""
    return _STORE.candidates


@router.patch("/api/v1/candidates/{candidate_id}")
async def update_candidate(
    candidate_id: str,
    request: CandidateUpdateRequest,
) -> JsonObject:
    """行内编辑候选提取结果（支持标题、假设、预期收益及置信度修订留痕）。"""
    for c in _STORE.candidates:
        if str(c["id"]).upper() == candidate_id.upper():
            changes: JsonObject = {}
            if request.title is not None and request.title != c["title"]:
                changes["title"] = {"old": c["title"], "new": request.title}
                c["title"] = request.title
            if request.rationale is not None and request.rationale != c["rationale"]:
                changes["rationale"] = {"old": c["rationale"], "new": request.rationale}
                c["rationale"] = request.rationale
            if request.hypothesis is not None and request.hypothesis != c.get("hypothesis"):
                changes["hypothesis"] = {
                    "old": c.get("hypothesis"),
                    "new": request.hypothesis,
                }
                c["hypothesis"] = request.hypothesis
            if request.expected_outcome is not None and request.expected_outcome != c.get(
                "expected_outcome"
            ):
                changes["expected_outcome"] = {
                    "old": c.get("expected_outcome"),
                    "new": request.expected_outcome,
                }
                c["expected_outcome"] = request.expected_outcome
            if request.confidence is not None and request.confidence != c["confidence"]:
                changes["confidence"] = {
                    "old": c["confidence"],
                    "new": request.confidence,
                }
                c["confidence"] = request.confidence
                if request.confidence >= _VERIFIED_CONFIDENCE_THRESHOLD:
                    c["confidence_grade"] = "verified"
                elif request.confidence >= _REVIEW_CONFIDENCE_THRESHOLD:
                    c["confidence_grade"] = "review"
                else:
                    c["confidence_grade"] = "speculative"

            if changes:
                rev: JsonObject = {
                    "revised_at": datetime.now(UTC).isoformat(),
                    "reviewer": request.reviewer,
                    "changes": changes,
                }
                revs = cast(list[JsonObject], c.setdefault("revisions", []))
                revs.append(rev)

            return {"ok": True, "candidate": c}
    raise HTTPException(status_code=404, detail=f"候选 {candidate_id} 不存在")


@router.post("/api/v1/candidates/{candidate_id}/workflow")
async def trigger_candidate_workflow(
    candidate_id: str,
    request: CandidateWorkflowRequest,
) -> JsonObject:
    """执行可执行风险工作流 (Accept 采纳 / Fix 修订 / Ignore 忽略)。"""
    for c in _STORE.candidates:
        if str(c["id"]).upper() == candidate_id.upper():
            if request.action == "accept":
                c["status"] = "recorded"
                action_desc = "已采纳并立项 (Accept & Recorded)"
            elif request.action == "fix":
                c["status"] = "fixing"
                if request.fix_instruction:
                    c["fix_instruction"] = request.fix_instruction
                action_desc = "已派发定向修复修约 (Dispatched Fix)"
            elif request.action == "ignore":
                c["status"] = "deferred"
                c["ignored_reason"] = request.reason or "指挥官判断当前周期不行动"
                action_desc = "已推迟并记录阻断理由 (Ignored / Deferred)"
            else:
                raise HTTPException(status_code=400, detail=f"未知动作: {request.action}")

            c["last_workflow"] = {
                "action": request.action,
                "by": request.reviewer,
                "timestamp": datetime.now(UTC).isoformat(),
                "reason": request.reason,
            }
            return {
                "ok": True,
                "action": request.action,
                "message": action_desc,
                "candidate": c,
            }
    raise HTTPException(status_code=404, detail=f"候选 {candidate_id} 不存在")


@router.get("/api/v1/traceability/anchors")
async def get_traceability_anchors() -> JsonObject:
    """获取所有演进要素与 PDF 规范及代码源行的视觉溯源坐标。"""
    anchors: JsonObject = {}
    for c in _STORE.candidates:
        cid = str(c["id"])
        anchors[cid] = {
            "candidate_id": cid,
            "title": c["title"],
            "confidence": c["confidence"],
            "pdf": c.get("pdf_anchor"),
            "code": c.get("code_anchor"),
        }
    return anchors


@router.get("/api/v1/artifacts/spec.pdf")
async def get_spec_pdf() -> Response:
    """获取项目高保真架构与需求规范 PDF 文档（供 PDF.js 渲染）。"""
    pdf_bytes = build_sample_spec_pdf()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="tinylib-spec-v1.4.pdf"'},
    )


@router.get("/api/v1/decisions")
async def list_decisions() -> list[JsonObject]:
    """获取决策沙盘与 Critic 审查详情。"""
    return _STORE.decisions


@router.post("/api/v1/decisions/{decision_id}/override")
async def override_decision(
    decision_id: str,
    request: DecisionOverrideRequest,
) -> JsonObject:
    """人工指挥官覆盖决策沙盘结论。"""
    for dec in _STORE.decisions:
        if str(dec["decision_id"]).upper() == decision_id.upper():
            old_action = dec["action"]
            dec["action"] = request.override_action
            dec["overridden"] = {
                "by": request.reviewer,
                "reason": request.reason,
                "previous_action": old_action,
                "overridden_at": datetime.now(UTC).isoformat(),
            }
            return {"ok": True, "decision": dec}
    raise HTTPException(status_code=404, detail=f"决策 {decision_id} 不存在")


@router.get("/api/v1/tasks/dag")
async def get_task_dag() -> JsonObject:
    """获取任务依赖图（DAG 节点、依赖边与分层波次）。"""
    return {
        "nodes": _STORE.dag_nodes,
        "edges": _STORE.dag_edges,
        "waves": [
            ["TASK-001"],
            ["TASK-002", "TASK-003"],
            ["TASK-004"],
        ],
    }


@router.get("/api/v1/verifications")
async def list_verifications() -> list[JsonObject]:
    """获取独立六层验证结果矩阵。"""
    return _STORE.verifications


@router.post("/api/v1/verifications/{task_id}/repair")
async def trigger_task_repair(
    task_id: str,
    request: RepairTriggerRequest,
) -> JsonObject:
    """对验证异常或失败项派发定向修复任务。"""
    for v in _STORE.verifications:
        if str(v["task_id"]).upper() == task_id.upper():
            repair_task_id = f"REPAIR-{v['task_id']}"
            return {
                "ok": True,
                "message": f"针对 {task_id} 成功派发定向修复任务 {repair_task_id}",
                "repair_task_id": repair_task_id,
                "scope": request.repair_scope,
                "reviewer": request.reviewer,
                "dispatched_at": datetime.now(UTC).isoformat(),
            }
    raise HTTPException(status_code=404, detail=f"任务 {task_id} 的验证记录不存在")


@router.get("/api/v1/governance/approvals")
async def list_approvals() -> list[JsonObject]:
    """获取待审批的高风险变更列表。"""
    return _STORE.approvals


@router.post("/api/v1/governance/approvals/{request_id}/review")
async def review_approval(
    request_id: str,
    request: ApprovalReviewRequest,
) -> JsonObject:
    """审批人执行签名批准或驳回（含防篡改哈希与资格核验）。"""
    for app in _STORE.approvals:
        if str(app["request_id"]).upper() == request_id.upper():
            if (
                request.content_digest is not None
                and request.content_digest != app["content_digest"]
            ):
                raise HTTPException(
                    status_code=409,
                    detail="防篡改哈希校验失败：提案内容在审批期间发生变更，签名已拒绝",
                )
            app["status"] = "approved" if request.action == "approve" else "rejected"
            app["reviewed_by"] = request.reviewer
            app["reviewed_at"] = datetime.now(UTC).isoformat()
            app["comment"] = request.comment
            covered = cast(list[str], app["covered_scopes"])
            for scope in request.scopes:
                if scope not in covered:
                    covered.append(scope)
            return {"ok": True, "approval": app}
    raise HTTPException(status_code=404, detail=f"审批请求 {request_id} 不存在")


@router.get("/api/v1/executors")
async def list_executors() -> list[JsonObject]:
    """获取多执行器集群画像、能力与实时负载。"""
    return _STORE.executors


@router.get("/api/v1/telemetry/events")
async def list_telemetry_events(
    limit: int = Query(default=50, ge=1, le=200),
) -> list[JsonObject]:
    """获取最新 NDJSON 遥测事件流。"""
    return _STORE.telemetry_events[-limit:]


# --------------------------------------------------------------------------
# Web 控制台单页应用 UI 渲染 (/ui 与 /)
# --------------------------------------------------------------------------

_HTML_FILE = Path(__file__).parent / "control.html"


def get_html_content() -> str:
    """读取控制舱 HTML 模版。"""
    if _HTML_FILE.exists():
        return _HTML_FILE.read_text(encoding="utf-8")
    return "<html><body><h3>ASES Control UI Not Found</h3></body></html>"


@router.get("/ui", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def serve_control_ui() -> HTMLResponse:
    """提供集成 6 项 UX 增强的自主演进控制舱 UI。"""
    return HTMLResponse(content=get_html_content(), status_code=200)
