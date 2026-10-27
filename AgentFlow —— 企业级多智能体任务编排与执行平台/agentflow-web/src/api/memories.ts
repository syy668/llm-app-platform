/**
 * 记忆管理接口 —— 对应后端 `app/routers/memory_router.py`。
 *
 * 写操作（新增 / 删除）仅 ADMIN；短期层（redis）不在管理 API 中。
 * 当 `ENABLE_LAYERED_MEMORY=false` 时：查询返回空列表 + enabled=false，
 * 写操作返回 503 SERVICE_UNAVAILABLE —— 前端要显式提示"功能未启用"。
 */
import { api } from './http'
import type {
  MemoryCreateRequest,
  MemoryItem,
  MemoryListData,
  MemoryListQuery,
  MemoryMetaData,
  MemoryRecallRequest,
  RecallData,
} from '@/types/memory'

/** GET /memories —— 分页查询（注意分页信息在 data.meta 里） */
export function listMemories(query: MemoryListQuery = {}): Promise<MemoryListData> {
  const params: Record<string, unknown> = {
    page: query.page ?? 1,
    size: query.size ?? 20,
  }
  if (query.scope) params.scope = query.scope
  if (query.task_id) params.task_id = query.task_id
  return api.get<MemoryListData>('/memories', params)
}

/** GET /memories/meta —— 分层配置自检（开关 / 存储 / TTL） */
export function getMemoryMeta(): Promise<MemoryMetaData> {
  return api.get<MemoryMetaData>('/memories/meta')
}

/** POST /memories/recall —— 相似度召回（只读，会累加 hit_count） */
export function recallMemories(payload: MemoryRecallRequest): Promise<RecallData> {
  return api.post<RecallData>('/memories/recall', payload)
}

/** POST /memories —— 手工新增（scope 只能是 mid / long） */
export function createMemory(payload: MemoryCreateRequest): Promise<MemoryItem> {
  return api.post<MemoryItem>('/memories', payload)
}

/** GET /memories/{memory_id} —— 单条详情 */
export function getMemory(memoryId: number): Promise<MemoryItem> {
  return api.get<MemoryItem>(`/memories/${memoryId}`)
}

/** DELETE /memories/{memory_id} —— long 层删除后同时失去召回资格 */
export function deleteMemory(memoryId: number): Promise<{ memory_id: number; deleted: boolean }> {
  return api.delete<{ memory_id: number; deleted: boolean }>(`/memories/${memoryId}`)
}
