/**
 * 可拖拽分屏。
 *
 * **为什么自己写而不是用现成的分屏组件。**
 * 控制舱的分屏只有一条规则：拖一条 4px 的分割条，改两侧的比例，比例记到
 * localStorage，双击复位。这个逻辑自己写是六十行；引一个分屏库要付出
 * "它的模型和我要的模型不完全一样"的适配成本，还要多背一份依赖。
 * 依赖越少，离线环境里装起来越省事 —— 这个前端是要跑在隔离网络里的。
 *
 * 比例以**百分比**存储，不存像素：窗口改了尺寸，百分比还能还原出合理
 * 的布局，像素比例会失真。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export interface ResizableSplitOptions {
  /** 初始比例（0~1），左侧面板占比。 */
  initial: number;
  /** 左侧面板最小占比。 */
  min?: number;
  /** 左侧面板最大占比。 */
  max?: number;
  /** localStorage 键。给了就持久化比例。 */
  storageKey?: string;
  /** 分隔条方向。 */
  direction?: 'horizontal' | 'vertical';
}

export interface ResizableSplit {
  /** 左侧面板的占比（0~1）。 */
  size: number;
  /** 挂到分屏容器上的 ref —— 拖拽时靠它算比例。 */
  containerRef: React.RefObject<HTMLDivElement | null>;
  /** 拖拽分割条的 props（挂到分割条元素上）。 */
  separatorProps: {
    onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => void;
    onDoubleClick: () => void;
    role: 'separator';
    'aria-orientation': 'horizontal' | 'vertical';
    'aria-valuenow': number;
    'aria-valuemin': number;
    'aria-valuemax': number;
  };
  /** 复位到初始比例。 */
  reset: () => void;
}

const clamp = (value: number, min: number, max: number): number =>
  Math.min(max, Math.max(min, value));

export function useResizableSplit(options: ResizableSplitOptions): ResizableSplit {
  const { initial, min = 0.1, max = 0.9, storageKey, direction = 'horizontal' } = options;

  const containerRef = useRef<HTMLDivElement | null>(null);
  const draggingRef = useRef(false);
  const [size, setSize] = useState<number>(() => {
    if (!storageKey) return initial;
    try {
      const stored = window.localStorage.getItem(storageKey);
      if (stored !== null) {
        const parsed = Number(stored);
        if (!Number.isNaN(parsed)) return clamp(parsed, min, max);
      }
    } catch {
      // localStorage 在隐私模式里会抛 —— 退回初始比例，不影响主流程。
    }
    return initial;
  });

  // 记住最新的 size，供拖拽结束时的持久化使用（避免闭包拿到旧值）。
  const sizeRef = useRef(size);
  sizeRef.current = size;

  const persist = useCallback(
    (value: number) => {
      if (!storageKey) return;
      try {
        window.localStorage.setItem(storageKey, String(value));
      } catch {
        // 存不进去就算了 —— 布局持久化是锦上添花，不是必需品。
      }
    },
    [storageKey]
  );

  const applyFromPointer = useCallback(
    (clientX: number, clientY: number) => {
      const container = containerRef.current;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      const ratio =
        direction === 'horizontal'
          ? (clientX - rect.left) / rect.width
          : (clientY - rect.top) / rect.height;
      setSize(clamp(ratio, min, max));
    },
    [direction, min, max]
  );

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      draggingRef.current = true;
      const target = event.currentTarget;
      target.setPointerCapture(event.pointerId);
    },
    []
  );

  // 拖拽期间在 window 上监听 move/up —— 指针移出分割条也要继续跟手。
  //
  // 监听器**常驻挂载**，不是在 pointerdown 时才挂的：如果在 down 里挂，
  // 那次渲染闭包里的 `draggingRef` 永远是 false，move 事件直接被忽略，
  // 分割条就成了死物。所以这里挂载时无条件监听，在处理函数内部再判断
  // 当前是不是在拖。
  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      if (!draggingRef.current) return;
      applyFromPointer(event.clientX, event.clientY);
    };
    const onUp = () => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      persist(sizeRef.current);
    };

    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
  }, [applyFromPointer, persist]);

  const reset = useCallback(() => {
    setSize(initial);
    persist(initial);
  }, [initial, persist]);

  const separatorProps = {
    onPointerDown,
    onDoubleClick: reset,
    role: 'separator' as const,
    'aria-orientation': direction,
    'aria-valuenow': Math.round(size * 100),
    'aria-valuemin': Math.round(min * 100),
    'aria-valuemax': Math.round(max * 100),
  };

  return { size, containerRef, separatorProps, reset };
}

/** 分隔条的通用外观类。4px 宽，悬停高亮。 */
export const SEPARATOR_CLASSES = {
  horizontal:
    'w-1 cursor-col-resize bg-slate-800 hover:bg-blue-500 transition-colors shrink-0 group-hover:bg-blue-500/60',
  vertical:
    'h-1 cursor-row-resize bg-slate-800 hover:bg-blue-500 transition-colors shrink-0 group-hover:bg-blue-500/60',
};
