/**
 * 视图 4：独立六层验证矩阵。
 *
 * **验证独立于执行。** Agent 的自我报告不参与判定，这里展示的是独立
 * 进程跑出来的六层结果，每层保留自己的结论与细节 —— 不坍缩成一个布尔值。
 * 一个"总体通过"没法告诉操作员是哪一层擦边通过的。
 *
 * **数据只来自后端。** 控制面 API 没有暴露完整的 diff 制品端点，所以这里
 * 展示的是接口真给的 `diff_summary`，以及该任务所属候选的源码锚点片段
 * （`code_anchor.snippet` 是后端存的真实代码行）。早期版本在这里硬编码了
 * 一段假的 diff —— 那种东西看着专业，但它教操作员相信界面，而界面在说谎。
 */

import React, { useMemo, useState } from 'react';
import { FileCode2, ShieldCheck, Wrench } from 'lucide-react';
import type { Candidate, Decision, VerificationRecord } from '../../types/control-plane.ts';
import { verificationResultVisual } from '../../lib/confidence.ts';
import { Badge, EmptyState, Panel } from '../shared/Primitives.tsx';

interface VerificationMatrixProps {
  verifications: VerificationRecord[];
  candidates: Candidate[];
  decisions: Decision[];
  onDispatchRepair: (taskId: string, layerName: string) => Promise<void>;
  busy: boolean;
  error: string | null;
}

/** 找到承载某个任务的候选 —— 用来取它的源码锚点。 */
function findSourceCandidate(
  taskId: string,
  candidates: Candidate[],
  decisions: Decision[]
): Candidate | undefined {
  const decision = decisions.find((d) => d.allocated_tasks.includes(taskId));
  if (decision) {
    const matched = candidates.find((c) => c.id === decision.candidate_id);
    if (matched) return matched;
  }
  return candidates.find((c) => c.code_anchor && c.evidence_ref.includes(taskId));
}

const VerificationMatrix: React.FC<VerificationMatrixProps> = ({
  verifications,
  candidates,
  decisions,
  onDispatchRepair,
  busy,
  error,
}) => {
  const [selectedTask, setSelectedTask] = useState<string | null>(null);

  const current =
    verifications.find((v) => v.task_id === selectedTask) ?? verifications[0] ?? undefined;

  const sourceCandidate = useMemo(
    () => (current ? findSourceCandidate(current.task_id, candidates, decisions) : undefined),
    [current, candidates, decisions]
  );

  if (verifications.length === 0) {
    return (
      <section className="flex-1 p-6 overflow-y-auto space-y-6">
        <div>
          <h2 className="text-lg font-bold text-white flex items-center space-x-2">
            <ShieldCheck className="w-5 h-5 text-emerald-400" />
            <span>工作区变更摘要与独立六层验证矩阵</span>
          </h2>
        </div>
        <Panel title="六层独立验证矩阵">
          <EmptyState label="尚无验证记录" />
        </Panel>
      </section>
    );
  }

  return (
    <section className="flex-1 p-6 overflow-y-auto space-y-6">
      <div>
        <h2 className="text-lg font-bold text-white flex items-center space-x-2">
          <ShieldCheck className="w-5 h-5 text-emerald-400" />
          <span>工作区变更摘要与独立六层验证矩阵</span>
        </h2>
        <p className="text-xs text-slate-400 mt-1">
          六层验证由独立进程判定并逐层留痕，绝不坍缩为单薄的布尔值。
        </p>
      </div>

      {error ? (
        <div className="px-3 py-2 rounded border border-rose-800/50 bg-rose-950/40 text-[11px] text-rose-300">
          {error}
        </div>
      ) : null}

      {verifications.length > 1 ? (
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-slate-500">任务:</span>
          {verifications.map((v) => (
            <button
              key={v.task_id}
              onClick={() => setSelectedTask(v.task_id)}
              className={`px-2 py-0.5 rounded text-[11px] font-mono border transition ${
                current?.task_id === v.task_id
                  ? 'bg-slate-800 text-white border-slate-600'
                  : 'bg-slate-900 text-slate-400 border-slate-800 hover:text-white'
              }`}
            >
              {v.task_id}
            </button>
          ))}
        </div>
      ) : null}

      {current ? (
        <div className="grid grid-cols-2 gap-4">
          {/* 左：变更摘要与源码锚点 */}
          <div className="rounded-lg bg-slate-900 border border-slate-800 flex flex-col overflow-hidden">
            <div className="px-3 py-2 bg-slate-800/50 border-b border-slate-800 text-xs font-semibold text-slate-300 flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5">
                <FileCode2 className="w-3.5 h-3.5 text-cyan-400" />
                工作区变更摘要
              </span>
              <span className="text-[10px] text-slate-500 font-mono truncate max-w-[50%]">
                {current.diff_summary}
              </span>
            </div>
            <div className="flex-1 p-3 overflow-auto">
              {sourceCandidate?.code_anchor ? (
                <>
                  <div className="text-[11px] text-slate-400">
                    源码锚点:{' '}
                    <span className="font-mono text-cyan-300">
                      {sourceCandidate.code_anchor.file}:{sourceCandidate.code_anchor.line}
                    </span>
                  </div>
                  <pre className="mt-2 p-2.5 rounded bg-slate-950 border border-slate-800 text-[11px] font-mono text-slate-300 overflow-auto leading-relaxed">
                    {sourceCandidate.code_anchor.snippet}
                  </pre>
                  <div className="mt-2 text-[11px] text-slate-500">
                    候选: <span className="font-mono text-blue-300">{sourceCandidate.id}</span> ·{' '}
                    {sourceCandidate.title}
                  </div>
                </>
              ) : (
                <div className="text-[11px] text-slate-500 leading-relaxed">
                  <p className="text-slate-400 mb-1">变更摘要</p>
                  <p className="font-mono">{current.diff_summary}</p>
                  <p className="mt-3 text-slate-600">
                    控制面 API 目前只暴露变更摘要，完整 diff 制品端点尚未开放 ——
                    这里不展示任何未经后端证实的代码内容。
                  </p>
                </div>
              )}
              <div className="mt-3 pt-3 border-t border-slate-800 text-[11px] text-slate-500">
                验证时间: <span className="font-mono">{current.verified_at}</span>
              </div>
            </div>
          </div>

          {/* 右：六层矩阵 */}
          <div className="rounded-lg bg-slate-900 border border-slate-800 flex flex-col overflow-hidden">
            <div className="px-3 py-2 bg-slate-800/50 border-b border-slate-800 text-xs font-semibold text-slate-300 flex items-center justify-between gap-2">
              <span>六层独立验证矩阵 ({current.task_id})</span>
              <Badge
                className={
                  current.overall_result.toLowerCase() === 'passed'
                    ? 'bg-emerald-950 text-emerald-300 border-emerald-700/60'
                    : 'bg-rose-950 text-rose-300 border-rose-700/60'
                }
              >
                总体 {current.overall_result}
              </Badge>
            </div>
            <div className="p-4 space-y-2.5 overflow-y-auto">
              {current.layers.map((layer, idx) => {
                const visual = verificationResultVisual(layer.result);
                const failed = layer.result.toLowerCase() !== 'passed';
                return (
                  <div
                    key={layer.name}
                    className="p-2.5 rounded bg-slate-950 border border-slate-800"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className="text-xs font-semibold text-slate-200">
                        {idx + 1}. {layer.label}
                      </span>
                      <Badge className={visual.badge}>{visual.label}</Badge>
                    </div>
                    <div className={`text-[11px] mt-1 ${visual.text}`}>{layer.details}</div>
                    {failed ? (
                      <button
                        onClick={() => void onDispatchRepair(current.task_id, layer.name)}
                        disabled={busy}
                        className="mt-2 px-2 py-0.5 rounded bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white text-[10px] font-semibold inline-flex items-center gap-1"
                      >
                        <Wrench className="w-3 h-3" />
                        派发定向修复
                      </button>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
};

export default VerificationMatrix;
