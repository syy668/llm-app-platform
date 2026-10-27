/** 数据集接口（复用项目一 /eval/datasets* 已有接口）。 */
import http from '@/api/http'
import type { Dataset, DatasetItem, ItemInput, Page } from '@/api/types'

export function listDatasets(page = 1, size = 100): Promise<Page<Dataset>> {
  return http.get('/eval/datasets', { params: { page, size } }) as Promise<Page<Dataset>>
}

export interface DatasetCreatePayload {
  kb_id: number
  name: string
  description?: string | null
}

/** 创建数据集（仅管理员）。 */
export function createDataset(payload: DatasetCreatePayload): Promise<Dataset> {
  return http.post('/eval/datasets', payload) as Promise<Dataset>
}

/** 批量导入样本（单次最多 500 条，与上游 ItemsImport 约束一致）。 */
export function addDatasetItems(datasetId: number, items: ItemInput[]): Promise<{ imported: number; item_count: number }> {
  return http.post(`/eval/datasets/${datasetId}/items`, { items }) as Promise<{
    imported: number
    item_count: number
  }>
}

/** 样本分页列表。 */
export function listDatasetItems(datasetId: number, page = 1, size = 10): Promise<Page<DatasetItem>> {
  return http.get(`/eval/datasets/${datasetId}/items`, { params: { page, size } }) as Promise<
    Page<DatasetItem>
  >
}

/** 删除数据集（级联删除样本；已生成的评测运行记录保留）。仅管理员。 */
export function deleteDataset(datasetId: number): Promise<{ deleted_dataset_id: number; deleted_items: number }> {
  return http.delete(`/eval/datasets/${datasetId}`) as Promise<{
    deleted_dataset_id: number
    deleted_items: number
  }>
}
