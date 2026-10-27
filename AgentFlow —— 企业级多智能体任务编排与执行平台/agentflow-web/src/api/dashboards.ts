/**
 * 大盘聚合接口 —— W6 文档 §2（对接清单 G2）。
 *
 * 后端**已实现**（`app/routers/dashboards.py`）：summary / tools 由 W6 §2 提供，
 * trend 由缺口 G4 补充。调用方仍保留 `isMissingEndpoint` 回落——仅用于旧版本后端
 * 或路由异常时显示 StateBlock 占位，不伪造数据。
 *
 * 角色约定：A/R/V 均可调用；viewer 脱敏由后端完成（token 字段返回 null），前端只管渲染。
 */
import { api } from './http'
import type { DashboardSummary, DashboardTools, DashboardTrend, DashboardFailures } from '@/types/dashboard'

function windowParams(start?: string, end?: string): Record<string, unknown> {
  const params: Record<string, unknown> = {}
  if (start) params.start = start
  if (end) params.end = end
  return params
}

/** GET /dashboards/summary —— P02 五张指标卡（字段与前端卡片一一对应，见 W6 §2.2） */
export function getDashboardSummary(start?: string, end?: string): Promise<DashboardSummary> {
  return api.get<DashboardSummary>('/dashboards/summary', windowParams(start, end))
}

/**
 * GET /dashboards/tools —— 工具调用分布（数据源 task_executions 实际调用记录，W6 §2.3）。
 * 按 call_count 降序；无记录时 items 为空数组（不报错）。
 */
export function getDashboardTools(start?: string, end?: string): Promise<DashboardTools> {
  return api.get<DashboardTools>('/dashboards/tools', windowParams(start, end))
}

/**
 * GET /dashboards/trend —— 任务完成趋势（按天聚合，缺口 G4）。
 *
 * 口径（后端 `app/routers/dashboards.py::dashboard_trend`）：
 *  - `task_total` = 当日新建任务数（created_at 归日）；
 *  - `completed`  = 当日 COMPLETED 任务数；
 *  - `completion_rate` = completed / 当日终态总数（与 summary 口径一致）。
 * 窗口内每天都返回一个点（含 0 值空桶），前端无需补点。
 */
export function getDashboardTrend(start?: string, end?: string): Promise<DashboardTrend> {
  return api.get<DashboardTrend>('/dashboards/trend', windowParams(start, end))
}

/**
 * GET /dashboards/failure-reasons —— 失败原因 Top 5（数据源 tasks.error.code，原型 P02）。
 *
 * 口径：窗口内 status=FAILED 的任务按 error.code 聚合；缺码归入 `UNKNOWN`；
 * CANCELLED 不计入（取消是用户动作不是失败）。按 count 降序，最多 5 条。
 */
export function getDashboardFailureReasons(start?: string, end?: string): Promise<DashboardFailures> {
  return api.get<DashboardFailures>('/dashboards/failure-reasons', windowParams(start, end))
}
