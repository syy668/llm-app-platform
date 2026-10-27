/**
 * 知识库接口（创建数据集时需选择 kb_id，复用项目一 /kbs 已有接口）。
 * 【第四步】知识库目录属于上游域 → 走 upstreamHttp（8010）。
 */
import { upstreamHttp as http } from '@/api/http'
import type { KB, Page } from '@/api/types'

export function listKbs(page = 1, size = 100): Promise<Page<KB>> {
  return http.get('/kbs', { params: { page, size } }) as Promise<Page<KB>>
}
