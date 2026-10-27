/**
 * 评测接口组 —— W6 文档 §3（对接清单 G3，FR-016）。
 *
 * 后端**已实现**（`app/routers/eval.py`，W6 §3 / FR-016）。
 * 调用方仍保留 `isMissingEndpoint` 回落——仅用于旧版本后端或路由异常时显示占位，不伪造数据。
 *
 * 角色约定（后端裁决，前端不做二次拦截，只做显隐优化）：
 *  - GET /eval/runs、POST /eval/runs：仅 A/R，viewer 调用返回 403 FORBIDDEN；
 *  - GET /eval/runs/summary、GET /eval/metrics：A/R/V 均可（V 的 token 字段为 null）。
 */
import { api } from './http'
import type { PageData } from '@/types/common'
import type {
  EvalRunCreateRequest,
  EvalRunCreateResult,
  EvalRunItem,
  EvalRunQuery,
  EvalRunSummary,
  EvalMetricsTrend,
} from '@/types/eval'

/** GET /eval/runs —— 评测运行分页列表（仅 A/R） */
export function listEvalRuns(query: EvalRunQuery = {}): Promise<PageData<EvalRunItem>> {
  return api.get<PageData<EvalRunItem>>('/eval/runs', query as Record<string, unknown>)
}

/**
 * GET /eval/runs/summary —— 按 variant 聚合（A/B 对比卡）。
 * @param variants 可传多个；不传 = 全部 variant 各聚合一行。仅统计 status=DONE。
 */
export function getEvalRunSummary(variants?: string[]): Promise<EvalRunSummary> {
  const params: Record<string, unknown> = {}
  if (variants && variants.length > 0) params.variant = variants.join(',')
  return api.get<EvalRunSummary>('/eval/runs/summary', params)
}

/** GET /eval/metrics —— 按自然日聚合的趋势点（指标趋势卡） */
export function getEvalMetricsTrend(start?: string, end?: string): Promise<EvalMetricsTrend> {
  const params: Record<string, unknown> = {}
  if (start) params.start = start
  if (end) params.end = end
  return api.get<EvalMetricsTrend>('/eval/metrics', params)
}

/**
 * POST /eval/runs —— 触发回归评测（仅 A/R；viewer 403）。
 * 后端立即返回 PENDING，随后由后端每 10s 轮询项目二回写 metrics（W6 §3.5）；
 * 前端提交成功后轮询 listEvalRuns 刷新即可。
 */
export function createEvalRun(payload: EvalRunCreateRequest = {}): Promise<EvalRunCreateResult> {
  return api.post<EvalRunCreateResult>('/eval/runs', payload)
}
