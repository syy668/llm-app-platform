/**
 * 健康探针类型 —— 对应后端 `app/routers/health.py`。
 * 注意这三个探针**不在 /api/v1 前缀下**（`/healthz`、`/readyz`、`/api/v1/health` 混排）。
 */

/** GET /healthz（不依赖任何外部组件） */
export interface HealthzData {
  status: string
  service: string
  version: string
}

/** GET /readyz（MySQL + Redis，含运行模式标识） */
export interface ReadyzData {
  status: string
  /** 运行模式：demo = 嵌入式 SQLite/fakeredis 演示环境；production = 真实 MySQL/Redis */
  mode?: 'demo' | 'production'
  checks?: Record<string, boolean | string>
  [key: string]: unknown
}

/** GET /api/v1/health（运行态摘要） */
export interface HealthSummaryData {
  status: string
  service: string
  version: string
  env: string
  mock: {
    external_rag: boolean
    external_eval: boolean
  }
  auth: {
    access_expire_minutes: number
    refresh_expire_days: number
    blacklist_enabled: boolean
  }
  langgraph: Record<string, unknown>
}
