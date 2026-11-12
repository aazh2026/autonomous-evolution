/**
 * 异步动作的挂起态与错误态。
 *
 * 控制舱里每个按钮都是"点一下 → 调后端 → 刷新数据"三步。少了挂起态，
 * 用户会连点；少了错误态，后端返回 409（防篡改哈希校验失败）时界面
 * 一点反应都没有，看起来像按钮坏了。
 *
 * 用法：
 * ```tsx
 * const review = useAsyncAction(async (id: string) => {
 *   await reviewApproval(id, { ... });
 * });
 * <button disabled={review.pending} onClick={() => review.run(id)}>放行</button>
 * {review.error && <p>{review.error}</p>}
 * ```
 */

import { useCallback, useRef, useState } from 'react';
import { describeError } from './useCockpitData.ts';

export interface AsyncActionState<Args extends unknown[], R> {
  /** 动作执行中。 */
  pending: boolean;
  /** 最近一次失败的原因，成功后清空。 */
  error: string | null;
  run: (...args: Args) => Promise<R | undefined>;
  reset: () => void;
}

export function useAsyncAction<Args extends unknown[], R>(
  action: (...args: Args) => Promise<R>
): AsyncActionState<Args, R> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 组件卸载后不要再 setState。 */
  const aliveRef = useRef(true);

  const run = useCallback(
    async (...args: Args): Promise<R | undefined> => {
      setPending(true);
      setError(null);
      try {
        const result = await action(...args);
        return result;
      } catch (err) {
        if (aliveRef.current) {
          setError(describeError(err));
        }
        return undefined;
      } finally {
        if (aliveRef.current) {
          setPending(false);
        }
      }
    },
    [action]
  );

  const reset = useCallback(() => setError(null), []);

  return { pending, error, run, reset };
}
