/**
 * 评测接口类型 —— 与 `agentflow-platform/docs/W6接口设计文档.md` §3 一一对应。
 *
 * 后端已实现（`app/routers/eval.py`，W6 §3）；页面保留 404 回落占位，仅用于旧版本/异常兜底。
 * 角色约定（W6 §3.2）：GET /eval/runs 与 POST /eval/runs 仅 A/R（viewer 403）；
 * GET /eval/runs/summary 与 GET /eval/metrics 对 V 开放（token 字段脱敏为 null）。
 */

/** GET /eval/runs 行 —— 直接映射 eval_runs 表列，不造新字段（W6 §3.2） */
export interface EvalRunItem {
  run_id: number
  run_no: string
  /** reviewer_gate / regression / ab_test（后端 EvalSource 枚举） */
  source: 'reviewer_gate' | 'regression' | 'ab_test'
  /** compress_on / compress_off；可空 */
  variant: 'compress_on' | 'compress_off' | null
  dataset_id: string | null
  /** G8 评测标签：由任务创建时的 eval_tag 透传绑定，供按标签筛选 */
  eval_tag: string | null
  /** 项目二 eval run 回溯 id */
  ext_run_id: string | null
  /** PENDING / RUNNING / DONE / FAILED（后端 EvalRunStatus 枚举） */
  status: 'PENDING' | 'RUNNING' | 'DONE' | 'FAILED'
  /**
   * 键名沿用 eval_runs 表注释既有键；DONE 前可为 null。
   * viewer 脱敏：token_cost 恒为 null。
   *
   * v1.4 起真实评测（项目一 RAGAS 底座）写入的是 ragas 四指标原名：
   * faithfulness / answer_relevancy / context_precision / context_recall。
   * task_completion / plan_f1 / tool_acc 是**智能体编排**指标，与 RAG 质量
   * 指标语义不同，后端刻意不做映射 —— 该字段缺数据就是 undefined，
   * 前端必须显示「—」，不得补 0 或复用别的分数顶替。
   */
  metrics: {
    task_completion?: number
    plan_f1?: number
    tool_acc?: number
    faithfulness?: number | null
    answer_relevancy?: number | null
    context_precision?: number | null
    context_recall?: number | null
    token_cost?: number | null
    latency_p50?: number
    latency_p95?: number
    sample_count?: number | null
    success_items?: number
    failed_items?: number
    duration_s?: number | null
  } | null
  /** detail JSON 的样本计数（逐样本明细不下发） */
  sample_count: number
  error_message: string | null
  /**
   * true = 该行指标由后端 `_mock_metrics()` 随机生成（MOCK_EXTERNAL_EVAL=true），
   * **不可用于业务结论**。前端据此打红标「Mock」并把指标置灰。
   */
  is_mock: boolean
  /** v1.4 真实评测来源：project1_ragas = 项目一 RAGAS 底座；null = 历史/mock 记录 */
  eval_backend: string | null
  /** 真实评测的判分模型名（项目一 eval_llm_model，temperature 固定 0） */
  judge_model: string | null
  /**
   * 指标口径：kb_retrieval_chain = 评的是「知识库检索 + 生成」链路，
   * **不是** AgentFlow 端到端多智能体编排质量。前端必须明示，防止误读。
   */
  metrics_scope: string | null
  /** 真实评测墙钟耗时（秒），取自项目一 started_at→finished_at */
  duration_s: number | null
  created_at: string | null
  finished_at: string | null
}

/** GET /eval/runs 查询参数（source/variant/status 均可逗号分隔多值） */
export interface EvalRunQuery {
  page?: number
  size?: number
  source?: string
  variant?: string
  status?: string
  /** G8：按评测标签精确过滤（与任务创建时的 eval_tag 对应） */
  eval_tag?: string
}

/** POST /eval/runs 请求体（extra=forbid，W6 §3.5） */
export interface EvalRunCreateRequest {
  /** ≤64，默认 "agentflow-30"（PRD 写死数据集） */
  dataset_id?: string
  variant?: 'compress_on' | 'compress_off'
  /** ≤200，写入 detail.note */
  note?: string
  /** G8：评测标签（≤64），与任务创建的 eval_tag 同值时可把两者关联起来 */
  eval_tag?: string
}

/** POST /eval/runs 响应 —— 立即返回，前端拿 run_id 轮询列表 */
export interface EvalRunCreateResult {
  run_id: number
  run_no: string
  status: 'PENDING' | 'RUNNING' | 'DONE' | 'FAILED'
}

/** GET /eval/runs/summary 分组行 —— P06「上下文压缩 A/B 对比」数据源（W6 §3.3） */
export interface EvalSummaryGroup {
  variant: 'compress_on' | 'compress_off' | null
  run_count: number
  avg_task_completion: number | null
  avg_plan_f1: number | null
  avg_tool_acc: number | null
  /** v1.4 真实 RAGAS 四指标；mock 行恒为 null（显示「—」，不补 0） */
  avg_faithfulness: number | null
  avg_answer_relevancy: number | null
  avg_context_precision: number | null
  avg_context_recall: number | null
  /** viewer 恒为 null（脱敏） */
  avg_token_cost: number | null
  /**
   * 口径（W6 写死）：以 compress_off 组 avg_token_cost 为基线，1 - 该组/基线；
   * 基线组自身为 null；无基线组时两组均 null。仅统计 status=DONE。
   */
  token_saving_rate: number | null
  avg_latency_p95_s: number | null
}

export interface EvalRunSummary {
  window: unknown | null
  groups: EvalSummaryGroup[]
}

/** GET /eval/metrics 按天聚合点 —— P06「指标趋势」数据源（W6 §3.4） */
export interface EvalMetricsPoint {
  date: string
  run_count: number
  avg_task_completion: number | null
  avg_plan_f1: number | null
  avg_tool_acc: number | null
  /** v1.4 真实 RAGAS 四指标（同 run 日内算术均值；空值不参与） */
  avg_faithfulness: number | null
  avg_answer_relevancy: number | null
  avg_context_precision: number | null
  avg_context_recall: number | null
  /** viewer 恒为 null（脱敏） */
  avg_token_cost: number | null
  avg_latency_p95_s: number | null
}

export interface EvalMetricsTrend {
  /** 按日期升序；无数据日期不出点 */
  points: EvalMetricsPoint[]
}
