<script setup lang="ts">
/**
 * P05 审批收件箱（Tab1 待审批 / Tab2 已过期 / Tab3 已处理）。
 *
 * 数据源（三个互补口径，别混用）：
 *  - 待审批：`GET /approvals` → **未过期**的 AWAITING_APPROVAL 任务 + 最新 PENDING 审批单；
 *  - 已过期：`GET /approvals?status=EXPIRED` → 超时/被定时器收口的单（只读，v1.3）；
 *  - 已处理：`GET /approvals?status=APPROVED|REJECTED|EXPIRED` → `approvals` 表终态记录
 *    （缺口 G4，含决策人 / 意见 / 决策时间）。
 *
 * v1.3 口径（PRD L245「审批挂起超 24h 自动失效」）：
 *  - 待审批列表由后端过滤掉过期单，所以侧边栏红点（用 `data.total`）能真正归零；
 *  - 每条带 `resumable`：`false` 表示 checkpoint 已丢、批了必 409，行内按钮置灰并提示。
 *
 * 审批动作：
 *  - 单条：`POST /tasks/{id}/resume`（抽屉）；批量：`POST /approvals/batch`（缺口 G9，
 *    后端逐条复用 resume、单条失败隔离，返回 succeeded / failed / results[]，前端按此分组提示）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Message, Modal } from '@arco-design/web-vue'
import { batchApprove, listApprovals, listProcessedApprovals } from '@/api/approvals'
import { getInterrupt, resumeTask } from '@/api/tasks'
import { isApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { useApprovalStore } from '@/stores/approval'
import { APPROVAL_STATUS_COLOR, APPROVAL_STATUS_TEXT, APPROVAL_TYPE_TEXT } from '@/utils/status'
import { formatCountdown, formatShortTime } from '@/utils/format'
import ApprovalDrawer from '@/components/ApprovalDrawer.vue'
import StateBlock from '@/components/StateBlock.vue'
import type { ApprovalInboxItem, InterruptContext, ProcessedApprovalItem } from '@/types/approval'

const router = useRouter()
const auth = useAuthStore()
const approvalStore = useApprovalStore()

const activeTab = ref('pending')

// ---------------------------------------------------------------------------
// 待审批
// ---------------------------------------------------------------------------
const loading = ref(false)
const items = ref<ApprovalInboxItem[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const errorState = ref<{ code: string; message: string } | null>(null)

const selectedKeys = ref<number[]>([])
const batchLoading = ref(false)
const now = ref(Date.now())
let ticker: number | null = null

// ---------------------------------------------------------------------------
// 已处理（G4）
// ---------------------------------------------------------------------------
/** 终态多选；后端 status 接受逗号分隔，不传会回退待处理收件箱，所以必须至少带一个 */
const PROCESSED_STATUS_OPTIONS = ['APPROVED', 'REJECTED', 'EXPIRED'] as const
const processedStatuses = ref<string[]>([...PROCESSED_STATUS_OPTIONS])
const processedLoading = ref(false)
const processedItems = ref<ProcessedApprovalItem[]>([])
const processedTotal = ref(0)
const processedPage = ref(1)
const processedSize = ref(20)
const processedError = ref<{ code: string; message: string } | null>(null)
/** 首次切到该 Tab 才请求，避免进页面就多打一次接口 */
const processedLoaded = ref(false)

// ---------------------------------------------------------------------------
// 已过期（v1.3：超 24h 被定时器收口的单，只读）
// ---------------------------------------------------------------------------
const expiredLoading = ref(false)
const expiredItems = ref<ProcessedApprovalItem[]>([])
const expiredTotal = ref(0)
const expiredPage = ref(1)
const expiredSize = ref(20)
const expiredError = ref<{ code: string; message: string } | null>(null)
const expiredLoaded = ref(false)

async function loadExpired(): Promise<void> {
  expiredLoading.value = true
  expiredError.value = null
  try {
    const data = await listProcessedApprovals({
      status: 'EXPIRED',
      page: expiredPage.value,
      size: expiredSize.value,
    })
    expiredItems.value = data.items
    expiredTotal.value = data.total
    expiredLoaded.value = true
  } catch (error) {
    expiredItems.value = []
    expiredTotal.value = 0
    expiredError.value = toErrorState(error)
  } finally {
    expiredLoading.value = false
  }
}

function onExpiredPageChange(next: number): void {
  expiredPage.value = next
  void loadExpired()
}

function onExpiredPageSizeChange(next: number): void {
  expiredSize.value = next
  expiredPage.value = 1
  void loadExpired()
}

const drawerVisible = ref(false)
const drawerLoading = ref(false)
const submitting = ref(false)
const currentTaskId = ref<number | null>(null)
const currentContext = ref<InterruptContext | null>(null)
const currentItem = ref<ApprovalInboxItem | null>(null)

const isCreator = computed(() => currentItem.value?.creator_id === auth.user?.id)
const pendingTabTitle = computed(() => `待审批（${total.value}）`)
const expiredTabTitle = computed(() =>
  expiredLoaded.value ? `已过期（${expiredTotal.value}）` : '已过期',
)
const processedTabTitle = computed(() =>
  processedLoaded.value ? `已处理（${processedTotal.value}）` : '已处理',
)

function toErrorState(error: unknown): { code: string; message: string } {
  if (isApiError(error)) return { code: error.code, message: error.message }
  return { code: 'NETWORK_ERROR', message: '无法连接后端服务' }
}

async function loadPending(): Promise<void> {
  loading.value = true
  errorState.value = null
  try {
    const data = await listApprovals({ page: page.value, size: size.value })
    // 批处理 / 他端审批后当前页可能已空：回到最后一页，避免用户看到"空列表 + 有总数"的错觉
    if (data.items.length === 0 && data.total > 0 && page.value > 1) {
      page.value = Math.max(1, Math.ceil(data.total / size.value))
      const retry = await listApprovals({ page: page.value, size: size.value })
      items.value = retry.items
      total.value = retry.total
    } else {
      items.value = data.items
      total.value = data.total
    }
    selectedKeys.value = []
    await approvalStore.refresh()
  } catch (error) {
    errorState.value = toErrorState(error)
  } finally {
    loading.value = false
  }
}

/** 待审批列表分页：服务端分页（total 由 GET /approvals 返回） */
function onPendingPageChange(next: number): void {
  page.value = next
  void loadPending()
}

function onPendingPageSizeChange(next: number): void {
  size.value = next
  page.value = 1
  void loadPending()
}

/** 已处理列表：status 为空数组时不做空请求（后端会回退成待处理收件箱，语义就错了） */
async function loadProcessed(): Promise<void> {
  if (processedStatuses.value.length === 0) {
    processedItems.value = []
    processedTotal.value = 0
    return
  }
  processedLoading.value = true
  processedError.value = null
  try {
    const data = await listProcessedApprovals({
      status: processedStatuses.value.join(','),
      page: processedPage.value,
      size: processedSize.value,
    })
    processedItems.value = data.items
    processedTotal.value = data.total
    processedLoaded.value = true
  } catch (error) {
    processedItems.value = []
    processedTotal.value = 0
    processedError.value = toErrorState(error)
  } finally {
    processedLoading.value = false
  }
}

function onTabChange(key: string | number): void {
  activeTab.value = String(key)
  if (activeTab.value === 'processed' && !processedLoaded.value) void loadProcessed()
  if (activeTab.value === 'expired' && !expiredLoaded.value) void loadExpired()
}

function onProcessedStatusChange(value: unknown): void {
  processedStatuses.value = Array.isArray(value) ? value.map((v) => String(v)) : []
  processedPage.value = 1
  void loadProcessed()
}

function onProcessedPageChange(next: number): void {
  processedPage.value = next
  void loadProcessed()
}

function onProcessedPageSizeChange(next: number): void {
  processedSize.value = next
  processedPage.value = 1
  void loadProcessed()
}

watch(activeTab, (key) => {
  if (key === 'processed' && !processedLoaded.value) void loadProcessed()
  if (key === 'expired' && !expiredLoaded.value) void loadExpired()
})

function countdownOf(item: ApprovalInboxItem): { text: string; level: string } {
  // 依赖 now 触发重算（每 30s 刷新一次显示）
  void now.value
  return formatCountdown(item.expires_at)
}

function openTask(taskId: number): void {
  router.push(`/tasks/${taskId}`)
}

async function openDrawer(item: ApprovalInboxItem): Promise<void> {
  currentTaskId.value = item.task_id
  currentItem.value = item
  drawerVisible.value = true
  drawerLoading.value = true
  currentContext.value = null
  try {
    currentContext.value = await getInterrupt(item.task_id)
  } catch (error) {
    Message.error(isApiError(error) ? error.message : '挂起上下文加载失败')
  } finally {
    drawerLoading.value = false
  }
}

async function handleDrawerSubmit(payload: { decision: 'APPROVED' | 'REJECTED'; comment: string }): Promise<void> {
  if (!currentTaskId.value) return
  submitting.value = true
  try {
    const result = await resumeTask(currentTaskId.value, payload)
    Message.success(result.resumed ? '决策已提交，任务继续执行' : '决策已提交')
    drawerVisible.value = false
    await refreshAll()
  } catch (error) {
    Message.error(isApiError(error) ? error.message : '提交失败')
  } finally {
    submitting.value = false
  }
}

/**
 * 批量通过（G9）：一次 `POST /approvals/batch` 提交，后端逐条复用 resume 且失败隔离。
 * 结果按 `results[].ok` 分组：全成功只提示条数；有失败则弹窗列出每条原因。
 */
function handleBatchApprove(): void {
  // 死锁行不参与批量：批了也是 409，把它塞进批量只会制造"成功 3 失败 2"的噪音
  const targets = items.value.filter((item) => selectedKeys.value.includes(item.task_id) && item.resumable)
  if (targets.length === 0) return
  Modal.confirm({
    title: `批量通过 ${targets.length} 条审批`,
    content: '调用 POST /approvals/batch 一次性提交；单条失败不影响其余，结束后按成功/失败汇总。',
    okText: '开始',
    cancelText: '取消',
    onOk: async () => {
      batchLoading.value = true
      try {
        const result = await batchApprove({
          decisions: targets.map((item) => ({
            task_id: item.task_id,
            decision: 'APPROVED' as const,
            comment: '',
          })),
        })
        if (result.failed === 0) {
          Message.success(`批量通过成功 ${result.succeeded} 条`)
        } else {
          const failures = result.results.filter((item) => !item.ok)
          Modal.warning({
            title: `成功 ${result.succeeded} 条，失败 ${result.failed} 条`,
            content: failures
              .map((item) => `任务 ${item.task_id}：${item.code ?? 'ERROR'} ${item.message ?? ''}`.trim())
              .join('\n'),
          })
        }
      } catch (error) {
        Message.error(isApiError(error) ? error.message : '批量审批失败')
      } finally {
        batchLoading.value = false
        await refreshAll()
      }
    },
  })
}

function selectAll(checked: boolean): void {
  selectedKeys.value = checked ? items.value.filter((item) => item.resumable).map((item) => item.task_id) : []
}

async function refreshAll(): Promise<void> {
  await loadPending()
  if (processedLoaded.value) await loadProcessed()
  if (expiredLoaded.value) await loadExpired()
}

onMounted(() => {
  void loadPending()
  ticker = window.setInterval(() => {
    now.value = Date.now()
  }, 30000)
})

onBeforeUnmount(() => {
  if (ticker !== null) window.clearInterval(ticker)
})
</script>

<template>
  <div class="af-page">
    <div class="af-page-head">
      <div>
        <h1>审批收件箱</h1>
        <div class="af-sub">
          待处理 {{ total }} 条（仅统计未过期）· 数据源 <span class="af-mono">GET /approvals</span>
          <span v-if="auth.isAdmin">（管理员仅可见本人创建的任务）</span>
        </div>
      </div>
      <a-space>
        <a-button :loading="loading || processedLoading" @click="refreshAll">刷新</a-button>
      </a-space>
    </div>

    <a-card :bordered="true">
      <a-tabs :active-key="activeTab" size="medium" @change="onTabChange">
        <a-tab-pane key="pending" :title="pendingTabTitle">
          <StateBlock v-if="errorState" state="error" :description="errorState.message" :code="errorState.code" @retry="loadPending" />

          <StateBlock
            v-else-if="!loading && items.length === 0"
            state="empty"
            title="没有待审批任务"
            description="当执行命中高危工具（如 notify）时，任务会停在 human_gate 并出现在这里"
            :min-height="200"
          />

          <template v-else>
            <a-table
              :data="items"
              :loading="loading"
              row-key="task_id"
              size="small"
              :pagination="{
                pageSize: size,
                current: page,
                total,
                showTotal: true,
                showPageSize: true,
              }"
              :bordered="false"
              :row-selection="{ type: 'checkbox', showCheckedAll: true }"
              v-model:selectedKeys="selectedKeys"
              @page-change="onPendingPageChange"
              @page-size-change="onPendingPageSizeChange"
            >
              <template #columns>
                <a-table-column title="审批单" :width="86">
                  <template #cell="{ record }">
                    <span class="af-mono">#{{ record.approval_id ?? '—' }}</span>
                  </template>
                </a-table-column>
                <a-table-column title="任务编号" :width="164">
                  <template #cell="{ record }">
                    <a-link class="af-mono" @click="openTask(record.task_id)">{{ record.task_no }}</a-link>
                  </template>
                </a-table-column>
                <a-table-column title="目标" data-index="goal" :ellipsis="true" :tooltip="true" />
                <a-table-column title="类型" :width="120">
                  <template #cell="{ record }">
                    <a-tag size="small" color="red">
                      {{ APPROVAL_TYPE_TEXT[record.approval_type ?? ''] ?? record.approval_type ?? '—' }}
                    </a-tag>
                  </template>
                </a-table-column>
                <a-table-column title="阻断原因" :width="180" :ellipsis="true" :tooltip="true">
                  <template #cell="{ record }">
                    <span class="af-muted">{{ record.reason || '—' }}</span>
                  </template>
                </a-table-column>
                <a-table-column title="倒计时" :width="104">
                  <template #cell="{ record }">
                    <span class="countdown" :class="`is-${countdownOf(record).level}`">
                      {{ countdownOf(record).text }}
                    </span>
                  </template>
                </a-table-column>
                <a-table-column title="挂起时间" :width="120">
                  <template #cell="{ record }">
                    <span class="af-muted af-num">{{ formatShortTime(record.created_at) }}</span>
                  </template>
                </a-table-column>
                <a-table-column title="操作" :width="120">
                  <template #cell="{ record }">
                    <a-space :size="4">
                      <!-- 死锁（checkpoint 已丢）时置灰：点了必然 409，不如把原因直接说清楚 -->
                      <a-tooltip v-if="!record.resumable" content="任务已死锁，请走一键重发或归档" position="top">
                        <span class="link-disabled">审批</span>
                      </a-tooltip>
                      <a-link v-else @click="openDrawer(record)">审批</a-link>
                      <a-link @click="openTask(record.task_id)">详情</a-link>
                    </a-space>
                  </template>
                </a-table-column>
              </template>
            </a-table>

            <!-- 吸底批量条 -->
            <div v-if="selectedKeys.length" class="batch-bar">
              <span>已选 {{ selectedKeys.length }} 条</span>
              <a-space>
                <a-button size="small" :disabled="batchLoading" @click="selectAll(false)">取消选择</a-button>
                <a-button size="small" type="primary" :loading="batchLoading" @click="handleBatchApprove">
                  批量通过
                </a-button>
              </a-space>
            </div>
          </template>
        </a-tab-pane>

        <a-tab-pane key="expired" :title="expiredTabTitle">
          <a-alert type="warning" class="expired-tip">
            审批挂起超过 24h 即自动失效（PRD L245），任务由定时收口置为
            <span class="af-mono">FAILED / APPROVAL_EXPIRED</span>。此页签只读——请让创建人重新发起任务。
          </a-alert>

          <StateBlock
            v-if="expiredError"
            state="error"
            :description="expiredError.message"
            :code="expiredError.code"
            @retry="loadExpired"
          />

          <StateBlock
            v-else-if="!expiredLoading && expiredItems.length === 0"
            state="empty"
            title="没有已过期的审批"
            description="超时未处理的审批单会自动落到这里（只读留痕），同时对应任务被置为 FAILED"
            :min-height="200"
          />

          <a-table
            v-else
            :data="expiredItems"
            :loading="expiredLoading"
            row-key="approval_id"
            size="small"
            :bordered="false"
            :pagination="{
              pageSize: expiredSize,
              current: expiredPage,
              total: expiredTotal,
              showTotal: true,
              showPageSize: true,
            }"
            @page-change="onExpiredPageChange"
            @page-size-change="onExpiredPageSizeChange"
          >
            <template #columns>
              <a-table-column title="审批单" :width="86">
                <template #cell="{ record }">
                  <span class="af-mono">#{{ record.approval_id }}</span>
                </template>
              </a-table-column>
              <a-table-column title="任务编号" :width="164">
                <template #cell="{ record }">
                  <a-link v-if="record.task_no" class="af-mono" @click="openTask(record.task_id)">
                    {{ record.task_no }}
                  </a-link>
                  <span v-else class="af-mono af-muted">#{{ record.task_id }}</span>
                </template>
              </a-table-column>
              <a-table-column title="目标" :ellipsis="true" :tooltip="true">
                <template #cell="{ record }">
                  <span>{{ record.goal || '—' }}</span>
                </template>
              </a-table-column>
              <a-table-column title="类型" :width="118">
                <template #cell="{ record }">
                  <a-tag size="small">{{ APPROVAL_TYPE_TEXT[record.type ?? ''] ?? record.type ?? '—' }}</a-tag>
                </template>
              </a-table-column>
              <a-table-column title="阻断原因" :width="180" :ellipsis="true" :tooltip="true">
                <template #cell="{ record }">
                  <span class="af-muted">{{ record.reason || '—' }}</span>
                </template>
              </a-table-column>
              <a-table-column title="过期时间" :width="120">
                <template #cell="{ record }">
                  <span class="countdown is-danger">{{ formatShortTime(record.expires_at) }}</span>
                </template>
              </a-table-column>
              <a-table-column title="失效时间" :width="120">
                <template #cell="{ record }">
                  <span class="af-num af-muted">{{ formatShortTime(record.decided_at) }}</span>
                </template>
              </a-table-column>
            </template>
          </a-table>
        </a-tab-pane>

        <a-tab-pane key="processed" :title="processedTabTitle">
          <div class="processed-filter">
            <span class="af-muted">决策结果</span>
            <a-select
              :model-value="processedStatuses"
              multiple
              size="small"
              placeholder="至少选择一种结果"
              @change="onProcessedStatusChange"
            >
              <a-option v-for="item in PROCESSED_STATUS_OPTIONS" :key="item" :value="item">
                {{ APPROVAL_STATUS_TEXT[item] }}
              </a-option>
            </a-select>
            <span class="af-muted">
              数据源 <span class="af-mono">GET /approvals?status=</span> · 与待处理收件箱口径互补
            </span>
          </div>

          <StateBlock
            v-if="processedError"
            state="error"
            :description="processedError.message"
            :code="processedError.code"
            @retry="loadProcessed"
          />

          <StateBlock
            v-else-if="!processedLoading && processedItems.length === 0"
            state="empty"
            title="还没有已处理的审批"
            description="审批通过或驳回后，记录会出现在这里（含决策人、意见与决策时间）"
            :min-height="200"
          />

          <a-table
            v-else
            :data="processedItems"
            :loading="processedLoading"
            row-key="approval_id"
            size="small"
            :bordered="false"
            :pagination="{
              pageSize: processedSize,
              current: processedPage,
              total: processedTotal,
              showTotal: true,
              showPageSize: true,
            }"
            @page-change="onProcessedPageChange"
            @page-size-change="onProcessedPageSizeChange"
          >
            <template #columns>
              <a-table-column title="审批单" :width="86">
                <template #cell="{ record }">
                  <span class="af-mono">#{{ record.approval_id }}</span>
                </template>
              </a-table-column>
              <a-table-column title="任务编号" :width="164">
                <template #cell="{ record }">
                  <a-link v-if="record.task_no" class="af-mono" @click="openTask(record.task_id)">
                    {{ record.task_no }}
                  </a-link>
                  <span v-else class="af-mono af-muted">#{{ record.task_id }}</span>
                </template>
              </a-table-column>
              <a-table-column title="目标" :ellipsis="true" :tooltip="true">
                <template #cell="{ record }">
                  <span>{{ record.goal || '—' }}</span>
                </template>
              </a-table-column>
              <a-table-column title="类型" :width="118">
                <template #cell="{ record }">
                  <a-tag size="small">{{ APPROVAL_TYPE_TEXT[record.type ?? ''] ?? record.type ?? '—' }}</a-tag>
                </template>
              </a-table-column>
              <a-table-column title="决策结果" :width="96">
                <template #cell="{ record }">
                  <a-tag size="small" :color="APPROVAL_STATUS_COLOR[record.status ?? ''] ?? 'gray'">
                    {{ APPROVAL_STATUS_TEXT[record.status ?? ''] ?? record.status }}
                  </a-tag>
                </template>
              </a-table-column>
              <a-table-column title="决策人" :width="90">
                <template #cell="{ record }">
                  <span class="af-mono af-muted">
                    {{ record.decided_by === null ? '系统超时' : `#${record.decided_by}` }}
                  </span>
                </template>
              </a-table-column>
              <a-table-column title="审批意见" :width="200" :ellipsis="true" :tooltip="true">
                <template #cell="{ record }">
                  <span class="af-muted">{{ record.decide_comment || '—' }}</span>
                </template>
              </a-table-column>
              <a-table-column title="决策时间" :width="120">
                <template #cell="{ record }">
                  <span class="af-num af-muted">{{ formatShortTime(record.decided_at) }}</span>
                </template>
              </a-table-column>
            </template>
          </a-table>
        </a-tab-pane>
      </a-tabs>
    </a-card>

    <ApprovalDrawer
      v-model:visible="drawerVisible"
      :task-id="currentTaskId"
      :context="currentContext"
      :loading="drawerLoading"
      :submitting="submitting"
      :is-creator="isCreator"
      @submit="handleDrawerSubmit"
    />
  </div>
</template>

<style scoped>
.countdown {
  font-variant-numeric: tabular-nums;
  font-size: 12px;
}

.countdown.is-normal {
  color: var(--color-text-2);
}

.countdown.is-warning {
  color: rgb(var(--warning-6));
}

.countdown.is-danger {
  color: rgb(var(--danger-6));
  font-weight: 500;
}

.batch-bar {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
  padding: 8px 12px;
  background: rgb(var(--primary-1));
  border: 1px solid rgb(var(--primary-2));
  border-radius: 2px;
  font-size: 12.5px;
}

.processed-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  font-size: 12.5px;
}

.processed-filter :deep(.arco-select) {
  width: 240px;
}

/* 死锁行：审批入口置灰但保留占位，避免"链接凭空消失"让人以为权限被收回 */
.link-disabled {
  display: inline-block;
  color: var(--color-text-4);
  cursor: not-allowed;
  font-size: 12.5px;
}

.expired-tip {
  margin-bottom: 10px;
  font-size: 12.5px;
}
</style>
