/**
 * 控制舱应用外壳。
 *
 * **这里是"前后端分离"的落点**：前端不拥有任何业务状态，所有数据都从
 * `/api/v1/*` 拿，所有动作都打回 `/api/v1/*`。组件里没有一行硬编码的
 * 指标、没有一行假数据 —— 界面上看到的每一个数字，背后都有一个后端端点。
 *
 * 状态分两层：
 * - `useCockpitData` 管**读**（轮询、装载、错误）；
 * - `useAsyncAction` 管**写**（挂起态、错误态）。
 *
 * 写操作完成后统一 `refresh()` 重新拉取，而不是在本地改状态 —— 后端是
 * 唯一事实来源，本地乐观更新一旦和后端不一致，界面就会开始说谎。
 */

import React, { useCallback, useState } from 'react';
import { TopBar } from './components/TopBar.tsx';
import { Sidebar, type TabKey } from './components/Sidebar.tsx';
import { TelemetryDrawer } from './components/TelemetryDrawer.tsx';
import DualLoopDashboard from './components/views/DualLoopDashboard.tsx';
import DecisionSandbox from './components/views/DecisionSandbox.tsx';
import DagTopology from './components/views/DagTopology.tsx';
import VerificationMatrix from './components/views/VerificationMatrix.tsx';
import GovernanceCourt from './components/views/GovernanceCourt.tsx';
import { FatalError, LoadingState } from './components/shared/Primitives.tsx';
import { useCockpitData } from './hooks/useCockpitData.ts';
import { useAsyncAction } from './hooks/useAsyncAction.ts';
import { useResizableSplit } from './hooks/useResizableSplit.ts';
import {
  dispatchRepair,
  overrideDecision,
  reviewApproval,
  triggerCandidateWorkflow,
  updateAutonomy,
  updateCandidate,
} from './api/client.ts';
import type { AutonomyLevel } from './types/control-plane.ts';

/** 演练用的评审人标识。后端把它写进审计记录。 */
const REVIEWER = 'chief-architect';

export const App: React.FC = () => {
  const [currentTab, setCurrentTab] = useState<TabKey>('dashboard');
  const { data, loading, error, partialErrors, refresh } = useCockpitData();

  // 主舞台与底部抽屉的比例（纵向）；侧栏与主舞台的比例（横向）。
  const verticalSplit = useResizableSplit({
    initial: 0.82,
    min: 0.45,
    max: 0.95,
    storageKey: 'ases.main.vertical',
    direction: 'vertical',
  });
  const horizontalSplit = useResizableSplit({
    initial: 0.16,
    min: 0.1,
    max: 0.3,
    storageKey: 'ases.main.horizontal',
  });

  // ---- 写操作 ----

  const autonomyAction = useAsyncAction(async (payload: {
    autonomy_level?: AutonomyLevel;
    is_paused?: boolean;
  }) => {
    await updateAutonomy(payload);
    await refresh();
  });

  const overrideAction = useAsyncAction(
    async (decisionId: string, action: string, reason: string) => {
      await overrideDecision(decisionId, {
        override_action: action,
        reviewer: REVIEWER,
        reason,
      });
      await refresh();
    }
  );

  const candidateUpdateAction = useAsyncAction(
    async (
      candidateId: string,
      payload: {
        title?: string;
        hypothesis?: string;
        expected_outcome?: string;
        confidence?: number;
      }
    ) => {
      await updateCandidate(candidateId, { ...payload, reviewer: REVIEWER });
      await refresh();
    }
  );

  const workflowAction = useAsyncAction(
    async (candidateId: string, action: 'accept' | 'fix' | 'ignore', reason: string) => {
      await triggerCandidateWorkflow(candidateId, { action, reviewer: REVIEWER, reason });
      await refresh();
    }
  );

  const reviewAction = useAsyncAction(
    async (
      requestId: string,
      action: 'approve' | 'reject',
      scopes: string[],
      comment: string,
      contentDigest: string
    ) => {
      await reviewApproval(requestId, {
        action,
        reviewer: REVIEWER,
        scopes,
        comment,
        content_digest: contentDigest,
      });
      await refresh();
    }
  );

  const repairAction = useAsyncAction(async (taskId: string, scope: string) => {
    await dispatchRepair(taskId, { reviewer: REVIEWER, repair_scope: scope });
    await refresh();
  });

  // ---- 顶栏动作 ----

  const handleSetAutonomy = useCallback(
    (lvl: AutonomyLevel) => {
      void autonomyAction.run({ autonomy_level: lvl });
    },
    [autonomyAction]
  );

  const handleTogglePause = useCallback(() => {
    if (!data.status) return;
    void autonomyAction.run({ is_paused: !data.status.is_paused });
  }, [autonomyAction, data.status]);

  const handleEmergencyStop = useCallback(() => {
    const confirmed = window.confirm(
      '警告：是否确认触发物理级急停熔断？系统将立即中断全部执行器并冻结租约！'
    );
    if (!confirmed) return;
    void autonomyAction.run({ is_paused: true });
  }, [autonomyAction]);

  // ---- 整体不可达 ----

  if (error && !data.status) {
    return (
      <div className="h-full flex flex-col bg-slate-950 text-slate-100">
        <TopBar
          status={null}
          onSetAutonomy={handleSetAutonomy}
          onTogglePause={handleTogglePause}
          onEmergencyStop={handleEmergencyStop}
        />
        <FatalError message={error} onRetry={() => void refresh()} />
      </div>
    );
  }

  const pendingApprovalsCount = data.approvals.filter((a) => a.status === 'pending').length;

  return (
    <div className="h-full flex flex-col font-sans overflow-hidden bg-slate-950 text-slate-100">
      <TopBar
        status={data.status}
        onSetAutonomy={handleSetAutonomy}
        onTogglePause={handleTogglePause}
        onEmergencyStop={handleEmergencyStop}
        busy={autonomyAction.pending}
      />

      <div ref={verticalSplit.containerRef} className="flex-1 flex flex-col min-h-0">
        <div className="flex min-h-0" style={{ height: `${verticalSplit.size * 100}%` }}>
          {/* 侧栏 */}
          <div
            className="shrink-0 min-h-0"
            style={{ width: `${horizontalSplit.size * 100}%` }}
          >
            <Sidebar
              currentTab={currentTab}
              onSelectTab={setCurrentTab}
              pendingApprovalsCount={pendingApprovalsCount}
              cycles={data.cycles}
              executors={data.executors}
              status={data.status}
            />
          </div>

          <div
            {...horizontalSplit.separatorProps}
            className="w-1 cursor-col-resize bg-slate-800 hover:bg-blue-500 transition-colors shrink-0"
            title="拖拽调整侧栏宽度，双击复位"
          />

          {/* 主舞台 */}
          <main className="flex-1 min-w-0 flex flex-col bg-slate-950 overflow-hidden">
            {loading ? (
              <LoadingState />
            ) : (
              <>
                {currentTab === 'dashboard' ? (
                  <DualLoopDashboard
                    cycles={data.cycles}
                    trends={data.trends}
                    trendError={partialErrors.trends}
                    onRetry={() => void refresh()}
                  />
                ) : null}

                {currentTab === 'sandbox' ? (
                  <DecisionSandbox
                    candidates={data.candidates}
                    decisions={data.decisions}
                    actions={{
                      overrideDecision: async (decisionId, action, reason) => {
                        await overrideAction.run(decisionId, action, reason);
                      },
                      updateCandidate: async (candidateId, payload) => {
                        await candidateUpdateAction.run(candidateId, payload);
                      },
                      runWorkflow: async (candidateId, action, reason) => {
                        await workflowAction.run(candidateId, action, reason);
                      },
                      busy:
                        overrideAction.pending ||
                        candidateUpdateAction.pending ||
                        workflowAction.pending,
                      error:
                        overrideAction.error ?? candidateUpdateAction.error ?? workflowAction.error,
                    }}
                  />
                ) : null}

                {currentTab === 'dag' ? (
                  <DagTopology
                    nodes={data.dag.nodes}
                    edges={data.dag.edges}
                    waves={data.dag.waves}
                    executors={data.executors}
                  />
                ) : null}

                {currentTab === 'verify' ? (
                  <VerificationMatrix
                    verifications={data.verifications}
                    candidates={data.candidates}
                    decisions={data.decisions}
                    busy={repairAction.pending}
                    error={repairAction.error}
                    onDispatchRepair={async (taskId, layerName) => {
                      await repairAction.run(taskId, layerName);
                    }}
                  />
                ) : null}

                {currentTab === 'governance' ? (
                  <GovernanceCourt
                    approvals={data.approvals}
                    busy={reviewAction.pending}
                    error={reviewAction.error}
                    onReview={async (requestId, action, scopes, comment, contentDigest) => {
                      await reviewAction.run(requestId, action, scopes, comment, contentDigest);
                    }}
                  />
                ) : null}
              </>
            )}
          </main>
        </div>

        <div
          {...verticalSplit.separatorProps}
          className="h-1 cursor-row-resize bg-slate-800 hover:bg-blue-500 transition-colors shrink-0"
          title="拖拽调整遥测抽屉高度，双击复位"
        />

        {/* 底部遥测抽屉 */}
        <div className="min-h-0 shrink-0" style={{ height: `${(1 - verticalSplit.size) * 100}%` }}>
          <TelemetryDrawer
            events={data.events}
            onRefresh={() => void refresh()}
            connected={!partialErrors.events && !partialErrors.status}
          />
        </div>
      </div>
    </div>
  );
};
