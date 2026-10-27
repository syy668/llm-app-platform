// Trace 链路日志接口封装（仅管理员）
import request from '@/utils/request'
import type { Paginated, TraceDetail, TraceLog, TraceSummary, TraceToolCall } from '@/types'

export interface TraceQuery {
  page?: number
  size?: number
  user_id?: number
  route?: string
  status?: string
  start?: string
  end?: string
}

export const listTraces = (params: TraceQuery = {}) =>
  request.get<unknown, Paginated<TraceLog>>('/traces', { params })

export const traceSummary = (params: { start?: string; end?: string } = {}) =>
  request.get<unknown, TraceSummary>('/traces/stats/summary', { params })

export const getTrace = (traceId: string) =>
  request.get<unknown, TraceDetail>(`/traces/${traceId}`)

export const listToolCalls = (traceId: string) =>
  request.get<unknown, Paginated<TraceToolCall>>(`/traces/${traceId}/tool-calls`)
