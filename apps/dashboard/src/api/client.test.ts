/**
 * API 客户端测试。
 *
 * 这里守的是**前后端分离的接缝**。前端所有数据都从这里出去，所以这里的
 * 行为一旦变了，整个控制舱跟着变 —— 而且是那种"看起来还在跑"的变化。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  API_BASE_URL,
  fetchApprovals,
  fetchCandidates,
  fetchDecisions,
  fetchSystemStatus,
  fetchTelemetryEvents,
  fetchTrends,
  overrideDecision,
  reviewApproval,
  updateAutonomy,
} from './client.ts';

/** 造一个可控的 fetch 桩。 */
function mockFetch(response: unknown, ok = true, status = 200) {
  const fetchMock = vi.fn(
    async (_input: RequestInfo | URL, _init?: RequestInit): Promise<unknown> => ({
      ok,
      status,
      statusText: status === 200 ? 'OK' : 'Error',
      json: async () => response,
    })
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('API_BASE_URL', () => {
  it('默认是相对路径 /api/v1（开发态走 Vite 代理，前后端同源）', () => {
    expect(API_BASE_URL).toBe('/api/v1');
  });
});

describe('读取端点', () => {
  beforeEach(() => {
    mockFetch({ project_id: 'pilot-tinylib', autonomy_level: 'L2' });
  });

  it('状态走 /api/v1/status', async () => {
    await fetchSystemStatus();
    expect(fetch).toHaveBeenCalledWith('/api/v1/status', expect.any(Object));
  });

  it('候选走 /api/v1/candidates', async () => {
    await fetchCandidates();
    expect(fetch).toHaveBeenCalledWith('/api/v1/candidates', expect.any(Object));
  });

  it('决策走 /api/v1/decisions', async () => {
    await fetchDecisions();
    expect(fetch).toHaveBeenCalledWith('/api/v1/decisions', expect.any(Object));
  });

  it('审批走 /api/v1/governance/approvals', async () => {
    await fetchApprovals();
    expect(fetch).toHaveBeenCalledWith('/api/v1/governance/approvals', expect.any(Object));
  });

  it('遥测带上 limit 参数', async () => {
    await fetchTelemetryEvents(40);
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/telemetry/events?limit=40',
      expect.any(Object)
    );
  });

  it('趋势默认不带查询参数', async () => {
    await fetchTrends();
    expect(fetch).toHaveBeenCalledWith('/api/v1/trends', expect.any(Object));
  });

  it('趋势带 metric_id 时拼进查询串', async () => {
    await fetchTrends({ metricId: 'task_success_rate', days: 7 });
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/trends?metric_id=task_success_rate&days=7',
      expect.any(Object)
    );
  });
});

describe('错误处理', () => {
  it('后端返回 400 时抛出 ApiError，并带上后端的 detail 原文', async () => {
    mockFetch({ detail: '趋势窗口不得少于 7 天（验收要求 ≥7 天连续运行）' }, false, 400);

    await expect(fetchTrends({ days: 3 })).rejects.toThrow(ApiError);

    try {
      await fetchTrends({ days: 3 });
      expect.unreachable('应该抛异常');
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      const apiError = err as ApiError;
      expect(apiError.status).toBe(400);
      expect(apiError.detail).toContain('7 天');
      expect(apiError.isClientError).toBe(true);
    }
  });

  it('响应体不是 JSON 时用状态码兜底，不抛解析异常', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
        ok: false,
        status: 502,
        statusText: 'Bad Gateway',
        json: async () => {
          throw new Error('not json');
        },
      }))
    );

    await expect(fetchSystemStatus()).rejects.toThrow(/502/);
  });

  it('网络不通时抛出 ApiError 且状态码为 0', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
        throw new TypeError('Failed to fetch');
      })
    );

    try {
      await fetchSystemStatus();
      expect.unreachable('应该抛异常');
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(0);
      expect((err as ApiError).detail).toContain('无法连接后端');
    }
  });
});

describe('写入端点', () => {
  it('自主权切换用 POST 并带 JSON 体', async () => {
    const fetchMock = mockFetch({ ok: true });

    await updateAutonomy({ autonomy_level: 'L3' });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/autonomy',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ autonomy_level: 'L3' }),
      })
    );
  });

  it('决策覆盖用的就是传入的 decision_id —— 不会自己去猜', async () => {
    // 回归测试：早期版本按候选 id 硬编码推导决策 id（DEC-001/DEC-002），
    // 第三个候选永远覆盖到错的决策上。这里锁死"用调用方给的 id"。
    const fetchMock = mockFetch({ ok: true });

    await overrideDecision('DEC-003', {
      override_action: 'record',
      reviewer: 'chief-architect',
      reason: '人工确认',
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/decisions/DEC-003/override',
      expect.objectContaining({ method: 'POST' })
    );
  });

  it('审批评审原样回传 content_digest（防篡改锁）', async () => {
    const fetchMock = mockFetch({ ok: true });
    const digest = 'sha256:d82f716aa8c92e104f2910fa4d1b8c19';

    await reviewApproval('APPR-001', {
      action: 'approve',
      reviewer: 'chief-architect',
      scopes: ['architecture'],
      comment: '通过',
      content_digest: digest,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/governance/approvals/APPR-001/review',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          action: 'approve',
          reviewer: 'chief-architect',
          scopes: ['architecture'],
          comment: '通过',
          content_digest: digest,
        }),
      })
    );
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});
