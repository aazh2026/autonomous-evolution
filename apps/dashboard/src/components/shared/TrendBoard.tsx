/**
 * 跨周期趋势看板。
 *
 * **核心约束：算不出来 ≠ 0。**
 *
 * 趋势点里的 `null` 表示"该日无样本"或"缺人工标注基线"。把它画成 0 或者
 * 直接跳过，都会让看板说谎 —— 一个"回归缺陷率 0%"的看板比没有看板更危险，
 * 因为看板上的人会信它。所以：
 *
 * - `null` 点画成**空心虚线圆点**，位置落在基线上（不是零值位置），
 *   并带 tooltip 说明为什么没有；
 * - 折线在 `null` 处**断开**（不跨越缺失点连线），因为连线本身就是在
 *   暗示"这两个点之间有连续趋势"；
 * - 数值一律显示 `—`。
 *
 * 另外，R-117 禁用的虚荣指标（任务数 / Token 数 / 提交量 / 代码行数）
 * 在这里**结构性不存在** —— 后端不会返回它们，前端也不画。
 * 少几个指标比多几个骗人的指标强。
 */

import React from 'react';
import type { TrendMetric } from '../../types/control-plane.ts';
import { MISSING, formatMetricValue } from '../../lib/format.ts';

interface TrendBoardProps {
  metrics: TrendMetric[];
  days: string[];
}

const CHART_WIDTH = 132;
const CHART_HEIGHT = 34;
const PADDING = 3;

/** 按指标分四层 + 诊断量分组。 */
const LAYER_ORDER = ['discovery', 'decision', 'delivery', 'evolution', 'diagnostic'];

const LAYER_TITLES: Record<string, string> = {
  discovery: '自主发现层',
  decision: '决策质量层',
  delivery: '工程交付层',
  evolution: '项目演进层',
  diagnostic: '诊断量（背景参考）',
};

/**
 * 一条折线的坐标。
 *
 * 值域用这条指标**自己**的非空点的最小最大值 —— 混用全局值域会让
 * 波动很小的指标（比如重复发现率 0.04~0.12）被压成一条直线，看不出趋势。
 */
interface ScaledPoint {
  x: number;
  y: number;
  value: number;
}

/**
 * 把点序列缩放到画布坐标。
 *
 * **返回的数组与 `points` 逐位对齐** —— 缺失的位置是 `null`，而不是被跳过。
 * 这一点很关键：早先的版本把缺失点直接过滤掉，得到的数组比 `points` 短，
 * 于是"第几天"和"第几个点"对不上号，折线反而跨越缺失点连了起来 ——
 * 恰好是本文件禁止的那件事。
 */
function scalePoints(points: (number | null)[], dayCount: number): (ScaledPoint | null)[] {
  const present = points.filter((p): p is number => p !== null && !Number.isNaN(p));
  if (present.length === 0) return points.map(() => null);

  const min = Math.min(...present);
  const max = Math.max(...present);
  // 全部相等时（常量指标）画在中线，否则除零。
  const span = max - min || 1;

  const stepX = dayCount > 1 ? (CHART_WIDTH - PADDING * 2) / (dayCount - 1) : 0;

  return points.map((value, idx) => {
    if (value === null || Number.isNaN(value)) return null;
    const x = PADDING + idx * stepX;
    const ratio = (value - min) / span;
    const y = CHART_HEIGHT - PADDING - ratio * (CHART_HEIGHT - PADDING * 2);
    return { x, y, value };
  });
}

/**
 * 把连续的非空点切成若干段。
 *
 * 缺失点会断开连线 —— 见文件头的说明。
 */
function splitSegments(scaled: (ScaledPoint | null)[]): ScaledPoint[][] {
  const segments: ScaledPoint[][] = [];
  let current: ScaledPoint[] = [];

  for (const point of scaled) {
    if (!point) {
      // 缺失点：断段。
      if (current.length > 0) segments.push(current);
      current = [];
      continue;
    }
    current.push(point);
  }
  if (current.length > 0) segments.push(current);
  return segments;
}

const TrendChart: React.FC<{ metric: TrendMetric; dayCount: number }> = ({ metric, dayCount }) => {
  const scaled = scalePoints(metric.points, dayCount);
  const segments = splitSegments(scaled);
  const present = scaled.filter((p): p is ScaledPoint => p !== null);
  const hasData = present.length > 0;

  // 缺失点的横坐标（画空心虚线圆点用）。
  const stepX = dayCount > 1 ? (CHART_WIDTH - PADDING * 2) / (dayCount - 1) : 0;
  const missingIndices = metric.points
    .map((p, idx) => (p === null || Number.isNaN(p) ? idx : -1))
    .filter((idx) => idx >= 0);

  return (
    <svg
      viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
      className="w-full h-9"
      role="img"
      aria-label={`${metric.label} 趋势曲线`}
      preserveAspectRatio="none"
    >
      {/* 基线 */}
      <line
        x1={0}
        y1={CHART_HEIGHT - PADDING}
        x2={CHART_WIDTH}
        y2={CHART_HEIGHT - PADDING}
        stroke="#1e293b"
        strokeWidth={1}
      />
      {segments.map((segment, segIdx) => (
        <polyline
          key={segIdx}
          points={segment.map((p) => `${p.x},${p.y}`).join(' ')}
          fill="none"
          stroke="#38bdf8"
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      ))}
      {present.map((p) => (
        <circle key={`v-${p.x}`} cx={p.x} cy={p.y} r={1.8} fill="#38bdf8" />
      ))}
      {missingIndices.map((idx) => (
        <circle
          key={`m-${idx}`}
          cx={PADDING + idx * stepX}
          cy={CHART_HEIGHT - PADDING}
          r={2.4}
          fill="none"
          stroke="#475569"
          strokeWidth={1}
          strokeDasharray="2 1.5"
        >
          <title>{metric.missing_reason ?? '该日无样本'}</title>
        </circle>
      ))}
      {!hasData ? (
        <text
          x={CHART_WIDTH / 2}
          y={CHART_HEIGHT / 2 + 3}
          textAnchor="middle"
          fill="#64748b"
          fontSize={9}
        >
          无样本
        </text>
      ) : null}
    </svg>
  );
};

const TrendBoard: React.FC<TrendBoardProps> = ({ metrics, days }) => {
  const groups = LAYER_ORDER.map((layer) => ({
    layer,
    title: LAYER_TITLES[layer] ?? layer,
    items: metrics.filter((m) => m.layer === layer),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="space-y-4">
      {groups.map((group) => (
        <div key={group.layer}>
          <div className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-2">
            {group.title}
          </div>
          <div className="grid grid-cols-2 gap-2">
            {group.items.map((metric) => {
              const latest = metric.points[metric.points.length - 1];
              const missing = latest === null || latest === undefined;
              const missingState = metric.status !== 'computed';
              return (
                <div
                  key={metric.metric_id}
                  className="p-3 rounded-md bg-slate-950 border border-slate-800"
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-[11px] text-slate-300 leading-tight">
                      {metric.label}
                    </span>
                    {metric.is_diagnostic ? (
                      <span className="text-[9px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700 shrink-0">
                        诊断量
                      </span>
                    ) : null}
                  </div>

                  <div className="flex items-baseline gap-2 mt-1">
                    <span
                      className={`text-lg font-bold font-mono ${missingState ? 'text-slate-500' : 'text-slate-100'}`}
                    >
                      {missingState ? MISSING : formatMetricValue(latest)}
                    </span>
                    {metric.metric_id === 'cumulative_cost_usd' && !missingState ? (
                      <span className="text-[10px] text-slate-500">USD</span>
                    ) : null}
                  </div>

                  <div className="mt-1">
                    <TrendChart metric={metric} dayCount={days.length} />
                  </div>

                  <div className="flex items-center justify-between text-[9px] text-slate-600 mt-0.5">
                    <span>{days[0] ?? ''}</span>
                    <span>{days[days.length - 1] ?? ''}</span>
                  </div>

                  {missingState ? (
                    <p className="mt-1 text-[10px] leading-relaxed text-amber-600/90">
                      {metric.missing_reason ?? '该指标当前无样本'}
                    </p>
                  ) : null}
                  {missing && !missingState ? (
                    <p className="mt-1 text-[10px] text-slate-600">最新一日无样本</p>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
};

export default TrendBoard;
