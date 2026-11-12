/**
 * 控制面 API 客户端。
 *
 * 这是前端与后端之间**唯一**的耦合点 —— 组件不直接碰 `fetch`，全部经过这里。
 * 好处很实在：换后端地址、加重试、加鉴权头、改错误语义，都只改这一个文件。
 *
 * **基址怎么来的**
 * - 开发态：默认走 `/api/v1` 相对路径，由 Vite 代理转发到 `localhost:8000`
 *   （见 `vite.config.ts`）。前后端同源，不需要 CORS。
 * - 分离部署（前端独立域名/端口）：用 `VITE_API_BASE_URL` 指到后端绝对地址，
 *   这时才需要后端的 CORS 放行（见 `ASES_CORS_ALLOWED_ORIGINS`）。
 */

import type {
  Approval,
  ApprovalReviewResponse,
  AutonomyLevel,
  AutonomyUpdateResponse,
  Candidate,
  CandidateUpdateResponse,
  CandidateWorkflowResponse,
  CycleSummary,
  DagResponse,
  Decision,
  DecisionOverrideResponse,
  ExecutorProfile,
  RepairDispatchResponse,
  SystemStatus,
  TelemetryEvent,
  TraceabilityAnchorMap,
  TrendBoardResponse,
  VerificationRecord,
} from '../types/control-plane.ts';

const DEFAULT_BASE_URL = '/api/v1';

/**
 * API 基址。
 *
 * 末尾的斜杠在这里统一去掉 —— 后端路由是 `/api/v1/status` 这种形式，
 * 基址带不带尾斜杠拼出来的 URL 不一样，在这里抹平，调用方不用操心。
 */
export const API_BASE_URL: string = (
  import.meta.env.VITE_API_BASE_URL ?? DEFAULT_BASE_URL
).replace(/\/+$/, '');

/**
 * API 调用失败。
 *
 * 带上 HTTP 状态码与后端返回的 `detail` —— FastAPI 的错误体是
 * `{"detail": "..."}`，直接透传给用户看比"请求失败"有用得多。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }

  /** 4xx 是"请求本身有问题"，5xx 是"服务端的问题" —— UI 上的处理方式不同。 */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
}

/** 从 FastAPI 的错误体里掏出人话。 */
async function readErrorDetail(res: Response): Promise<string> {
  try {
    const payload: unknown = await res.json();
    if (
      typeof payload === 'object' &&
      payload !== null &&
      'detail' in payload &&
      typeof (payload as { detail: unknown }).detail === 'string'
    ) {
      return (payload as { detail: string }).detail;
    }
  } catch {
    // 响应体不是 JSON（比如 nginx 返的 HTML 502 页面）—— 落到下面的兜底。
  }
  return `${res.status} ${res.statusText || '请求失败'}`;
}

/**
 * 发一次请求并解析 JSON。
 *
 * `res.ok` 为假时抛 `ApiError`，**不是**返回 `null` —— 调用方要么处理异常，
 * 要么显式 `.catch()`。返回 `null` 会让"后端挂了"和"后端返回了空"混成一样，
 * 排查时看不出区别。
 */
async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal } = options;

  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (err) {
    // 网络层失败（后端没起、断网、CORS 拦截）。Fetch 在这里抛 TypeError。
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw err;
    }
    throw new ApiError(0, `无法连接后端 (${API_BASE_URL})：${(err as Error).message}`);
  }

  if (!res.ok) {
    throw new ApiError(res.status, await readErrorDetail(res));
  }

  return (await res.json()) as T;
}

/** 取二进制（PDF 规范文档）。 */
async function requestBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const res = await fetch(`${API_BASE_URL}${path}`, { signal });
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorDetail(res));
  }
  return res.blob();
}

// ---------------------------------------------------------------------------
// 态势
// ---------------------------------------------------------------------------

export function fetchSystemStatus(signal?: AbortSignal): Promise<SystemStatus> {
  return request<SystemStatus>('/status', { signal });
}

export function updateAutonomy(payload: {
  autonomy_level?: AutonomyLevel;
  is_paused?: boolean;
}): Promise<AutonomyUpdateResponse> {
  return request<AutonomyUpdateResponse>('/autonomy', { method: 'POST', body: payload });
}

export function fetchCycles(signal?: AbortSignal): Promise<CycleSummary[]> {
  return request<CycleSummary[]>('/cycles', { signal });
}

// ---------------------------------------------------------------------------
// 趋势看板
// ---------------------------------------------------------------------------

/**
 * 拉取跨周期趋势。
 *
 * 后端对 R-117 禁用的虚荣指标（`task_count` / `token_count` / …）会直接
 * 返回 400。前端**不主动请求**它们 —— 但万一有人传了 `metricId`，
 * `ApiError` 会把后端那句"结构性拒绝"原样带出来。
 */
export function fetchTrends(
  params: { metricId?: string; days?: number } = {},
  signal?: AbortSignal
): Promise<TrendBoardResponse> {
  const query = new URLSearchParams();
  if (params.metricId) query.set('metric_id', params.metricId);
  if (params.days !== undefined) query.set('days', String(params.days));
  const suffix = query.size > 0 ? `?${query.toString()}` : '';
  return request<TrendBoardResponse>(`/trends${suffix}`, { signal });
}

// ---------------------------------------------------------------------------
// 候选与决策
// ---------------------------------------------------------------------------

export function fetchCandidates(signal?: AbortSignal): Promise<Candidate[]> {
  return request<Candidate[]>('/candidates', { signal });
}

/** 行内编辑候选字段（标题 / 假设 / 预期收益 / 置信度）。后端留痕。 */
export function updateCandidate(
  candidateId: string,
  payload: {
    title?: string;
    rationale?: string;
    hypothesis?: string;
    expected_outcome?: string;
    confidence?: number;
    reviewer?: string;
  }
): Promise<CandidateUpdateResponse> {
  return request<CandidateUpdateResponse>(`/candidates/${candidateId}`, {
    method: 'PATCH',
    body: payload,
  });
}

/** Actionable Risk 工作流：Accept（采纳）/ Fix（定向修复）/ Ignore（推迟）。 */
export function triggerCandidateWorkflow(
  candidateId: string,
  payload: {
    action: 'accept' | 'fix' | 'ignore';
    reviewer?: string;
    reason?: string;
    fix_instruction?: string;
  }
): Promise<CandidateWorkflowResponse> {
  return request<CandidateWorkflowResponse>(`/candidates/${candidateId}/workflow`, {
    method: 'POST',
    body: payload,
  });
}

export function fetchDecisions(signal?: AbortSignal): Promise<Decision[]> {
  return request<Decision[]>('/decisions', { signal });
}

/**
 * 人工覆盖决策沙盘结论。
 *
 * **参数是 `decision_id`，不是 `candidate_id`。** 两者不是一回事 ——
 * 候选是"发现了什么问题"，决策是"对它下了什么裁决"。一个候选对应一条决策，
 * 但前端不能靠候选 id 猜决策 id（早期版本就是这么干的，结果第三个候选
 * 永远覆盖到错误的决策上）。调用方先 `fetchDecisions()` 拿到真实映射。
 */
export function overrideDecision(
  decisionId: string,
  payload: { override_action: string; reviewer: string; reason: string }
): Promise<DecisionOverrideResponse> {
  return request<DecisionOverrideResponse>(`/decisions/${decisionId}/override`, {
    method: 'POST',
    body: payload,
  });
}

export function fetchTraceabilityAnchors(signal?: AbortSignal): Promise<TraceabilityAnchorMap> {
  return request<TraceabilityAnchorMap>('/traceability/anchors', { signal });
}

/** 项目规范 PDF（供 PDF.js 渲染高保真证据）。 */
export function fetchSpecPdf(signal?: AbortSignal): Promise<Blob> {
  return requestBlob('/artifacts/spec.pdf', signal);
}

// ---------------------------------------------------------------------------
// DAG 与验证
// ---------------------------------------------------------------------------

export function fetchTaskDag(signal?: AbortSignal): Promise<DagResponse> {
  return request<DagResponse>('/tasks/dag', { signal });
}

export function fetchVerifications(signal?: AbortSignal): Promise<VerificationRecord[]> {
  return request<VerificationRecord[]>('/verifications', { signal });
}

/** 针对失败验证层派发定向修复任务。 */
export function dispatchRepair(
  taskId: string,
  payload: { reviewer?: string; repair_scope?: string; instruction?: string } = {}
): Promise<RepairDispatchResponse> {
  return request<RepairDispatchResponse>(`/verifications/${taskId}/repair`, {
    method: 'POST',
    body: payload,
  });
}

// ---------------------------------------------------------------------------
// 治理审批
// ---------------------------------------------------------------------------

export function fetchApprovals(signal?: AbortSignal): Promise<Approval[]> {
  return request<Approval[]>('/governance/approvals', { signal });
}

/**
 * 审批签名放行 / 驳回。
 *
 * **`contentDigest` 必须原样回传** —— 后端会比对它与提案内容指纹是否仍然
 * 一致，不一致就返回 409 拒绝签名（防"批后偷改内容"）。前端拿到的是
 * `sha256:xxx` 字符串，别自己重新算一遍。
 */
export function reviewApproval(
  requestId: string,
  payload: {
    action: 'approve' | 'reject';
    reviewer: string;
    scopes?: string[];
    comment?: string;
    content_digest?: string;
  }
): Promise<ApprovalReviewResponse> {
  return request<ApprovalReviewResponse>(`/governance/approvals/${requestId}/review`, {
    method: 'POST',
    body: payload,
  });
}

// ---------------------------------------------------------------------------
// 执行器与遥测
// ---------------------------------------------------------------------------

export function fetchExecutors(signal?: AbortSignal): Promise<ExecutorProfile[]> {
  return request<ExecutorProfile[]>('/executors', { signal });
}

export function fetchTelemetryEvents(
  limit = 50,
  signal?: AbortSignal
): Promise<TelemetryEvent[]> {
  return request<TelemetryEvent[]>(`/telemetry/events?limit=${limit}`, { signal });
}
