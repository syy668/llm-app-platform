<script setup lang="ts">
/**
 * 评测任务页：
 * - 顶部大盘四卡片（总数 / 成功率 / 平均忠实度 / 累计 Token）：管理层视角，数据走后端聚合接口
 * - 创建评测任务表单（管理员）：选择数据集 + RAG 实验参数（rag_config 随任务持久化；
 *   top_k / search_mode 均请求级生效，chunk_size 为实验记录展示）
 * - 任务列表：状态筛选、进度条（running 时轮询已完成样本数）、平均指标、
 *   停止（执行中/排队中 → 红色按钮 + 二次确认）/ 删除（已结束任务，管理员）
 * - 任务详情抽屉：指标雷达图（单任务 / A/B 对比）、stratum 分层指标聚合、
 *   得分分页表格（BadCase 模式走后端 /badcases 分页筛选）+ 行点击查看样本详情 + 导出 CSV（管理员）
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Aim, CircleCheck, Coin, Tickets } from '@element-plus/icons-vue'
import EmptyState from '@/components/EmptyState.vue'
import BadCaseFilter from '@/components/BadCaseFilter.vue'
import RagRadarChart from '@/components/RagRadarChart.vue'
import BadcaseDetailDrawer from '@/components/BadcaseDetailDrawer.vue'
import type { BadCaseThreshold } from '@/components/BadCaseFilter.vue'
import {
  cancelRun,
  createRun,
  deleteRun,
  exportRunCsv,
  getRunStats,
  getStratumMetrics,
  listBadcases,
  listRuns,
  listScores,
} from '@/api/eval'
import { listDatasets } from '@/api/dataset'
import { useAuthStore } from '@/store/auth'
import { downloadBlob } from '@/utils/download'
import { formatExpLabel, saveExpParams } from '@/utils/expParams'
import type {
  BadcaseMetric,
  BadcaseRow,
  Dataset,
  EvalRun,
  RunScore,
  RunStats,
  StratumMetricsResponse,
} from '@/api/types'

const { isAdmin } = useAuthStore()

const loading = ref(false)
const runs = ref<EvalRun[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const statusFilter = ref<string>('')

const datasets = ref<Dataset[]>([])
const datasetNameMap = computed(() => new Map(datasets.value.map((d) => [d.id, d.name])))

const STATUS_META: Record<string, { label: string; type: 'info' | 'warning' | 'success' | 'danger' }> = {
  pending: { label: '等待中', type: 'info' },
  running: { label: '执行中', type: 'warning' },
  done: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  cancelled: { label: '已取消', type: 'info' },
}

/** 只有这两种状态可以「停止」（后端对其它状态返回 409） */
function canCancel(run: EvalRun): boolean {
  return run.status === 'running' || run.status === 'pending'
}

// ---------- 顶部大盘：四项指标走后端聚合（列表是分页的，前端聚合必然算错）----------
const stats = ref<RunStats | null>(null)
const statsLoading = ref(false)

async function loadStats() {
  statsLoading.value = true
  try {
    stats.value = await getRunStats()
  } catch {
    // 大盘失败不影响列表：卡片保持上一份数据/占位符
  } finally {
    statsLoading.value = false
  }
}

const successRateText = computed(() => {
  const s = stats.value
  if (!s || s.total_runs === 0) return '—'
  return `${(s.success_rate * 100).toFixed(1)}%`
})

const faithfulnessText = computed(() => {
  const v = stats.value?.avg_faithfulness
  return v === null || v === undefined ? '—' : v.toFixed(4)
})

/** 1234567 → 123.5 万（管理层看的是量级，不是个位） */
function formatTokens(n: number): string {
  if (!n) return '0'
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)} 亿`
  if (n >= 1e4) return `${(n / 1e4).toFixed(1)} 万`
  return String(n)
}

async function loadRuns() {
  loading.value = true
  try {
    const res = await listRuns(page.value, pageSize, statusFilter.value || undefined)
    runs.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

// ---------- 进度轮询：running 任务每 5s 查询已完成样本数 ----------
let pollTimer: number | null = null
let pollTick = 0
const progressMap = reactive<Record<number, number>>({})

async function pollProgress() {
  const running = runs.value.filter((r) => r.status === 'running')
  for (const run of running) {
    try {
      const res = await listScores(run.id, 1, 1)
      const pct = run.total_items > 0 ? Math.min(100, Math.round((res.total / run.total_items) * 100)) : 0
      progressMap[run.id] = pct
    } catch {
      // 单次轮询失败忽略，下一轮继续
    }
  }
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(() => {
    if (runs.value.some((r) => r.status === 'running' || r.status === 'pending')) {
      void pollProgress()
      // 状态可能已变化，轻量刷新列表
      void loadRuns()
      // 大盘跟着动，但没必要 5s 一次（三次聚合查询），10s 足够
      pollTick += 1
      if (pollTick % 2 === 0) void loadStats()
    }
  }, 5000)
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// ---------- 创建任务 ----------
const createVisible = ref(false)
const createRef = ref()
const createForm = reactive({
  datasetId: undefined as number | undefined,
  chunkSize: 500,
  searchMode: 'vector_only' as 'vector_only' | 'bm25_vector_hybrid',
  topK: 5,
})
const createRules = {
  datasetId: [{ required: true, message: '请选择数据集', trigger: 'change' }],
}
const creating = ref(false)

async function openCreate() {
  if (datasets.value.length === 0) {
    const res = await listDatasets(1, 100)
    datasets.value = res.items
  }
  createVisible.value = true
}

async function submitCreate() {
  await createRef.value?.validate()
  creating.value = true
  try {
    // rag_config 随任务持久化到上游 eval_runs；top_k 请求级生效
    const run = await createRun({
      dataset_id: createForm.datasetId!,
      rag_config: {
        chunk_size: createForm.chunkSize,
        search_mode: createForm.searchMode,
        top_k: createForm.topK,
      },
    })
    // 本地兜底记录（历史任务展示用）
    saveExpParams(run.id, {
      chunk_size: createForm.chunkSize,
      search_mode: createForm.searchMode,
      top_k: createForm.topK,
    })
    ElMessage.success(`评测任务 #${run.id} 已创建，后台执行中`)
    createVisible.value = false
    await Promise.all([loadRuns(), loadStats()])
    startPolling()
  } finally {
    creating.value = false
  }
}

// ---------- 详情抽屉 ----------
const detailVisible = ref(false)
const detailRun = ref<EvalRun | null>(null)
const detailLoading = ref(false)
const detailScores = ref<RunScore[]>([])
const detailTotal = ref(0)
const detailPage = ref(1)
const detailPageSize = 10
const badcaseEnabled = ref(false)
const badcaseThreshold = reactive<BadCaseThreshold>({ faithfulness: 0.6, contextRecall: 0.6 })

// ---------- 【本轮新增】BadCase 后端分页筛选（/eval/runs/{id}/badcases） ----------
const badcaseMetric = ref<BadcaseMetric>('faithfulness')
const badcaseRows = ref<BadcaseRow[]>([])
const badcaseTotal = ref(0)
const badcasePage = ref(1)
const badcasePageSize = 10

/** 后端筛选的阈值来源：context_recall 取筛选器第二项，其余指标取 faithfulness 阈值项 */
const badcaseQueryThreshold = computed(() =>
  badcaseMetric.value === 'context_recall'
    ? badcaseThreshold.contextRecall
    : badcaseThreshold.faithfulness,
)

const displayScores = computed<RunScore[]>(() =>
  badcaseEnabled.value ? badcaseRows.value : detailScores.value,
)

const displayTotal = computed(() => (badcaseEnabled.value ? badcaseTotal.value : detailTotal.value))
const displayPage = computed(() => (badcaseEnabled.value ? badcasePage.value : detailPage.value))

function openDetail(run: EvalRun) {
  detailRun.value = run
  detailPage.value = 1
  badcaseEnabled.value = false
  badcasePage.value = 1
  badcaseMetric.value = 'faithfulness'
  badcaseRows.value = []
  badcaseTotal.value = 0
  stratumData.value = null
  detailVisible.value = true
  void loadDetailScores()
  if (run.status === 'done') void loadStratumMetrics(run.id)
}

async function loadDetailScores() {
  if (!detailRun.value) return
  detailLoading.value = true
  try {
    if (badcaseEnabled.value) {
      // BadCase 模式：后端筛选分页（metric < threshold），行内含 ground_truth
      const res = await listBadcases(detailRun.value.id, {
        metric: badcaseMetric.value,
        threshold: badcaseQueryThreshold.value,
        page: badcasePage.value,
        page_size: badcasePageSize,
      })
      badcaseRows.value = res.items
      badcaseTotal.value = res.total
    } else {
      const res = await listScores(detailRun.value.id, detailPage.value, detailPageSize)
      detailScores.value = res.items
      detailTotal.value = res.total
    }
  } finally {
    detailLoading.value = false
  }
}

function handleDisplayPageChange(p: number) {
  if (badcaseEnabled.value) {
    badcasePage.value = p
  } else {
    detailPage.value = p
  }
  void loadDetailScores()
}

// ---------- 【本轮新增】stratum 分层指标聚合 ----------
const stratumLoading = ref(false)
const stratumData = ref<StratumMetricsResponse | null>(null)

async function loadStratumMetrics(runId: number) {
  stratumLoading.value = true
  try {
    stratumData.value = await getStratumMetrics(runId)
  } finally {
    stratumLoading.value = false
  }
}

function stratumLabel(stratum: string | null): string {
  return stratum ?? '未分层'
}

// ---------- 【本轮新增】BadCase 样本详情抽屉 ----------
const badcaseDetailVisible = ref(false)
const badcaseDetailRow = ref<BadcaseRow | RunScore | null>(null)

function openBadcaseDetail(row: BadcaseRow | RunScore) {
  badcaseDetailRow.value = row
  badcaseDetailVisible.value = true
}

watch(badcaseEnabled, () => {
  badcasePage.value = 1
  void loadDetailScores()
})

// BadCase 模式下指标或阈值变化：重置到第一页并按后端筛选重新加载
watch([badcaseMetric, () => badcaseThreshold.faithfulness, () => badcaseThreshold.contextRecall], () => {
  if (!badcaseEnabled.value) return
  badcasePage.value = 1
  void loadDetailScores()
})

// ---------- 导出 CSV ----------
const exporting = ref(false)

async function handleExport(run: EvalRun) {
  exporting.value = true
  try {
    const blob = await exportRunCsv(run.id)
    downloadBlob(`eval_run_${run.id}.csv`, blob)
    ElMessage.success('CSV 已开始下载')
  } catch (err) {
    ElMessage.error(`导出失败：${err instanceof Error ? err.message : '未知错误'}`)
  } finally {
    exporting.value = false
  }
}

// ---------- 停止任务（止损，仅管理员；执行中/排队中才有这个按钮）----------
const cancellingId = ref<number | null>(null)

async function handleCancelRun(run: EvalRun) {
  const processed = run.processed_items ?? run.success_items
  try {
    await ElMessageBox.confirm(
      `确定停止评测任务 #${run.id}？\n\n已完成的 ${processed}/${run.total_items} 条得分会保留（不丢数据），剩余样本不再打分，本轮也不会产出指标结论。`,
      '停止任务',
      {
        confirmButtonText: '停止任务',
        cancelButtonText: '继续执行',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return // 用户点了「继续执行」
  }

  cancellingId.value = run.id
  try {
    const res = await cancelRun(run.id)
    ElMessage.success(`任务 #${res.id} 已停止`)
    await Promise.all([loadRuns(), loadStats()])
    void pollProgress()
  } finally {
    cancellingId.value = null
  }
}

// ---------- 删除任务（仅管理员；已结束的任务才有这个按钮）----------
async function handleDeleteRun(run: EvalRun) {
  try {
    await ElMessageBox.confirm(
      `确定删除评测任务 #${run.id}？将级联删除其全部得分明细，A/B 看板中将无法再选择该任务。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 取消删除：ElMessageBox 以 reject 结束，不接住会变成 Unhandled Rejection
  }
  const res = await deleteRun(run.id)
  ElMessage.success(`已删除任务 #${res.deleted_run_id} 及 ${res.deleted_scores} 条得分`)
  await Promise.all([loadRuns(), loadStats()])
}

function pct(run: EvalRun): number {
  if (run.status === 'done') return 100
  if (run.status === 'failed' || run.status === 'cancelled') return 0
  return progressMap[run.id] ?? 0
}

watch(statusFilter, () => {
  page.value = 1
  void loadRuns()
})

onMounted(async () => {
  await Promise.all([loadRuns(), loadStats()])
  await pollProgress()
  startPolling()
  try {
    const res = await listDatasets(1, 100)
    datasets.value = res.items
  } catch {
    // 数据集名称仅用于展示
  }
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="ea-content">
    <div class="ea-page-header" style="display: flex; align-items: flex-end; justify-content: space-between">
      <div>
        <div class="ea-page-title">评测任务</div>
        <div class="ea-page-sub">发起 RAGAS 异步评测，跟踪执行进度，筛选 BadCase 并导出 CSV</div>
      </div>
      <el-button v-if="isAdmin" type="primary" @click="openCreate">创建评测任务</el-button>
    </div>

    <!-- 顶部大盘：管理层视角（数据全部来自 GET /eval/runs/stats 后端聚合） -->
    <div v-loading="statsLoading" class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon stat-icon--blue">
          <el-icon><Tickets /></el-icon>
        </div>
        <div class="stat-body">
          <div class="stat-label">任务总数</div>
          <div class="stat-value">{{ stats?.total_runs ?? 0 }}</div>
          <div class="stat-hint">
            完成 {{ stats?.done_runs ?? 0 }} · 失败 {{ stats?.failed_runs ?? 0 }} · 取消 {{ stats?.cancelled_runs ?? 0 }}
          </div>
        </div>
      </div>

      <div class="stat-card">
        <div class="stat-icon stat-icon--green">
          <el-icon><CircleCheck /></el-icon>
        </div>
        <div class="stat-body">
          <div class="stat-label">任务成功率</div>
          <div class="stat-value">{{ successRateText }}</div>
          <div class="stat-hint">
            done / total = {{ stats?.done_runs ?? 0 }} / {{ stats?.total_runs ?? 0 }}
          </div>
        </div>
      </div>

      <div class="stat-card">
        <div class="stat-icon stat-icon--purple">
          <el-icon><Aim /></el-icon>
        </div>
        <div class="stat-body">
          <div class="stat-label">全局平均 Faithfulness</div>
          <div class="stat-value">{{ faithfulnessText }}</div>
          <div class="stat-hint">样本级均值 · {{ stats?.scored_samples ?? 0 }} 条已评分样本</div>
        </div>
      </div>

      <div class="stat-card">
        <div class="stat-icon stat-icon--orange">
          <el-icon><Coin /></el-icon>
        </div>
        <div class="stat-body">
          <div class="stat-label">累计 Token 消耗</div>
          <div class="stat-value">{{ formatTokens(stats?.total_tokens ?? 0) }}</div>
          <div class="stat-hint">含已取消任务已烧掉的部分</div>
        </div>
      </div>
    </div>

    <div class="ea-card">
      <div class="ea-card-header">
        <div class="ea-card-title">任务列表</div>
        <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 160px">
          <el-option label="等待中" value="pending" />
          <el-option label="执行中" value="running" />
          <el-option label="已完成" value="done" />
          <el-option label="失败" value="failed" />
          <el-option label="已取消" value="cancelled" />
        </el-select>
      </div>
      <div class="ea-card-body" style="padding-top: 8px">
        <el-table v-loading="loading" :data="runs" stripe>
          <el-table-column prop="id" label="任务 ID" width="82" />
          <el-table-column label="数据集" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">
              {{ datasetNameMap.get(row.dataset_id) ?? `dataset_id=${row.dataset_id}` }}
            </template>
          </el-table-column>
          <el-table-column label="RAG 实验配置" min-width="175" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="ea-mono">{{ formatExpLabel(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="92">
            <template #default="{ row }">
              <el-tag :type="STATUS_META[row.status]?.type ?? 'info'" size="small">
                {{ STATUS_META[row.status]?.label ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="进度" width="150">
            <template #default="{ row }">
              <el-progress
                v-if="row.status === 'running' || row.status === 'pending'"
                :percentage="pct(row)"
                :stroke-width="8"
              />
              <span v-else-if="row.status === 'done'" class="ea-mono">
                {{ row.success_items }}/{{ row.total_items }}
              </span>
              <el-tooltip v-else-if="row.status === 'cancelled'" :content="row.error_msg || '已手动停止'" placement="top">
                <span class="ea-mono" style="color: var(--ea-text-3)">已停止</span>
              </el-tooltip>
              <el-tooltip v-else :content="row.error_msg || '评测失败'" placement="top">
                <span class="ea-mono">失败</span>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="faithfulness" width="112">
            <template #default="{ row }">
              <span class="ea-mono">{{ row.metrics?.faithfulness?.toFixed(4) ?? '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="context_recall" width="124">
            <template #default="{ row }">
              <span class="ea-mono">{{ row.metrics?.context_recall?.toFixed(4) ?? '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" width="168">
            <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
          </el-table-column>
          <el-table-column label="操作" width="210" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" :disabled="row.status === 'pending'" @click="openDetail(row)">详情</el-button>
              <el-button
                v-if="isAdmin"
                link
                type="primary"
                :disabled="row.status !== 'done'"
                :loading="exporting"
                @click="handleExport(row)"
              >
                导出 CSV
              </el-button>
              <!-- 执行中/排队中：删除换成红色「停止」（防跑飞止损）；已结束才给删除 -->
              <el-button
                v-if="isAdmin && canCancel(row)"
                link
                type="danger"
                :loading="cancellingId === row.id"
                @click="handleCancelRun(row)"
              >
                停止
              </el-button>
              <el-button v-else-if="isAdmin" link type="danger" @click="handleDeleteRun(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState
              :description="isAdmin ? '暂无评测任务' : '暂无评测任务（创建任务需管理员权限）'"
              :action-text="isAdmin ? '创建评测任务' : undefined"
              @action="openCreate"
            />
          </template>
        </el-table>
        <div style="display: flex; justify-content: flex-end; margin-top: 12px">
          <el-pagination
            layout="total, prev, pager, next"
            :total="total"
            :page-size="pageSize"
            :current-page="page"
            @current-change="(p: number) => { page = p; void loadRuns() }"
          />
        </div>
      </div>
    </div>

    <!-- 创建任务 -->
    <el-dialog v-model="createVisible" title="创建评测任务" width="560px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
        title="实验参数说明"
        description="rag_config 将随任务持久化：top_k / search_mode 在问答检索时请求级生效（search_mode：vector_only 纯向量 / bm25_vector_hybrid 混合，可真正做检索模式 A/B 对比）；chunk_size 属于入库切片参数，仅作为实验记录展示。"
      />
      <el-form ref="createRef" :model="createForm" :rules="createRules" label-width="110px">
        <el-form-item label="评测数据集" prop="datasetId">
          <el-select v-model="createForm.datasetId" placeholder="选择数据集" style="width: 100%">
            <el-option
              v-for="ds in datasets"
              :key="ds.id"
              :label="`#${ds.id} ${ds.name}（${ds.item_count} 条）`"
              :value="ds.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="chunk_size">
          <el-input-number v-model="createForm.chunkSize" :min="100" :max="4000" :step="100" />
        </el-form-item>
        <el-form-item label="search_mode">
          <el-select v-model="createForm.searchMode" style="width: 100%">
            <el-option label="vector_only（纯向量检索）" value="vector_only" />
            <el-option label="bm25_vector_hybrid（BM25+向量混合）" value="bm25_vector_hybrid" />
          </el-select>
        </el-form-item>
        <el-form-item label="top_k">
          <el-input-number v-model="createForm.topK" :min="1" :max="50" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建任务</el-button>
      </template>
    </el-dialog>

    <!-- 任务详情抽屉 -->
    <el-drawer v-model="detailVisible" :title="`任务 #${detailRun?.id ?? ''} 评测明细`" size="72%">
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px">
          <span style="font-weight: 600">任务 #{{ detailRun?.id }} 评测明细</span>
          <el-tag v-if="detailRun" :type="STATUS_META[detailRun.status]?.type" size="small">
            {{ STATUS_META[detailRun.status]?.label }}
          </el-tag>
        </div>
      </template>

      <div v-if="detailRun" class="detail-meta ea-card">
        <div class="meta-grid">
          <div><span class="meta-label">评测模型：</span>{{ detailRun.model }}</div>
          <div>
            <span class="meta-label">向量模型：</span>
            {{ detailRun.judge_embedding_model || '—' }}
            <span style="font-size: 11px; color: var(--ea-text-3)">（answer_relevancy 用）</span>
          </div>
          <div><span class="meta-label">样本：</span>{{ detailRun.success_items }}/{{ detailRun.total_items }}</div>
          <div><span class="meta-label">faithfulness：</span>{{ detailRun.metrics?.faithfulness?.toFixed(4) ?? '—' }}</div>
          <div><span class="meta-label">answer_relevancy：</span>{{ detailRun.metrics?.answer_relevancy?.toFixed(4) ?? '—' }}</div>
          <div><span class="meta-label">context_precision：</span>{{ detailRun.metrics?.context_precision?.toFixed(4) ?? '—' }}</div>
          <div><span class="meta-label">context_recall：</span>{{ detailRun.metrics?.context_recall?.toFixed(4) ?? '—' }}</div>
          <div style="grid-column: 1 / -1"><span class="meta-label">RAG 实验配置：</span>{{ formatExpLabel(detailRun) }}</div>
        </div>
        <div style="margin-top: 10px">
          <el-button v-if="isAdmin" type="primary" size="small" :disabled="detailRun.status !== 'done'" :loading="exporting" @click="handleExport(detailRun)">
            导出 CSV
          </el-button>
        </div>
      </div>

      <!-- 【本轮新增】指标雷达图：单任务四指标画像（数据来自后端 GET /eval/runs/{id}；
           A/B 双雷达归 A/B 实验看板，避免同功能两处入口） -->
      <div v-if="detailRun && detailRun.status === 'done'" class="ea-card" style="margin-top: 12px">
        <div class="ea-card-header">
          <div class="ea-card-title">指标雷达图（单任务）</div>
          <span style="font-size: 12px; color: var(--ea-text-3)">四项 RAGAS 指标画像 · A/B 对比请前往 A/B 实验看板</span>
        </div>
        <div class="ea-card-body">
          <RagRadarChart :run-id-a="detailRun.id" />
        </div>
      </div>

      <!-- 【本轮新增】stratum 分层指标聚合（数据来自 GET /eval/runs/{id}/stratum_metrics） -->
      <div v-if="detailRun && detailRun.status === 'done'" class="ea-card" style="margin-top: 12px">
        <div class="ea-card-header">
          <div class="ea-card-title">分层指标（stratum）</div>
          <span style="font-size: 12px; color: var(--ea-text-3)">共 {{ stratumData?.total ?? 0 }} 层 · 后端聚合结果</span>
        </div>
        <div class="ea-card-body" style="padding-top: 8px">
          <el-table v-loading="stratumLoading" :data="stratumData?.items ?? []" stripe size="small">
            <el-table-column label="分层" width="140">
              <template #default="{ row }">
                <el-tag v-if="row.stratum" size="small">{{ stratumLabel(row.stratum) }}</el-tag>
                <el-tag v-else size="small" type="info">未分层</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="sample_count" label="样本数" width="90" />
            <el-table-column label="faithfulness" width="120">
              <template #default="{ row }"><span class="ea-mono">{{ row.metrics.faithfulness?.toFixed(4) ?? '—' }}</span></template>
            </el-table-column>
            <el-table-column label="answer_relevancy" width="150">
              <template #default="{ row }"><span class="ea-mono">{{ row.metrics.answer_relevancy?.toFixed(4) ?? '—' }}</span></template>
            </el-table-column>
            <el-table-column label="context_precision" width="160">
              <template #default="{ row }"><span class="ea-mono">{{ row.metrics.context_precision?.toFixed(4) ?? '—' }}</span></template>
            </el-table-column>
            <el-table-column label="context_recall" width="130">
              <template #default="{ row }"><span class="ea-mono">{{ row.metrics.context_recall?.toFixed(4) ?? '—' }}</span></template>
            </el-table-column>
            <template #empty>
              <EmptyState description="该任务暂无分层聚合数据" />
            </template>
          </el-table>
        </div>
      </div>

      <div style="display: flex; justify-content: space-between; align-items: center; margin: 12px 0; flex-wrap: wrap; gap: 8px">
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
          <BadCaseFilter v-model="badcaseThreshold" :enabled="badcaseEnabled" @update:enabled="badcaseEnabled = $event" />
          <!-- 【本轮新增】后端筛选指标选择：BadCase 模式改走 /badcases 后端分页筛选 -->
          <template v-if="badcaseEnabled">
            <div style="display: flex; align-items: center; gap: 6px">
              <span style="font-size: 12px; color: var(--ea-text-3)">筛选指标</span>
              <el-select v-model="badcaseMetric" size="small" style="width: 180px">
                <el-option label="faithfulness（忠实度）" value="faithfulness" />
                <el-option label="answer_relevancy（答案相关性）" value="answer_relevancy" />
                <el-option label="context_precision（上下文精确率）" value="context_precision" />
                <el-option label="context_recall（上下文召回率）" value="context_recall" />
              </el-select>
            </div>
            <span style="font-size: 12px; color: var(--ea-text-3)">
              阈值取自上方对应阈值项（context_recall 取第二项，其余取 faithfulness 项）
            </span>
          </template>
        </div>
        <span class="ea-mono" style="color: var(--ea-text-3)">
          {{ badcaseEnabled ? `后端筛得 ${displayTotal} 条 BadCase` : `共 ${detailTotal} 条` }}
        </span>
      </div>

      <!-- 行点击打开样本详情抽屉（本轮新增） -->
      <el-table
        v-loading="detailLoading"
        :data="displayScores"
        stripe
        max-height="520"
        @row-click="openBadcaseDetail"
      >
        <el-table-column prop="question" label="question" min-width="200" show-overflow-tooltip />
        <el-table-column prop="answer" label="answer（模型生成）" min-width="220" show-overflow-tooltip />
        <el-table-column label="contexts（召回）" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            {{ (row.retrieved_contexts ?? []).length ? `${row.retrieved_contexts.length} 段` : '—' }}
          </template>
        </el-table-column>
        <!-- ground_truth：/badcases 与 /scores 均返回（后端为同一列的别名），
             回落 reference_answer 兼容早于 v1.24 的历史快照 -->
        <el-table-column label="ground_truth" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.ground_truth ?? row.reference_answer ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="faithfulness" width="110">
          <template #default="{ row }">
            <span class="ea-mono">{{ row.faithfulness?.toFixed(4) ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="context_recall" width="140">
          <template #default="{ row }">
            <span class="ea-mono">{{ row.context_recall?.toFixed(4) ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="answer_relevancy" width="150">
          <template #default="{ row }">
            <span class="ea-mono">{{ row.answer_relevancy?.toFixed(4) ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="latency(ms)" width="110">
          <template #default="{ row }">
            <span class="ea-mono">{{ row.latency_ms }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState :description="badcaseEnabled ? '无低于阈值的 BadCase' : '暂无评测明细（点击行可查看样本详情）'" />
        </template>
      </el-table>

      <div style="display: flex; justify-content: flex-end; margin-top: 12px">
        <el-pagination
          layout="total, prev, pager, next"
          :total="displayTotal"
          :page-size="badcaseEnabled ? badcasePageSize : detailPageSize"
          :current-page="displayPage"
          @current-change="handleDisplayPageChange"
        />
      </div>

      <!-- 【本轮新增】样本详情抽屉：完整展示 question / ground_truth / answer / contexts / 四项指标 / Judge 理由 -->
      <BadcaseDetailDrawer v-model="badcaseDetailVisible" :row="badcaseDetailRow" />
    </el-drawer>
  </div>
</template>

<style scoped>
/* 顶部大盘四卡片：干净白底 + 彩色图标底 + 加粗放大数字（管理层视角，一眼看结论） */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
  position: relative;
}

.stat-card {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
  padding: 16px 18px;
  background: var(--ea-card);
  border: 1px solid var(--ea-border);
  border-radius: 8px;
  box-shadow: 0 1px 2px #1d21290a;
}

.stat-icon {
  flex: none;
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  font-size: 22px;
}

.stat-icon--blue {
  background: #e8f3ff;
  color: #165dff;
}

.stat-icon--green {
  background: #e8ffea;
  color: #00b42a;
}

.stat-icon--purple {
  background: #f5e8ff;
  color: #722ed1;
}

.stat-icon--orange {
  background: #fff3e8;
  color: #ff7d00;
}

.stat-body {
  min-width: 0;
}

.stat-label {
  font-size: 12px;
  color: var(--ea-text-3);
  white-space: nowrap;
}

.stat-value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.25;
  color: var(--ea-text-1);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.stat-hint {
  font-size: 11px;
  color: var(--ea-text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

@media (max-width: 1280px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .stat-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.detail-meta {
  padding: 14px 18px;
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px 18px;
  font-size: 13px;
  color: var(--ea-text-2);
}

.meta-label {
  color: var(--ea-text-3);
}
</style>
