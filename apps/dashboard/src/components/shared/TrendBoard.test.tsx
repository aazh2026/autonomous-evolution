/**
 * 趋势看板的渲染测试。
 *
 * 守的是**看板的诚实性**：缺失的点必须显示成"缺失"，不能显示成 0。
 * 这条一旦破了，看板就开始说谎 —— 而且看起来完全正常。
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import TrendBoard from './TrendBoard.tsx';
import type { TrendMetric } from '../../types/control-plane.ts';

function metric(overrides: Partial<TrendMetric> = {}): TrendMetric {
  return {
    metric_id: 'duplicate_discovery_rate',
    layer: 'discovery',
    layer_label: '自主发现层',
    label: '重复发现率',
    is_diagnostic: false,
    status: 'computed',
    missing_reason: null,
    points: [0.1, 0.2, 0.3],
    ...overrides,
  };
}

const THREE_DAYS = ['2026-11-08', '2026-11-09', '2026-11-10'];

describe('TrendBoard 缺失值渲染', () => {
  it('最新一天无样本时显示破折号，绝不显示 0.00', () => {
    const { container } = render(
      <TrendBoard metrics={[metric({ points: [0.1, 0.2, null] })]} days={THREE_DAYS} />
    );

    expect(screen.getByText('—')).toBeInTheDocument();
    expect(container.textContent).not.toContain('0.00');
  });

  it('真实的 0 就是 0.00 —— 与缺失区分开', () => {
    const { container } = render(
      <TrendBoard metrics={[metric({ points: [0.1, 0.2, 0] })]} days={THREE_DAYS} />
    );

    expect(container.textContent).toContain('0.00');
    expect(container.textContent).not.toContain('—');
  });

  it('状态非 computed 时显示破折号，并把后端给的缺失原因原样摊开', () => {
    render(
      <TrendBoard
        metrics={[
          metric({
            status: 'baseline_missing',
            missing_reason: '缺少人工标注基线，无法计算回归缺陷率',
            points: [null, null, null],
          }),
        ]}
        days={THREE_DAYS}
      />
    );

    expect(screen.getByText('—')).toBeInTheDocument();
    // 缺失原因同时出现在说明文字和 tooltip 里 —— 两处都在，才算真的摊开了。
    expect(screen.getAllByText('缺少人工标注基线，无法计算回归缺陷率').length).toBeGreaterThan(0);
    expect(screen.getByText('无样本')).toBeInTheDocument();
  });
});

describe('TrendBoard 图形语义', () => {
  it('缺失点画成空心虚线圆点（落在基线上，不是零值位置）', () => {
    const { container } = render(
      <TrendBoard metrics={[metric({ points: [0.1, null, 0.3] })]} days={THREE_DAYS} />
    );

    const dashed = container.querySelectorAll('circle[stroke-dasharray]');
    expect(dashed).toHaveLength(1);
    expect(dashed[0]?.getAttribute('fill')).toBe('none');
    // 落在基线上：cy 等于 CHART_HEIGHT - PADDING = 34 - 3 = 31
    expect(dashed[0]?.getAttribute('cy')).toBe('31');
  });

  it('缺失点带 tooltip 说明为什么没有', () => {
    const { container } = render(
      <TrendBoard
        metrics={[
          metric({ points: [0.1, null, 0.3], missing_reason: '该日无样本（执行器离线）' }),
        ]}
        days={THREE_DAYS}
      />
    );

    const title = container.querySelector('circle[stroke-dasharray] title');
    expect(title?.textContent).toBe('该日无样本（执行器离线）');
  });

  it('折线在缺失处断开 —— 不跨越缺失点连线', () => {
    const { container } = render(
      <TrendBoard metrics={[metric({ points: [0.1, null, 0.3] })]} days={THREE_DAYS} />
    );

    // 两段：一段是第一个点，一段是第三个点。
    expect(container.querySelectorAll('polyline')).toHaveLength(2);
  });

  it('没有缺失时是完整的一条折线', () => {
    const { container } = render(
      <TrendBoard metrics={[metric({ points: [0.1, 0.2, 0.3] })]} days={THREE_DAYS} />
    );

    expect(container.querySelectorAll('polyline')).toHaveLength(1);
  });
});

describe('TrendBoard 分组', () => {
  it('按层级分组展示，诊断量带标记', () => {
    render(
      <TrendBoard
        metrics={[
          metric(),
          metric({
            metric_id: 'cycle_duration_hours',
            layer: 'diagnostic',
            label: '周期时长',
            is_diagnostic: true,
          }),
        ]}
        days={THREE_DAYS}
      />
    );

    expect(screen.getByText('自主发现层')).toBeInTheDocument();
    expect(screen.getByText('诊断量（背景参考）')).toBeInTheDocument();
    expect(screen.getByText('诊断量')).toBeInTheDocument();
  });
});
