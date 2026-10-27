/**
 * 鉴权接口 —— 对应后端 `app/routers/auth.py`。
 */
import { api, getAccessToken } from './http'
import type { LoginRequest, TokenPair, UserOut } from '@/types/auth'

/** POST /auth/login —— 注意响应里**没有 role** */
export function login(payload: LoginRequest): Promise<TokenPair> {
  return api.post<TokenPair>('/auth/login', payload)
}

/** POST /auth/refresh —— refresh_token 一次性消费，重复使用返回 AUTH_REFRESH_REVOKED */
export function refresh(refreshToken: string): Promise<TokenPair> {
  return api.post<TokenPair>('/auth/refresh', { refresh_token: refreshToken })
}

/** POST /auth/logout —— 当前 access_token 的 jti 进黑名单 */
export function logout(): Promise<null> {
  return api.post<null>('/auth/logout')
}

/**
 * GET /auth/me —— 角色唯一权威来源。
 * 登录后必须调用一次，前端拿 role 做菜单裁剪。
 */
export function fetchMe(): Promise<UserOut> {
  return api.get<UserOut>('/auth/me')
}

/** 是否已有本地令牌（用于进入 /login 时判断是否需要直接跳走） */
export function hasToken(): boolean {
  return Boolean(getAccessToken())
}
