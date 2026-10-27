/**
 * 轻量全局鉴权状态（模块级单例，避免额外引入 pinia）。
 * token 与用户信息持久化在 localStorage，key 带 evalagent_ 前缀，与项目一前端隔离。
 *
 * 【双Token改造】
 * - 同时存储 access_token（短期）与 refresh_token（长期）；
 * - refreshAccessToken()：调用 /auth/refresh-token 换发新令牌（一次性轮换，双 token 都更新）；
 * - logout()：调用后端登出接口（后端把 refresh_token 加入 Redis 黑名单踢下线），
 *   再清空本地双 token 并跳转登录页。
 * 注意：方法内部动态 import('@/api/auth') 以避免 store -> api -> http -> store 循环依赖。
 */
import { computed, reactive } from 'vue'
import type { UserInfo } from '@/api/types'

const TOKEN_KEY = 'evalagent_token'
const REFRESH_TOKEN_KEY = 'evalagent_refresh_token'
const USER_KEY = 'evalagent_user'

function readStoredUser(): UserInfo | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? (JSON.parse(raw) as UserInfo) : null
  } catch {
    return null
  }
}

const state = reactive({
  token: localStorage.getItem(TOKEN_KEY) ?? '',
  refreshToken: localStorage.getItem(REFRESH_TOKEN_KEY) ?? '',
  user: readStoredUser(),
})

const isAdmin = computed(() => state.user?.role === 'admin')
const isLoggedIn = computed(() => !!state.token)

function setAuth(token: string, user: UserInfo, refreshToken = '') {
  state.token = token
  state.refreshToken = refreshToken
  state.user = user
  localStorage.setItem(TOKEN_KEY, token)
  // 旧调用（手动粘贴 token）不传 refreshToken 时清除旧值，防止残留过期令牌
  if (refreshToken) {
    localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken)
  } else {
    localStorage.removeItem(REFRESH_TOKEN_KEY)
  }
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

/** 仅供「设置页手动粘贴 token」场景使用，用户信息随后由 /auth/me 回填 */
function setTokenOnly(token: string) {
  state.token = token
  localStorage.setItem(TOKEN_KEY, token)
}

function setUser(user: UserInfo) {
  state.user = user
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

/** 清空本地双 token 与用户信息（不调后端；后端拉黑由 logout() 负责） */
function clearAuth() {
  state.token = ''
  state.refreshToken = ''
  state.user = null
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/**
 * 【双Token改造】使用 refresh_token 换发新令牌（后端一次性轮换）。
 * 成功：更新本地 access/refresh 双 token 并返回 true；
 * 失败（refresh 过期/被拉黑）：返回 false，由调用方决定跳转登录。
 */
async function refreshAccessToken(): Promise<boolean> {
  const current = state.refreshToken
  if (!current) return false
  try {
    const { refreshAccessTokenApi } = await import('@/api/auth')
    const result = await refreshAccessTokenApi(current)
    setAuth(result.access_token, state.user as UserInfo, result.refresh_token)
    return true
  } catch {
    return false
  }
}

/**
 * 【双Token改造】登出：调用后端登出（携带 refresh_token 触发 Redis 黑名单踢下线），
 * 无论后端调用是否成功都清空本地凭据并跳转登录页。
 */
async function logout(): Promise<void> {
  const current = state.refreshToken
  try {
    const { logoutApi } = await import('@/api/auth')
    await logoutApi(current || undefined)
  } catch {
    // 后端不可达/令牌已失效不阻塞登出：本地凭据照常清空
  }
  clearAuth()
}

export function useAuthStore() {
  return {
    state,
    isAdmin,
    isLoggedIn,
    setAuth,
    setTokenOnly,
    setUser,
    clearAuth,
    refreshAccessToken,
    logout,
  }
}
