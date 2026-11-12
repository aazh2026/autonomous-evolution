/// <reference types="vite/client" />

/**
 * 构建期注入的环境变量。
 *
 * 只有 `VITE_` 前缀的变量会进入前端产物 —— 这是 Vite 的安全边界，
 * 后端的密钥永远不会因为写错前缀而被打包进 JS。
 */
interface ImportMetaEnv {
  /**
   * 控制面 API 基址。
   *
   * 留空则走 `/api/v1` 相对路径（同源，由 Vite 代理或 nginx 反代）。
   * 前后端分离部署到不同域名时，指向后端绝对地址，例如
   * `https://api.example.com/api/v1`。
   */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
