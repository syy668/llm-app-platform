/**
 * 任务 / 子任务 / 产物类型。
 *
 * 字段来源：`TaskService.get_detail()` 的真实返回（app/services/task_service.py L307-353），
 * 以及 `app/schemas/graph_state.py` 的 TimeRange、`app/schemas/agent_io.py` 的 TaskCreateRequest。
 *
 * 红线：
 *  - 任务主键叫 `task_id`，不是 `id`；子任务主键才是 `id`，序号是 `seq`；
 *  - 没有任何 `duration` / `elapsed` 字段，耗时一律前端按 `finished_at - created_at` 计算；
 *  - `summary` / `review` / `eval_score` / `token_ledger` / `artifacts` 是**开放结构**，
 *    只在运行时快照可用时下发，UI 必须"有则展示、无则隐藏"。
 */
import type { SubtaskRiskLevel, SubtaskStatus, SubtaskToolHint, TaskStatus } from './enums'

/** 业务时间范围（后端 `TimeRange`：start/end 为 date，序列化成 YYYY-MM-DD） */
export interface TimeRange {
  start: string
  end: string
}

/**
 * POST /tasks、POST /tasks/stream 请求体（`TaskCreateRequest`，extra="forbid"）。
 * 只能传下列字段，多传任何一个都会 422。
 *
 * G8 三开关（v1.0 已闭环，后端 `app/schemas/agent_io.py` + `app/agents/graph.py`）：
 *  - `compress`   → 规划后走 `context_compress` 分支，产出 context_digest 供下游复用；
 *  - `use_memory` → START 后先走 `memory_recall` 召回长期记忆，命中并入 planner/replan prompt；
 *  - `eval_tag`   → 任务级评测标签（≤64），落库后可按 `GET /eval/runs?eval_tag=` 关联筛选。
 * 三者默认全关，关闭时走原链路（图上条件边直连，默认行为零变化）。
 */
export interface TaskCreateRequest {
  /** 自然语言目标，1~500 字 */
  goal: string
  time_range: TimeRange
  /** true=阻塞到终态或下次挂起；false=后台执行（默认） */
  sync?: boolean
  /** 上下文压缩（默认 false）：True 时规划后启用压缩分支 */
  compress?: boolean
  /** 长期记忆检索（默认 false）：True 时规划前召回记忆并入 prompt */
  use_memory?: boolean
  /** 评测标签（默认 null，≤64 字符）：有值时绑定到 eval_run，供后续评测筛选 */
  eval_tag?: string | null
}

/** 创建任务的返回（create_task 的 dict + stream_url） */
export interface TaskCreateResult {
  task_id: number
  trace_id: string
  status: TaskStatus
  task_no: string
  /** 仅 POST /tasks/stream 返回：SSE 相对路径 */
  stream_url?: string
  stream_accept?: string
}

/** 子任务（`subtasks` 表 + 运行期 result_summary/retry_count） */
export interface Subtask {
  /** 子任务主键 */
  id: number
  /** 计划内序号，从 1 开始 */
  seq: number
  name: string
  description: string
  tool_hint: SubtaskToolHint
  depends_on: number[]
  risk_level: SubtaskRiskLevel
  status: SubtaskStatus
  result_summary: string | null
  retry_count: number
}

/** 任务错误结构（`tasks.error` JSON 列） */
export interface TaskError {
  code?: string
  message?: string
  /** 出错的 span id，可直达 Trace 行；后端可能为 null */
  span_id?: number | null
  [key: string]: unknown
}

/** 产物引用（`state.artifacts[]`，开放结构，只取已确认存在的键） */
export interface ArtifactItem {
  artifact_id?: number | string
  /** 形如 `/static/charts/xxx.png` 的相对路径，需拼 API Base */
  url?: string
  type?: string
  name?: string
  title?: string
  [key: string]: unknown
}

/** 审阅结果（`state.review`，开放结构） */
export interface ReviewResult {
  decision?: string
  score?: number
  /** 5 维平均总分（后端 review.total） */
  total?: number
  /** 是否通过（后端字段名是 pass，TS 里用 pass 更直观） */
  pass?: boolean
  /** 可信度 0~1（后端服务端计算，非 LLM 自报） */
  confidence?: number
  /** 三态判定：PASS / REVISE / REJECT */
  verdict?: string
  confidence_reason?: string
  /** 放行原因（评审被跳过时后端下发） */
  skip_reason?: string
  scores?: Record<string, number>
  degraded?: boolean
  skipped?: boolean
  suggestions?: unknown[]
  [key: string]: unknown
}

/** Token 记账（`state.token_ledger`，开放结构：按节点累计） */
export type TokenLedger = Record<string, number>

/**
 * GET /tasks/{task_id} 的 data。
 * 第 1~20 行为必存在字段；第 21 行起为运行时快照字段（失败时整体缺省）。
 */
export interface TaskDetail {
  task_id: number
  task_no: string
  goal: string
  time_range: TimeRange | Record<string, string>
  status: TaskStatus
  replan_count: number
  token_usage: number
  trace_id: string
  creator_id: number
  error: TaskError | null
  created_at: string | null
  finished_at: string | null
  subtasks: Subtask[]

  /**
   * G8 三开关的落库回读（后端 `tasks` 表同名三列，详情接口原样下发）。
   * 旧版本后端可能不返回，故为可选；详情页据此展示"本任务实际启用了哪些高级分支"。
   */
  compress?: boolean | null
  use_memory?: boolean | null
  eval_tag?: string | null

  /** 以下来自 checkpoint 快照，可能整体不存在 */
  summary?: string
  review?: ReviewResult | null
  /** v1.1：REJECT 后的人工批注记录（status=ANNOTATED 重规划 / APPROVED 人工放行） */
  review_intervention?: ReviewIntervention | null
  eval_score?: number | Record<string, unknown> | null
  token_ledger?: TokenLedger
  approvals?: Record<string, unknown>
  artifacts?: ArtifactItem[]
}

/** `state.review_intervention`：人工批注结果 */
export interface ReviewIntervention {
  status?: 'ANNOTATED' | 'APPROVED' | string
  decision?: string
  comment?: string
  decided_by?: number | null
  verdict?: string
}

/**
 * 任务列表行 —— 字段与 `agentflow-platform/docs/W6接口设计文档.md` §1.3 一一对应
 * （复用 task_service.get_detail 顶层字段名，不含 subtasks 重负载）。
 *
 * 权限约定（W6 §1.1）：viewer 调用时服务端强制 creator_id=本人，且 `creator_name` 恒为 null（脱敏）。
 * 后端已实现（`app/routers/tasks_w6.py`，W6 §1）；异常时页面回落"本机最近访问"。
 */
export interface TaskListItem {
  task_id: number
  task_no: string
  goal: string
  time_range: TimeRange | Record<string, string>
  status: TaskStatus
  replan_count: number
  token_usage: number
  trace_id: string
  creator_id: number
  /** JOIN users.username；viewer 角色恒为 null */
  creator_name: string | null
  /** 列表仅下发 {code, message}，span_id 不出（W6 §1.3） */
  error: { code: string; message: string } | null
  created_at: string | null
  finished_at: string | null
  /** finished_at - created_at，服务端算好；未终态为 null */
  duration_s: number | null
  /** 归档时间（v1.3）；null=未归档。默认列表不含已归档任务 */
  archived_at: string | null
}

/** GET /tasks 查询参数（W6 §1.2；均走 query string） */
export interface TaskListQuery {
  page?: number
  size?: number
  /** TaskStatus 枚举，可逗号分隔多值 */
  status?: string
  /** viewer 下服务端忽略并强制覆盖为本人 */
  creator_id?: number
  /** goal LIKE %kw% */
  keyword?: string
  /** ISO8601，过滤 created_at 闭区间 */
  start?: string
  end?: string
  order?: 'created_desc' | 'created_asc'
  /** 归档过滤：不传=默认（排除已归档）；true=只看已归档；false=只看未归档 */
  archived?: boolean
}

/** POST /tasks/{id}/archive 的返回（v1.3） */
export interface TaskArchiveResult {
  task_id: number
  task_no: string
  status: TaskStatus
  archived: boolean
  archived_at: string | null
  /** false 表示目标状态已满足（幂等无操作） */
  changed: boolean
}
