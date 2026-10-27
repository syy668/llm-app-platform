/**
 * 鉴权相关类型 —— 对应后端 `app/schemas/auth.py`。
 *
 * 关键事实（决定了登录页不出现"角色选择"）：
 *  - `LoginRequest` 只有 username / password 两个字段；
 *  - `POST /auth/login` 返回 `TokenPair`，**不含 role**；
 *  - 角色必须再调 `GET /auth/me` 取 `UserOut.role`。
 */
import type { UserRole } from './enums'

/** POST /auth/login 请求体 */
export interface LoginRequest {
  username: string
  password: string
}

/** POST /auth/login、POST /auth/refresh 的 data */
export interface TokenPair {
  /** 访问令牌，默认 2h 有效 */
  access_token: string
  /** 刷新令牌，默认 7d 有效，一次性消费 */
  refresh_token: string
  /** 固定为 "Bearer" */
  token_type: string
  /** access_token 剩余有效期（秒） */
  expires_in: number
}

/** POST /auth/refresh 请求体 */
export interface RefreshRequest {
  refresh_token: string
}

/** GET /auth/me 的 data（`UserOut`） */
export interface UserOut {
  id: number
  username: string
  role: UserRole
  /** 1=启用，其他=禁用 */
  status: number
  last_login_at: string | null
}

/** 前端本地会话（role 只用于菜单/按钮裁剪，不作为安全边界） */
export interface SessionUser {
  id: number
  username: string
  role: UserRole
  lastLoginAt: string | null
}
