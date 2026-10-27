/**
 * 健康探针接口 —— 对应后端 `app/routers/health.py`。
 *
 * 三个探针路径不统一（注意前缀）：
 *  - `/healthz`         不依赖外部组件
 *  - `/readyz`          MySQL + Redis
 *  - `/api/v1/health`   运行态摘要（Mock 开关 / 令牌有效期 / checkpointer）
 */
import axios from 'axios'
import { API_BASE_URL, API_ROOT } from './http'
import type { HealthSummaryData, HealthzData, ReadyzData } from '@/types/health'

/** GET /healthz */
export async function fetchHealthz(): Promise<HealthzData> {
  const { data } = await axios.get<HealthzData>(`${API_BASE_URL}/healthz`, { timeout: 5000 })
  return data
}

/** GET /readyz —— 未就绪时后端返回 503，这里把 503 也当作有效响应读出内容 */
export async function fetchReadyz(): Promise<{ ok: boolean; data: ReadyzData }> {
  try {
    const { data } = await axios.get<ReadyzData>(`${API_BASE_URL}/readyz`, { timeout: 5000 })
    return { ok: true, data }
  } catch (error) {
    if (axios.isAxiosError(error) && error.response) {
      return { ok: false, data: error.response.data as ReadyzData }
    }
    throw error
  }
}

/** GET /api/v1/health */
export async function fetchHealthSummary(): Promise<HealthSummaryData> {
  const { data } = await axios.get<HealthSummaryData>(`${API_ROOT}/health`, { timeout: 5000 })
  return data
}
