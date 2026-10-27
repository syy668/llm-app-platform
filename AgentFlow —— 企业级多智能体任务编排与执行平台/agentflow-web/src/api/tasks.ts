/**
 * 任务接口 —— 对应后端 `app/routers/tasks.py` 与 `app/routers/tasks_ext.py`。
 *
 * 路由事实（务必按此调用，不要臆造）：
 *  - `POST /tasks`            创建任务并**异步**执行（无事件流，W2 契约）
 *  - `POST /tasks/stream`     创建任务并驱动**流式**执行，返回 stream_url（W3 演示入口，仅 ADMIN）
 *  - `GET  /tasks/{id}`       任务详情
 *  - `GET  /tasks/{id}/stream`        SSE 进度流（见 api/stream.ts）
 *  - `GET  /tasks/{id}/interrupt`     当前挂起审批上下文
 *  - `POST /tasks/{id}/resume`        审批决策 + 断点续跑
 *  - `POST /tasks/{id}/cancel`        终止任务
 *  - `GET  /tasks/{id}/snapshot`      完整状态快照（回放）
 *  - `GET  /tasks/{id}/trace`         历史步骤与 checkpoint 时间轴
 *
 * `GET /tasks`（分页任务列表）由 `app/routers/tasks_w6.py` 提供（W6 §1）；
 * 页面保留 isMissingEndpoint 回落"本机最近访问"，仅作旧版本/异常兜底，不模拟数据。
 */
import { api } from './http'
import { downloadJson } from '@/utils/download'
import type { PageData } from '@/types/common'
import type { InterruptContext, ResumeRequest, ResumeResult, CancelRequest, CancelResult } from '@/types/approval'
import type { SnapshotData, TraceReplayData } from '@/types/trace'
import type { TaskArchiveResult, TaskCreateRequest, TaskCreateResult, TaskDetail, TaskListItem, TaskListQuery } from '@/types/task'

/** POST /tasks —— 非流式创建（后台 asyncio 任务执行，前端只能轮询详情） */
export function createTask(payload: TaskCreateRequest): Promise<TaskCreateResult> {
  return api.post<TaskCreateResult>('/tasks', payload)
}

/**
 * POST /tasks/stream —— 流式创建，返回带 stream_url 的提交结果。
 * 仅 admin 可用；创建成功后应立即用 stream_url 建立 SSE 连接。
 */
export function createTaskStream(payload: TaskCreateRequest): Promise<TaskCreateResult> {
  return api.post<TaskCreateResult>('/tasks/stream', payload)
}

/**
 * GET /tasks —— 分页任务列表（W6 文档 §1；对接清单 G1）。
 *
 * 权限：A/R 可查全部（可传 creator_id 过滤）；viewer 服务端强制本人并脱敏 creator_name，
 * 前端无需也不应做额外过滤。异常或旧版本 404 时，调用方用 isMissingEndpoint 回落占位。
 */
export function listTasks(query: TaskListQuery = {}): Promise<PageData<TaskListItem>> {
  return api.get<PageData<TaskListItem>>('/tasks', query as Record<string, unknown>)
}

/** GET /tasks/{task_ref} —— 详情（数字主键或任务编号 AF2026xxxx-xxxx 均可，后端自动识别） */
export function getTaskDetail(taskRef: number | string): Promise<TaskDetail> {
  return api.get<TaskDetail>(`/tasks/${encodeURIComponent(String(taskRef))}`)
}

/**
 * POST /tasks/{task_id}/archive —— 归档 / 取消归档（v1.3，仅终态任务）。
 *
 * 归档不是删除：数据、审批单、轨迹、产物全部保留，详情按 ID 仍可打开。
 * 非终态任务后端返回 409 TASK_STATE_CONFLICT（提示"先终止再归档"）。
 */
export function archiveTask(taskId: number, archived: boolean): Promise<TaskArchiveResult> {
  return api.post<TaskArchiveResult>(`/tasks/${taskId}/archive`, { archived })
}

/** GET /tasks/{task_id}/interrupt —— 挂起审批上下文；未挂起时 paused=false */
export function getInterrupt(taskId: number): Promise<InterruptContext> {
  return api.get<InterruptContext>(`/tasks/${taskId}/interrupt`)
}

/** POST /tasks/{task_id}/resume —— 审批通过/驳回，从 human_gate 断点续跑 */
export function resumeTask(taskId: number, payload: ResumeRequest): Promise<ResumeResult> {
  return api.post<ResumeResult>(`/tasks/${taskId}/resume`, payload)
}

/** POST /tasks/{task_id}/cancel —— 置 CANCELLED，不再执行 */
export function cancelTask(taskId: number, payload: CancelRequest = {}): Promise<CancelResult> {
  return api.post<CancelResult>(`/tasks/${taskId}/cancel`, payload)
}

/** GET /tasks/{task_id}/snapshot —— 完整状态快照（回放用，含 LangGraph values） */
export function getSnapshot(taskId: number): Promise<SnapshotData> {
  return api.get<SnapshotData>(`/tasks/${taskId}/snapshot`)
}

/**
 * GET /tasks/{task_id}/trace —— 历史 span + checkpoint 时间轴。
 * @param afterId 增量拉取：只返回该 span id 之后的记录（首屏传 0）
 */
export function getTrace(taskId: number, afterId = 0): Promise<TraceReplayData> {
  return api.get<TraceReplayData>(`/tasks/${taskId}/trace`, { after_id: afterId })
}

/**
 * GET /tasks/{task_id}/export —— 任务归档导出（详情 + 子任务 + 评审 + 审批 + 轨迹）。
 * 服务端生成文件并带 Content-Disposition，前端只负责落盘。
 */
export function downloadTaskExport(taskId: number): Promise<string> {
  return downloadJson(`/tasks/${taskId}/export`, `task-${taskId}-export.json`)
}

/** GET /tasks/{task_id}/trace/export —— 纯轨迹导出（steps + checkpoints + interrupt） */
export function downloadTraceExport(taskId: number): Promise<string> {
  return downloadJson(`/tasks/${taskId}/trace/export`, `task-${taskId}-trace.json`)
}
