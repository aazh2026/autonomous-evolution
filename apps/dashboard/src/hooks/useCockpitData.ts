/**
 * 控制舱数据装载。
 *
 * **三个设计取舍，写下来是为了后来的人别改回去：**
 *
 * 1. **局部失败不拖垮全局。** 用 `Promise.allSettled` 而不是 `Promise.all` ——
 *    趋势端点返回 400（比如有人请求了被 R-117 禁用的指标）不应该让整个
 *    控制舱变成白屏。哪个端点挂了就在状态里记哪一项，其余照常显示。
 *
 * 2. **首屏加载与后台刷新分开。** `loading` 只在第一次装载时为 `true`。
 *    每 3 秒轮询一次，如果轮询也把界面切回加载态，控制舱会一直闪。
 *
 * 3. **卸载时中止在途请求。** `AbortController` 随 effect 清理一起销毁，
 *    避免组件卸载后 setState 触发 React 的内存泄漏警告。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiError,
  fetchApprovals,
  fetchCandidates,
  fetchCycles,
  fetchDecisions,
  fetchExecutors,
  fetchSystemStatus,
  fetchTaskDag,
  fetchTelemetryEvents,
  fetchTrends,
  fetchVerifications,
} from '../api/client.ts';
import type {
  Approval,
  Candidate,
  CycleSummary,
  DagResponse,
  Decision,
  ExecutorProfile,
  SystemStatus,
  TelemetryEvent,
  TrendBoardResponse,
  VerificationRecord,
} from '../types/control-plane.ts';

export interface CockpitData {
  status: SystemStatus | null;
  cycles: CycleSummary[];
  trends: TrendBoardResponse | null;
  candidates: Candidate[];
  decisions: Decision[];
  dag: DagResponse;
  verifications: VerificationRecord[];
  approvals: Approval[];
  executors: ExecutorProfile[];
  events: TelemetryEvent[];
}

export interface CockpitState {
  data: CockpitData;
  /** 首次装载中（不是后台刷新）。 */
  loading: boolean;
  /** 整体错误 —— 只有连态势都拿不到才算整体失败。 */
  error: string | null;
  /** 分端点的错误，键是端点名。用于在对应视图上局部提示。 */
  partialErrors: Record<string, string>;
  refresh: () => Promise<void>;
}

const EMPTY_DATA: CockpitData = {
  status: null,
  cycles: [],
  trends: null,
  candidates: [],
  decisions: [],
  dag: { nodes: [], edges: [], waves: [] },
  verifications: [],
  approvals: [],
  executors: [],
  events: [],
};

/** 后台轮询间隔（毫秒）。 */
const FAST_POLL_MS = 3000;
const SLOW_POLL_MS = 15000;

/** 把任意异常变成一句能给人看的话。 */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

type Fetcher = (signal: AbortSignal) => Promise<unknown>;

interface EndpointSpec {
  key: keyof CockpitData;
  fetch: Fetcher;
}

/** 一次全量装载要拉的端点。 */
const FULL_LOAD: EndpointSpec[] = [
  { key: 'status', fetch: (s) => fetchSystemStatus(s) },
  { key: 'cycles', fetch: (s) => fetchCycles(s) },
  { key: 'trends', fetch: (s) => fetchTrends({}, s) },
  { key: 'candidates', fetch: (s) => fetchCandidates(s) },
  { key: 'decisions', fetch: (s) => fetchDecisions(s) },
  { key: 'dag', fetch: (s) => fetchTaskDag(s) },
  { key: 'verifications', fetch: (s) => fetchVerifications(s) },
  { key: 'approvals', fetch: (s) => fetchApprovals(s) },
  { key: 'executors', fetch: (s) => fetchExecutors(s) },
];

/** 快轮询只拉会频繁变的两项。 */
const FAST_LOAD: EndpointSpec[] = [
  { key: 'status', fetch: (s) => fetchSystemStatus(s) },
  { key: 'events', fetch: (s) => fetchTelemetryEvents(50, s) },
];

export function useCockpitData(): CockpitState {
  const [data, setData] = useState<CockpitData>(EMPTY_DATA);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [partialErrors, setPartialErrors] = useState<Record<string, string>>({});

  /** 首个已经装载完成的标记 —— 用来区分首屏加载与后台刷新。 */
  const loadedRef = useRef(false);
  /** 当前在途的请求，卸载时中止。 */
  const abortRef = useRef<AbortController | null>(null);

  const load = useCallback(async (endpoints: EndpointSpec[]) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const settled = await Promise.allSettled(
      endpoints.map((spec) => spec.fetch(controller.signal))
    );

    if (controller.signal.aborted) return;

    const updates: Partial<CockpitData> = {};
    const errors: Record<string, string> = {};

    settled.forEach((result, idx) => {
      const spec = endpoints[idx];
      if (!spec) return;
      if (result.status === 'fulfilled') {
        (updates as Record<string, unknown>)[spec.key] = result.value;
      } else {
        errors[spec.key] = describeError(result.reason);
      }
    });

    setData((prev) => ({ ...prev, ...updates }));
    setPartialErrors(errors);

    // 整体失败 = 连系统态势都拿不到。其他端点挂了不算整体失败。
    const statusError = errors.status;
    setError(statusError ?? null);

    if (!loadedRef.current) {
      loadedRef.current = true;
      setLoading(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    await load(FULL_LOAD);
  }, [load]);

  // 首屏全量装载。
  useEffect(() => {
    void load(FULL_LOAD);
    return () => {
      abortRef.current?.abort();
    };
  }, [load]);

  // 快轮询：态势与遥测。
  useEffect(() => {
    const timer = setInterval(() => {
      void load(FAST_LOAD);
    }, FAST_POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  // 慢轮询：其余业务数据（决策、审批、DAG…）。
  useEffect(() => {
    const timer = setInterval(() => {
      void load(FULL_LOAD);
    }, SLOW_POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  return { data, loading, error, partialErrors, refresh };
}
