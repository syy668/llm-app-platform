// 知识库 / 文档接口封装
import request from '@/utils/request'
import type { Chunk, DocItem, KBStats, KnowledgeBase, Paginated } from '@/types'

export interface KBQuery {
  page?: number
  size?: number
  keyword?: string
}

export const listKbs = (params: KBQuery = {}) =>
  request.get<unknown, Paginated<KnowledgeBase>>('/kbs', { params })

export const createKb = (data: Record<string, unknown>) =>
  request.post<unknown, KnowledgeBase>('/kbs', data)

export const getKb = (id: number) => request.get<unknown, KnowledgeBase>(`/kbs/${id}`)

export const updateKb = (id: number, data: Record<string, unknown>) =>
  request.put<unknown, KnowledgeBase>(`/kbs/${id}`, data)

export const deleteKb = (id: number) => request.delete<unknown, Record<string, never>>(`/kbs/${id}`)

export const kbStats = (id: number) => request.get<unknown, KBStats>(`/kbs/${id}/stats`)

/** 上传文档：后端同步完成 解析 -> 分块 -> 向量化 */
export const uploadDocument = (kbId: number, file: File) => {
  const form = new FormData()
  form.append('file', file)
  return request.post<unknown, DocItem>(`/kbs/${kbId}/documents`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

export interface DocQuery {
  page?: number
  size?: number
  parse_status?: string
  keyword?: string
}

export const listDocuments = (kbId: number, params: DocQuery = {}) =>
  request.get<unknown, Paginated<DocItem>>(`/kbs/${kbId}/documents`, { params })

export const deleteDocument = (docId: number) =>
  request.delete<unknown, Record<string, never>>(`/documents/${docId}`)

export const listChunks = (docId: number, params: { page?: number; size?: number } = {}) =>
  request.get<unknown, Paginated<Chunk>>(`/documents/${docId}/chunks`, { params })

export const queryKb = (kbId: number, question: string, topK?: number) =>
  request.post<unknown, { answer: string; sources: Array<Record<string, unknown>> }>(
    `/kbs/${kbId}/query`,
    { question, top_k: topK },
  )

/** 下载文档原文（文件流，浏览器触发保存） */
export async function downloadDocument(docId: number, title: string): Promise<void> {
  const blob = await request.get<unknown, Blob>(`/documents/${docId}/download`, { responseType: 'blob' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = title
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/** 重新解析文档（解析失败恢复或参数变更后重建索引） */
export const reprocessDocument = (docId: number) =>
  request.post<unknown, DocItem>(`/documents/${docId}/reprocess`)
