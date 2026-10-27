/**
 * axios 统一实例 —— 鉴权、响应解包、错误归一、静默续期。
 *
 * 三条与后端强相关的约定：
 *  1. 业务成功判据是 `code === 'OK'`，不是 HTTP 200（后端错误码也走 2xx 的场景不存在，
 *     但 detail 里可能带业务码，因此解包层必须显式判 code）；
 *  2. 401 + `AUTH_TOKEN_EXPIRED` 时用 refresh_token 换新对并重放一次原请求；
 *     刷新是**一次性消费**的，必须做单飞（single-flight），并发请求共享同一个刷新 Promise；
 *  3. 分页接口有两套结构（/approvals 平铺、/memories 包 meta），解包层不做归一，
 *     由各自 api 模块处理。
 */
import axios, {
  type AxiosError,
  type AxiosInstance,
  type AxiosRequestConfig,
  type InternalAxiosRequestConfig,
} from 'axios'
import type { ApiErrorShape, ApiResponse } from '@/types/common'
import type { TokenPair } from '@/types/auth'

/** 后端服务根地址（不含 /api/v1） */
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '')
/** API 版本前缀 */
export const API_PREFIX = import.meta.env.VITE_API_PREFIX || '/api/v1'
/** 拼接后的完整前缀，用于静态资源等需要绝对地址的场景 */
export const API_ROOT = `${API_BASE_URL}${API_PREFIX}`

const ACCESS_TOKEN_KEY = 'af_access_token'
const REFRESH_TOKEN_KEY = 'af_refresh_token'

// ---------------------------------------------------------------------------
// Token 存取（「保持登录 7 天」勾选 → localStorage，否则 sessionStorage）
// ---------------------------------------------------------------------------
export type TokenStorage = 'local' | 'session'

let tokenStorage: TokenStorage = (localStorage.getItem(ACCESS_TOKEN_KEY) ? 'local' : 'session')

/** 由 auth store 在登录成功后指定本次会话的存储位置 */
export function setTokenStorage(kind: TokenStorage): void {
  tokenStorage = kind
}

function store(): Storage {
  return tokenStorage === 'local' ? localStorage : sessionStorage
}

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY) ?? sessionStorage.getItem(ACCESS_TOKEN_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY) ?? sessionStorage.getItem(REFRESH_TOKEN_KEY)
}

export function saveTokens(pair: TokenPair, kind?: TokenStorage): void {
  if (kind) tokenStorage = kind
  // 清理另一侧，避免两份 token 互相覆盖
  clearTokens()
  store().setItem(ACCESS_TOKEN_KEY, pair.access_token)
  store().setItem(REFRESH_TOKEN_KEY, pair.refresh_token)
}

export function clearTokens(): void {
  for (const storage of [localStorage, sessionStorage]) {
    storage.removeItem(ACCESS_TOKEN_KEY)
    storage.removeItem(REFRESH_TOKEN_KEY)
  }
}

// ---------------------------------------------------------------------------
// 错误类型
// ---------------------------------------------------------------------------
export class ApiError extends Error implements ApiErrorShape {
  code: string
  traceId?: string | null
  httpStatus?: number
  detail?: unknown

  constructor(shape: ApiErrorShape) {
    super(shape.message)
    this.name = 'ApiError'
    this.code = shape.code
    this.traceId = shape.traceId
    this.httpStatus = shape.httpStatus
    this.detail = shape.detail
  }
}

/** 页面里区分"业务错误"与"网络错误"的统一入口 */
export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

/** 网络层失败（无响应）时使用的伪错误码，与后端错误码不冲突 */
export const NETWORK_ERROR = 'NETWORK_ERROR'

/**
 * 判断错误是否为「该路由不存在」。
 *
 * 用途：新接口上线期做「预接线 + 回落」的兜底判断。当前 W6 接口均已由后端实现，
 * 本函数退化为**旧版本后端 / 路由异常**的保护网：
 *  - 旧版本后端无该路由 → FastAPI 默认 404（无 ApiResponse 信封）→ 归一为 HTTP_404；
 *    业务 404（NOT_FOUND）也归入此类；
 *  - 此时页面**回落到占位态**（StateBlock pending / 本机最近访问），不报错、不造假数据。
 */
export function isMissingEndpoint(error: unknown): boolean {
  if (!isApiError(error)) return false
  return error.code === 'HTTP_404' || error.code === 'NOT_FOUND'
}

// ---------------------------------------------------------------------------
// 实例
// ---------------------------------------------------------------------------
export const http: AxiosInstance = axios.create({
  baseURL: API_ROOT,
  timeout: Number(import.meta.env.VITE_REQUEST_TIMEOUT || 20000),
  headers: { 'Content-Type': 'application/json' },
})

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = getAccessToken()
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  return config
})

/** 单飞刷新：并发 401 只发一次 /auth/refresh */
let refreshing: Promise<string | null> | null = null

async function refreshAccessToken(): Promise<string | null> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) return null
  if (!refreshing) {
    refreshing = axios
      .post<ApiResponse<TokenPair>>(
        `${API_ROOT}/auth/refresh`,
        { refresh_token: refreshToken },
        { headers: { 'Content-Type': 'application/json' }, timeout: 10000 },
      )
      .then((res) => {
        if (res.data?.code !== 'OK') return null
        saveTokens(res.data.data)
        return res.data.data.access_token
      })
      .catch(() => null)
      .finally(() => {
        refreshing = null
      })
  }
  return refreshing
}

/** 跳登录页（带 redirect），并清空本地令牌 */
export function redirectToLogin(): void {
  clearTokens()
  const current = window.location.hash.replace(/^#/, '') || '/'
  if (current.startsWith('/login')) return
  window.location.hash = `/login?redirect=${encodeURIComponent(current)}`
}

http.interceptors.response.use(
  (response) => {
    const body = response.data as ApiResponse<unknown> | undefined
    // 非标准包（如 /healthz 直接返回裸对象）原样透传
    if (!body || typeof body !== 'object' || !('code' in body)) {
      return response
    }
    if (body.code === 'OK') {
      // 把 data 提到 response.data，调用方无需层层解包
      response.data = body.data
      return response
    }
    throw new ApiError({
      code: body.code,
      message: body.message || '请求失败',
      traceId: body.trace_id,
      httpStatus: response.status,
      detail: body.data,
    })
  },
  async (error: AxiosError<ApiResponse<unknown>>) => {
    const status = error.response?.status
    const body = error.response?.data
    const original = error.config as (AxiosRequestConfig & { _retried?: boolean }) | undefined
    const code = body?.code ?? (status ? `HTTP_${status}` : NETWORK_ERROR)

    // 1) access_token 过期 → 静默续期并重放一次
    if (status === 401 && code === 'AUTH_TOKEN_EXPIRED' && original && !original._retried) {
      original._retried = true
      const token = await refreshAccessToken()
      if (token) {
        const retryConfig: AxiosRequestConfig = {
          ...original,
          headers: {
            ...(original.headers as Record<string, unknown>),
            Authorization: `Bearer ${token}`,
          },
        }
        return http.request(retryConfig)
      }
      redirectToLogin()
    }

    // 2) 令牌缺失/非法/被注销 → 直接回登录页
    if (
      status === 401 &&
      ['AUTH_TOKEN_MISSING', 'AUTH_TOKEN_INVALID', 'AUTH_TOKEN_REVOKED', 'AUTH_REFRESH_REVOKED'].includes(code)
    ) {
      redirectToLogin()
    }

    throw new ApiError({
      code,
      message: body?.message || (status ? `请求失败（HTTP ${status}）` : '无法连接后端服务'),
      traceId: body?.trace_id,
      httpStatus: status,
      detail: body?.data,
    })
  },
)

/** 类型友好的请求包装：返回值即后端 data 字段 */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await http.request<T>(config)
  return response.data as T
}

export const api = {
  get: <T>(url: string, params?: Record<string, unknown>) => request<T>({ method: 'GET', url, params }),
  post: <T>(url: string, data?: unknown) => request<T>({ method: 'POST', url, data }),
  put: <T>(url: string, data?: unknown) => request<T>({ method: 'PUT', url, data }),
  patch: <T>(url: string, data?: unknown) => request<T>({ method: 'PATCH', url, data }),
  delete: <T>(url: string, params?: Record<string, unknown>) => request<T>({ method: 'DELETE', url, params }),
}

/**
 * 把后端 /static/xxx 这类相对路径拼成可直接访问的绝对地址。
 * 后端的图表产物 url 形如 `/static/charts/xxx.png`，只拼到 API Base（不含 /api/v1）。
 */
export function resolveAssetUrl(url?: string | null): string {
  if (!url) return ''
  if (/^https?:\/\//i.test(url)) return url
  return `${API_BASE_URL}${url.startsWith('/') ? '' : '/'}${url}`
}
