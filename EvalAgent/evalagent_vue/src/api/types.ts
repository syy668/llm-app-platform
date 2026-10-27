/** 上游项目一接口的 TS 类型定义（与 app/schemas/eval.py、user.py、kb.py 对齐）。 */

export interface UserInfo {
  id: number
  username: string
  display_name: string | null
  email: string | null
  role: 'admin' | 'user' | string
  status: string
  created_at: string
}

export interface LoginResult {
  access_token: string
  refresh_token: string
  user: UserInfo
}

export interface KB {
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
}

export interface Dataset {
  id: number
  kb_id: number
  name: string
  description: string | null
  item_count: number
  created_by: number
  created_at: string
}

export interface DatasetItem {
  id: number
  dataset_id: number
  question: string
  reference_answer: string
  reference_contexts: string[] | null
  /** 【B2-4 新增】分层难例标签；历史数据为 null */
  stratum?: string | null
  created_at: string
}

export type RunStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled'

/**
 * 列表页顶部「大盘」四项指标（GET /eval/runs/stats）。
 * 口径与后端一致，前端只渲染不二次计算：
 * - success_rate = done / total，cancelled 与 failed 都算未成功；
 * - avg_faithfulness 是**样本级**均值，无样本时为 null（不是 0）。
 */
export interface RunStats {
  total_runs: number
  done_runs: number
  failed_runs: number
  cancelled_runs: number
  running_runs: number
  pending_runs: number
  success_rate: number
  avg_faithfulness: number | null
  scored_samples: number
  total_tokens: number
  total_cost_usd: number
}

/** 请求级 RAG 实验参数（上游 eval_runs.rag_config，全部键可选） */
export interface RagConfig {
  chunk_size?: number | null
  search_mode?: 'vector_only' | 'bm25_vector_hybrid' | null
  top_k?: number | null
}

export interface RunMetrics {
  faithfulness: number | null
  answer_relevancy: number | null
  context_precision: number | null
  context_recall: number | null
}

export interface EvalRun {
  id: number
  dataset_id: number
  kb_id: number
  model: string
  status: RunStatus
  total_items: number
  success_items: number
  /** 已处理（成功+失败+跳过）—— 停止任务时用于算「已保住了多少」 */
  processed_items?: number
  /** 本轮评测实际消耗的裁判 token */
  total_tokens?: number
  variant?: string | null
  /** 本次打分用的向量模型（answer_relevancy 依赖）；换 embedding 前后不可直接比 */
  judge_embedding_model?: string
  metrics: RunMetrics | null
  /** 创建任务时传入的 RAG 实验参数；未传/历史记录为 null */
  rag_config: RagConfig | null
  created_by: number
  started_at: string | null
  finished_at: string | null
  error_msg: string | null
  created_at: string
}

export interface RunScore {
  id: number
  run_id: number
  item_id: number
  question: string
  answer: string
  /**
   * 标准答案。打分时 runner 已把 `eval_items.reference_answer` 冗余落库到 eval_scores，
   * 后端自 v1.24 起同时以 `ground_truth` 别名返回（与 /badcases 同名字段对齐）。
   * 两个字段值相同，展示侧优先 ground_truth、回落 reference_answer（兼容旧快照/旧缓存）。
   */
  reference_answer?: string | null
  /** 同 reference_answer；仅 BadcaseRow 声明为必填，明细行可能缺省 */
  ground_truth?: string | null
  retrieved_contexts: string[] | null
  faithfulness: number | null
  answer_relevancy: number | null
  context_precision: number | null
  context_recall: number | null
  latency_ms: number
  created_at: string
}

/** 统一分页响应体 {total, items} */
export interface Page<T> {
  total: number
  items: T[]
}

/** 可参与 BadCase 筛选的指标名（与后端白名单一致） */
export type BadcaseMetric =
  | 'faithfulness'
  | 'answer_relevancy'
  | 'context_precision'
  | 'context_recall'

/** 【B1-4 新增】BadCase 行：得分明细 + ground_truth（后端 LEFT JOIN 数据集样本补齐） */
export interface BadcaseRow extends RunScore {
  /** 标准答案（eval_dataset_items.reference_answer）；数据集被删/样本缺失时为 null */
  ground_truth: string | null
}

/** 【B1-4 新增】BadCase 分页响应体（后端筛选完成，前端无需全量计算） */
export interface BadcasePage {
  total: number
  page: number
  page_size: number
  metric: BadcaseMetric
  threshold: number
  items: BadcaseRow[]
}

/** 【B1-4 新增】BadCase 查询参数（全部可选，后端有默认值） */
export interface BadcaseQuery {
  metric?: BadcaseMetric
  threshold?: number
  page?: number
  page_size?: number
}

/** 创建评测任务请求体（rag_config 可选；top_k / search_mode 请求级生效，chunk_size 为实验记录） */
export interface RunCreatePayload {
  dataset_id: number
  rag_config?: RagConfig | null
}

/** 样本导入请求体（question-ground_truth CSV 的 ground_truth 映射为 reference_answer） */
export interface ItemInput {
  question: string
  reference_answer: string
  reference_contexts?: string[] | null
  /** 【B2-4 新增】分层难例标签（可选，后端 max_length=64）；不传为未分层 */
  stratum?: string | null
  /** 可选：难度 1~5（JSON 导入可携带；CSV 无此列） */
  difficulty?: number | null
  /** 可选：样本类型（默认 rag_qa）；JSON 导入可携带 */
  item_type?: string | null
}

/** 数据集文件（CSV/JSON）解析结果 —— DatasetFileInput 组件向上抛出 */
export interface DatasetFileParsed {
  items: ItemInput[]
  errors: string[]
  warnings: string[]
  fileName: string
}

/** 【B2-4 新增】分层指标聚合行（GET /eval/runs/{run_id}/stratum_metrics 返回项） */
export interface StratumMetricsItem {
  /** 分层标签；null = 未分层（含历史数据 / 数据集样本已删除） */
  stratum: string | null
  /** 该层样本数 */
  sample_count: number
  /** 该层四项 RAGAS 指标均值（SQL AVG 忽略 NULL；全空时为 null） */
  metrics: RunMetrics
}

/** 【B2-4 新增】分层指标聚合响应体（后端聚合完成，前端只渲染） */
export interface StratumMetricsResponse {
  run_id: number
  total: number
  items: StratumMetricsItem[]
}

/** 【B1-5 新增】A/B 对比逐样本行（对齐、差值、is_diff 均由后端计算） */
export interface RunCompareRow {
  /** 对齐用的 question 文本 */
  question: string
  /** A 组得分明细；该样本仅在 B 侧时为 null */
  a: RunScore | null
  /** B 组得分明细；该样本仅在 A 侧时为 null */
  b: RunScore | null
  /** 四项指标差值（b - a，保留 6 位小数）；任一侧为 null 时该指标差值为 null */
  diffs: Record<BadcaseMetric, number | null>
  /** 任一指标 |Δ| > diff_threshold 即为差异样本 */
  is_diff: boolean
}

/** 【B1-5 新增】A/B 对比汇总行（均值优先取 run.metrics 落库值，后端兜底现算） */
export interface RunCompareSummaryItem {
  metric: BadcaseMetric
  avg_a: number | null
  avg_b: number | null
  /** avg_b - avg_a；任一侧为 null 时为 null */
  diff: number | null
}

/** 【B1-5 新增】A/B 对比聚合响应体（POST /eval/runs/compare） */
export interface RunCompareResponse {
  run_a: EvalRun
  run_b: EvalRun
  /** 差异样本判定阈值（后端常量，当前为 0.1） */
  diff_threshold: number
  summary: RunCompareSummaryItem[]
  rows: RunCompareRow[]
}

/** 系统配置（GET /system/config 返回；llm_api_key 恒为掩码 '******' 或空串，不回显真实值） */
export interface SystemConfig {
  ai_rate_limit_per_minute: number
  tool_timeout_seconds: number
  tool_max_attempts: number
  tool_retry_delay_seconds: number
  llm_temperature: number
  llm_model: string
  llm_api_base: string
  llm_api_key: string
}

/** 更新系统配置请求体（仅提交需要修改的项；llm_api_key 留空 = 保持现有密钥不变） */
export interface SystemConfigPayload {
  llm_model?: string
  llm_api_base?: string
  llm_temperature?: number
  llm_api_key?: string
}

/* ---------------- 裁判（考官）配置：本中台 8200 /system/judge-config ---------------- */

/** 裁判配置视图。temperature 恒为 0.0（temperature_locked=true），前端只读展示。 */
export interface JudgeConfigView {
  model: string
  api_base: string
  /** 已配置时恒为 '******'，绝不返回明文 */
  api_key: string
  api_key_configured: boolean
  embedding_model: string
  /** 向量模型独立 base_url；空 = 与裁判 LLM 同源 */
  embedding_api_base: string
  /** 已配置时恒为 '******' */
  embedding_api_key: string
  embedding_api_key_configured: boolean
  /** 实际生效的向量地址（后端已算好回落） */
  effective_embedding_api_base: string
  /** true = 向量与裁判共用同一地址与 Key */
  embedding_shares_judge_endpoint: boolean
  temperature: number
  temperature_locked: boolean
  /** 只读展示项：超时 / 并发 / 重试 / 关思考 等 */
  readonly: Record<string, number | boolean>
  /** 每个键的生效来源：db（界面改过）/ env（沿用 .env） */
  source: Record<string, 'db' | 'env'>
  /** 相对 .env 被界面覆盖过的键 */
  overridden: string[]
  env_default: Record<string, string | null>
  descriptions: Record<string, string>
  updated_at: string | null
  updated_by: string | null
}

/** 更新裁判配置：只提交要改的字段；api_key 留空或传掩码 = 不修改。无 temperature 字段。 */
export interface JudgeConfigPayload {
  model?: string
  api_base?: string
  api_key?: string
  embedding_model?: string
  /** 向量模型独立 base_url；传空串 = 显式回落「与裁判同源」 */
  embedding_api_base?: string
  embedding_api_key?: string
}

export interface JudgePreflightResult {
  ok: boolean
  message: string
  model?: string | null
  latency_ms?: number | null
}
