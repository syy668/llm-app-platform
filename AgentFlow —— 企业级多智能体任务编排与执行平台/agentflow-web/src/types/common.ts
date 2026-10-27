/**
 * 通用响应结构与分页结构。
 *
 * 后端 `app/schemas/common.py` 的 ApiResponse 统一包成 {code, message, data, trace_id}，
 * 判成功必须以 `code === 'OK'` 为准，而不是只看 HTTP 200。
 */

/** 业务码。目前后端只定义 OK 与各类错误码，此处保留 string 以便透传未知码 */
export type ApiCode = 'OK' | (string & {})

export interface ApiResponse<T> {
  code: ApiCode
  message: string
  data: T
  /** 本次 HTTP 请求的链路 ID（用于排障日志关联），**不是**任务的 trace_id */
  trace_id?: string | null
}

/**
 * 分页信息。
 * 注意：后端两套分页结构不统一 ——
 *  - `/approvals`  → { items, page, size, total }
 *  - `/memories`   → { items, meta: { page, size, total }, enabled }
 */
export interface PagedMeta {
  page: number
  size: number
  total: number
}

export interface PagedItems<T> extends PagedMeta {
  items: T[]
}

/**
 * W6 分页信封（`agentflow-platform/docs/W6接口设计文档.md` §0）：
 * `ApiResponse[PageData[T]]` → 解包后为 `{ items, meta: { page, size, total } }`。
 * G1/G2/G3 新接口一律用这套；`PagedItems`（扁平）仅存量为 /approvals 兼容保留。
 */
export interface PageData<T> {
  items: T[]
  meta: PagedMeta
}

/** 统一错误对象：由 axios 拦截器抛出，页面据此渲染 Alert */
export interface ApiErrorShape {
  code: ApiCode
  message: string
  traceId?: string | null
  httpStatus?: number
  /** 后端 detail 原样透传（仅用于控制台，不直接展示给用户） */
  detail?: unknown
}
