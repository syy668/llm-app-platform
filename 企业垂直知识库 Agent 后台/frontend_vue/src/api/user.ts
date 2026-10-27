// 用户管理接口封装（仅管理员，对接 /users）
import request from '@/utils/request'
import type { Paginated, User } from '@/types'

export interface UserQuery {
  page?: number
  size?: number
  keyword?: string
  role?: string
}

export interface UserCreatePayload {
  username: string
  password: string
  display_name?: string | null
  email?: string | null
  role: 'admin' | 'user'
}

export interface UserUpdatePayload {
  display_name?: string | null
  email?: string | null
  role?: 'admin' | 'user'
  status?: 'active' | 'disabled'
}

export const listUsers = (params: UserQuery = {}) =>
  request.get<unknown, Paginated<User>>('/users', { params })

export const createUser = (data: UserCreatePayload) =>
  request.post<unknown, User>('/users', data)

export const updateUser = (id: number, data: UserUpdatePayload) =>
  request.put<unknown, User>(`/users/${id}`, data)

/** 删除用户（后端为软删除：禁用账号；不能禁用自己，409） */
export const deleteUser = (id: number) =>
  request.delete<unknown, Record<string, never>>(`/users/${id}`)

export const resetPassword = (id: number, newPassword: string) =>
  request.put<unknown, Record<string, never>>(`/users/${id}/reset-password`, {
    new_password: newPassword,
  })
