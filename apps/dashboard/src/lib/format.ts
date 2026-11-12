/**
 * 数值与时间格式化。
 *
 * **有一条不能破的规矩：算不出来就显示 `—`，绝不显示 `0`。**
 *
 * 趋势看板的 `points` 里 `null` 表示"该日无样本"或"缺人工标注基线"。
 * 把它渲染成 `0%` 就是在说谎 —— 一个"回归缺陷率 0%"的看板比没有看板
 * 更危险，因为看板上的人会信它。所以 `formatMetricValue` 对 `null` 一律
 * 返回 `—`，由调用方附上原因说明。
 */

/** 缺失值的统一显示。 */
export const MISSING = '—';

/**
 * 指标点 → 展示文本。
 *
 * `null` → `—`。数字按指标量级决定小数位：比率类 2 位，费用类 2 位，
 * 达成度增量 4 位（Δ 通常很小，两位小数会把 0.051 显示成 0.05）。
 */
export function formatMetricValue(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return MISSING;
  }
  return value.toFixed(digits);
}

/** 比率 → 百分比文本。`null` 仍然是 `—`，不是 `0%`。 */
export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return MISSING;
  }
  return `${(value * 100).toFixed(digits)}%`;
}

/** 带符号的增量（用于 Δ 这类"涨了还是跌了"的语义）。 */
export function formatDelta(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return MISSING;
  }
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(digits)}`;
}

/**
 * 货币金额。
 *
 * 后端的 `budget_spent_usd` 是 `Decimal`，序列化成**字符串**（"1.2628"）。
 * 这里显式 `Number()` 转一下 —— 直接对字符串做算术会得到 `NaN`，
 * 而且 `NaN` 在界面上会渲染成 "NaN"，很难一眼看出是类型问题。
 */
export function formatCurrency(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === '') {
    return MISSING;
  }
  const num = typeof value === 'number' ? value : Number(value);
  if (Number.isNaN(num)) {
    return MISSING;
  }
  return `$${num.toFixed(digits)}`;
}

/** 预算占用比例（0~1）。算不出比例时返回 `null` 而不是 0。 */
export function budgetRatio(
  spent: string | number | null | undefined,
  cap: string | number | null | undefined
): number | null {
  if (spent === null || spent === undefined || cap === null || cap === undefined) {
    return null;
  }
  const spentNum = typeof spent === 'number' ? spent : Number(spent);
  const capNum = typeof cap === 'number' ? cap : Number(cap);
  if (Number.isNaN(spentNum) || Number.isNaN(capNum) || capNum <= 0) {
    return null;
  }
  return spentNum / capNum;
}

/** ISO 时间戳 → `MM-DD HH:mm`。无效输入原样返回。 */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return MISSING;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes()
  )}`;
}

/** ISO 时间戳 → 本地 `HH:mm:ss`。遥测流用的是裸时间戳（如 "19:30:02"），原样透传。 */
export function formatClock(value: string | null | undefined): string {
  if (!value) return MISSING;
  if (/^\d{2}:\d{2}(:\d{2})?$/.test(value)) return value;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
