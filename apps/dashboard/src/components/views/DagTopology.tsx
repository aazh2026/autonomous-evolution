/**
 * 视图 3：DAG 多任务调度与执行器舰队。
 *
 * **级联阻断必须看得见。** 上游失败导致下游进入 `BLOCKED` 时，光标一个
 * "BLOCKED" 标签不够 —— 操作员需要知道"是谁挡的"，否则只能自己去查日志。
 * 所以被阻断的节点带着 `blocked_by` 根因徽标，连向它的边画成红色虚线。
 *
 * 布局用波次做列（x = 波次，y = 波次内序），边走 SVG 贝塞尔曲线。
 * 自己画而不用图库，是因为波次列布局太规整，图库那套自动布局反而会
 * 把"波次"这个最有业务含义的信息抹掉。
 */

import React, { useMemo, useState } from 'react';
import { GitMerge, Unplug } from 'lucide-react';
import type { DagEdge, DagNode, ExecutorProfile } from '../../types/control-plane.ts';
import { dagStatusVisual } from '../../lib/confidence.ts';
import { Badge, EmptyState, Panel } from '../shared/Primitives.tsx';

interface DagTopologyProps {
  nodes: DagNode[];
  edges: DagEdge[];
  waves: string[][];
  executors: ExecutorProfile[];
}

const NODE_W = 152;
const NODE_H = 62;
const GAP_X = 68;
const GAP_Y = 18;
const PADDING = 16;

interface Positioned extends DagNode {
  x: number;
  y: number;
}

/** 给每个节点算坐标：x 按波次，y 按波次内的排布。 */
function layoutNodes(nodes: DagNode[], waves: string[][]): Positioned[] {
  const positioned: Positioned[] = [];

  waves.forEach((waveTasks, waveIdx) => {
    waveTasks.forEach((taskId, rowIdx) => {
      const node = nodes.find((n) => n.task_id === taskId);
      if (!node) return;
      positioned.push({
        ...node,
        x: PADDING + waveIdx * (NODE_W + GAP_X),
        y: PADDING + rowIdx * (NODE_H + GAP_Y),
      });
    });
  });

  // 不在波次列表里的节点兜底排到最后一列，避免"任务消失了"。
  const known = new Set(positioned.map((n) => n.task_id));
  const orphans = nodes.filter((n) => !known.has(n.task_id));
  const maxWave = waves.length;
  orphans.forEach((node, idx) => {
    positioned.push({
      ...node,
      x: PADDING + maxWave * (NODE_W + GAP_X),
      y: PADDING + idx * (NODE_H + GAP_Y),
    });
  });

  return positioned;
}

/** 判断一条边是否落在"级联阻断"路径上：终点被阻断，且源头就是它的根因。 */
function isBlockingEdge(edge: DagEdge, nodeById: Map<string, DagNode>): boolean {
  const successor = nodeById.get(edge.successor_id);
  if (!successor || successor.status !== 'BLOCKED') return false;
  return successor.blocked_by === edge.predecessor_id;
}

const DagTopology: React.FC<DagTopologyProps> = ({ nodes, edges, waves, executors }) => {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const positioned = useMemo(() => layoutNodes(nodes, waves), [nodes, waves]);
  const positionById = useMemo(
    () => new Map(positioned.map((n) => [n.task_id, n])),
    [positioned]
  );
  const nodeById = useMemo(() => new Map(nodes.map((n) => [n.task_id, n])), [nodes]);

  const canvasWidth =
    PADDING * 2 +
    Math.max(1, waves.length) * NODE_W +
    Math.max(0, waves.length - 1) * GAP_X +
    40;
  const canvasHeight = Math.max(
    220,
    PADDING * 2 +
      Math.max(1, ...waves.map((w) => w.length)) * NODE_H +
      Math.max(0, Math.max(1, ...waves.map((w) => w.length)) - 1) * GAP_Y
  );

  const selected = selectedId ? nodeById.get(selectedId) : undefined;

  return (
    <section className="flex-1 p-6 overflow-y-auto space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-white flex items-center space-x-2">
            <GitMerge className="w-5 h-5 text-cyan-400" />
            <span>DAG 多任务调度拓扑流</span>
          </h2>
          <p className="text-xs text-slate-400 mt-1">
            按波次分层调度并发执行；依赖未满足不启动；上游失败下游全量级联阻断。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          {(
            [
              ['COMPLETED', '已完成'],
              ['RUNNING', '执行中'],
              ['READY', '就绪'],
              ['BLOCKED', '级联阻断'],
            ] as const
          ).map(([status, label]) => {
            const visual = dagStatusVisual(status);
            return (
              <span key={status} className="flex items-center gap-1">
                <span className={`w-2.5 h-2.5 rounded-full ${visual.dot}`} />
                <span className="text-slate-300">{label}</span>
              </span>
            );
          })}
        </div>
      </div>

      {nodes.length === 0 ? (
        <Panel title="任务依赖图">
          <EmptyState label="当前没有任务依赖图数据" />
        </Panel>
      ) : (
        <Panel
          title="任务依赖图（按执行波次分层）"
          actions={
            selected ? (
              <span className="text-[10px] text-slate-400">
                已选 {selected.task_id} · {selected.executor}
              </span>
            ) : (
              <span className="text-[10px] text-slate-500">点击节点查看详情</span>
            )
          }
        >
          <div className="p-2 overflow-auto">
            <svg
              width={canvasWidth}
              height={canvasHeight}
              className="min-w-full"
              role="img"
              aria-label="任务依赖拓扑图"
            >
              <defs>
                <marker
                  id="dag-arrow"
                  markerWidth={8}
                  markerHeight={8}
                  refX={7}
                  refY={4}
                  orient="auto"
                >
                  <path d="M0,0 L8,4 L0,8 z" fill="#475569" />
                </marker>
                <marker
                  id="dag-arrow-blocked"
                  markerWidth={8}
                  markerHeight={8}
                  refX={7}
                  refY={4}
                  orient="auto"
                >
                  <path d="M0,0 L8,4 L0,8 z" fill="#fb7185" />
                </marker>
              </defs>

              {/* 边 */}
              {edges.map((edge) => {
                const from = positionById.get(edge.predecessor_id);
                const to = positionById.get(edge.successor_id);
                if (!from || !to) return;

                const x1 = from.x + NODE_W;
                const y1 = from.y + NODE_H / 2;
                const x2 = to.x;
                const y2 = to.y + NODE_H / 2;
                const mid = (x1 + x2) / 2;
                const path = `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
                const blocking = isBlockingEdge(edge, nodeById);

                return (
                  <path
                    key={`${edge.predecessor_id}->${edge.successor_id}`}
                    d={path}
                    fill="none"
                    stroke={blocking ? '#fb7185' : '#475569'}
                    strokeWidth={blocking ? 1.8 : 1.2}
                    strokeDasharray={blocking ? '5 3' : undefined}
                    markerEnd={blocking ? 'url(#dag-arrow-blocked)' : 'url(#dag-arrow)'}
                  />
                );
              })}

              {/* 节点 */}
              {positioned.map((node) => {
                const visual = dagStatusVisual(node.status);
                const isSelected = node.task_id === selectedId;
                return (
                  <g
                    key={node.task_id}
                    transform={`translate(${node.x}, ${node.y})`}
                    onClick={() => setSelectedId(isSelected ? null : node.task_id)}
                    className="cursor-pointer"
                  >
                    <rect
                      width={NODE_W}
                      height={NODE_H}
                      rx={7}
                      fill="#0f172a"
                      stroke={isSelected ? '#38bdf8' : '#1e293b'}
                      strokeWidth={isSelected ? 2 : 1}
                    />
                    <rect width={4} height={NODE_H} rx={2} fill={visual.dot.replace('bg-', 'bg-')} />
                    <text x={12} y={20} fill="#e2e8f0" fontSize={11} fontWeight={700}>
                      {node.task_id}
                    </text>
                    <text x={NODE_W - 10} y={20} fill="#94a3b8" fontSize={9} textAnchor="end">
                      W{node.wave}
                    </text>
                    {/* 标题截断，避免长标题溢出卡片。 */}
                    <text x={12} y={35} fill="#cbd5e1" fontSize={10}>
                      {node.title.length > 20 ? `${node.title.slice(0, 20)}…` : node.title}
                    </text>
                    <text x={12} y={51} fill="#64748b" fontSize={9}>
                      {node.executor} · P{node.priority}
                    </text>
                    <circle
                      cx={NODE_W - 14}
                      cy={50}
                      r={4}
                      fill={visual.dot === 'bg-emerald-500' ? '#10b981' : visual.dot === 'bg-blue-500' ? '#3b82f6' : visual.dot === 'bg-rose-500' ? '#f43f5e' : '#64748b'}
                    />
                  </g>
                );
              })}
            </svg>
          </div>
        </Panel>
      )}

      {/* 选中节点的详情 */}
      {selected ? (
        <Panel title={`节点详情 · ${selected.task_id}`}>
          <div className="p-4 text-xs space-y-1.5 text-slate-300">
            <div>标题: <span className="text-white">{selected.title}</span></div>
            <div>
              状态: <Badge className={dagStatusVisual(selected.status).badge}>{selected.status}</Badge>
            </div>
            <div>
              类型: <span className="font-mono">{selected.type}</span> · 执行器:{' '}
              <span className="font-mono text-blue-400">{selected.executor}</span>
            </div>
            <div>
              优先级: <span className="font-mono">{selected.priority}</span> · 波次:{' '}
              <span className="font-mono">Wave {selected.wave}</span>
            </div>
            {selected.blocked_by ? (
              <div className="text-rose-300">
                级联阻断根因: <span className="font-mono">{selected.blocked_by}</span>
                {selected.blockage_reason ? ` · ${selected.blockage_reason}` : ''}
              </div>
            ) : null}
          </div>
        </Panel>
      ) : null}

      {/* 执行器舰队 */}
      <Panel title="执行器集群舰队（动态选择器画像）">
        {executors.length === 0 ? (
          <EmptyState label="暂无执行器数据" />
        ) : (
          <div className="grid grid-cols-2 gap-3 p-4">
            {executors.map((executor) => (
              <div
                key={executor.name}
                className="p-3 rounded bg-slate-950 border border-slate-800"
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-slate-200">{executor.label}</span>
                  {executor.is_healthy ? (
                    <span className="text-[10px] text-emerald-400">健康</span>
                  ) : (
                    <span className="text-[10px] text-rose-400">不健康</span>
                  )}
                </div>
                <div className="flex items-center gap-3 mt-1.5 text-[11px] text-slate-500">
                  <span>
                    负载{' '}
                    <span className="font-mono text-slate-300">
                      {executor.current_load}/{executor.max_concurrency}
                    </span>
                  </span>
                  <span>
                    成功率{' '}
                    <span className="font-mono text-slate-300">
                      {(executor.success_rate * 100).toFixed(0)}%
                    </span>
                  </span>
                  <span>
                    成本档 <span className="font-mono text-slate-300">{executor.cost_tier}</span>
                  </span>
                </div>
                <div className="flex flex-wrap gap-1 mt-2">
                  {executor.affinity.map((tag) => (
                    <span
                      key={tag}
                      className="px-1.5 py-0.5 rounded bg-slate-800 text-[10px] text-cyan-300 border border-slate-700"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
                <div className="text-[10px] text-slate-600 mt-1.5 flex items-center gap-1">
                  <Unplug className="w-3 h-3" />
                  <span>能力: {executor.capabilities.join(', ')}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>
    </section>
  );
};

export default DagTopology;
