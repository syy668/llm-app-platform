/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 评测域：本项目 FastAPI（中央评测中台）基础地址，如 http://127.0.0.1:8200/api/v1 */
  readonly VITE_EVAL_API_BASE_URL: string
  /** 上游域：项目一 FastAPI 服务基础地址，如 http://127.0.0.1:8010/api/v1 */
  readonly VITE_UPSTREAM_API_BASE_URL: string
  /** 项目一前端登录页地址，401 时跳转 */
  readonly VITE_LOGIN_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const component: DefineComponent<Record<string, never>, Record<string, never>, any>
  export default component
}
