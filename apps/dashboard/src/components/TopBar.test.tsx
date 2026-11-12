/**
 * 顶部控制中枢的渲染测试。
 *
 * 重点：**预算水位必须来自 API**。早期版本把 `$1.26 / $5.00` 写死在界面里，
 * 于是后端真实花多少钱都看不见 —— 预算条是操作员判断"还能不能再跑一轮"
 * 的唯一依据，写死等于没有。
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TopBar } from './TopBar.tsx';
import type { SystemStatus } from '../types/control-plane.ts';

function status(overrides: Partial<SystemStatus> = {}): SystemStatus {
  return {
    project_id: 'pilot-tinylib',
    project_name: 'TinyLib 试点',
    autonomy_level: 'L2',
    is_paused: false,
    active_cycle_id: 'CYCLE-0007',
    budget_spent_usd: '3.4000',
    budget_daily_cap_usd: '5.0000',
    active_executors_count: 2,
    max_concurrency: 4,
    currency: 'USD',
    system_health: 'healthy',
    ...overrides,
  };
}

const noop = vi.fn();

describe('TopBar 预算水位', () => {
  it('显示后端给的真实金额，不是写死的默认值', () => {
    render(
      <TopBar
        status={status()}
        onSetAutonomy={noop}
        onTogglePause={noop}
        onEmergencyStop={noop}
      />
    );

    // 3.4000 / 5.0000 —— 旧版本这里恒为 "$1.26 / $5.00"
    expect(screen.getByText('$3.40 / $5.00')).toBeInTheDocument();
    expect(screen.queryByText('$1.26 / $5.00')).not.toBeInTheDocument();
  });

  it('预算条宽度跟随真实占用比例', () => {
    const { container } = render(
      <TopBar
        status={status()}
        onSetAutonomy={noop}
        onTogglePause={noop}
        onEmergencyStop={noop}
      />
    );

    // 3.4 / 5 = 68%
    const bar = container.querySelector('div[style]');
    expect(bar?.getAttribute('style')).toContain('width: 68%');
  });

  it('超支时进度条封顶在 100%（不会溢出布局）', () => {
    const { container } = render(
      <TopBar
        status={status({ budget_spent_usd: '7.5000' })}
        onSetAutonomy={noop}
        onTogglePause={noop}
        onEmergencyStop={noop}
      />
    );

    const bar = container.querySelector('div[style]');
    expect(bar?.getAttribute('style')).toContain('width: 100%');
  });

  it('后端没给状态时显示破折号，不显示 0', () => {
    render(
      <TopBar
        status={null}
        onSetAutonomy={noop}
        onTogglePause={noop}
        onEmergencyStop={noop}
      />
    );

    // 项目 id 与预算两处都是"缺失"，不是 0。
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    expect(screen.queryByText('$0.00 / $0.00')).not.toBeInTheDocument();
  });
});

describe('TopBar 自主权与运行态', () => {
  it('高亮当前自主权等级', () => {
    render(
      <TopBar
        status={status({ autonomy_level: 'L3' })}
        onSetAutonomy={noop}
        onTogglePause={noop}
        onEmergencyStop={noop}
      />
    );

    const l3 = screen.getByTitle('L3 全自主');
    expect(l3.className).toContain('bg-blue-600');
    const l2 = screen.getByTitle('L2 半自主');
    expect(l2.className).not.toContain('bg-blue-600');
  });

  it('暂停时按钮文案切换成"恢复演进"', () => {
    render(
      <TopBar
        status={status({ is_paused: true })}
        onSetAutonomy={noop}
        onTogglePause={noop}
        onEmergencyStop={noop}
      />
    );

    expect(screen.getByText('恢复演进')).toBeInTheDocument();
    expect(screen.queryByText('暂停闭环')).not.toBeInTheDocument();
  });

  it('显示真实并发占用', () => {
    render(
      <TopBar
        status={status({ active_executors_count: 3, max_concurrency: 4 })}
        onSetAutonomy={noop}
        onTogglePause={noop}
        onEmergencyStop={noop}
      />
    );

    expect(screen.getByText('3/4')).toBeInTheDocument();
  });
});
