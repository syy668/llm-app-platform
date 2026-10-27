/**
 * Trace / 快照 / checkpoint 类型 —— 对应后端 `app/schemas/w3.py`。
 *
 * G7 已闭环：`TraceStep` 现在出参 `span_id` / `parent_span_id` / `subtask_id`
 *（后端 `trace_service._to_dict`，表列本就存在，零迁移），
 * 因此子任务 ↔ 工具调用可以**精确归属**，不必再按 tool_name / 时间窗猜。
 * 旧数据这三列可能为 null，UI 需保留按 tool_name 的兜底展示。
 */
import type { TaskStatus } from './enums'

/** task_executions 的一行（回放按 id 顺序，等价于 started_at 顺序） */
export interface TraceStep {
  id: number
  trace_id: string
  task_id: number
  /** span 唯一标识（G7 新增出参） */
  span_id?: string | null
  /** 父 span，用于还原调用层级（G7 新增出参） */
  parent_span_id?: string | null
  /** 该 span 归属的子任务主键；planner/reviewer 等任务级节点为 null（G7 新增出参） */
  subtask_id?: number | null
  /** planner / executor / human_gate / reviewer / replan / summarizer / notifier */
  node: string
  /** enter / exit / tool.call / tool.result / replan / human_edit / error */
  event: string
  tool_name: string | null
  tool_error: string | null
  input_tokens: number
  output_tokens: number
  latency_ms: number
  /** 入参出参等原始负载（已脱敏），开放结构 */
  payload: Record<string, unknown> | null
  started_at: string | null
}

/** 一次 checkpoint 快照的摘要（DAG 回放的时间轴节点） */
export interface CheckpointStep {
  step: number | null
  /** 触发来源节点 */
  source: string | null
  status: string | null
  next_nodes: string[]
  plan_size: number
  done_count: number
  created_at: string | null
}

/** GET /tasks/{id}/trace 的 data（TraceReplayData） */
export interface TraceReplayData {
  task_id: number
  trace_id: string | null
  steps: TraceStep[]
  checkpoints: CheckpointStep[]
  interrupt: Record<string, unknown> | null
  total_steps: number
}

/** GET /tasks/{id}/snapshot 的 data（SnapshotData） */
export interface SnapshotData {
  task_id: number
  trace_id: string | null
  status: TaskStatus | null
  next_nodes: string[]
  created_at: string | null
  step: number | null
  /** LangGraph 状态值，开放结构 */
  values: Record<string, unknown>
  messages_count: number
  interrupt: Record<string, unknown> | null
}
