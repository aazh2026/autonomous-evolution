/**
 * 视图 1：双环态势与跨周期趋势看板。
 *
 * **双环严格解耦** —— 循环 B（宏观机会发现与路线图规划）与循环 A（微观受控
 * 执行与独立六层验证）是两套独立的状态机，界面上分区呈现，绝不混成一条流。
 * 把它们画在一起会让人以为"发现"直接等于"执行"，而实际上中间隔着
 * Critic 质询、决策裁决与治理审批三道闸。
 */

import React from 'react';
import { Activity, Compass, Cpu } from 'lucide-react';
import type { CycleSummary, TrendBoardResponse } from '../../types/control-plane.ts';
import { formatCurrency, formatDelta, formatPercent, MISSING } from '../../lib/format.ts';
import { cycleStatusVisual } from '../../lib/confidence.ts';
import TrendBoard from '../shared/TrendBoard.tsx';
import { Badge, EmptyState, ErrorNote, Panel, StatusDot } from '../shared/Primitives.tsx';

interface DualLoopDashboardProps {
  cycles: CycleSummary[];
  trends: TrendBoardResponse | null;
  trendError?: string;
  onRetry: () => void;
}

const LOOP_B_STAGES = [
  { name: '认知', detail: '扫描项目代码、历史快照与已知缺陷基线' },
  { name: '诊断', detail: '发掘候选，提取真实代码行证据' },
  { name: '决策', detail: 'Critic 质询与沙盘裁决，不行动也是合法决策' },
  { name: '规划', detail: '拆解为有向无环图与多波次排期' },
];

const LOOP_A_STAGES = [
  { name: '调度与选择器', detail: '动态分配至 Codex / Gemini CLI / Claude Code' },
  { name: '工作空间隔离', detail: '独立 Worktree 租约发放，消除文件冲突' },
  { name: '六层独立验证', detail: '功能、质量、架构、安全、性能、契约逐层把关' },
  { name: '集成与收敛', detail: '合入主分支，更新差距模型并沉淀快照' },
];

/** 从周期历史里取最新一轮作为"当前轮"。 */
function latestCycle(cycles: CycleSummary[]): CycleSummary | undefined {
  return cycles.length > 0 ? cycles[0] : undefined;
}

const DualLoopDashboard: React.FC<DualLoopDashboardProps> = ({
  cycles,
  trends,
  trendError,
  onRetry,
}) => {
  const current = latestCycle(cycles);

  return (
    <section className="flex-1 p-6 overflow-y-auto space-y-6">
      <div>
        <h2 className="text-lg font-bold text-white flex items-center space-x-2">
          <Activity className="w-5 h-5 text-blue-400" />
          <span>自主演进双环态势看板</span>
        </h2>
        <p className="text-xs text-slate-400 mt-1">
          循环 B（宏观机会发现与路线图规划）与 循环 A（微观受控执行与独立六层验证）实时联动。
        </p>
      </div>

      {/* ---- 顶部指标卡：全部来自 /api/v1/cycles，没有硬编码 ---- */}
      <div className="grid grid-cols-4 gap-4">
        <div className="p-4 rounded-lg bg-slate-900 border border-slate-800">
          <div className="text-xs text-slate-400 font-medium">目标达成度 (Attainment)</div>
          {current ? (
            <>
              <div className="text-2xl font-bold font-mono text-emerald-400 mt-1 flex items-baseline space-x-2">
                <span>{current.after_attainment.toFixed(4)}</span>
                <span className="text-xs text-emerald-500 font-semibold">
                  ▲ {formatDelta(current.attainment_delta)}
                </span>
              </div>
              <div className="text-[11px] text-slate-500 mt-2">
                轮前: {current.before_attainment.toFixed(4)} ➔ 轮后:{' '}
                {current.after_attainment.toFixed(4)}
              </div>
            </>
          ) : (
            <div className="text-2xl font-bold font-mono text-slate-600 mt-1">{MISSING}</div>
          )}
        </div>

        <div className="p-4 rounded-lg bg-slate-900 border border-slate-800">
          <div className="text-xs text-slate-400 font-medium">本轮候选与任务</div>
          {current ? (
            <>
              <div className="text-2xl font-bold font-mono text-white mt-1 flex items-baseline space-x-2">
                <span>{current.candidates_count}</span>
                <span className="text-xs text-slate-500">候选 /</span>
                <span>{current.tasks_count}</span>
                <span className="text-xs text-slate-500">任务</span>
              </div>
              <div className="text-[11px] text-slate-500 mt-2">周期: {current.cycle_id}</div>
            </>
          ) : (
            <div className="text-2xl font-bold font-mono text-slate-600 mt-1">{MISSING}</div>
          )}
        </div>

        <div className="p-4 rounded-lg bg-slate-900 border border-slate-800">
          <div className="text-xs text-slate-400 font-medium">单位增益成本效率</div>
          {current ? (
            <>
              <div className="text-2xl font-bold font-mono text-white mt-1">
                {formatCurrency(current.cost_usd)}
              </div>
              <div className="text-[11px] text-slate-500 mt-2">
                本周期总花费 · 达成度增益 {formatDelta(current.attainment_delta)}
              </div>
            </>
          ) : (
            <div className="text-2xl font-bold font-mono text-slate-600 mt-1">{MISSING}</div>
          )}
        </div>

        <div className="p-4 rounded-lg bg-slate-900 border border-slate-800">
          <div className="text-xs text-slate-400 font-medium">独立验收通过率</div>
          {trends ? (
            <>
              <div className="text-2xl font-bold font-mono text-white mt-1">
                {(() => {
                  const metric = trends.metrics.find((m) => m.metric_id === 'verification_pass_rate');
                  const latest = metric?.points[metric.points.length - 1];
                  return latest === null || latest === undefined
                    ? MISSING
                    : formatPercent(latest);
                })()}
              </div>
              <div className="text-[11px] text-slate-500 mt-2">诊断量 · 不计入核心指标</div>
            </>
          ) : (
            <div className="text-2xl font-bold font-mono text-slate-600 mt-1">{MISSING}</div>
          )}
        </div>
      </div>

      {/* ---- 跨周期趋势看板 ---- */}
      <Panel
        title="跨周期 7 天趋势看板（4 层 8 项核心指标 + 2 诊断量）"
        actions={
          <span className="text-[10px] text-slate-500">
            窗口 {trends?.window_days ?? MISSING} 天 · 缺失值显示为 {MISSING}，不等于 0
          </span>
        }
        scroll
      >
        <div className="p-4">
          {trendError ? (
            <ErrorNote message={trendError} onRetry={onRetry} />
          ) : trends && trends.metrics.length > 0 ? (
            <TrendBoard metrics={trends.metrics} days={trends.days} />
          ) : (
            <EmptyState label="暂无趋势数据" />
          )}
        </div>
      </Panel>

      {/* ---- 双环状态机 ---- */}
      <div className="p-5 rounded-lg bg-slate-900 border border-slate-800">
        <div className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-4 flex items-center justify-between">
          <span>双环闭环流转状态图 (Dual-Loop Architecture)</span>
          <span className="text-emerald-400 flex items-center space-x-1">
            <StatusDot className="bg-emerald-400" pulse />
            <span className="text-[11px]">闭环健康巡航中</span>
          </span>
        </div>

        <div className="grid grid-cols-2 gap-6">
          <div className="p-4 rounded-lg bg-slate-950 border border-purple-900/40">
            <div className="flex items-center space-x-2 text-purple-400 font-bold text-xs uppercase mb-3">
              <Compass className="w-4 h-4" />
              <span>循环 B：宏观演进发现环 (Macro)</span>
            </div>
            <ol className="space-y-2 text-xs">
              {LOOP_B_STAGES.map((stage, idx) => (
                <li key={stage.name} className="flex items-start space-x-2 text-slate-300">
                  <span className="w-5 h-5 shrink-0 rounded-full bg-purple-900/60 text-purple-300 flex items-center justify-center font-bold text-[10px] mt-0.5">
                    {idx + 1}
                  </span>
                  <span>
                    <strong>{stage.name}</strong>: {stage.detail}
                  </span>
                </li>
              ))}
            </ol>
          </div>

          <div className="p-4 rounded-lg bg-slate-950 border border-blue-900/40">
            <div className="flex items-center space-x-2 text-blue-400 font-bold text-xs uppercase mb-3">
              <Cpu className="w-4 h-4" />
              <span>循环 A：微观任务执行环 (Micro)</span>
            </div>
            <ol className="space-y-2 text-xs">
              {LOOP_A_STAGES.map((stage, idx) => (
                <li key={stage.name} className="flex items-start space-x-2 text-slate-300">
                  <span className="w-5 h-5 shrink-0 rounded-full bg-blue-900/60 text-blue-300 flex items-center justify-center font-bold text-[10px] mt-0.5">
                    {idx + 1}
                  </span>
                  <span>
                    <strong>{stage.name}</strong>: {stage.detail}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>

      {/* ---- 周期历史 ---- */}
      <Panel title="演进周期历程" scroll>
        {cycles.length === 0 ? (
          <EmptyState label="尚无演进周期记录" />
        ) : (
          <div className="divide-y divide-slate-800">
            {cycles.map((cycle) => {
              const visual = cycleStatusVisual(cycle.status);
              return (
                <div key={cycle.cycle_id} className="p-3 flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold text-white">{cycle.cycle_id}</span>
                      <Badge className={visual.badge}>{cycle.status}</Badge>
                    </div>
                    <div className="text-[11px] text-slate-500 mt-1">
                      候选 {cycle.candidates_count} · 任务 {cycle.tasks_count} · 花费{' '}
                      {formatCurrency(cycle.cost_usd)} ·{' '}
                      {cycle.started_at.slice(0, 16).replace('T', ' ')}
                    </div>
                  </div>
                  <div className="text-right shrink-0">
                    <div className={`text-xs font-mono font-bold ${visual.text}`}>
                      Δ {formatDelta(cycle.attainment_delta)}
                    </div>
                    <div className="text-[10px] text-slate-600">
                      {cycle.before_attainment.toFixed(4)} ➔ {cycle.after_attainment.toFixed(4)}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </section>
  );
};

export default DualLoopDashboard;
