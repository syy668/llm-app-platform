// Axios 封装：自动携带 session 鉴权、401 自动刷新、403 权限提示、统一响应体解包。
// 后端统一响应体 { code, message, data }，成功 code=0，HTTP 状态码与错误码对齐。
import axios, { AxiosError, type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { useUserStore } from '@/store/useUserStore'

/** 后端 API 基础地址（含 /api/v1 前缀，去掉尾部多余斜杠） */
export const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api/v1').replace(/\/+$/, '')

/**
 * URL 安全拼接：各段自动去除首尾多余斜杠后重组，杜绝 /api/v1//xxx 这类 404。
 */
export function joinUrl(...parts: Array<string | number | null | undefined>): string {
  return parts
    .map((p) => String(p ?? '').replace(/^\/+|\/+$/g, ''))
    .filter(Boolean)
    .join('/')
}

const request = axios.create({ baseURL: API_BASE, timeout: 60_000 })

// 请求拦截：自动携带 session 鉴权信息
request.interceptors.request.use((config) => {
  const store = useUserStore()
  if (store.accessToken) {
    config.headers.Authorization = `Bearer ${store.accessToken}`
  }
  return config
})

// 并发 401 时仅刷新一次
let refreshPromise: Promise<boolean> | null = null

function doRefresh(): Promise<boolean> {
  const store = useUserStore()
  if (!refreshPromise) {
    refreshPromise = store.refresh().finally(() => {
      refreshPromise = null
    })
  }
  return refreshPromise
}

request.interceptors.response.use(
  (response) => {
    const body = response.data
    // 解包统一响应体：code=0 直接返回 data
    if (body && typeof body === 'object' && body.code === 0) {
      return body.data
    }
    return body
  },
  async (error: AxiosError) => {
    const { response, config } = error
    if (!response) {
      ElMessage.error('网络异常，请检查后端服务是否已启动')
      return Promise.reject(error)
    }

    const status = response.status
    const message = (response.data as { message?: string } | undefined)?.message || `请求失败（${status}）`

    // 401：登录/刷新接口直接提示；其余先尝试刷新 token 重试一次
    if (status === 401 && config) {
      const isAuthUrl = (config.url || '').includes('/auth/')
      if (isAuthUrl) {
        ElMessage.error(message)
        return Promise.reject(error)
      }
      if (!(config as AxiosRequestConfig & { _retried?: boolean })._retried) {
        ;(config as AxiosRequestConfig & { _retried?: boolean })._retried = true
        const ok = await doRefresh()
        if (ok) {
          const store = useUserStore()
          config.headers.Authorization = `Bearer ${store.accessToken}`
          return request(config)
        }
      }
      const store = useUserStore()
      store.clear()
      ElMessage.error('登录已过期，请重新登录')
      router.push('/login')
      return Promise.reject(error)
    }

    // 403：权限不足
    if (status === 403) {
      ElMessage.error(message || '权限不足，无权访问该资源')
      return Promise.reject(error)
    }

    ElMessage.error(message)
    return Promise.reject(error)
  },
)

export default request
