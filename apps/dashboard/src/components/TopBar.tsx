/**
 * 顶部安全与自主权控制中枢。
 *
 * 全部数据来自 `/api/v1/status` —— 包括预算水位。早期版本这里写死了
 * `$1.26 / $5.00`，于是无论后端真实花多少，界面都显示这个数。
 * 预算条是操作员判断"还能不能再跑一轮"的唯一依据，写死等于没有。
 */

import React from 'react';
import { Wallet, Pause, Play, OctagonAlert, Activity } from 'lucide-react';
import type { AutonomyLevel, SystemStatus } from '../types/control-plane.ts';
import { budgetRatio, formatCurrency } from '../lib/format.ts';
import { StatusDot } from './shared/Primitives.tsx';

interface TopBarProps {
  status: SystemStatus | null;
  onSetAutonomy: (lvl: AutonomyLevel) => void;
  onTogglePause: () => void;
  onEmergencyStop: () => void;
  busy?: boolean;
}

const LEVELS: AutonomyLevel[] = ['L0', 'L1', 'L2', 'L3'];

const LEVEL_LABELS: Record<AutonomyLevel, string> = {
  L0: '手动',
  L1: '监督',
  L2: '半自主',
  L3: '全自主',
};

export const TopBar: React.FC<TopBarProps> = ({
  status,
  onSetAutonomy,
  onTogglePause,
  onEmergencyStop,
  busy = false,
}) => {
  const currentLvl = status?.autonomy_level ?? 'L2';
  const ratio = status ? budgetRatio(status.budget_spent_usd, status.budget_daily_cap_usd) : null;

  // 预算水位配色：低于 80% 绿，80%~100% 琥珀，超支红。
  const budgetBarClass =
    ratio === null
      ? 'bg-slate-600'
      : ratio >= 1
        ? 'bg-rose-500'
        : ratio >= 0.8
          ? 'bg-amber-500'
          : 'bg-emerald-500';

  return (
    <header className="h-14 border-b border-slate-800 bg-slate-900/90 backdrop-blur px-4 flex items-center justify-between gap-4 shrink-0">
      <div className="flex items-center space-x-3 min-w-0">
        <div className="h-8 w-8 shrink-0 rounded-lg bg-blue-600 flex items-center justify-center font-black tracking-wider text-white shadow-lg shadow-blue-500/30">
          AE
        </div>
        <div className="min-w-0">
          <div className="flex items-center space-x-2">
            <span className="font-bold text-sm tracking-wide text-white whitespace-nowrap">
              ASES 控制舱
            </span>
            <span className="text-xs px-2 py-0.5 rounded bg-blue-900/50 text-blue-300 border border-blue-700/50 font-mono truncate">
              {status?.project_id ?? '—'}
            </span>
          </div>
          {status ? (
            <div className="text-[10px] text-slate-500 truncate">{status.project_name}</div>
          ) : null}
        </div>
      </div>

      {/* 自主权拨杆 */}
      <div className="flex items-center bg-slate-950 border border-slate-800 rounded-lg p-1 text-xs shrink-0">
        <span className="text-slate-400 px-2 font-medium">自主权:</span>
        {LEVELS.map((lvl) => {
          const isActive = lvl === currentLvl;
          return (
            <button
              key={lvl}
              onClick={() => onSetAutonomy(lvl)}
              disabled={busy}
              title={`${lvl} ${LEVEL_LABELS[lvl]}`}
              className={`px-2.5 py-1 rounded transition-colors disabled:opacity-50 ${
                isActive
                  ? 'bg-blue-600 text-white font-semibold shadow'
                  : 'text-slate-400 hover:text-white'
              }`}
            >
              {lvl} {LEVEL_LABELS[lvl]}
            </button>
          );
        })}
      </div>

      <div className="flex items-center gap-4 text-xs shrink-0">
        {/* 预算水位 */}
        <div className="bg-slate-800/60 px-3 py-1.5 rounded-md border border-slate-700/50 min-w-[190px]">
          <div className="flex items-center space-x-2">
            <Wallet className="w-4 h-4 text-emerald-400 shrink-0" />
            <span className="text-slate-400">本日预算:</span>
            <span className="font-mono font-semibold text-emerald-400">
              {status
                ? `${formatCurrency(status.budget_spent_usd)} / ${formatCurrency(
                    status.budget_daily_cap_usd
                  )}`
                : '—'}
            </span>
          </div>
          <div className="h-1 mt-1.5 rounded bg-slate-950 overflow-hidden">
            <div
              className={`h-full rounded transition-all ${budgetBarClass}`}
              style={{ width: `${Math.min(100, (ratio ?? 0) * 100)}%` }}
            />
          </div>
        </div>

        {/* 并发与健康 */}
        {status ? (
          <div className="flex items-center gap-1.5 text-slate-400">
            <Activity className="w-3.5 h-3.5" />
            <span className="font-mono">
              {status.active_executors_count}/{status.max_concurrency}
            </span>
            <StatusDot
              className={status.system_health === 'healthy' ? 'bg-emerald-400' : 'bg-amber-400'}
            />
          </div>
        ) : null}

        <div className="flex items-center space-x-2">
          <button
            onClick={onTogglePause}
            disabled={busy}
            className={`px-3 py-1.5 rounded-md border transition flex items-center space-x-1 font-medium disabled:opacity-50 ${
              status?.is_paused
                ? 'bg-emerald-600/20 text-emerald-300 border-emerald-600/40 hover:bg-emerald-600/30'
                : 'bg-amber-600/20 text-amber-300 border-amber-600/40 hover:bg-amber-600/30'
            }`}
          >
            {status?.is_paused ? <Play className="w-3.5 h-3.5" /> : <Pause className="w-3.5 h-3.5" />}
            <span>{status?.is_paused ? '恢复演进' : '暂停闭环'}</span>
          </button>
          <button
            onClick={onEmergencyStop}
            disabled={busy}
            className="px-3 py-1.5 rounded-md bg-rose-600 text-white hover:bg-rose-700 transition flex items-center space-x-1 font-bold shadow-lg shadow-rose-900/30 disabled:opacity-50"
          >
            <OctagonAlert className="w-3.5 h-3.5" />
            <span>急停熔断</span>
          </button>
        </div>
      </div>
    </header>
  );
};
