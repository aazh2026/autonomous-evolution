/**
 * 控制面 API 的 TypeScript 契约。
 *
 * **这份文件是前后端分离的接缝** —— 后端 `apps/api/control.py` 改一个字段，
 * 这里就得跟着改，否则 TypeScript 的类型安全就变成了"类型安全地传错字段"。
 *
 * 两条纪律：
 * 1. 字段名与后端 **逐字一致**（snake_case）。不做 camelCase 转换 ——
 *    转换层就是一层会静默丢字段的黑箱，出错时排查成本极高。
 * 2. 后端返回 `Decimal` 的地方，Pydantic v2 序列化成 **字符串**（不是数字）。
 *    `budget_spent_usd: "1.2628"` 而不是 `1.2628`。数字类型的字段一律用
 *    `number`，字符串类型的一律用 `string` —— 别把两者混起来。
 */

// ---------------------------------------------------------------------------
// 基础别名
// ---------------------------------------------------------------------------

/** 自主权等级。L0 纯手动 → L3 全自主。 */
export type AutonomyLevel = 'L0' | 'L1' | 'L2' | 'L3';

/**
 * 置信度三级视觉编码。
 *
 * 阈值由后端 `apps/api/control.py` 的 `_VERIFIED_CONFIDENCE_THRESHOLD` (0.8)
 * 与 `_REVIEW_CONFIDENCE_THRESHOLD` (0.3) 定义。前端**不重算**等级 ——
 * 后端算好了就照着用，避免两边阈值漂移。
 */
export type ConfidenceGrade = 'verified' | 'review' | 'speculative';

/** 趋势指标的数据可得性状态。 */
export type TrendStatus = 'computed' | 'no_samples' | 'baseline_missing';

// ---------------------------------------------------------------------------
// GET /api/v1/status
// ---------------------------------------------------------------------------

export interface SystemStatus {
  project_id: string;
  project_name: string;
  autonomy_level: AutonomyLevel;
  is_paused: boolean;
  active_cycle_id: string | null;
  /** 后端是 `Decimal`，Pydantic v2 序列化成字符串。 */
  budget_spent_usd: string;
  budget_daily_cap_usd: string;
  active_executors_count: number;
  max_concurrency: number;
  currency: string;
  system_health: string;
}

export interface AutonomyUpdateResponse {
  ok: boolean;
  autonomy_level: AutonomyLevel;
  is_paused: boolean;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// GET /api/v1/cycles
// ---------------------------------------------------------------------------

export interface CycleSummary {
  cycle_id: string;
  status: string;
  started_at: string;
  closed_at: string;
  attainment_delta: number;
  before_attainment: number;
  after_attainment: number;
  candidates_count: number;
  tasks_count: number;
  cost_usd: number;
}

// ---------------------------------------------------------------------------
// GET /api/v1/trends
// ---------------------------------------------------------------------------

export interface TrendMetric {
  metric_id: string;
  layer: string;
  layer_label: string;
  label: string;
  /** 诊断量（背景参考）不计入核心指标。 */
  is_diagnostic: boolean;
  status: TrendStatus;
  /** `status !== 'computed'` 时说明为什么算不出来 —— 前端要原样展示，不能吞掉。 */
  missing_reason: string | null;
  /** `null` 表示该日无样本，**不等于 0**。 */
  points: (number | null)[];
}

export interface TrendBoardResponse {
  window_days: number;
  days: string[];
  /** R-117 禁用的虚荣指标清单 —— 前端结构性拒绝渲染它们。 */
  forbidden_metrics: string[];
  metrics: TrendMetric[];
}

// ---------------------------------------------------------------------------
// GET /api/v1/candidates
// ---------------------------------------------------------------------------

/** 优先级六维分解：$Priority = (V × I × C × U) / (E × (1+R))$。 */
export interface PriorityBreakdown {
  value: number;
  impact: number;
  confidence: number;
  urgency: number;
  effort: number;
  risk: number;
}

/** 一次行内修订留下的痕迹。 */
export interface CandidateRevision {
  revised_at: string;
  reviewer: string;
  changes: Record<string, { old: unknown; new: unknown }>;
}

/** PDF 规范里的锚点 —— 前端用它定位到具体段落。 */
export interface PdfAnchor {
  page: number;
  section: string;
  /** `[x, y, width, height]`，PDF 用户坐标系。 */
  bbox: number[];
  text: string;
}

/** 源码锚点 —— 前端用它定位到具体行。 */
export interface CodeAnchor {
  file: string;
  line: number;
  snippet: string;
}

export interface Candidate {
  id: string;
  title: string;
  category: string;
  confidence: number;
  confidence_grade: ConfidenceGrade;
  evidence_ref: string;
  status: string;
  rationale: string;
  hypothesis?: string;
  expected_outcome?: string;
  priority_breakdown?: PriorityBreakdown;
  revisions?: CandidateRevision[];
  pdf_anchor?: PdfAnchor | null;
  code_anchor?: CodeAnchor | null;
  /** workflow 端点写入的字段。 */
  fix_instruction?: string;
  ignored_reason?: string;
  last_workflow?: {
    action: string;
    by: string;
    timestamp: string;
    reason: string;
  };
}

export interface CandidateUpdateResponse {
  ok: boolean;
  candidate: Candidate;
}

export interface CandidateWorkflowResponse {
  ok: boolean;
  action: string;
  message: string;
  candidate: Candidate;
}

// ---------------------------------------------------------------------------
// GET /api/v1/traceability/anchors
// ---------------------------------------------------------------------------

export interface TraceabilityAnchor {
  candidate_id: string;
  title: string;
  confidence: number;
  pdf: PdfAnchor | null;
  code: CodeAnchor | null;
}

export type TraceabilityAnchorMap = Record<string, TraceabilityAnchor>;

// ---------------------------------------------------------------------------
// GET /api/v1/decisions
// ---------------------------------------------------------------------------

export interface CriticCheck {
  question: string;
  passed: boolean;
}

export interface Decision {
  decision_id: string;
  candidate_id: string;
  action: string;
  confidence: number;
  risk: string;
  allocated_tasks: string[];
  critic_checks: CriticCheck[];
  rationale: string;
  /** 人工覆盖后才有。 */
  overridden?: {
    by: string;
    reason: string;
    previous_action: string;
    overridden_at: string;
  };
}

export interface DecisionOverrideResponse {
  ok: boolean;
  decision: Decision;
}

// ---------------------------------------------------------------------------
// GET /api/v1/tasks/dag
// ---------------------------------------------------------------------------

export type DagNodeStatus = 'COMPLETED' | 'RUNNING' | 'READY' | 'BLOCKED' | 'PENDING' | 'FAILED';

export interface DagNode {
  task_id: string;
  title: string;
  type: string;
  status: DagNodeStatus;
  priority: number;
  executor: string;
  wave: number;
  /** 级联阻断的根因任务 id。 */
  blocked_by: string | null;
  blockage_reason: string | null;
}

export interface DagEdge {
  predecessor_id: string;
  successor_id: string;
  type: string;
}

export interface DagResponse {
  nodes: DagNode[];
  edges: DagEdge[];
  /** 每个波次里的任务 id 列表。 */
  waves: string[][];
}

// ---------------------------------------------------------------------------
// GET /api/v1/verifications
// ---------------------------------------------------------------------------

export interface VerificationLayer {
  name: string;
  label: string;
  /** `passed` / `failed` / `skipped` 等。独立判定，不坍缩成布尔。 */
  result: string;
  details: string;
}

export interface VerificationRecord {
  task_id: string;
  overall_result: string;
  verified_at: string;
  diff_summary: string;
  layers: VerificationLayer[];
}

export interface RepairDispatchResponse {
  ok: boolean;
  message: string;
  repair_task_id: string;
  scope: string;
  reviewer: string;
  dispatched_at: string;
}

// ---------------------------------------------------------------------------
// GET /api/v1/governance/approvals
// ---------------------------------------------------------------------------

export interface Approval {
  request_id: string;
  task_id: string;
  operation: string;
  risk_level: string;
  required_scopes: string[];
  covered_scopes: string[];
  /** SHA-256 内容指纹锁 —— 评审时必须原样回传。 */
  content_digest: string;
  diff_summary: string;
  status: 'pending' | 'approved' | 'rejected';
  created_at: string;
  reviewed_by?: string;
  reviewed_at?: string;
  comment?: string;
}

export interface ApprovalReviewResponse {
  ok: boolean;
  approval: Approval;
}

// ---------------------------------------------------------------------------
// GET /api/v1/executors
// ---------------------------------------------------------------------------

export interface ExecutorProfile {
  name: string;
  label: string;
  is_healthy: boolean;
  current_load: number;
  max_concurrency: number;
  cost_tier: number;
  success_rate: number;
  capabilities: string[];
  affinity: string[];
}

// ---------------------------------------------------------------------------
// GET /api/v1/telemetry/events
// ---------------------------------------------------------------------------

export interface TelemetryEvent {
  seq?: number;
  timestamp: string;
  task_id: string;
  type: string;
  message: string;
}
