/**
 * 记忆管理类型 —— 对应后端 `app/routers/memory_router.py` 与 `app/memory/layered_memory.py`。
 *
 * 短期层（redis）不出现在管理 API 中：它是任务会话的运行时缓存，无人工干预语义。
 */
import type { MemoryScope } from './enums'

/** `MemoryRecord.to_dict()` —— 记忆列表项 / 详情 */
export interface MemoryItem {
  id: number
  scope: MemoryScope
  user_id: number
  task_id: number | null
  content: string
  /** 1~5，long 层强制 >=3 */
  importance: number
  hit_count: number
  chroma_doc_id: string | null
  expires_at: string | null
  created_at: string | null
  /** 列表查询一般为 0，召回命中时为相似度 */
  score: number
}

/** GET /memories 的 data（注意分页信息在 meta 里，且多一个 enabled 开关） */
export interface MemoryListData {
  items: MemoryItem[]
  meta: { page: number; size: number; total: number }
  enabled: boolean
}

/** GET /memories/meta 的分层配置 */
export interface MemoryLayerConfig {
  backend: string
  ttl_seconds?: number
  ttl_days?: number | null
  summarized?: boolean
  min_review_score?: number
}

export interface MemoryMetaData {
  enabled: boolean
  store: string
  fallback: string
  short_ttl_seconds: number
  mid_ttl_days: number
  long_min_review_score: number
  layers: {
    short: MemoryLayerConfig
    mid: MemoryLayerConfig
    long: MemoryLayerConfig
  }
  viewer: {
    user_id: number
    role: string
    /** true 表示受 owner 过滤（非 admin 只看本人） */
    scope_filter: boolean
  }
}

/** 召回命中项（`RecallHit.to_dict()`，键是 memory_id 而不是 id） */
export interface RecallHit {
  memory_id: number
  content: string
  score: number
  scope: string
  importance: number
}

/** 本次召回的检索通道自省（后端 `service.last_retrieval`；未启用/旧版本缺省） */
export interface RecallRetrieval {
  store: string
  /** vector = 语义向量检索；local-keyword = 本地关键词打分（score 是重合度，非相似度） */
  scorer: 'vector' | 'local-keyword'
  /** true = 配置的向量通道故障后走了关键词兜底 */
  degraded: boolean
}

/** POST /memories/recall 的 data */
export interface RecallData {
  enabled: boolean
  goal: string
  /** 未启用时后端不返回该字段 */
  store?: string
  /** 检索通道自省；未启用或旧后端缺省 */
  retrieval?: RecallRetrieval | null
  hits: RecallHit[]
  /** 可直接拼进 Planner prompt 的文本片段；未启用时缺省 */
  injection?: string[]
}

/** POST /memories/recall 请求体（top_k 上限 3，与 PlannerInput.memory_hits 对齐） */
export interface MemoryRecallRequest {
  goal: string
  top_k?: 1 | 2 | 3
}

/** POST /memories 请求体（只能新增 mid / long） */
export interface MemoryCreateRequest {
  scope: 'mid' | 'long'
  content: string
  task_id?: number | null
  importance?: number
}

/** GET /memories 查询参数 */
export interface MemoryListQuery {
  page?: number
  size?: number
  scope?: MemoryScope
  task_id?: number
}
