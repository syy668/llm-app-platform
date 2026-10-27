/// <reference types="vite/client" />

/** .env 变量的强类型（与 .env.example 一一对应） */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string
  readonly VITE_API_PREFIX: string
  readonly VITE_PROXY_TARGET: string
  readonly VITE_REQUEST_TIMEOUT: string
  readonly VITE_APP_TITLE: string
  readonly VITE_APP_VERSION: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
