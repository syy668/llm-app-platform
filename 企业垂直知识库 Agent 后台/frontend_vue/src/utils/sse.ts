// SSE 流式客户端。
//
// 说明：浏览器原生 EventSource 仅支持 GET 且无法携带 Authorization 头，而后端
// /conversations/{id}/chat/stream 为 POST + Bearer 鉴权，故此处使用 fetch +
// ReadableStream 读取 text/event-stream，按字节 utf-8 解码并解析事件帧
// （route / delta / report / done / error），功能与 EventSource 等价。
import type { SSEEvent } from '@/types'
import { API_BASE, joinUrl } from './request'

export class SSEError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message)
    this.name = 'SSEError'
  }
}

/** 解析单个 SSE 事件帧（event: / data: 行） */
function parseFrame(frame: string): SSEEvent | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return { event, data: { content: dataLines.join('\n') } }
  }
}

/** 通用 POST SSE 流读取器，逐事件产出 SSEEvent */
export async function* fetchSSE(
  url: string,
  body: Record<string, unknown>,
  token: string,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body ?? {}),
    signal,
  })

  if (!resp.ok || !resp.body) {
    let message = `HTTP ${resp.status}`
    try {
      const j = await resp.json()
      message = (j && j.message) || message
    } catch {
      /* ignore */
    }
    throw new SSEError(message, resp.status)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let idx: number
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const ev = parseFrame(frame)
        if (ev) yield ev
      }
    }
    // 收尾：处理无换行结尾的最后一帧
    if (buffer.trim()) {
      const ev = parseFrame(buffer)
      if (ev) yield ev
    }
  } finally {
    reader.releaseLock()
  }
}

/** 对话页随请求传入的运行参数（仅透传后端支持的键） */
export interface ChatOptions {
  top_k?: number | null
  route_enabled?: Record<string, boolean> | null
}

/** 流式问答：对接 POST /conversations/{id}/chat/stream */
export function streamChat(
  conversationId: number,
  question: string,
  token: string,
  kbId?: number | null,
  options?: ChatOptions,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const url = joinUrl(API_BASE, `/conversations/${conversationId}/chat/stream`)
  const body: Record<string, unknown> = { question, ...(kbId ? { kb_id: kbId } : {}) }
  // 仅在用户调整过参数时携带，未调整时不发（后端走默认行为）
  if (options?.top_k != null) body.top_k = options.top_k
  if (options?.route_enabled) body.route_enabled = options.route_enabled
  return fetchSSE(url, body, token, signal)
}
