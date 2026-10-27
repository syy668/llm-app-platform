/**
 * SSE 流式连接封装。
 *
 * 为什么不用 fetch + ReadableStream：
 * 后端 `/tasks/{id}/stream` 是标准 SSE（id/event/data 帧 + `: ping` 心跳 + retry），
 * 原生 EventSource 自带断线重连与 Last-Event-ID 回传，比自己解析收益更大。
 *
 * 两个坑必须处理：
 *  1. EventSource **不能设置自定义请求头**，所以令牌只能走 `?access_token=`；
 *     后端 `sse_current_user` 明确支持这一方式（Bearer 头优先，其次查询参数）。
 *  2. `interrupt` / `complete` 是终帧（后端 TERMINAL_EVENTS），收到后必须主动 close()，
 *     否则浏览器会自动重连，把已经收尾的任务重新挂上。
 */
import { API_ROOT, getAccessToken } from './http'
import {
  SseEventType,
  TERMINAL_EVENTS,
  type CompletePayload,
  type InterruptPayload,
  type StateUpdatePayload,
  type StreamHandlers,
  type ToolCallPayload,
} from '@/types/sse'

export interface TaskStreamOptions {
  taskId: number
  handlers: StreamHandlers
  /** 断点续传游标；一般不需要手动传，浏览器会用 Last-Event-ID */
  lastEventId?: string | null
  /** 连接意外关闭后的最大重试次数 */
  maxRetries?: number
}

/** 构造 SSE 地址（令牌放查询参数，EventSource 不支持自定义头） */
export function buildStreamUrl(taskId: number, lastEventId?: string | null): string {
  const token = getAccessToken() ?? ''
  const params = new URLSearchParams({ access_token: token })
  if (lastEventId) params.set('last_event_id', lastEventId)
  return `${API_ROOT}/tasks/${taskId}/stream?${params.toString()}`
}

export class TaskStreamClient {
  private source: EventSource | null = null
  private readonly taskId: number
  private readonly handlers: StreamHandlers
  private readonly maxRetries: number
  private retries = 0
  private lastEventId: string | null
  /** 主动关闭或被终帧收尾后置 true，阻止重连 */
  private finished = false

  constructor(options: TaskStreamOptions) {
    this.taskId = options.taskId
    this.handlers = options.handlers
    this.lastEventId = options.lastEventId ?? null
    this.maxRetries = options.maxRetries ?? 5
  }

  get isClosed(): boolean {
    return this.finished || this.source === null
  }

  /**
   * 当前事件游标（最后收到的 SSE id）。
   *
   * 重连时**必须**带着它：后端 `sse_event_frames` 的 `START_ID = "0-0"`，
   * 不带游标会从任务的第一条事件重放，把历史上已经消费过的 `interrupt`
   * 终止帧再投一次（表现为"审批完又弹出一个空的审批抽屉"）。
   */
  get cursor(): string | null {
    return this.lastEventId
  }

  /** 建立连接；重复调用会先关闭旧连接 */
  connect(): void {
    this.close()
    this.finished = false
    const source = new EventSource(buildStreamUrl(this.taskId, this.lastEventId))
    this.source = source

    source.onopen = () => {
      this.retries = 0
      this.handlers.onOpen?.()
    }

    source.addEventListener(SseEventType.STATE_UPDATE, (event) => {
      const payload = this.parse<StateUpdatePayload>(event)
      if (payload) this.handlers.onStateUpdate?.(payload)
    })

    source.addEventListener(SseEventType.TOOL_CALL, (event) => {
      const payload = this.parse<ToolCallPayload>(event)
      if (payload) this.handlers.onToolCall?.(payload)
    })

    source.addEventListener(SseEventType.INTERRUPT, (event) => {
      const payload = this.parse<InterruptPayload>(event)
      if (payload) this.handlers.onInterrupt?.(payload)
      this.finish()
    })

    source.addEventListener(SseEventType.COMPLETE, (event) => {
      const payload = this.parse<CompletePayload>(event)
      if (payload) this.handlers.onComplete?.(payload)
      this.finish()
    })

    source.onerror = (error) => {
      this.handlers.onError?.(error)
      // readyState=CLOSED 说明浏览器不会再自动重连（鉴权失败 / 服务端关闭 / 任务终态）
      if (source.readyState === EventSource.CLOSED && !this.finished && this.retries < this.maxRetries) {
        this.retries += 1
        const delay = Math.min(1000 * 2 ** (this.retries - 1), 15000)
        window.setTimeout(() => {
          if (!this.finished) this.connect()
        }, delay)
      }
    }
  }

  /** 主动关闭（页面卸载、用户点击"停止跟随"） */
  close(): void {
    if (this.source) {
      this.source.close()
      this.source = null
    }
  }

  /** 收到终帧：记录游标并关闭，不再重连 */
  private finish(): void {
    this.finished = true
    this.close()
  }

  /** 解析事件负载；坏帧不应打断整个流 */
  private parse<T>(event: MessageEvent<string>): T | null {
    if (event.lastEventId) this.lastEventId = event.lastEventId
    try {
      return JSON.parse(event.data) as T
    } catch (error) {
      console.warn('[TaskStream] 无法解析 SSE 负载', error, event.data)
      return null
    }
  }
}

/** 判断某个事件名是否属于终帧（供页面复用） */
export function isTerminalEvent(eventName: string): boolean {
  return TERMINAL_EVENTS.has(eventName)
}
