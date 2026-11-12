/**
 * 底部实时遥测与审计流水抽屉。
 *
 * 事件流来自 `/api/v1/telemetry/events`，由 `useCockpitData` 每 3 秒轮询。
 * 这里只负责呈现 —— 轮询逻辑在 hook 里，避免每个视图各拉一次。
 */

import React, { useEffect, useRef } from 'react';
import { Terminal, RefreshCw } from 'lucide-react';
import type { TelemetryEvent } from '../types/control-plane.ts';
import { formatClock } from '../lib/format.ts';
import { EmptyState, StatusDot } from './shared/Primitives.tsx';

interface TelemetryDrawerProps {
  events: TelemetryEvent[];
  onRefresh: () => void;
  connected: boolean;
}

/** 事件类型 → 颜色。类型多了就在这里加一行，别在 JSX 里写三元链。 */
const EVENT_COLOR: Record<string, string> = {
  'session.start': 'text-blue-300 border-blue-800',
  thought: 'text-purple-300 border-purple-800',
  action: 'text-cyan-300 border-cyan-800',
  verify: 'text-emerald-300 border-emerald-800',
  result: 'text-emerald-300 border-emerald-800',
  ready: 'text-slate-300 border-slate-700',
  start: 'text-blue-300 border-blue-800',
};

export const TelemetryDrawer: React.FC<TelemetryDrawerProps> = ({
  events,
  onRefresh,
  connected,
}) => {
  const scrollRef = useRef<HTMLDivElement>(null);

  // 新事件到达时滚到底部 —— 遥测流是"看最新的"，不是"看历史档案"。
  useEffect(() => {
    const el = scrollRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [events.length]);

  return (
    <footer className="h-44 border-t border-slate-800 bg-slate-950 flex flex-col shrink-0">
      <div className="px-3 py-1.5 bg-slate-900 border-b border-slate-800 flex items-center justify-between text-xs">
        <div className="flex items-center space-x-2">
          <Terminal className="w-3.5 h-3.5 text-blue-400" />
          <span className="font-semibold text-slate-300">实时遥测流 (Telemetry NDJSON Stream)</span>
          <span className="text-[10px] text-slate-600">{events.length} 条</span>
        </div>
        <div className="flex items-center space-x-3 text-[11px] text-slate-400">
          <span className="flex items-center space-x-1">
            <StatusDot className={connected ? 'bg-emerald-400' : 'bg-rose-400'} pulse={connected} />
            <span>{connected ? '实时流接入中' : '实时流已断开'}</span>
          </span>
          <button
            onClick={onRefresh}
            className="hover:text-white transition flex items-center space-x-1"
          >
            <RefreshCw className="w-3 h-3" />
            <span>刷新</span>
          </button>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 p-3 font-mono text-[11px] overflow-y-auto space-y-1 bg-slate-950">
        {events.length === 0 ? (
          <EmptyState label="暂无遥测事件" />
        ) : (
          events.map((ev, idx) => (
            <div
              key={ev.seq ?? idx}
              className="flex items-center space-x-2 text-slate-400 leading-tight"
            >
              <span className="text-slate-600 shrink-0">[{formatClock(ev.timestamp)}]</span>
              <span className="text-blue-400 font-semibold shrink-0">{ev.task_id}</span>
              <span
                className={`px-1.5 py-0.2 rounded bg-slate-900 border text-[10px] shrink-0 ${
                  EVENT_COLOR[ev.type] ?? 'text-slate-400 border-slate-800'
                }`}
              >
                {ev.type}
              </span>
              <span className="text-slate-300 truncate">{ev.message}</span>
            </div>
          ))
        )}
      </div>
    </footer>
  );
};
