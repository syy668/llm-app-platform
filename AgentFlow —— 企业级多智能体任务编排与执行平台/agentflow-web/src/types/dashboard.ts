/**
 * 大盘聚合类型 —— 与 `agentflow-platform/docs/W6接口设计文档.md` §2 一一对应。
 *
 * 后端已实现（`app/routers/dashboards.py`，含 G4 的 GET /dashboards/trend）；
 * 页面保留 404 回落占位，仅用于旧版本/异常兜底。
 * 角色约定（W6 §2.1）：A/R/V 均可调用；viewer 脱敏 = token_usage_total / avg_token_per_task 恒为 null。
 */

/** GET /dashboards/summary —— P02 五张指标卡的数据源 */
export interface DashboardSummary {
  window: { start: string; end: string }
  /** COUNT(tasks) in window（按 created_at）→「今日任务总量」 */
  task_total: number
  /** COMPLETED / 终态总数；窗口内无终态任务为 0 */
  completion_rate: number
  /** AWAITING_APPROVAL 计数，不限定 window（反映当前积压）→「待审批数量」 */
  pending_approvals: number
  /** 终态任务 AVG(duration_s)；无样本为 null */
  avg_duration_s: number | null
  /** viewer 恒为 null（脱敏） */
  token_usage_total: number | null
  /** viewer 恒为 null（脱敏） */
  avg_token_per_task: number | null
  /** 窗口内 GROUP BY status，六键恒齐；预留状态分布图 */
  status_breakdown: Record<string, number>
}

/** GET /dashboards/tools 行 —— 数据源 task_executions（实际调用，非规划意图） */
export interface ToolUsageItem {
  tool_name: string
  call_count: number
  /** 成功次数/总数 */
  success_rate: number
  avg_latency_ms: number | null
}

export interface DashboardTools {
  window: { start: string; end: string }
  /** 按 call_count 降序；无记录为空数组 */
  items: ToolUsageItem[]
}

/** GET /dashboards/failure-reasons 行 —— 数据源 tasks.error.code（缺码归入 UNKNOWN） */
export interface FailureReasonItem {
  reason_code: string
  count: number
  /** 该码下首条 error.message，悬浮提示用；无则 null */
  sample_message: string | null
}

export interface DashboardFailures {
  window: { start: string; end: string }
  /** 窗口内 FAILED 任务总数（含未标注），用于卡片脚注对账 */
  failed_total: number
  /** 按 count 降序取前 5；窗口内无失败时为空数组 */
  items: FailureReasonItem[]
}

/** GET /dashboards/trend 单个时间桶（按天，缺口 G4-趋势卡） */
export interface TrendPoint {
  /** 日期 YYYY-MM-DD；空桶也会补齐，保证折线连续 */
  date: string
  /** 当日新建任务数（按 created_at 归日） */
  task_total: number
  /** 当日完成（COMPLETED）任务数 */
  completed: number
  /** completed / 当日终态总数；当日无终态任务为 0 */
  completion_rate: number
}

export interface DashboardTrend {
  window: { start: string; end: string }
  /** 按日期升序；窗口内每天一点（含 0 值空桶） */
  points: TrendPoint[]
}
