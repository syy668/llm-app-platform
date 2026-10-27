// 通用与业务类型定义：严格对齐后端 /api/v1 响应结构

// ---------------- 通用 ----------------
export interface ApiResponse<T> {
  code: number
  message: string
  data: T
}

export interface Paginated<T> {
  total: number
  items: T[]
}

// ---------------- 认证 / 用户 ----------------
export type Role = 'admin' | 'user'

export interface User {
  id: number
  username: string
  display_name: string | null
  email: string | null
  role: Role
  status: string
  created_at: string
}

export interface LoginResult {
  access_token: string
  refresh_token: string
  user: User
}

// ---------------- 知识库 / 文档 ----------------
export interface KnowledgeBase {
  id: number
  name: string
  description: string | null
  owner_id: number
  embedding_model: string
  chunk_size: number
  chunk_overlap: number
  top_k: number
  rerank_threshold: number
  status: string
  doc_count: number
  created_at: string
  // 列表接口注入：非管理员且非本人创建的库（被授权可见）为 true，否则 false
  is_shared?: boolean
}

export interface DocItem {
  id: number
  kb_id: number
  title: string
  file_type: string
  file_size: number
  file_hash: string | null
  parse_status: string
  chunk_count: number
  error_msg: string | null
  uploaded_by: number
  created_at: string
}

export interface Chunk {
  id: number
  doc_id: number
  kb_id: number
  chunk_index: number
  content: string
  token_count: number
  metadata: Record<string, unknown> | null
  chroma_id: string | null
}

export interface KBStats {
  doc_count: number
  chunk_count: number
  token_sum: number
}

// ---------------- 会话 / 消息 ----------------
export interface Conversation {
  id: number
  user_id: number
  kb_id: number | null
  title: string | null
  created_at: string
  updated_at: string
}

export interface Message {
  id: number
  conversation_id: number
  user_id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  agent_route: AgentRoute | null
  trace_id: string | null
  input_tokens: number
  output_tokens: number
  latency_ms: number
  status: string
  error_msg: string | null
  created_at: string
  /** 本人反馈：1=赞 -1=踩 null=未反馈（仅消息列表接口返回） */
  feedback?: number | null
  /** 回答的引用来源快照（v1.11 起落库；此前的老消息为 null） */
  sources?: MessageSource[] | null
}

/** 回答引用来源（答案里 [来源i] / 《文档》第N块 的溯源目标） */
export interface MessageSource {
  index: number
  chunk_id?: number
  document_id?: number
  document_title?: string
  /** 存储序号（0-based），仅用于排查 */
  chunk_index?: number
  /** 展示序号（1-based），前端展示与匹配一律用它 */
  chunk_no?: number
  score?: number
  content?: string
  /** 并行对比路由的来源分组 */
  group?: 'A' | 'B'
}

export type AgentRoute = 'rag' | 'db_query' | 'report' | 'parallel_compare' | 'direct'

// ---------------- 报表 ----------------
export interface ReportItem {
  id: number
  conversation_id: number
  title: string
  preview: string
  created_at: string | null
}

// ---------------- 全局统计（/stats/overview） ----------------
export interface StatsOverview {
  user_count: number
  kb_count: number
  doc_count: number
  qa_count: number
  avg_latency: number
  token_sum: number
}

// ---------------- 知识库授权 ----------------
export interface KBPermissionItem {
  user_id: number
  username: string
  display_name: string | null
  permission: 'read' | 'write'
  granted_by: number | null
  created_at: string | null
}

// ---------------- Trace ----------------
export interface TraceLog {
  id: number
  trace_id: string
  conversation_id: number | null
  message_id: number | null
  user_id: number | null
  kb_id: number | null
  agent_route: AgentRoute | null
  model: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  /** 【v1.16】回答是否被 max_tokens 截断（finish_reason=length） */
  is_truncated?: boolean
  llm_call_count: number
  tool_call_count: number
  num_chunks: number
  retrieval_ms: number
  total_latency_ms: number
  status: string
  error_msg: string | null
  started_at: string | null
  ended_at: string | null
  created_at: string
}

export interface TraceToolCall {
  id: number
  trace_id: string
  step_name: string
  tool_name: string
  tool_input: Record<string, unknown> | null
  tool_output: string | null
  latency_ms: number
  status: string
  error_msg: string | null
  started_at: string | null
  ended_at: string | null
}

export interface TraceDetail extends TraceLog {
  tool_calls: TraceToolCall[]
}

export interface TraceSummary {
  total: number
  avg_latency_ms: number
  token_sum: number
  llm_call_sum: number
  route_distribution: Record<string, number>
}

// ---------------- RAGAS 评测 ----------------
export interface EvalDataset {
  id: number
  kb_id: number
  name: string
  description: string | null
  item_count: number
  /** 参考答案缺失（空串/纯空白）的样本数；仅列表接口回填，缺省视为 0 */
  missing_reference?: number | null
  created_by: number
  created_at: string
}

export interface EvalItem {
  id: number
  dataset_id: number
  question: string
  reference_answer: string
  reference_contexts: unknown[] | null
  /** 分层难例标签（可选），历史样本为 null */
  stratum?: string | null
  created_at: string
}

export interface EvalRun {
  id: number
  dataset_id: number
  kb_id: number
  model: string
  /** pending / running / done / failed / cancelled。cancelled = 用户主动停止，
   *  与 failed 刻意区分：已完成部分的指标仍然真实，但样本数不全，不参与 A/B 对比 */
  status: string
  total_items: number
  success_items: number
  /** 已处理样本数（成功 + 失败 + 跳过），进度百分比用它算；
   *  v1.13.1 之前的历史任务为 0，此时退回 success_items/total_items 估算 */
  processed_items?: number
  /** 评测失败条数（已重试仍失败），历史任务为 0 */
  failed_items?: number
  metrics: Record<string, number | null> | null
  /** 创建任务时的请求级 RAG 实验参数；未传（沿用 KB 配置）时为 null */
  rag_config?: Record<string, unknown> | null
  created_by: number
  started_at: string | null
  finished_at: string | null
  error_msg: string | null
  created_at: string
}

/**
 * 大盘聚合统计（GET /eval/runs/stats，v1.13.2）。
 *
 * 刻意由后端算而不是前端拉列表 reduce：
 * 前端原实现只取第一页 100 条，任务超过 100 后数字会静默失真。
 *
 * metrics 里某项为 null 表示「没有任何可用样本」，前端必须显示「—」；
 * 绝不 fallback 成 0 —— 0 是「测出很差」，null 是「没测」。
 */
export interface EvalRunStats {
  total: number
  pending: number
  running: number
  done: number
  failed: number
  cancelled: number
  metrics: Record<string, number | null>
  /** 每项指标的样本级分母（四项可能不同：某指标为 null 时不进分母） */
  metric_items: Record<string, number>
  /** 实际参与加权计算的已完成任务数 */
  metric_runs: number
  /** 口径标识，当前只有 done_weighted（仅 done，按成功样本数加权） */
  scope: string
}

export interface EvalScore {
  id: number
  run_id: number
  item_id: number
  question: string
  answer: string
  retrieved_contexts: unknown[] | null
  faithfulness: number | null
  answer_relevancy: number | null
  context_precision: number | null
  context_recall: number | null
  latency_ms: number
  created_at: string
}

/** A/B 对比：单指标汇总（后端 POST /eval/runs/compare 返回） */
export interface EvalCompareMetric {
  metric: string
  avg_a: number | null
  avg_b: number | null
  /** Δ = B - A；任一侧缺值时为 null（空值不做 0 参与计算） */
  diff: number | null
}

/** A/B 对比：逐样本对齐行；仅单侧存在的样本，另一侧为 null */
export interface EvalCompareRow {
  question: string
  a: EvalScore | null
  b: EvalScore | null
  diffs: Record<string, number | null>
  /** 任一指标 |Δ| > diff_threshold 时为 true */
  is_diff: boolean
}

export interface EvalCompareResult {
  run_a: EvalRun
  run_b: EvalRun
  diff_threshold: number
  summary: EvalCompareMetric[]
  rows: EvalCompareRow[]
}

// ---------------- SSE 事件 ----------------
export interface SSEEvent {
  event: string
  data: Record<string, unknown>
}

export interface ChatRouteEvent { route: AgentRoute }
export interface ChatDeltaEvent { content: string }
export interface ChatReportEvent { content: string }
export interface ChatDoneEvent {
  trace_id: string
  conversation_id: number
  message_id: number
  agent_route: AgentRoute
}
export interface ChatErrorEvent { message: string }
