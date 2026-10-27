/**
 * 日志检索接口 —— 系统设置页「请求日志」区（对接清单 G6）。
 *
 * 后端**已实现**（`app/routers/logs.py`）：`POST /logs/search`，数据源为
 * `task_executions` 结构化执行记录（唯一持久化的运行轨迹），不接外部日志平台。
 *
 * 角色：A/R/V 均可调用（只读轨迹回放，与 `GET /tasks/{id}/trace` 同源）。
 * 调用方仍保留 `isMissingEndpoint` 回落——仅用于旧版本后端或路由异常时显示占位，不伪造数据。
 */
import { api } from './http'
import type { LogSearchData, LogSearchQuery } from '@/types/log'

/**
 * POST /logs/search —— 按 trace_id / task_id / event / level / 时间窗检索，分页返回。
 *
 * 后端按 `started_at DESC, id DESC` 排序；`total` 为命中去重后总数（用于分页）。
 * 注意请求体 `extra="forbid"`：调用方必须先剔除空值再传。
 */
export function searchLogs(query: LogSearchQuery = {}): Promise<LogSearchData> {
  return api.post<LogSearchData>('/logs/search', query)
}
