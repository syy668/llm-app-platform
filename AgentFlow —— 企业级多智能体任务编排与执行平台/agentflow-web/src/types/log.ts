/**
 * 日志检索类型 —— 与后端 `app/schemas/w6.py::LogItem / LogSearchData / LogSearchRequest` 一一对应。
 *
 * 数据源是 `task_executions` 结构化执行记录（不是 stdout 日志），所以字段是执行轨迹语义：
 * `node` 为执行节点、`event` 为生命周期事件、`payload` 为该步骤的开放结构入参出参。
 */

/**
 * POST /logs/search 请求体。
 *
 * 后端 `LogSearchRequest` 设了 `extra="forbid"`：**不能多传字段**；
 * 空字符串/undefined 必须整键省略（否则 `task_id=0` 会触发 422），故此处全部可选。
 */
export interface LogSearchQuery {
  /** trace_id 精确匹配 */
  trace_id?: string
  /** 任务 ID（≥1） */
  task_id?: number
  /** 事件类型：enter / exit / tool.call / tool.result / replan / human_edit / error */
  event?: string
  /** 仅识别 ERROR（其它值等价于全部） */
  level?: string
  /** 时间窗起点 ISO8601（按 started_at 过滤） */
  start?: string
  /** 时间窗终点 ISO8601 */
  end?: string
  page?: number
  size?: number
}

/** 单条日志（task_executions 一行） */
export interface LogItem {
  id: number
  trace_id: string
  span_id: string
  task_id: number
  /** 执行节点：planner / executor / reviewer / replan / summarizer / notifier / human_gate */
  node: string
  /** 生命周期事件 */
  event: string
  tool_name: string | null
  /** 工具错误信息；非空即视为 ERROR 级别 */
  tool_error: string | null
  /** 后端口径：tool_error 非空或 event=error → ERROR */
  level: 'INFO' | 'ERROR'
  input_tokens: number
  output_tokens: number
  latency_ms: number
  /** 开放结构，键名不固定（后端 JSON 列原样下发） */
  payload: unknown
  started_at: string | null
}

/** POST /logs/search 响应 data（扁平分页，与 /approvals 同构，非 W6 的 PageData 信封） */
export interface LogSearchData {
  items: LogItem[]
  page: number
  size: number
  total: number
}
