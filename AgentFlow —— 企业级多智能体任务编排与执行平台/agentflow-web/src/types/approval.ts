/**
 * 审批相关类型。
 *
 * - 收件箱：`GET /approvals` → `ApprovalInboxData`（app/schemas/w3.py ApprovalInboxData）
 * - 单条详情：`GET /approvals/{approval_id}` → `ApprovalDetail`（interrupt_service._approval_to_dict）
 * - 挂起上下文：`GET /tasks/{id}/interrupt` → `InterruptContext`
 */
import type { ApprovalStatus, ApprovalType, TaskStatus } from './enums'

/** 高危子任务预览（审批单 payload.subtasks[] 的元素，字段为开放结构） */
export interface RiskySubtaskPreview {
  subtask_id?: number
  seq?: number
  name?: string
  /** 工具名规范键（后端 human_gate._preview 下发） */
  tool?: string
  /** 与 tool 同值，保留别名以兼容旧数据 */
  tool_hint?: string
  /** 规范值为 normal / high；历史数据可能是 "SubtaskRiskLevel.HIGH" */
  risk_level?: string
  description?: string
  [key: string]: unknown
}

/** 收件箱条目：任务信息 + 审批单 + 阻断原因，一次查询给够 */
export interface ApprovalInboxItem {
  task_id: number
  task_no: string | null
  goal: string
  status: string
  creator_id: number | null
  approval_id: number | null
  approval_type: string | null
  /** 阻断原因（HIGH_RISK_TOOL 等） */
  reason: string | null
  risky_subtasks: RiskySubtaskPreview[]
  created_at: string | null
  /** 审批截止时间，用于倒计时 */
  expires_at: string | null
  /**
   * 断点是否还在（v1.3）。`false` = 死锁：Redis checkpoint 已丢（过期清理 / 重启降级内存），
   * 点「审批」必然 409 TASK_NOT_PAUSED。前端据此置灰行内审批按钮。
   * 后端 fail-open：探不出来时一律 `true`。
   */
  resumable: boolean
}

/** GET /approvals 的 data（注意分页字段是平铺的，不是 meta 包裹） */
export interface ApprovalInboxData {
  items: ApprovalInboxItem[]
  page: number
  size: number
  total: number
}

/** GET /approvals/{approval_id} 的 data */
export interface ApprovalDetail {
  approval_id: number
  task_id: number
  subtask_id: number | null
  type: ApprovalType
  reason: string | null
  status: ApprovalStatus
  /** 审批单原始 payload（含 subtasks 预览、参数等，脱敏后） */
  payload: Record<string, unknown>
  decided_by: number | null
  decide_comment: string | null
  decided_at: string | null
  expires_at: string | null
}

/**
 * GET /tasks/{id}/interrupt 的 data（InterruptContext）。
 * 任务不在挂起态时 `paused=false`，其余字段可能为 null。
 */
export interface InterruptContext {
  paused: boolean
  task_id: number
  trace_id: string | null
  status: string | null
  approval_id: number | null
  /** HIGH_RISK_TOOL / LARGE_SCAN */
  type: string | null
  reason: string | null
  preview: Record<string, unknown>
  interrupted_at: string | null
  /** 恢复后将要执行的节点 */
  next_node: string[]
  resumable: boolean
  /** 恢复接口路径 */
  resume_endpoint: string | null
}

/**
 * POST /tasks/{id}/resume 请求体（ResumeRequest，extra="forbid"）。
 * decision 大小写不敏感，后端会归一化为 APPROVED / REJECTED。
 */
export interface ResumeRequest {
  decision: 'APPROVED' | 'REJECTED' | 'APPROVE' | 'REJECT' | 'approve' | 'reject'
  /** 审批意见，max 500；驳回时作为重规划的硬约束 */
  comment?: string
  approval_id?: number
  sync?: boolean | null
}

/** POST /tasks/{id}/resume 的 data（ResumeResult） */
export interface ResumeResult {
  task_id: number
  resumed: boolean
  status: TaskStatus
  approval_id: number | null
  decision: string | null
  next_nodes: string[]
}

/** POST /tasks/{id}/cancel 请求体（CancelRequest，extra="forbid"） */
export interface CancelRequest {
  /** 终止原因，max 255，写入 task.error.message */
  reason?: string
}

/** POST /tasks/{id}/cancel 的 data（CancelResult） */
export interface CancelResult {
  task_id: number
  status: TaskStatus
  canceled: boolean
  reason: string | null
  cancel_requested?: boolean
}

/**
 * `GET /approvals?status=...` 的条目（缺口 G4，`list_processed_approvals`）。
 *
 * 与 `ApprovalInboxItem` 的区别：这里直接映射 `approvals` 表行 + 后端补的 `task_no` / `goal`，
 * 没有 `creator_id` / `risky_subtasks` / `approval_type`，决策信息是 `decided_by` / `decide_comment` / `decided_at`。
 */
export interface ProcessedApprovalItem {
  approval_id: number
  task_id: number
  subtask_id: number | null
  /** HIGH_RISK_TOOL / LARGE_SCAN */
  type: ApprovalType
  reason: string | null
  status: ApprovalStatus
  payload: Record<string, unknown>
  /** 决策人用户 ID（后端未下发用户名） */
  decided_by: number | null
  decide_comment: string | null
  decided_at: string | null
  expires_at: string | null
  /** 后端批量补齐，任务已被删除时为 null */
  task_no: string | null
  goal: string | null
}

/** GET /approvals?status=... 的 data（分页字段平铺，与收件箱一致） */
export interface ProcessedApprovalData {
  items: ProcessedApprovalItem[]
  page: number
  size: number
  total: number
}

/** POST /approvals/batch 的单条决策（BatchDecisionItem，extra="forbid"） */
export interface BatchDecisionItem {
  task_id: number
  /** 大小写不敏感，后端归一化为 APPROVED / REJECTED */
  decision: 'APPROVED' | 'REJECTED'
  /** 审批意见，max 500 */
  comment?: string
}

/** POST /approvals/batch 请求体（BatchDecisionRequest，extra="forbid"；decisions 1~100 条） */
export interface BatchDecisionRequest {
  decisions: BatchDecisionItem[]
  sync?: boolean | null
}

/** 批量审批单条结果 */
export interface BatchApproveItemResult {
  task_id: number
  ok: boolean
  /** 成功时：恢复后的任务状态 */
  status?: string
  approval_id?: number | null
  /** 失败时：错误码与原因 */
  code?: string
  message?: string
}

/** POST /approvals/batch 的 data */
export interface BatchApproveResult {
  succeeded: number
  failed: number
  results: BatchApproveItemResult[]
}
