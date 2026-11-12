/**
 * Vitest 全局准备。
 *
 * jsdom 缺的东西在这里补齐 —— 不补的话每个测试文件都要重复一遍样板。
 */

import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// jsdom 没有实现 `matchMedia`，Tailwind 的响应式工具与部分组件会调它。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
