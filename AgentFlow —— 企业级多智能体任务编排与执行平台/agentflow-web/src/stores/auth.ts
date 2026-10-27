/**
 * 全局登录态。
 *
 * 职责边界（重要）：
 *  - 这里缓存的 role **只用于前端渲染决策**（菜单裁剪、按钮置灰、默认筛选），
 *    它不是安全边界 —— 每个写接口后端都挂了 require_role()，越权会 403。
 *  - 角色唯一来源是 `GET /auth/me`，既不能由用户选择，也不能只解 JWT claim
 *    （claim 是签发时快照，角色被管理员改动后 2 小时内会与 DB 不一致）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as authApi from '@/api/auth'
import { clearTokens, getAccessToken, saveTokens, setTokenStorage, type TokenStorage } from '@/api/http'
import { ROLE_META } from '@/utils/status'
import { UserRole } from '@/types/enums'
import type { SessionUser } from '@/types/auth'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<SessionUser | null>(null)
  const token = ref<string | null>(getAccessToken())
  /** 是否已完成一次 /auth/me（避免重复请求） */
  const profileLoaded = ref(false)
  const loading = ref(false)

  const isAuthenticated = computed(() => Boolean(token.value))
  const role = computed(() => user.value?.role ?? null)
  const roleText = computed(() => (role.value ? ROLE_META[role.value].text : '—'))
  const roleScope = computed(() => (role.value ? ROLE_META[role.value].scope : ''))
  const isAdmin = computed(() => role.value === UserRole.ADMIN)
  const isApprover = computed(() => role.value === UserRole.APPROVER)
  const isViewer = computed(() => role.value === UserRole.VIEWER)
  const displayName = computed(() => user.value?.username ?? '未登录')

  /** 判断当前角色是否命中路由 meta.roles（不传表示不限制） */
  function hasAnyRole(roles?: UserRole[]): boolean {
    if (!roles || roles.length === 0) return true
    return role.value !== null && roles.includes(role.value)
  }

  /**
   * 登录：POST /auth/login 拿 TokenPair → GET /auth/me 拿 role。
   * 两步不能合并：登录响应里没有 role。
   */
  async function login(username: string, password: string, remember: boolean): Promise<SessionUser> {
    loading.value = true
    try {
      const pair = await authApi.login({ username, password })
      const storage: TokenStorage = remember ? 'local' : 'session'
      setTokenStorage(storage)
      saveTokens(pair, storage)
      token.value = pair.access_token
      const profile = await loadProfile()
      return profile
    } catch (error) {
      // 登录失败必须回滚本地令牌，避免半登录态
      clearTokens()
      token.value = null
      user.value = null
      profileLoaded.value = false
      throw error
    } finally {
      loading.value = false
    }
  }

  /** GET /auth/me —— 角色唯一权威来源 */
  async function loadProfile(): Promise<SessionUser> {
    const me = await authApi.fetchMe()
    user.value = {
      id: me.id,
      username: me.username,
      role: me.role,
      lastLoginAt: me.last_login_at,
    }
    profileLoaded.value = true
    token.value = getAccessToken()
    return user.value
  }

  /** 路由守卫调用：有 token 但没拉过 profile 时补一次 */
  async function ensureProfile(): Promise<boolean> {
    if (!getAccessToken()) {
      reset()
      return false
    }
    if (profileLoaded.value && user.value) return true
    try {
      await loadProfile()
      return true
    } catch {
      reset()
      return false
    }
  }

  /** POST /auth/logout + 清本地状态 */
  async function logout(): Promise<void> {
    try {
      if (token.value) await authApi.logout()
    } catch {
      // 注销失败也必须让本地退出，令牌可能已过期
    } finally {
      reset()
    }
  }

  function reset(): void {
    clearTokens()
    token.value = null
    user.value = null
    profileLoaded.value = false
  }

  return {
    user,
    token,
    loading,
    profileLoaded,
    isAuthenticated,
    role,
    roleText,
    roleScope,
    isAdmin,
    isApprover,
    isViewer,
    displayName,
    hasAnyRole,
    login,
    loadProfile,
    ensureProfile,
    logout,
    reset,
  }
})
