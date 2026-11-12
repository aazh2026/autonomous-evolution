/**
 * 控制舱通用界面元件。
 *
 * 抽出来不是为了少写几行，而是为了让**"缺失"与"失败"在界面上长得一样**——
 * 一个"数据还没来"和一个"后端报错了"如果只是文字不同，操作员在余光里
 * 分辨不出来，误判成本很高。所以两者的视觉规格在这里统一定死。
 */

import React from 'react';
import { AlertTriangle, Inbox, Loader2 } from 'lucide-react';

// ---------------------------------------------------------------------------
// 面板外壳
// ---------------------------------------------------------------------------

interface PanelProps {
  title: string;
  /** 标题右侧的补充说明或操作区。 */
  actions?: React.ReactNode;
  children: React.ReactNode;
  /** 面板主体是否可滚动。 */
  scroll?: boolean;
  className?: string;
}

export const Panel: React.FC<PanelProps> = ({
  title,
  actions,
  children,
  scroll = false,
  className = '',
}) => (
  <section
    className={`rounded-lg bg-slate-900 border border-slate-800 flex flex-col overflow-hidden ${className}`}
  >
    <header className="px-3 py-2 bg-slate-800/50 border-b border-slate-800 flex items-center justify-between gap-2 shrink-0">
      <h3 className="text-xs font-semibold text-slate-300">{title}</h3>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
    <div className={`flex-1 min-h-0 ${scroll ? 'overflow-y-auto' : ''}`}>{children}</div>
  </section>
);

// ---------------------------------------------------------------------------
// 状态占位
// ---------------------------------------------------------------------------

/** 首屏装载中。 */
export const LoadingState: React.FC<{ label?: string }> = ({ label = '正在装载控制面数据…' }) => (
  <div className="flex items-center justify-center gap-2 py-16 text-xs text-slate-400">
    <Loader2 className="w-4 h-4 animate-spin" />
    <span>{label}</span>
  </div>
);

/**
 * 缺失 / 为空。
 *
 * 传了 `reason` 就把原因一起显示出来 —— "没有数据"和"因为还没有人工标注
 * 基线所以没有数据"是两回事，界面上必须能分辨。
 */
export const EmptyState: React.FC<{ reason?: string; label?: string }> = ({
  reason,
  label = '暂无数据',
}) => (
  <div className="flex flex-col items-center justify-center gap-2 py-10 text-xs text-slate-500">
    <Inbox className="w-5 h-5 opacity-60" />
    <span>{label}</span>
    {reason ? <span className="max-w-md text-center text-[11px] text-slate-600">{reason}</span> : null}
  </div>
);

/**
 * 失败提示。
 *
 * 局部失败用这个（只坏了一个端点），整体失败用 `FatalError`。
 */
export const ErrorNote: React.FC<{ message: string; onRetry?: () => void }> = ({
  message,
  onRetry,
}) => (
  <div className="flex items-start gap-2 px-3 py-2 rounded border border-rose-800/50 bg-rose-950/40 text-[11px] text-rose-300">
    <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
    <span className="flex-1 leading-relaxed">{message}</span>
    {onRetry ? (
      <button
        onClick={onRetry}
        className="shrink-0 px-2 py-0.5 rounded border border-rose-700/60 hover:bg-rose-900/50 transition"
      >
        重试
      </button>
    ) : null}
  </div>
);

/** 整体不可用（连态势都拿不到）。 */
export const FatalError: React.FC<{ message: string; onRetry?: () => void }> = ({
  message,
  onRetry,
}) => (
  <div className="flex-1 flex items-center justify-center p-8">
    <div className="max-w-lg space-y-3 text-center">
      <AlertTriangle className="w-8 h-8 text-rose-500 mx-auto" />
      <p className="text-sm font-semibold text-slate-200">控制面不可达</p>
      <p className="text-xs text-slate-400 leading-relaxed">{message}</p>
      <p className="text-[11px] text-slate-600 leading-relaxed">
        请确认后端已启动（<code className="text-slate-400">make up</code> 或{' '}
        <code className="text-slate-400">./scripts/start_cockpit.sh</code>），并检查{' '}
        <code className="text-slate-400">VITE_API_BASE_URL</code> 是否指向正确地址。
      </p>
      {onRetry ? (
        <button
          onClick={onRetry}
          className="mt-2 px-4 py-1.5 rounded-md bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold transition"
        >
          重新连接
        </button>
      ) : null}
    </div>
  </div>
);

// ---------------------------------------------------------------------------
// 徽标
// ---------------------------------------------------------------------------

export const Badge: React.FC<{
  children: React.ReactNode;
  className?: string;
  title?: string;
}> = ({ children, className = '', title }) => (
  <span
    title={title}
    className={`inline-flex items-center px-2 py-0.5 rounded border text-[10px] font-bold ${className}`}
  >
    {children}
  </span>
);

/** 小圆点状态指示。 */
export const StatusDot: React.FC<{ className: string; pulse?: boolean }> = ({
  className,
  pulse = false,
}) => (
  <span className={`inline-block w-2 h-2 rounded-full ${className} ${pulse ? 'animate-pulse' : ''}`} />
);
