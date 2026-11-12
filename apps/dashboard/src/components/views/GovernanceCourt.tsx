/**
 * 视图 5：高风险变更审批法庭。
 *
 * **双轴风险 + 资格名册 + SHA-256 内容锁。**
 *
 * 三个机制缺一不可：
 * - 双轴（操作 × 种类）决定最少独立评审份数 —— 改代码和改治理规则不是一回事；
 * - 资格名册决定谁能签 —— 架构师签不了安全评审；
 * - 内容指纹锁决定"签的是哪一版" —— 内容在审批期间被改过，签名立即失效。
 *
 * **指纹必须原样回传。** 前端拿到 `sha256:xxx` 就原样提交给后端，由后端
 * 比对。前端自己重算是错的做法：它算的是"我以为的内容"，不是"被审的内容"。
 */

import React, { useState } from 'react';
import { Check, Fingerprint, Landmark, ShieldAlert, X } from 'lucide-react';
import type { Approval } from '../../types/control-plane.ts';
import { approvalStatusVisual } from '../../lib/confidence.ts';
import { Badge, EmptyState, Panel } from '../shared/Primitives.tsx';

interface GovernanceCourtProps {
  approvals: Approval[];
  onReview: (
    requestId: string,
    action: 'approve' | 'reject',
    scopes: string[],
    comment: string,
    contentDigest: string
  ) => Promise<void>;
  busy: boolean;
  error: string | null;
}

/** 徽标上的作用域名 → 中文。 */
const SCOPE_LABELS: Record<string, string> = {
  architecture: '架构',
  security: '安全',
  governance: '治理',
  testing: '测试',
  performance: '性能',
};

const scopeLabel = (scope: string): string => SCOPE_LABELS[scope] ?? scope;

const ApprovalCard: React.FC<{
  approval: Approval;
  onReview: GovernanceCourtProps['onReview'];
  busy: boolean;
}> = ({ approval, onReview, busy }) => {
  const [comment, setComment] = useState('');
  const [scope, setScope] = useState<string>('');

  const visual = approvalStatusVisual(approval.status);
  const pending = approval.status === 'pending';

  /** 当前还缺哪个资格槽位。 */
  const missingScopes = approval.required_scopes.filter((s) => !approval.covered_scopes.includes(s));

  const review = (action: 'approve' | 'reject') => {
    const scopes = approval.required_scopes.filter((s) => s === scope);
    void onReview(
      approval.request_id,
      action,
      scopes.length > 0 ? scopes : approval.required_scopes,
      comment,
      approval.content_digest
    );
  };

  return (
    <article className="p-5 rounded-lg bg-slate-900 border border-amber-900/40">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-mono font-bold text-amber-400">{approval.request_id}</span>
          <span className="text-xs text-slate-300">
            目标任务: <strong className="font-mono">{approval.task_id}</strong>
          </span>
          <span className="text-[11px] text-slate-400">
            操作: <span className="font-mono">{approval.operation}</span>
          </span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <Badge className="bg-rose-950 text-rose-300 border-rose-700/50 uppercase">
            {approval.risk_level} RISK
          </Badge>
          <Badge className={visual.badge}>{approval.status}</Badge>
        </div>
      </div>

      <div className="mt-3 text-xs space-y-1.5 text-slate-300">
        <div>
          改动摘要: <span className="font-mono text-emerald-400">{approval.diff_summary}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <Fingerprint className="w-3.5 h-3.5 text-slate-500 shrink-0" />
          <span>防篡改哈希: </span>
          <span className="font-mono text-[10px] text-slate-400 break-all">
            {approval.content_digest}
          </span>
        </div>
      </div>

      {/* 资格名册 */}
      <div className="mt-3 p-3 rounded bg-slate-950 border border-slate-800">
        <div className="text-[11px] font-semibold text-slate-400 mb-2">
          资格名册（需要 {approval.required_scopes.length} 个独立评审资格）
        </div>
        <div className="flex flex-wrap gap-2">
          {approval.required_scopes.map((required) => {
            const covered = approval.covered_scopes.includes(required);
            return (
              <span
                key={required}
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[11px] ${
                  covered
                    ? 'bg-emerald-950 text-emerald-300 border-emerald-700/60'
                    : 'bg-amber-950 text-amber-300 border-amber-700/60'
                }`}
              >
                {covered ? <Check className="w-3 h-3" /> : <ShieldAlert className="w-3 h-3" />}
                {scopeLabel(required)}
                {covered ? '' : ' (待签)'}
              </span>
            );
          })}
        </div>
        {missingScopes.length > 0 ? (
          <p className="text-[11px] text-amber-500/90 mt-2">
            尚缺 {missingScopes.map(scopeLabel).join('、')} 资格 —— 全部签齐后方可放行。
          </p>
        ) : (
          <p className="text-[11px] text-emerald-500/90 mt-2">资格已齐备。</p>
        )}
      </div>

      {approval.reviewed_by ? (
        <div className="mt-3 text-[11px] text-slate-500">
          评审人: <span className="text-slate-300">{approval.reviewed_by}</span>
          {approval.reviewed_at ? ` · ${approval.reviewed_at}` : ''}
          {approval.comment ? ` · ${approval.comment}` : ''}
        </div>
      ) : null}

      {pending ? (
        <div className="mt-4 pt-3 border-t border-slate-800 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-slate-400">本次签署资格:</span>
            {approval.required_scopes.map((required) => (
              <button
                key={required}
                onClick={() => setScope(scope === required ? '' : required)}
                className={`px-2 py-0.5 rounded border text-[11px] transition ${
                  scope === required
                    ? 'bg-blue-600 text-white border-blue-500'
                    : 'bg-slate-800 text-slate-300 border-slate-700 hover:text-white'
                }`}
              >
                {scopeLabel(required)}
              </button>
            ))}
          </div>

          <input
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="评审意见（可选）"
            className="w-full px-2 py-1.5 rounded bg-slate-950 border border-slate-800 text-[11px] text-slate-200 outline-none focus:border-slate-600 placeholder:text-slate-600"
          />

          <div className="flex items-center gap-2">
            <button
              onClick={() => review('approve')}
              disabled={busy}
              className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white font-semibold text-xs transition inline-flex items-center gap-1"
            >
              <Check className="w-3.5 h-3.5" />
              签名放行 (Approve)
            </button>
            <button
              onClick={() => review('reject')}
              disabled={busy}
              className="px-3 py-1.5 rounded bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white font-semibold text-xs transition inline-flex items-center gap-1"
            >
              <X className="w-3.5 h-3.5" />
              驳回 (Reject)
            </button>
            <span className="text-[10px] text-slate-600">
              提交时将携带 content_digest，内容被改过则后端拒绝签名。
            </span>
          </div>
        </div>
      ) : null}
    </article>
  );
};

const GovernanceCourt: React.FC<GovernanceCourtProps> = ({ approvals, onReview, busy, error }) => (
  <section className="flex-1 p-6 overflow-y-auto space-y-6">
    <div>
      <h2 className="text-lg font-bold text-white flex items-center space-x-2">
        <Landmark className="w-5 h-5 text-amber-400" />
        <span>高风险变更与治理审批法庭 (Governance Court)</span>
      </h2>
      <p className="text-xs text-slate-400 mt-1">
        涉及架构修改、验收标准变更或治理策略的改动，必须经具有特定评审资格的人员双签放行。
      </p>
    </div>

    {error ? (
      <div className="px-3 py-2 rounded border border-rose-800/50 bg-rose-950/40 text-[11px] text-rose-300">
        {error}
      </div>
    ) : null}

    <Panel title="审批队列" scroll>
      {approvals.length === 0 ? (
        <EmptyState label="当前没有待审批的高风险变更" />
      ) : (
        <div className="p-4 space-y-4">
          {approvals.map((approval) => (
            <ApprovalCard key={approval.request_id} approval={approval} onReview={onReview} busy={busy} />
          ))}
        </div>
      )}
    </Panel>
  </section>
);

export default GovernanceCourt;
