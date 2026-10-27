// Pinia 用户状态：存储用户信息、角色、session（访问令牌 / 刷新令牌），并持久化到 localStorage。
import axios from 'axios'
import { defineStore } from 'pinia'
import type { LoginResult, User } from '@/types'
import { API_BASE, joinUrl } from '@/utils/request'

const TOKEN_KEY = 'kb_agent_access_token'
const REFRESH_KEY = 'kb_agent_refresh_token'
const USER_KEY = 'kb_agent_user'

interface UserState {
  accessToken: string
  refreshToken: string
  user: User | null
}

function readUser(): User | null {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch {
    return null
  }
}

export const useUserStore = defineStore('user', {
  state: (): UserState => ({
    accessToken: localStorage.getItem(TOKEN_KEY) || '',
    refreshToken: localStorage.getItem(REFRESH_KEY) || '',
    user: readUser(),
  }),

  getters: {
    isLoggedIn: (s) => !!s.accessToken,
    isAdmin: (s) => s.user?.role === 'admin',
    role: (s): string => s.user?.role || 'user',
    displayName: (s) => s.user?.display_name || s.user?.username || '',
  },

  actions: {
    /** 保存登录结果（登录接口返回的 token + 用户信息） */
    setAuth(result: LoginResult) {
      this.accessToken = result.access_token
      this.refreshToken = result.refresh_token
      this.user = result.user
      localStorage.setItem(TOKEN_KEY, this.accessToken)
      localStorage.setItem(REFRESH_KEY, this.refreshToken)
      localStorage.setItem(USER_KEY, JSON.stringify(this.user))
    },

    /** 用 refresh_token 换新 access_token（使用原生 axios，避免走拦截器递归刷新） */
    async refresh(): Promise<boolean> {
      if (!this.refreshToken) return false
      try {
        const resp = await axios.post(joinUrl(API_BASE, '/auth/refresh'), {
          refresh_token: this.refreshToken,
        })
        const data = resp.data?.data
        if (resp.data?.code === 0 && data?.access_token) {
          this.accessToken = data.access_token
          localStorage.setItem(TOKEN_KEY, this.accessToken)
          return true
        }
        return false
      } catch {
        return false
      }
    },

    /** 登出：尽力通知服务端删除会话后清空本地状态 */
    async logout() {
      try {
        await axios.post(
          joinUrl(API_BASE, '/auth/logout'),
          {},
          { headers: { Authorization: `Bearer ${this.accessToken}` } },
        )
      } catch {
        /* ignore */
      }
      this.clear()
    },

    /** 清空本地登录态 */
    clear() {
      this.accessToken = ''
      this.refreshToken = ''
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(REFRESH_KEY)
      localStorage.removeItem(USER_KEY)
    },
  },
})
