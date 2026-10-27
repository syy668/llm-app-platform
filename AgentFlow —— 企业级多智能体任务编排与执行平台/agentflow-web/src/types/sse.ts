/**
 * SSE 事件类型 —— 对应后端 `app/schemas/w3.py` 的 SseEventType 与 stream_context.emit()。
 *
 * 四类事件 + 一条通用信封字段：
 *  信封：task_id / trace_id / seq / node / ts / elapsed_ms
 *  - state_update：phase = run | enter | exit | error | cancel_requested
 *  - tool_call   ：phase = call | result
 *  - interrupt   ：审批挂起（终帧之一，收到即应关闭连接）
 *  - complete    ：任务终态（终帧之一）
 *
 * 心跳是 SSE 注释帧（`: ping`），不会触发 message 事件，前端无需处理。
 */
import type { TaskStatus } from './enums'

export const SseEventType = {
  STATE_UPDATE: 'state_update',
  TOOL_CALL: 'tool_call',
  INTERRUPT: 'interrupt',
  COMPLETE: 'complete',
} as const
export type SseEventType = (typeof SseEventType)[keyof typeof SseEventType]

/** 收到即代表"本次连接可收尾"的终止帧（与后端 TERMINAL_EVENTS 一致） */
export const TERMINAL_EVENTS: ReadonlySet<string> = new Set([SseEventType.INTERRUPT, SseEventType.COMPLETE])

/** 所有事件的公共信封 */
export interface SseEnvelope {
  task_id: number
  trace_id: string
  /** 事件序号（单调递增，用于排序） */
  seq: number
  /** 事件归属节点 */
  node: string | null
  /** 服务端时间（ISO8601） */
  ts: string
  /** 本次执行已耗时（毫秒） */
  elapsed_ms?: number
}

export type StateUpdatePhase = 'run' | 'enter' | 'exit' | 'error' | 'cancel_requested'

/** state_update 事件负载 */
export interface StateUpdatePayload extends SseEnvelope {
  phase: StateUpdatePhase
  /** 任务状态；enter/exit 时为 RUNNING 或节点输出里的 status */
  status?: TaskStatus | string
  /** 仅 phase=run：fresh（新执行） / resume（审批后继续） */
  mode?: 'fresh' | 'resume'
  /** 仅 phase=exit：节点输出字典的键名列表（最多 20 个） */
  output_keys?: string[]
  /** 仅 phase=error：错误摘要 */
  error?: string
}

export type ToolCallPhase = 'call' | 'result'

/** tool_call 事件负载 */
export interface ToolCallPayload extends SseEnvelope {
  phase: ToolCallPhase
  /** MCP 工具名：sql-query / chart-render / kb-search / notify */
  tool: string
  /** MCP server 名 */
  server: string | null
  /** phase=call：已脱敏的入参 */
  params?: Record<string, unknown>
  /** phase=result */
  ok?: boolean
  latency_ms?: number
  error_code?: string | null
  /** 结果摘要（已脱敏，不是完整结果集） */
  result_digest?: unknown
}

/** interrupt 事件负载：InterruptContext + phase=awaiting_approval */
export interface InterruptPayload extends SseEnvelope {
  phase: 'awaiting_approval'
  paused: boolean
  status: string | null
  approval_id: number | null
  type: string | null
  reason: string | null
  preview: Record<string, unknown>
  interrupted_at: string | null
  next_node: string[]
  resumable: boolean
  resume_endpoint: string | null
}

/** complete 事件负载：正常终态与取消终态字段不同，故全部可选 */
export interface CompletePayload extends SseEnvelope {
  status: TaskStatus | string
  token_usage?: number
  /** 产物列表（与详情接口 artifacts 同结构） */
  artifacts?: Array<Record<string, unknown>>
  degraded?: boolean
  replan_count?: number
  /** 取消时出现 */
  reason?: string
  expired_approvals?: number
  error?: { code?: string; message?: string }
}

/** 前端统一事件回调签名 */
export interface StreamHandlers {
  onStateUpdate?: (payload: StateUpdatePayload) => void
  onToolCall?: (payload: ToolCallPayload) => void
  onInterrupt?: (payload: InterruptPayload) => void
  onComplete?: (payload: CompletePayload) => void
  /** 连接异常（EventSource 自带重连，这里只做 UI 提示） */
  onError?: (error: Event) => void
  /** 连接已建立 */
  onOpen?: () => void
}
