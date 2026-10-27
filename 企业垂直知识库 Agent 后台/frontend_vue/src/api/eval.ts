// RAGAS 评测接口封装（仅管理员）
import request from '@/utils/request'
import type {
  EvalCompareResult,
  EvalDataset,
  EvalItem,
  EvalRun,
  EvalRunStats,
  EvalScore,
  Paginated,
} from '@/types'

export const listDatasets = (params: { page?: number; size?: number; kb_id?: number } = {}) =>
  request.get<unknown, Paginated<EvalDataset>>('/eval/datasets', { params })

export const createDataset = (data: { kb_id: number; name: string; description?: string | null }) =>
  request.post<unknown, EvalDataset>('/eval/datasets', data)

export const addItems = (
  datasetId: number,
  items: Array<{
    question: string
    reference_answer: string
    reference_contexts?: string[] | null
    stratum?: string | null
  }>,
) => request.post<unknown, { imported: number; item_count: number }>(`/eval/datasets/${datasetId}/items`, { items })

export const listItems = (datasetId: number, params: { page?: number; size?: number } = {}) =>
  request.get<unknown, Paginated<EvalItem>>(`/eval/datasets/${datasetId}/items`, { params })

/**
 * 修改单条样本（PATCH 语义：只提交需要改的字段，未提交的保持原值）。
 *
 * 为什么必须是 PATCH 而不是整条 PUT：badcase 自动沉淀的样本 stratum 可能为
 * null、reference_contexts 可能为空，若前端只补参考答案却回传空 stratum，
 * 覆盖式更新会把已有分层标签清掉。
 */
export const updateItem = (
  datasetId: number,
  itemId: number,
  data: { reference_answer?: string; reference_contexts?: string[] | null; stratum?: string | null },
) => request.put<unknown, EvalItem>(`/eval/datasets/${datasetId}/items/${itemId}`, data)

/** 删除单条脏样本；返回重算后的数据集样本数 */
export const deleteItem = (datasetId: number, itemId: number) =>
  request.delete<unknown, { deleted_item_id: number; item_count: number }>(
    `/eval/datasets/${datasetId}/items/${itemId}`,
  )

export const deleteDataset = (datasetId: number) =>
  request.delete<unknown, { deleted_dataset_id: number; deleted_items: number }>(
    `/eval/datasets/${datasetId}`,
  )

/**
 * 请求级 RAG 实验参数（A/B 用）。
 *
 * 口径与后端 `schemas/eval.py::RagConfig` 一致：
 * - top_k / search_mode 请求级生效，直接覆盖知识库默认检索；
 * - chunk_size 属于入库切片参数，查询期无法生效，仅作实验记录展示，故这里不暴露。
 */
export interface RagConfigPayload {
  top_k?: number
  search_mode?: 'vector_only' | 'bm25_vector_hybrid'
}

export const createRun = (datasetId: number, ragConfig?: RagConfigPayload | null) =>
  request.post<unknown, EvalRun>(
    '/eval/runs',
    ragConfig ? { dataset_id: datasetId, rag_config: ragConfig } : { dataset_id: datasetId },
  )

export const listRuns = (params: { page?: number; size?: number; status?: string } = {}) =>
  request.get<unknown, Paginated<EvalRun>>('/eval/runs', { params })

/**
 * 大盘聚合统计（v1.13.2）。
 *
 * 后端一次算完四项指标均值 + 全状态计数，前端**不要**再拉列表自己 reduce：
 * 列表接口 size 上限 100，任务超过 100 后前端算出来的数会静默失真。
 */
export const getRunStats = () => request.get<unknown, EvalRunStats>('/eval/runs/stats')

export const getRun = (runId: number) => request.get<unknown, EvalRun>(`/eval/runs/${runId}`)

/**
 * 删除评测任务（v1.13.1，管理员）。
 *
 * 会级联删除该 run 的全部得分明细，不可恢复 —— 前端必须二次确认。
 * 后端只允许删除**终态**任务（done/failed/cancelled）；pending/running 会返回 400
 * 要求先停止，否则后台线程收尾时会往已删除的 run_id 写孤儿得分。
 */
export const deleteRun = (runId: number) =>
  request.delete<unknown, { deleted_run_id: number; deleted_scores: number }>(
    `/eval/runs/${runId}`,
  )

/**
 * 停止评测任务（v1.13）。
 *
 * 协作式取消：接口立即返回并把状态置 cancelled，后台线程在**下一个样本检查点**
 * 退出，所以调用后可能还会再跑完当前那一条。返回体里的 success_items 是已完成的
 * 条数，前端据此告诉用户「已停止，保留已完成 N 条」。
 */
export const cancelRun = (runId: number) =>
  request.post<unknown, EvalRun>(`/eval/runs/${runId}/cancel`)

/**
 * 手动清理僵尸任务（v1.13）。
 *
 * 异常退出后残留的 running 任务已无执行线程，却永久占着「同时运行任务数」名额，
 * 导致新建任务一直 429 而界面上看不到在跑的任务。启动时会自动清一次，这个接口
 * 是运行期的显式出口 —— 刻意不放进 create_run 自动做，避免在创建任务的路径上
 * 偷偷改别的任务状态。
 *
 * 【v1.13.1】返回体多一个 repaired：同时回填历史脏数据的汇总指标（那些只落了
 * 得分没落 metrics 的中断任务）。
 */
export const cleanupStaleRuns = () =>
  request.post<unknown, { cleared: number; repaired?: number }>('/eval/runs/cleanup-stale')

/**
 * A/B 对比聚合（后端算好再下发，前端只渲染）。
 *
 * 口径要点（见 `eval_service.compare_runs`）：
 * - 两个 run 必须属于同一 dataset_id，否则 400（样本无法对齐）；
 * - summary 优先取 run.metrics，缺省时由逐样本得分现算（仅非空值求均值）；
 * - rows 按 question 对齐，只出现在单侧的样本另一侧为 null 并保留展示；
 * - is_diff = 任一指标 |Δ| > diff_threshold（响应里带回来，前端不自己定阈值）。
 */
export const compareRuns = (runIdA: number, runIdB: number) =>
  request.post<unknown, EvalCompareResult>('/eval/runs/compare', {
    run_id_a: runIdA,
    run_id_b: runIdB,
  })

export const listScores = (runId: number, params: { page?: number; size?: number } = {}) =>
  request.get<unknown, Paginated<EvalScore>>(`/eval/runs/${runId}/scores`, { params })

/**
 * 下载评测得分 CSV。
 * 统一走后端导出接口 GET /eval/runs/{run_id}/export：
 * 后端返回带 UTF-8 BOM 的 text/csv（含 retrieved_contexts 列），
 * 前端以 blob 原样接收并触发浏览器下载，避免本地拼接丢列 / 重复编码 BOM。
 */
export async function exportRunScoresCsv(runId: number): Promise<void> {
  const blob = await request.get<unknown, Blob>(`/eval/runs/${runId}/export`, {
    responseType: 'blob',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `ragas_run_${runId}.csv`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
