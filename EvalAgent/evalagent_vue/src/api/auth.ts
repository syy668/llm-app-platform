/**
 * 认证接口：登录、刷新、当前用户、登出（全部复用项目一 /auth/* 已有接口）。
 * 【第四步】身份属于上游域 → 走 upstreamHttp（8010），不要打到评测后端 8200。
 */
import { upstreamHttp as http } from '@/api/http'
import type { LoginResult, UserInfo } from '@/api/types'

/** 刷新接口响应：一次性轮换后同时返回双新令牌 */
export interface RefreshResult {
  access_token: string
  refresh_token: string
}

export function login(username: string, password: string): Promise<LoginResult> {
  return http.post('/auth/login', { username, password }) as Promise<LoginResult>
}

export function fetchMe(): Promise<UserInfo> {
  return http.get('/auth/me') as Promise<UserInfo>
}

/**
 * 【双Token改造】调用 POST /auth/refresh-token 换发新令牌。
 * 后端一次性轮换：成功返回双新令牌，旧 refresh_token 立即失效。
 */
export function refreshAccessTokenApi(refreshToken: string): Promise<RefreshResult> {
  return http.post('/auth/refresh-token', { refresh_token: refreshToken }) as Promise<RefreshResult>
}

/**
 * 【双Token改造】调用 POST /auth/logout 登出。
 * 携带 refresh_token 时后端将其加入 Redis 黑名单（主动踢下线）；不传兼容旧行为。
 */
export async function logoutApi(refreshToken?: string): Promise<void> {
  await http.post('/auth/logout', refreshToken ? { refresh_token: refreshToken } : {})
}
