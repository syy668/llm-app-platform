/**
 * axios 封装 —— 【第四步改造：双客户端拆分】
 *
 * 为什么不是一个 baseURL：
 * 评测中台同时对接两个不同职责的后端，混用一个基址必然互相打断：
 *   1. 评测域   http        → 本项目后端（FastAPI:8200）：「评测集 / 评测任务 / 得分 / 聚合 / 导出」
 *   2. 身份与目录域  upstreamHttp → 上游项目一「企业垂直知识库 Agent 后台」（8010）：
 *      /auth/*（登录、刷新、登出、me）、/kbs（建数据集时选知识库）、/system/config（LLM 热配置）
 * 身份是上游的资产，评测是本中台的资产 —— 强行把 /auth/login 也打到 8200 会立刻 404。
 *
 * 两个实例共用同一套拦截器：
 * - 请求拦截自动带 Authorization: Bearer ${token}
 * - 统一拆包 {code:0, message, data}（code !== 0 直接 reject）
 * - 401 无感刷新：isRefreshing 锁 + 等待队列，并发 401 只刷新一次；刷新失败清凭据跳项目一登录页
 * - 403 / 网络异常全局 ElMessage 提示，且提示语区分「评测服务」与「上游知识库服务」
 */
import axios, {
  AxiosError,
  type AxiosInstance,
  type InternalAxiosRequestConfig,
} from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/store/auth'

function trimSlash(url: string | undefined): string {
  return (url ?? '').trim().replace(/\/+$/, '')
}

/** 评测域基址：本项目 FastAPI（8200） */
export const EVAL_API_BASE =
  trimSlash(import.meta.env.VITE_EVAL_API_BASE_URL) || 'http://127.0.0.1:8200/api/v1'
/** 身份 / 知识库目录域基址：上游项目一（8010） */
export const UPSTREAM_API_BASE =
  trimSlash(import.meta.env.VITE_UPSTREAM_API_BASE_URL) || 'http://127.0.0.1:8010/api/v1'
/** 兼容旧引用（设置页展示默认地址用，语义 = 评测域基址） */
export const DEFAULT_API_BASE = EVAL_API_BASE

const EXTERNAL_LOGIN_URL = import.meta.env.VITE_LOGIN_URL

/** 全局评测域基址：设置页可本地覆盖（仅存 localStorage，不写后端） */
let apiBase = localStorage.getItem('evalagent_api_base') || DEFAULT_API_BASE

export function getApiBase(): string {
  return apiBase
}

export function setApiBase(url: string) {
  apiBase = url
  http.defaults.baseURL = url
}

export function resetApiBase() {
  apiBase = DEFAULT_API_BASE
  localStorage.removeItem('evalagent_api_base')
  http.defaults.baseURL = apiBase
}

export function persistApiBase(url: string) {
  apiBase = url
  localStorage.setItem('evalagent_api_base', url)
  http.defaults.baseURL = url
}

export interface ApiEnvelope<T> {
  code: number
  message: string
  data: T
}

/**
 * 【双Token改造】无感刷新锁与等待队列：
 * - 并发多条 401 请求时仅第一条触发刷新（isRefreshing 锁），其余入队等待；
 * - 刷新成功后逐个重发队列内请求（带新 token）；失败则全部拒绝并跳转登录。
 */
let isRefreshing = false
let pendingQueue: Array<(ok: boolean) => void> = []

/** 刷新接口自身与登录接口不参与刷新（防止死循环） */
function isAuthEndpoint(url: string): boolean {
  return url.includes('/auth/login') || url.includes('/auth/refresh')
}

function resolveQueue(ok: boolean) {
  pendingQueue.forEach((resolve) => resolve(ok))
  pendingQueue = []
}

/** 跳转项目一登录页（刷新失败时清空凭据并自动跳转） */
function redirectToExternalLogin() {
  const { clearAuth } = useAuthStore()
  clearAuth()
  window.location.href = EXTERNAL_LOGIN_URL
}

async function tryRefreshAndRetry(error: AxiosError, instance: AxiosInstance): Promise<unknown> {
  const { state, refreshAccessToken } = useAuthStore()
  // 无 refresh_token（旧会话/手动 token）无法刷新，直接走登出流程
  if (!state.refreshToken) {
    ElMessage.error('登录已过期，请重新登录')
    redirectToExternalLogin()
    return Promise.reject(error)
  }

  if (!isRefreshing) {
    isRefreshing = true
    refreshAccessToken()
      .then((ok) => {
        resolveQueue(ok)
        if (!ok) {
          ElMessage.error('登录已过期，请重新登录')
          redirectToExternalLogin()
        }
      })
      .finally(() => {
        isRefreshing = false
      })
  }

  // 当前 401 请求入队：刷新成功后携带新 token 在原实例上重发（不要串域）
  return new Promise((resolve, reject) => {
    pendingQueue.push((ok) => {
      if (!ok) {
        reject(error)
        return
      }
      const { state } = useAuthStore()
      const config = error.config as InternalAxiosRequestConfig
      config.headers.Authorization = `Bearer ${state.token}`
      resolve(instance.request(config))
    })
  })
}

/**
 * 给任一实例装上同一套拦截器。
 * @param label 网络不通时的提示语主体（区分评测域 / 上游域，排障靠这句话定位）
 */
function installInterceptors(instance: AxiosInstance, label: string) {
  instance.interceptors.request.use((config) => {
    const { state } = useAuthStore()
    if (state.token) {
      config.headers.Authorization = `Bearer ${state.token}`
    }
    return config
  })

  instance.interceptors.response.use(
    (response) => {
      const body = response.data as ApiEnvelope<unknown>
      // 两个域都返回统一响应体 {code, message, data}；code !== 0 视为业务失败
      if (body && typeof body === 'object' && 'code' in body) {
        if (body.code !== 0) {
          ElMessage.error(body.message || '请求失败')
          return Promise.reject(new Error(body.message || '请求失败'))
        }
        return body.data as never
      }
      // 非标准响应体（如 CSV 文件流）原样放行
      return response
    },
    (error: AxiosError) => {
      const status = error.response?.status
      const url = error.config?.url ?? ''

      if (status === 401) {
        const isLoginApi = url.includes('/auth/login')
        // 登录接口与刷新接口自身的 401 不参与刷新（防止死循环）
        if (!isLoginApi && !isAuthEndpoint(url)) {
          return tryRefreshAndRetry(error, instance)
        }
        ElMessage.error('未登录或登录已过期')
        return Promise.reject(error)
      }
      if (status === 403) {
        ElMessage.error('权限不足：该操作仅管理员可用')
        return Promise.reject(error)
      }
      if (status && status >= 400) {
        const data = error.response?.data as { message?: string; detail?: unknown } | undefined
        const detail = typeof data?.detail === 'string' ? data.detail : undefined
        ElMessage.error(data?.message || detail || `请求失败（HTTP ${status}）`)
        return Promise.reject(error)
      }
      // 无响应：服务未启动 / CORS / 超时
      const reason =
        error.code === 'ECONNABORTED' ? '请求超时' : `无法连接${label}，请确认对应后端已启动`
      ElMessage.error(reason)
      return Promise.reject(error)
    },
  )
}

/** 评测域客户端（默认导出）：评测集、评测任务、得分、聚合、导出 */
const http: AxiosInstance = axios.create({
  baseURL: apiBase,
  timeout: 60_000,
})
installInterceptors(http, '评测服务（8200）')

/** 上游客户端：仅 /auth/*、/kbs、/system/config —— 不要用它调评测接口 */
export const upstreamHttp: AxiosInstance = axios.create({
  baseURL: UPSTREAM_API_BASE,
  timeout: 60_000,
})
installInterceptors(upstreamHttp, '上游知识库服务（8010）')

export default http
