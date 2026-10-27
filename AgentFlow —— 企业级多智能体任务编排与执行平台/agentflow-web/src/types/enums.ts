/**
 * 领域枚举 —— 与后端 `app/schemas/enums.py` 逐字对齐，禁止增删。
 */

/** 用户角色（`users.role`） */
export const UserRole = {
  ADMIN: 'admin',
  APPROVER: 'approver',
  VIEWER: 'viewer',
} as const
export type UserRole = (typeof UserRole)[keyof typeof UserRole]

/**
 * 任务状态（`tasks.status`）—— 共 7 个值。
 * 最易漏的是 `SUMMARIZING`：Reviewer 通过后进入摘要生成阶段，与 RUNNING 视觉必须区分。
 */
export const TaskStatus = {
  PLANNING: 'PLANNING',
  AWAITING_APPROVAL: 'AWAITING_APPROVAL',
  RUNNING: 'RUNNING',
  SUMMARIZING: 'SUMMARIZING',
  COMPLETED: 'COMPLETED',
  FAILED: 'FAILED',
  CANCELLED: 'CANCELLED',
} as const
export type TaskStatus = (typeof TaskStatus)[keyof typeof TaskStatus]

/** 子任务工具提示（`subtasks.tool_hint`），下划线形式；展示时需转连字符工具名 */
export const SubtaskToolHint = {
  SQL_QUERY: 'sql_query',
  CHART_RENDER: 'chart_render',
  KB_SEARCH: 'kb_search',
  NOTIFY: 'notify',
  NONE: 'none',
} as const
export type SubtaskToolHint = (typeof SubtaskToolHint)[keyof typeof SubtaskToolHint]

/** 子任务风险等级 */
export const SubtaskRiskLevel = {
  NORMAL: 'normal',
  HIGH: 'high',
} as const
export type SubtaskRiskLevel = (typeof SubtaskRiskLevel)[keyof typeof SubtaskRiskLevel]

/** 子任务状态 */
export const SubtaskStatus = {
  PENDING: 'PENDING',
  READY: 'READY',
  RUNNING: 'RUNNING',
  DONE: 'DONE',
  FAILED: 'FAILED',
  SKIPPED: 'SKIPPED',
  CANCELLED: 'CANCELLED',
} as const
export type SubtaskStatus = (typeof SubtaskStatus)[keyof typeof SubtaskStatus]

/** 图节点名（`task_executions.node` / LangGraph 节点） */
export const ExecutionNode = {
  PLANNER: 'planner',
  EXECUTOR: 'executor',
  REVIEWER: 'reviewer',
  REPLAN: 'replan',
  SUMMARIZER: 'summarizer',
  NOTIFIER: 'notifier',
  HUMAN_GATE: 'human_gate',
} as const
export type ExecutionNode = (typeof ExecutionNode)[keyof typeof ExecutionNode]

/** 轨迹事件类型（`task_executions.event`） */
export const ExecutionEvent = {
  ENTER: 'enter',
  EXIT: 'exit',
  TOOL_CALL: 'tool.call',
  TOOL_RESULT: 'tool.result',
  REPLAN: 'replan',
  HUMAN_EDIT: 'human_edit',
  ERROR: 'error',
} as const
export type ExecutionEvent = (typeof ExecutionEvent)[keyof typeof ExecutionEvent]

/** 审批类型 */
export const ApprovalType = {
  HIGH_RISK_TOOL: 'HIGH_RISK_TOOL',
  LARGE_SCAN: 'LARGE_SCAN',
} as const
export type ApprovalType = (typeof ApprovalType)[keyof typeof ApprovalType]

/** 审批单状态 */
export const ApprovalStatus = {
  PENDING: 'PENDING',
  APPROVED: 'APPROVED',
  REJECTED: 'REJECTED',
  EXPIRED: 'EXPIRED',
} as const
export type ApprovalStatus = (typeof ApprovalStatus)[keyof typeof ApprovalStatus]

/** 记忆分层。短期层（redis）不出现在管理 API 中 */
export const MemoryScope = {
  SHORT: 'short',
  MID: 'mid',
  LONG: 'long',
} as const
export type MemoryScope = (typeof MemoryScope)[keyof typeof MemoryScope]

/** 评测来源 / 变体 / 运行状态 —— 仅用于读取 `eval_runs` 表的展示口径 */
export const EvalSource = {
  REVIEWER_GATE: 'reviewer_gate',
  REGRESSION: 'regression',
  AB_TEST: 'ab_test',
} as const
export type EvalSource = (typeof EvalSource)[keyof typeof EvalSource]

export const EvalVariant = {
  COMPRESS_ON: 'compress_on',
  COMPRESS_OFF: 'compress_off',
} as const
export type EvalVariant = (typeof EvalVariant)[keyof typeof EvalVariant]

export const EvalRunStatus = {
  PENDING: 'PENDING',
  RUNNING: 'RUNNING',
  DONE: 'DONE',
  FAILED: 'FAILED',
} as const
export type EvalRunStatus = (typeof EvalRunStatus)[keyof typeof EvalRunStatus]
