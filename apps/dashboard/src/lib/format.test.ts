/**
 * 格式化规则的测试。
 *
 * **重点测"缺失 ≠ 0"这条规则。** 它是整个趋势看板的诚实性基础，
 * 一旦破了，控制舱就开始说谎，而且没人看得出来。
 */

import { describe, expect, it } from 'vitest';
import {
  MISSING,
  budgetRatio,
  formatClock,
  formatCurrency,
  formatDelta,
  formatMetricValue,
  formatPercent,
  formatTimestamp,
} from './format.ts';

describe('formatMetricValue', () => {
  it('把 null 渲染成破折号，绝不渲染成 0', () => {
    expect(formatMetricValue(null)).toBe(MISSING);
    expect(formatMetricValue(undefined)).toBe(MISSING);
  });

  it('真实的 0 就是 0 —— 与缺失要区分开', () => {
    expect(formatMetricValue(0)).toBe('0.00');
  });

  it('按位数格式化', () => {
    expect(formatMetricValue(0.051, 4)).toBe('0.0510');
  });
});

describe('formatPercent', () => {
  it('缺失时是破折号，不是 0%', () => {
    expect(formatPercent(null)).toBe(MISSING);
  });

  it('正常换算成百分比', () => {
    expect(formatPercent(0.125)).toBe('12.5%');
  });
});

describe('formatDelta', () => {
  it('正数带加号', () => {
    expect(formatDelta(0.1962)).toBe('+0.1962');
  });

  it('负数保留负号', () => {
    expect(formatDelta(-0.0033)).toBe('-0.0033');
  });

  it('缺失时不带符号', () => {
    expect(formatDelta(null)).toBe(MISSING);
  });
});

describe('formatCurrency', () => {
  it('接受后端 Decimal 序列化出来的字符串', () => {
    // 后端 Pydantic v2 把 Decimal 序列化成字符串，这里不做转换会得到 NaN。
    expect(formatCurrency('1.2628')).toBe('$1.26');
    expect(formatCurrency('5.0000')).toBe('$5.00');
  });

  it('接受数字', () => {
    expect(formatCurrency(1.2628)).toBe('$1.26');
  });

  it('无法解析时是破折号，不是 NaN', () => {
    expect(formatCurrency('不是数字')).toBe(MISSING);
    expect(formatCurrency(null)).toBe(MISSING);
  });
});

describe('budgetRatio', () => {
  it('算出预算占用比例', () => {
    expect(budgetRatio('1.2628', '5.0000')).toBeCloseTo(0.25256, 4);
  });

  it('上限为零或非法时返回 null（而不是 0 或 Infinity）', () => {
    expect(budgetRatio('1', '0')).toBeNull();
    expect(budgetRatio('1', 'abc')).toBeNull();
    expect(budgetRatio(null, '5')).toBeNull();
  });
});

describe('formatTimestamp', () => {
  it('把 ISO 时间戳转成 MM-DD HH:mm', () => {
    expect(formatTimestamp('2026-11-10T19:30:00Z')).toMatch(/^\d{2}-\d{2} \d{2}:\d{2}$/);
  });

  it('无效输入原样返回', () => {
    expect(formatTimestamp('不是时间')).toBe('不是时间');
    expect(formatTimestamp(null)).toBe(MISSING);
  });
});

describe('formatClock', () => {
  it('裸时间戳原样透传', () => {
    expect(formatClock('19:30:02')).toBe('19:30:02');
  });

  it('ISO 时间戳转成本地时钟', () => {
    expect(formatClock('2026-11-10T19:30:02Z')).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });
});
