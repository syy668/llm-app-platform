// 对话会话 / 消息接口封装
import request from '@/utils/request'
import type { Conversation, Message, Paginated } from '@/types'

export const createConversation = (kbId?: number | null) =>
  request.post<unknown, Conversation>('/conversations', { kb_id: kbId ?? null })

export const listConversations = (params: { page?: number; size?: number } = {}) =>
  request.get<unknown, Paginated<Conversation>>('/conversations', { params })

export const listMessages = (cid: number, params: { page?: number; size?: number } = {}) =>
  request.get<unknown, Paginated<Message>>(`/conversations/${cid}/messages`, { params })

export const renameConversation = (cid: number, title: string) =>
  request.put<unknown, Conversation>(`/conversations/${cid}`, { title })

export const deleteConversation = (cid: number) =>
  request.delete<unknown, Record<string, never>>(`/conversations/${cid}`)
