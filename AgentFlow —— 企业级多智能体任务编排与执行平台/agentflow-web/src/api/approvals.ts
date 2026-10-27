/**
 * 审批接口 —— 对应后端 `app/routers/approvals.py`。
 *
 * 路由事实（务必按此调用，不要臆造）：
 *  - `GET  /approvals`                     待处理收件箱（缺省）/ 已处理列表（传 status）
 *  - `GET  /approvals/{approval_id}`       审批单详情
 *  - `POST /approvals/batch`               批量审批（逐条复用 resume、单条失败隔离）
 *  - 审批动作本身仍可走 `POST /tasks/{id}/resume`（见 api/tasks.ts），批量端点内部即此逻辑
 */
import { api } from './http'
import type {
  ApprovalDetail,
  ApprovalInboxData,
  BatchDecisionRequest,
  BatchApproveResult,
  ProcessedApprovalData,
} from '@/types/approval'

/**
 * GET /approvals —— 待审批任务列表（审批收件箱）。
 * 权限：admin / approver；admin 仅返回本人创建的任务。
 */
export function listApprovals(params: { page?: number; size?: number } = {}): Promise<ApprovalInboxData> {
  return api.get<ApprovalInboxData>('/approvals', { page: params.page ?? 1, size: params.size ?? 20 })
}

/**
 * GET /approvals?status=... —— 已处理审批列表（缺口 G4）。
 *
 * 口径：数据源是 `approvals` 表本身到终态的记录（APPROVED / REJECTED / EXPIRED），
 * 与待处理收件箱（任务卡在 AWAITING_APPROVAL）互补，**不是同一批数据的另一种视图**。
 * status 传逗号分隔多值；不传则后端回退待处理收件箱——因此这里必须显式传。
 */
export function listProcessedApprovals(params: {
  status?: string
  page?: number
  size?: number
} = {}): Promise<ProcessedApprovalData> {
  return api.get<ProcessedApprovalData>('/approvals', {
    status: params.status ?? 'APPROVED,REJECTED,EXPIRED',
    page: params.page ?? 1,
    size: params.size ?? 20,
  })
}

/** GET /approvals/{approval_id} —— 审批单详情（含脱敏后的 payload） */
export function getApproval(approvalId: number): Promise<ApprovalDetail> {
  return api.get<ApprovalDetail>(`/approvals/${approvalId}`)
}

/**
 * POST /approvals/batch —— 批量审批（缺口 G9）。
 *
 * 后端逐条复用 `StreamingTaskService.resume`：单条失败不中断其余，返回
 * `{succeeded, failed, results[]}`，results 每项带 `ok` 与失败原因，前端据此分组提示。
 * 约束：decisions 1~100 条；decision 大小写不敏感（APPROVED/REJECTED）。
 */
export function batchApprove(payload: BatchDecisionRequest): Promise<BatchApproveResult> {
  return api.post<BatchApproveResult>('/approvals/batch', payload)
}
