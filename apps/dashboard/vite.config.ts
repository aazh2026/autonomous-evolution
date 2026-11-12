/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 前端独立工程配置。
 *
 * **代理是开发态的关键**：前端跑在 3000 端口，后端在 8000。开发时浏览器
 * 看到的请求是同源的 `/api/v1/...`，由 Vite 转发到后端 —— 这样开发态
 * 根本不需要 CORS，生产态用 nginx 做同样的事。前后端分离不等于"必须
 * 跨域"，同源反代是更省事的部署形态。
 *
 * 生产态要跨域时（前端独立域名），设 `VITE_API_BASE_URL` 指到后端绝对
 * 地址，并给后端配 `ASES_CORS_ALLOWED_ORIGINS`。
 */

/** 开发态代理到的后端地址。 */
const API_TARGET = process.env.ASES_API_PROXY ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
      '/healthz': {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    // PDF.js 单独成块 —— 它只有决策沙盘用得到，不该拖累首屏。
    rollupOptions: {
      output: {
        manualChunks: {
          pdfjs: ['pdfjs-dist'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/main.tsx', 'src/vite-env.d.ts', 'src/test/**'],
    },
  },
});
