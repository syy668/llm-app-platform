/**
 * 审批待办计数（侧边栏徽标）。
 *
 * 数据源只有 `GET /approvals`（`data.total`）—— 后端没有独立的待办计数端点，
 * 也不存在"大盘聚合接口"，因此这里以 total 作为唯一真实来源。
 * 页面通过 `refresh()` 在进入收件箱、审批完成后主动刷新。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { listApprovals } from '@/api/approvals'

export const useApprovalStore = defineStore('approval', () => {
  const total = ref(0)
  const loading = ref(false)
  const lastLoadedAt = ref<number | null>(null)

  async function refresh(): Promise<number> {
    loading.value = true
    try {
      const data = await listApprovals({ page: 1, size: 1 })
      total.value = data.total
      lastLoadedAt.value = Date.now()
      return data.total
    } catch {
      // 无权限（viewer）或网络异常时不改徽标，避免误导
      return total.value
    } finally {
      loading.value = false
    }
  }

  function clear(): void {
    total.value = 0
    lastLoadedAt.value = null
  }

  return { total, loading, lastLoadedAt, refresh, clear }
})
