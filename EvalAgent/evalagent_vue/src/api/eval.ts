/** 评测任务接口（复用项目一 /eval/runs* 已有接口）。 */
import http from '@/api/http'
import type {
  BadcasePage,
  BadcaseQuery,
  EvalRun,
  Page,
  RunCompareResponse,
  RunCreatePayload,
  RunScore,
  RunStats,
  StratumMetricsResponse,
} from '@/api/types'

/** 任务列表（可按状态筛选：pending / running / done / failed / cancelled）。 */
export function listRuns(page = 1, size = 20, status?: string): Promise<Page<EvalRun>> {
  return http.get('/eval/runs', { params: { page, size, status } }) as Promise<Page<EvalRun>>
}

/**
 * 顶部大盘四指标（一次请求拿全，前端不做全量拉取后聚合 —— 列表是分页的，
 * 用当前页数据算「总数/成功率」必然是错的）。
 */
export function getRunStats(datasetId?: number): Promise<RunStats> {
  return http.get('/eval/runs/stats', {
    params: { dataset_id: datasetId },
  }) as Promise<RunStats>
}

/**
 * 停止任务（止损）：pending / running → cancelled，仅管理员。
 * 后端只写状态，执行器在 RUN_CANCEL_POLL_SECONDS 内自行停手并保留已产出的得分。
 * 已结束的任务会返回 409（不要当异常处理，UI 应当先判断状态再给按钮）。
 */
export function cancelRun(runId: number): Promise<EvalRun & { previous_status: string }> {
  return http.post(`/eval/runs/${runId}/cancel`) as Promise<EvalRun & { previous_status: string }>
}

/** 创建异步评测任务（仅管理员；上游 RunCreate 仅接收 dataset_id）。 */
export function createRun(payload: RunCreatePayload): Promise<EvalRun> {
  return http.post('/eval/runs', payload) as Promise<EvalRun>
}

/** 任务详情（含汇总 metrics）。 */
export function getRun(runId: number): Promise<EvalRun> {
  return http.get(`/eval/runs/${runId}`) as Promise<EvalRun>
}

/** 单条得分明细分页。 */
export function listScores(runId: number, page = 1, size = 20): Promise<Page<RunScore>> {
  return http.get(`/eval/runs/${runId}/scores`, { params: { page, size } }) as Promise<
    Page<RunScore>
  >
}

/**
 * 【B1-4 新增】BadCase 分页查询（后端筛选，替代前端全量拉取后过滤）。
 * 指定 metric 低于 threshold 的样本；NULL 指标不参与筛选（与后端统计口径一致）。
 */
export function listBadcases(runId: number, query: BadcaseQuery = {}): Promise<BadcasePage> {
  return http.get(`/eval/runs/${runId}/badcases`, {
    params: {
      metric: query.metric ?? 'faithfulness',
      threshold: query.threshold ?? 0.6,
      page: query.page ?? 1,
      page_size: query.page_size ?? 20,
    },
  }) as Promise<BadcasePage>
}

/**
 * 拉取某次任务的全量得分（跨页循环，size=100 为上游上限）。
 * A/B 对比与 BadCase 前端筛选都依赖全量数据。
 */
export async function listAllScores(runId: number, max = 2000): Promise<RunScore[]> {
  const all: RunScore[] = []
  let page = 1
  for (;;) {
    const res = await listScores(runId, page, 100)
    all.push(...res.items)
    if (all.length >= res.total || res.items.length === 0 || all.length >= max) break
    page += 1
  }
  return all
}

/** 导出单任务 CSV（返回文件 Blob，由调用方触发浏览器下载）。
 *  注意：拦截器对非标准响应体（CSV 文件流）原样放行完整 AxiosResponse，这里需要取出 .data。 */
export async function exportRunCsv(runId: number): Promise<Blob> {
  const res = await http.get(`/eval/runs/${runId}/export`, { responseType: 'blob' })
  // 防御：兼容拦截器返回完整 AxiosResponse 或直接返回 Blob 两种形态
  if (res instanceof Blob) return res
  const data = (res as { data?: unknown }).data
  if (data instanceof Blob) return data
  throw new Error('导出响应格式异常：预期收到 CSV 文件流')
}

/**
 * 【B2-4 新增】分层指标聚合：按 stratum 分层聚合四项 RAGAS 指标。
 * 后端 SQL AVG 聚合完成，前端只渲染，不做二次计算。
 */
export function getStratumMetrics(runId: number): Promise<StratumMetricsResponse> {
  return http.get(`/eval/runs/${runId}/stratum_metrics`) as Promise<StratumMetricsResponse>
}

/**
 * 【B1-5 新增】A/B 对比聚合：样本 question 对齐、四项指标差值 Δ 与汇总
 * 全部在后端完成；两个 run 必须属于同一 dataset_id（否则后端 400）。
 */
export function compareRuns(runIdA: number, runIdB: number): Promise<RunCompareResponse> {
  return http.post('/eval/runs/compare', {
    run_id_a: runIdA,
    run_id_b: runIdB,
  }) as Promise<RunCompareResponse>
}

/** 删除评测任务（级联删除得分明细）。仅管理员。 */
export function deleteRun(runId: number): Promise<{ deleted_run_id: number; deleted_scores: number }> {
  return http.delete(`/eval/runs/${runId}`) as Promise<{ deleted_run_id: number; deleted_scores: number }>
}
