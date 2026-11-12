/**
 * 置信度分级的视觉映射。
 *
 * **为什么这里是一张查表，而不是拼字符串。**
 * Tailwind 的 JIT 扫描器在**构建期**扫源码里出现过的完整类名。写
 * `` `bg-${color}-950` `` 的话，源码里根本没有 `bg-emerald-950` 这个字面量，
 * 扫描器看不见，产出的 CSS 里也就没有这条规则 —— 运行时颜色静默失效，
 * 页面看着"就是不对"但没有任何报错。
 *
 * 所以表里的每个值都必须是**完整写死的类名字符串**。新增颜色时，
 * 照着抄一行就行，别改成模板字符串。
 *
 * 分级阈值由后端 `apps/api/control.py` 定义（0.8 / 0.3），前端照抄后端
 * 给出的 `confidence_grade`，不自己重算 —— 两边各算一次迟早会漂。
 */

import type { ConfidenceGrade } from '../types/control-plane.ts';

export interface ConfidenceVisual {
  /** 中文标签：确凿级 / 待核级 / 投机级。 */
  label: string;
  /** 徽标文字样式。 */
  badge: string;
  /** 卡片边框样式。 */
  border: string;
  /** 置信度数值样式。 */
  value: string;
  /** 条形填充宽度百分比。 */
  bar: string;
}

const CONFIDENCE_VISUALS: Record<ConfidenceGrade, ConfidenceVisual> = {
  verified: {
    label: '确凿级',
    badge: 'bg-emerald-950 text-emerald-300 border-emerald-700/60',
    border: 'border-emerald-700/50',
    value: 'text-emerald-400',
    bar: 'bg-emerald-500',
  },
  review: {
    label: '待核级',
    badge: 'bg-amber-950 text-amber-300 border-amber-700/60',
    border: 'border-amber-600/50',
    value: 'text-amber-400',
    bar: 'bg-amber-500',
  },
  speculative: {
    label: '投机级',
    badge: 'bg-rose-950 text-rose-300 border-rose-800/60',
    border: 'border-rose-800/50',
    value: 'text-rose-400',
    bar: 'bg-rose-500',
  },
};

export function confidenceVisual(grade: ConfidenceGrade | string): ConfidenceVisual {
  return CONFIDENCE_VISUALS[grade as ConfidenceGrade] ?? CONFIDENCE_VISUALS.speculative;
}

/** 后端给的等级优先；后端没给就按阈值兜底算一次。 */
export function resolveGrade(confidence: number, grade?: ConfidenceGrade | string): ConfidenceGrade {
  if (grade && grade in CONFIDENCE_VISUALS) {
    return grade as ConfidenceGrade;
  }
  if (confidence >= 0.8) return 'verified';
  if (confidence >= 0.3) return 'review';
  return 'speculative';
}

/** 任务状态 → 徽标样式。同样是写死的完整类名。 */
export function dagStatusVisual(status: string): { badge: string; dot: string } {
  switch (status) {
    case 'COMPLETED':
      return { badge: 'bg-emerald-950 text-emerald-300 border-emerald-700/60', dot: 'bg-emerald-500' };
    case 'RUNNING':
      return { badge: 'bg-blue-950 text-blue-300 border-blue-700/60', dot: 'bg-blue-500' };
    case 'BLOCKED':
      return { badge: 'bg-rose-950 text-rose-300 border-rose-700/60', dot: 'bg-rose-500' };
    case 'FAILED':
      return { badge: 'bg-rose-950 text-rose-300 border-rose-700/60', dot: 'bg-rose-500' };
    case 'READY':
      return { badge: 'bg-slate-800 text-slate-200 border-slate-600/60', dot: 'bg-slate-400' };
    default:
      return { badge: 'bg-slate-800 text-slate-400 border-slate-700', dot: 'bg-slate-600' };
  }
}

/** 验证层结果 → 样式。`passed` 之外的一切都当失败看 —— 不给"部分通过"发绿牌。 */
export function verificationResultVisual(result: string): {
  text: string;
  badge: string;
  label: string;
} {
  const passed = result.toLowerCase() === 'passed';
  return {
    label: passed ? '通过' : result,
    text: passed ? 'text-emerald-400' : 'text-rose-400',
    badge: passed
      ? 'bg-emerald-950 text-emerald-300 border-emerald-700/60'
      : 'bg-rose-950 text-rose-300 border-rose-700/60',
  };
}

/** 审批状态 → 样式。 */
export function approvalStatusVisual(status: string): { text: string; badge: string } {
  switch (status) {
    case 'approved':
      return {
        text: 'text-emerald-400',
        badge: 'bg-emerald-950 text-emerald-300 border-emerald-700/60',
      };
    case 'rejected':
      return { text: 'text-rose-400', badge: 'bg-rose-950 text-rose-300 border-rose-700/60' };
    default:
      return { text: 'text-amber-400', badge: 'bg-amber-950 text-amber-300 border-amber-700/60' };
  }
}

/** 周期状态 → 样式。 */
export function cycleStatusVisual(status: string): { text: string; badge: string } {
  switch (status) {
    case 'improved':
      return {
        text: 'text-emerald-400',
        badge: 'bg-emerald-950 text-emerald-300 border-emerald-700/50',
      };
    case 'no_change':
      return {
        text: 'text-slate-400',
        badge: 'bg-slate-800 text-slate-300 border-slate-700/50',
      };
    default:
      return { text: 'text-amber-400', badge: 'bg-amber-950 text-amber-300 border-amber-700/50' };
  }
}
