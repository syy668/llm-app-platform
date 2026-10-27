// 认证接口封装
import request from '@/utils/request'
import type { LoginResult, User } from '@/types'

export const login = (username: string, password: string) =>
  request.post<unknown, LoginResult>('/auth/login', { username, password })

export const me = () => request.get<unknown, User>('/auth/me')

export const changePassword = (oldPassword: string, newPassword: string) =>
  request.post<unknown, Record<string, never>>('/auth/change-password', {
    old_password: oldPassword,
    new_password: newPassword,
  })
