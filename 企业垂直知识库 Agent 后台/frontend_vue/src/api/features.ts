// 报表中心 / 消息反馈 / 全局统计 / 知识库授权接口封装
import request from '@/utils/request'
import type { KBPermissionItem, Paginated, ReportItem, StatsOverview } from '@/types'

// ---------------- 报表中心 ----------------

export const listReports = (params: { page?: number; size?: number } = {}) =>
  request.get<unknown, Paginated<ReportItem>>('/reports', { params })

export const getReport = (id: number) =>
  request.get<unknown, { id: number; conversation_id: number; content: string }>(`/reports/${id}`)

/** 报表下载 .md 文件流 */
export async function downloadReport(id: number): Promise<void> {
  const blob = await request.get<unknown, Blob>(`/reports/${id}/download`, { responseType: 'blob' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `report_${id}.md`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/** 删除报表（物理删除该报表消息） */
export const deleteReport = (id: number) => request.delete<unknown, unknown>(`/reports/${id}`)

// ---------------- 消息反馈 ----------------

export const sendFeedback = (messageId: number, rating: 1 | -1, comment?: string | null) =>
  request.post<unknown, { message_id: number; rating: number }>('/chat/feedback', {
    message_id: messageId,
    rating,
    ...(comment ? { comment } : {}),
  })

/** 点踩样本入评测集的结果 */
export interface BadcaseResult {
  dataset_id: number
  dataset_name: string
  item_id: number
  /** true = 该问题已在集合里，本次只补了反馈原因 */
  duplicated: boolean
  /** 集合是否本次新建 */
  created_dataset?: boolean
  /** true = 参考答案为空，需人工补全后才能跑评测 */
  needs_reference: boolean
  message_id?: number
  reason_saved?: boolean
}

/**
 * 一键把被点踩的回答沉淀到「用户反馈Badcase」评测集（数据飞轮）。
 * 后端固定按会话绑定的知识库聚合，不接受任意 dataset_id。
 */
export const addBadcaseToEval = (messageId: number, reason?: string | null) =>
  request.post<unknown, BadcaseResult>('/chat/badcase', {
    message_id: messageId,
    ...(reason ? { reason } : {}),
  })

// ---------------- 全局统计（仅管理员） ----------------

export const statsOverview = () => request.get<unknown, StatsOverview>('/stats/overview')

// ---------------- 知识库授权 ----------------

export const listPermissions = (kbId: number) =>
  request.get<unknown, KBPermissionItem[]>(`/kbs/${kbId}/permissions`)

export const grantPermission = (kbId: number, userId: number, permission: 'read' | 'write') =>
  request.post<unknown, Record<string, never>>(`/kbs/${kbId}/permissions`, { user_id: userId, permission })

export const revokePermission = (kbId: number, userId: number) =>
  request.delete<unknown, Record<string, never>>(`/kbs/${kbId}/permissions/${userId}`)
