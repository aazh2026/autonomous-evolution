/**
 * 视图 2：演进决策沙盘。
 *
 * **三阶漏斗** —— 候选（发现了什么）→ 决策（裁决成什么）→ 任务（派发成什么）。
 * 这三个是三张表、三个外键，前端把它们画在一张卡片里是为了让人看懂因果，
 * 但**绝不能混为一谈**：覆盖决策动的是决策，不是候选。
 *
 * 每张卡片配备标准三态风险工作流（Accept / Fix / Ignore）—— 这不是只读报表。
 */

import React, { Suspense, lazy, useCallback, useMemo, useState } from 'react';
import { CheckCircle2, Link, Pencil, Scale, ShieldQuestion, XCircle } from 'lucide-react';
import type { Candidate, Decision } from '../../types/control-plane.ts';
import { confidenceVisual, resolveGrade } from '../../lib/confidence.ts';
import { useResizableSplit } from '../../hooks/useResizableSplit.ts';
import { Badge, EmptyState, LoadingState, Panel } from '../shared/Primitives.tsx';

// PDF.js 有 700KB+，只在决策沙盘里用得到。懒加载它，首屏就不用等它 ——
// 操作员点开控制舱的第一件事是看态势，不是看 PDF 规范。
const EvidenceViewer = lazy(() => import('./EvidenceViewer.tsx'));

export interface SandboxActions {
  /** 人工覆盖决策裁决。`decisionId` 是决策 id，不是候选 id。 */
  overrideDecision: (decisionId: string, action: string, reason: string) => Promise<void>;
  /** 行内编辑候选字段。 */
  updateCandidate: (
    candidateId: string,
    payload: { title?: string; hypothesis?: string; expected_outcome?: string; confidence?: number }
  ) => Promise<void>;
  /** Accept / Fix / Ignore 工作流。 */
  runWorkflow: (
    candidateId: string,
    action: 'accept' | 'fix' | 'ignore',
    reason: string
  ) => Promise<void>;
  busy: boolean;
  error: string | null;
}

interface DecisionSandboxProps {
  candidates: Candidate[];
  decisions: Decision[];
  actions: SandboxActions;
}

/** 候选 id → 决策。用后端给的 `candidate_id` 映射，不靠猜。 */
function buildDecisionIndex(decisions: Decision[]): Map<string, Decision> {
  const index = new Map<string, Decision>();
  for (const decision of decisions) {
    index.set(decision.candidate_id, decision);
  }
  return index;
}

// ---------------------------------------------------------------------------
// 优先级六维拆解
// ---------------------------------------------------------------------------

const PRIORITY_DIMENSIONS: { key: keyof NonNullable<Candidate['priority_breakdown']>; label: string }[] = [
  { key: 'value', label: '价值 V' },
  { key: 'impact', label: '影响 I' },
  { key: 'confidence', label: '置信 C' },
  { key: 'urgency', label: '紧急 U' },
  { key: 'effort', label: '成本 E' },
  { key: 'risk', label: '风险 R' },
];

const PriorityBreakdown: React.FC<{ breakdown: NonNullable<Candidate['priority_breakdown']> }> = ({
  breakdown,
}) => (
  <div className="grid grid-cols-3 gap-x-3 gap-y-1.5 mt-2">
    {PRIORITY_DIMENSIONS.map((dim) => {
      const value = breakdown[dim.key];
      // E 与 R 是分母，条形画反了会误导 —— 成本越高，条越"长"看着越像好事。
      const inverted = dim.key === 'effort' || dim.key === 'risk';
      const ratio = Math.max(0, Math.min(1, inverted ? 1 - value : value));
      return (
        <div key={dim.key}>
          <div className="flex items-center justify-between text-[10px] text-slate-500">
            <span>{dim.label}</span>
            <span className="font-mono">{value.toFixed(2)}</span>
          </div>
          <div className="h-1 mt-0.5 rounded bg-slate-800 overflow-hidden">
            <div
              className={`h-full rounded ${inverted ? 'bg-rose-500/70' : 'bg-blue-500/70'}`}
              style={{ width: `${ratio * 100}%` }}
            />
          </div>
        </div>
      );
    })}
  </div>
);

// ---------------------------------------------------------------------------
// Critic 审查清单
// ---------------------------------------------------------------------------

const CriticChecklist: React.FC<{ checks: Decision['critic_checks'] }> = ({ checks }) => {
  if (checks.length === 0) {
    return <p className="text-[11px] text-slate-600 mt-2">该决策尚未附带 Critic 质询明细。</p>;
  }
  return (
    <ul className="mt-2 space-y-1">
      {checks.map((check) => (
        <li key={check.question} className="flex items-start gap-1.5 text-[11px]">
          {check.passed ? (
            <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0 text-emerald-400" />
          ) : (
            <XCircle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-rose-400" />
          )}
          <span className={check.passed ? 'text-slate-400' : 'text-rose-300'}>{check.question}</span>
        </li>
      ))}
    </ul>
  );
};

// ---------------------------------------------------------------------------
// 行内编辑
// ---------------------------------------------------------------------------

/** 可编辑字段的显示 / 编辑切换。 */
const InlineField: React.FC<{
  label: string;
  value: string | undefined;
  multiline?: boolean;
  onSave: (next: string) => Promise<void>;
}> = ({ label, value, multiline = false, onSave }) => {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? '');

  const startEdit = () => {
    setDraft(value ?? '');
    setEditing(true);
  };

  const save = async () => {
    const next = draft.trim();
    setEditing(false);
    if (next && next !== value) {
      await onSave(next);
    }
  };

  if (!editing) {
    return (
      <div className="group mt-2">
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-slate-500">
          <span>{label}</span>
          <button
            onClick={startEdit}
            className="opacity-0 group-hover:opacity-100 transition text-slate-400 hover:text-white"
            title="行内编辑"
          >
            <Pencil className="w-3 h-3" />
          </button>
        </div>
        {value ? (
          <p className="text-[11px] text-slate-300 mt-0.5 leading-relaxed">{value}</p>
        ) : (
          <p className="text-[11px] text-slate-600 mt-0.5">（未提取）</p>
        )}
      </div>
    );
  }

  return (
    <div className="mt-2">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-blue-400">
        <span>编辑 {label}</span>
      </div>
      {multiline ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="w-full mt-1 p-2 rounded bg-slate-950 border border-blue-700/60 text-[11px] text-slate-200 outline-none focus:border-blue-500 resize-y"
          rows={3}
        />
      ) : (
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="w-full mt-1 px-2 py-1 rounded bg-slate-950 border border-blue-700/60 text-[11px] text-slate-200 outline-none focus:border-blue-500"
        />
      )}
      <div className="flex items-center gap-1.5 mt-1">
        <button
          onClick={() => void save()}
          className="px-2 py-0.5 rounded bg-blue-600 hover:bg-blue-700 text-white text-[10px] font-semibold"
        >
          保存
        </button>
        <button
          onClick={() => setEditing(false)}
          className="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[10px]"
        >
          取消
        </button>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// 候选卡片
// ---------------------------------------------------------------------------

interface CandidateCardProps {
  candidate: Candidate;
  decision: Decision | undefined;
  actions: SandboxActions;
  /** 该卡片是否是当前选中项（决定溯源窗展示谁）。 */
  selected?: boolean;
  onSelect?: () => void;
}

const CandidateCard: React.FC<CandidateCardProps> = ({
  candidate,
  decision,
  actions,
  selected = false,
  onSelect,
}) => {
  const grade = resolveGrade(candidate.confidence, candidate.confidence_grade);
  const visual = confidenceVisual(grade);
  const [showChecks, setShowChecks] = useState(false);

  const saveTitle = useCallback(
    (title: string) => actions.updateCandidate(candidate.id, { title }),
    [actions, candidate.id]
  );
  const saveHypothesis = useCallback(
    (hypothesis: string) => actions.updateCandidate(candidate.id, { hypothesis }),
    [actions, candidate.id]
  );
  const saveOutcome = useCallback(
    (expected_outcome: string) => actions.updateCandidate(candidate.id, { expected_outcome }),
    [actions, candidate.id]
  );

  const accept = () => void actions.runWorkflow(candidate.id, 'accept', '指挥官采纳');
  const ignore = () => void actions.runWorkflow(candidate.id, 'ignore', '指挥官推迟');
  const fix = () => void actions.runWorkflow(candidate.id, 'fix', '指挥官派发定向修复');

  const overrideTo = (action: string) => {
    if (!decision) return;
    const reason = window.prompt(`请输入将决策 ${decision.decision_id} 覆盖为 [${action}] 的人工理由:`);
    if (!reason) return;
    void actions.overrideDecision(decision.decision_id, action, reason);
  };

  return (
    <article
      onClick={onSelect}
      className={`p-4 rounded-lg bg-slate-900 border flex flex-col cursor-pointer transition ${
        selected
          ? 'border-blue-500 ring-1 ring-blue-500/40'
          : grade === 'speculative'
            ? 'border-rose-800/40 opacity-90'
            : visual.border
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-mono font-bold text-slate-400">{candidate.id}</span>
        <div className="flex items-center gap-1.5">
          <Badge className={visual.badge} title={`置信度 ${candidate.confidence.toFixed(2)}`}>
            置信度 {candidate.confidence.toFixed(2)} · {visual.label}
          </Badge>
          {decision ? (
            <Badge className="bg-slate-800 text-slate-300 border-slate-700">裁决: {decision.action}</Badge>
          ) : null}
        </div>
      </div>

      {/* 置信度条 */}
      <div className="h-1 mt-2 rounded bg-slate-800 overflow-hidden">
        <div
          className={`h-full rounded ${visual.bar}`}
          style={{ width: `${Math.max(0, Math.min(1, candidate.confidence)) * 100}%` }}
        />
      </div>

      <h3 className="text-sm font-semibold text-white mt-2 leading-snug">{candidate.title}</h3>

      <p className="text-[11px] text-slate-400 mt-2 bg-slate-950 p-2.5 rounded border border-slate-800 leading-relaxed">
        {candidate.rationale}
      </p>

      <div className="text-[11px] text-blue-400 mt-2 font-mono flex items-center gap-1 break-all">
        <Link className="w-3 h-3 shrink-0" />
        <span>证据: {candidate.evidence_ref}</span>
      </div>

      {candidate.code_anchor ? (
        <div className="text-[11px] text-slate-500 mt-1 font-mono break-all">
          源码锚点: {candidate.code_anchor.file}:{candidate.code_anchor.line}
        </div>
      ) : null}
      {candidate.pdf_anchor ? (
        <div className="text-[11px] text-slate-500 mt-1 font-mono break-all">
          规范锚点: {candidate.pdf_anchor.section}
        </div>
      ) : null}

      {candidate.priority_breakdown ? (
        <PriorityBreakdown breakdown={candidate.priority_breakdown} />
      ) : null}

      {/* 行内编辑 */}
      <InlineField label="演进假设" value={candidate.hypothesis} multiline onSave={saveHypothesis} />
      <InlineField label="预期收益" value={candidate.expected_outcome} multiline onSave={saveOutcome} />
      <InlineField label="标题" value={candidate.title} onSave={saveTitle} />

      {/* Critic 质询 */}
      {decision ? (
        <div className="mt-3 pt-3 border-t border-slate-800">
          <button
            onClick={() => setShowChecks((v) => !v)}
            className="flex items-center gap-1.5 text-[11px] text-slate-400 hover:text-white transition"
          >
            <ShieldQuestion className="w-3.5 h-3.5" />
            <span>
              Critic 质询 {decision.critic_checks.filter((c) => c.passed).length}/
              {decision.critic_checks.length} 通过
            </span>
            <span className="text-slate-600">{showChecks ? '▾' : '▸'}</span>
          </button>
          {showChecks ? (
            <>
              <CriticChecklist checks={decision.critic_checks} />
              {decision.rationale ? (
                <p className="text-[11px] text-slate-400 mt-2 leading-relaxed">
                  {decision.rationale}
                </p>
              ) : null}
              {decision.allocated_tasks.length > 0 ? (
                <div className="text-[11px] text-slate-500 mt-1 font-mono">
                  派发任务: {decision.allocated_tasks.join(', ')}
                </div>
              ) : null}
              {decision.overridden ? (
                <div className="text-[11px] text-amber-500/90 mt-1 leading-relaxed">
                  已被 {decision.overridden.by} 覆盖（原裁决 {decision.overridden.previous_action}）：
                  {decision.overridden.reason}
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}

      {/* 修订留痕 */}
      {candidate.revisions && candidate.revisions.length > 0 ? (
        <div className="text-[10px] text-slate-600 mt-2">
          已有 {candidate.revisions.length} 次人工修订（后端留痕）
        </div>
      ) : null}

      {/* 三态风险工作流 */}
      <div className="mt-4 pt-3 border-t border-slate-800 flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="text-slate-400">
          状态: <strong className="text-white">{candidate.status}</strong>
        </span>
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            onClick={accept}
            disabled={actions.busy}
            className="px-2.5 py-1 rounded bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white font-medium text-[11px]"
            title="采纳并立项"
          >
            Accept 采纳
          </button>
          <button
            onClick={fix}
            disabled={actions.busy}
            className="px-2.5 py-1 rounded bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-medium text-[11px]"
            title="派发定向修复"
          >
            Fix 修复
          </button>
          <button
            onClick={ignore}
            disabled={actions.busy}
            className="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-300 font-medium text-[11px]"
            title="推迟，加入反例库"
          >
            Ignore 推迟
          </button>
          {decision ? (
            <>
              <button
                onClick={() => overrideTo('record')}
                disabled={actions.busy}
                className="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-300 font-medium text-[11px]"
              >
                强制立项
              </button>
              <button
                onClick={() => overrideTo('defer')}
                disabled={actions.busy}
                className="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-300 font-medium text-[11px]"
              >
                推迟
              </button>
            </>
          ) : null}
        </div>
      </div>
    </article>
  );
};

// ---------------------------------------------------------------------------
// 主视图
// ---------------------------------------------------------------------------

const DecisionSandbox: React.FC<DecisionSandboxProps> = ({ candidates, decisions, actions }) => {
  const decisionIndex = useMemo(() => buildDecisionIndex(decisions), [decisions]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const split = useResizableSplit({ initial: 0.58, min: 0.35, max: 0.75, storageKey: 'ases.sandbox.split' });

  const selected =
    candidates.find((c) => c.id === selectedId) ?? candidates[0] ?? undefined;

  return (
    <section className="flex-1 flex flex-col min-h-0">
      <div className="px-6 pt-5 pb-3 shrink-0">
        <h2 className="text-lg font-bold text-white flex items-center space-x-2">
          <Scale className="w-5 h-5 text-purple-400" />
          <span>演进决策沙盘 (Decision Sandbox)</span>
        </h2>
        <p className="text-xs text-slate-400 mt-1">
          展示自发掘候选集、置信度分级（红黄绿）、Critic 质询与不行动（Defer/Reject）记录。
          每张卡片配备 Accept / Fix / Ignore 三态风险工作流。
        </p>
      </div>

      {actions.error ? (
        <div className="mx-6 mb-3 px-3 py-2 rounded border border-rose-800/50 bg-rose-950/40 text-[11px] text-rose-300 shrink-0">
          {actions.error}
        </div>
      ) : null}

      <div ref={split.containerRef} className="flex-1 flex min-h-0 border-t border-slate-800">
        {/* 左：候选集 */}
        <div
          className="min-w-0 overflow-y-auto p-6"
          style={{ width: `${split.size * 100}%` }}
        >
          {candidates.length === 0 ? (
            <Panel title="候选集">
              <EmptyState label="当前周期尚无演进候选" />
            </Panel>
          ) : (
            <div className="grid grid-cols-2 gap-4">
              {candidates.map((candidate) => (
                <CandidateCard
                  key={candidate.id}
                  candidate={candidate}
                  decision={decisionIndex.get(candidate.id)}
                  actions={actions}
                  selected={selected?.id === candidate.id}
                  onSelect={() => setSelectedId(candidate.id)}
                />
              ))}
            </div>
          )}
        </div>

        {/* 分隔条 */}
        <div
          {...split.separatorProps}
          className="w-1 cursor-col-resize bg-slate-800 hover:bg-blue-500 transition-colors shrink-0"
          title="拖拽调整比例，双击复位"
        />

        {/* 右：溯源窗 */}
        <div className="flex-1 min-w-0 bg-slate-900/40">
          <Suspense fallback={<LoadingState label="正在装载证据渲染器…" />}>
            <EvidenceViewer
              pdfAnchor={selected?.pdf_anchor}
              codeAnchor={selected?.code_anchor}
              title={selected ? `证据溯源 · ${selected.id}` : '证据与源码溯源'}
            />
          </Suspense>
        </div>
      </div>
    </section>
  );
};

export default DecisionSandbox;
