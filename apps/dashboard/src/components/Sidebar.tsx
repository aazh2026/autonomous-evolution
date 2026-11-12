/**
 * 左侧导航栏：演进周期历程 + 五大视图导航 + 执行器集群舰队。
 *
 * 周期列表与执行器画像都来自后端 —— 早期版本这里是四行写死的假数据，
 * 于是控制舱看起来"什么都知道"，其实对当前运行的真实状态一无所知。
 */

import React from 'react';
import { Activity, Scale, GitMerge, ShieldCheck, Landmark } from 'lucide-react';
import type { CycleSummary, ExecutorProfile, SystemStatus } from '../types/control-plane.ts';
import { cycleStatusVisual } from '../lib/confidence.ts';
import { formatCurrency, formatDelta } from '../lib/format.ts';

export type TabKey = 'dashboard' | 'sandbox' | 'dag' | 'verify' | 'governance';

interface SidebarProps {
  currentTab: TabKey;
  onSelectTab: (tab: TabKey) => void;
  pendingApprovalsCount: number;
  cycles: CycleSummary[];
  executors: ExecutorProfile[];
  status: SystemStatus | null;
}

export const Sidebar: React.FC<SidebarProps> = ({
  currentTab,
  onSelectTab,
  pendingApprovalsCount,
  cycles,
  executors,
  status,
}) => {
  const navItems: { key: TabKey; label: string; icon: typeof Activity; color: string; badge?: number }[] =
    [
      { key: 'dashboard', label: '双环态势与目标达成', icon: Activity, color: 'text-blue-400' },
      { key: 'sandbox', label: '演进决策沙盘', icon: Scale, color: 'text-purple-400' },
      { key: 'dag', label: 'DAG 多任务调度流', icon: GitMerge, color: 'text-cyan-400' },
      { key: 'verify', label: '六层验证与变更摘要', icon: ShieldCheck, color: 'text-emerald-400' },
      {
        key: 'governance',
        label: '高风险审批法庭',
        icon: Landmark,
        color: 'text-amber-400',
        badge: pendingApprovalsCount,
      },
    ];

  const activeExecutors = executors.filter((e) => e.current_load > 0).length;

  return (
    <aside className="w-64 border-r border-slate-800 bg-slate-900/40 flex flex-col shrink-0">
      {/* 周期历程 */}
      <div className="p-3 border-b border-slate-800">
        <div className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-2">
          演进周期历程 (Cycles)
        </div>
        <div className="space-y-1.5 max-h-40 overflow-y-auto">
          {cycles.length === 0 ? (
            <p className="text-[11px] text-slate-600 px-1">尚无周期记录</p>
          ) : (
            cycles.map((cycle, idx) => {
              const visual = cycleStatusVisual(cycle.status);
              return (
                <div
                  key={cycle.cycle_id}
                  className={`p-2.5 rounded-md border ${
                    idx === 0
                      ? 'bg-blue-950/40 border-blue-500/40'
                      : 'bg-slate-950/40 border-slate-800'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2 text-xs font-semibold">
                    <span className="text-white truncate">{cycle.cycle_id}</span>
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded border font-mono shrink-0 ${visual.badge}`}
                    >
                      Δ {formatDelta(cycle.attainment_delta)}
                    </span>
                  </div>
                  <div className="text-[11px] text-slate-400 mt-1 flex justify-between gap-2">
                    <span className="truncate">
                      状态: <span className={visual.text}>{cycle.status}</span>
                    </span>
                    <span className="shrink-0">耗费: {formatCurrency(cycle.cost_usd)}</span>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* 视图导航 */}
      <nav className="flex-1 p-2 space-y-1 overflow-y-auto">
        <div className="text-[11px] font-semibold text-slate-500 px-3 py-1 uppercase tracking-wider">
          视图导航
        </div>
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = currentTab === item.key;
          return (
            <button
              key={item.key}
              onClick={() => onSelectTab(item.key)}
              className={`w-full flex items-center space-x-2.5 px-3 py-2 rounded-md text-xs font-medium transition ${
                isActive
                  ? 'bg-slate-800 text-white font-semibold'
                  : 'text-slate-400 hover:text-white hover:bg-slate-800/60'
              }`}
            >
              <Icon className={`w-4 h-4 shrink-0 ${item.color}`} />
              <span className="truncate">{item.label}</span>
              {item.badge !== undefined && item.badge > 0 ? (
                <span className="ml-auto px-1.5 py-0.2 rounded-full text-[10px] bg-amber-500/20 text-amber-400 font-bold shrink-0">
                  {item.badge}
                </span>
              ) : null}
            </button>
          );
        })}
      </nav>

      {/* 执行器舰队 */}
      <div className="p-3 border-t border-slate-800 bg-slate-900/60 text-xs">
        <div className="text-[11px] font-semibold text-slate-400 mb-2 flex items-center justify-between">
          <span>执行器集群舰队</span>
          <span className="text-[10px] text-blue-400 font-mono">
            并发 {status ? `${status.active_executors_count}/${status.max_concurrency}` : '—'}
          </span>
        </div>
        <div className="space-y-1.5">
          {executors.length === 0 ? (
            <p className="text-[11px] text-slate-600">暂无执行器数据</p>
          ) : (
            executors.map((executor) => (
              <div key={executor.name} className="flex items-center justify-between gap-2 text-[11px]">
                <span className="flex items-center space-x-1.5 min-w-0">
                  <span
                    className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                      executor.is_healthy ? 'bg-emerald-500' : 'bg-rose-500'
                    }`}
                  />
                  <span className="text-slate-300 truncate">{executor.label}</span>
                </span>
                <span className="text-slate-400 font-mono shrink-0">
                  {executor.current_load} 活跃 ({(executor.success_rate * 100).toFixed(0)}%)
                </span>
              </div>
            ))
          )}
        </div>
        {executors.length > 0 ? (
          <div className="text-[10px] text-slate-600 mt-2">
            舰队健康: {activeExecutors}/{executors.length} 个执行器有负载
          </div>
        ) : null}
      </div>
    </aside>
  );
};
