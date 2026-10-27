<script setup lang="ts">
// RAGAS 评测管理页（仅管理员）：统计卡片 / 评测任务分页表格 / 行展开得分明细 / CSV 下载。
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Download, Files, Plus, Refresh, TrendCharts, Upload, VideoPlay } from '@element-plus/icons-vue'
import {
  addItems,
  cancelRun,
  cleanupStaleRuns,
  compareRuns,
  createDataset,
  createRun,
  deleteDataset,
  deleteItem,
  deleteRun,
  exportRunScoresCsv,
  getRunStats,
  listDatasets,
  listItems,
  listRuns,
  listScores,
  updateItem,
} from '@/api/eval'
import type { RagConfigPayload } from '@/api/eval'
import { listKbs } from '@/api/kb'
import { describeOutcome, MAX_IMPORT_ITEMS, parseEvalImport } from '@/utils/evalImport'
import type {
  EvalCompareResult,
  EvalDataset,
  EvalItem,
  EvalRun,
  EvalRunStats,
  EvalScore,
  KnowledgeBase,
} from '@/types'
import EmptyState from '@/components/EmptyState.vue'
import PagePagination from '@/components/PagePagination.vue'

// 大盘统计（v1.13.2）：直接读后端聚合接口，前端不再拉列表自己算。
// 原实现是 listRuns({page:1,size:100}) 拿第一页 reduce，任务超过 100 个之后数字
// 会静默失真（不报错，只是变小）；而且不筛 status，failed/cancelled 的 run 也
// 混进均值。口径统一收敛到后端 run_stats()，前端只负责显示。
const runStats = ref<EvalRunStats | null>(null)

// 任务列表分页
const runs = ref<EvalRun[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(8)
const loading = ref(false)

const datasets = ref<EvalDataset[]>([])

// 新建评测任务
const newRunDialogVisible = ref(false)
const newRunDatasetId = ref<number | null>(null)
const creating = ref(false)
// 【P2】A/B 实验参数：请求级覆盖知识库默认检索，随任务持久化到 eval_runs.rag_config
const runTopK = ref(5)
const runSearchMode = ref<'vector_only' | 'bm25_vector_hybrid'>('bm25_vector_hybrid')

// 行展开得分
const scoresMap = reactive<Record<number, EvalScore[]>>({})
const scoresLoading = reactive<Record<number, boolean>>({})

// 【v1.13.2】运行中 = 排队 + 执行，数据来自后端聚合，不依赖列表页的取样范围
const runningCount = computed(
  () => (runStats.value?.running ?? 0) + (runStats.value?.pending ?? 0),
)

/**
 * 取大盘指标值。
 * 后端在某指标完全没有可用样本时返回 null（不是 0），这里必须原样透出 null，
 * 由模板渲染成灰色的「—」。绝不 `?? 0` —— 0 分是「测得很差」，null 是「没测」。
 */
function statMetric(key: string): number | null {
  const v = runStats.value?.metrics?.[key]
  return typeof v === 'number' ? v : null
}

/** 该指标实际参与加权的样本数（四项可能不同：某指标为 null 时不进分母） */
function statItems(key: string): number {
  return runStats.value?.metric_items?.[key] ?? 0
}

/** 大盘数值格式化：空值给「—」，避免出现 0.0000 这种误导性数字 */
function fmtStat(v: number | null): string {
  return v == null ? '—' : v.toFixed(4)
}

function fmt(v: number | null | undefined): string {
  return v == null ? '-' : v.toFixed(4)
}

function statusClass(s: string) {
  if (s === 'done') return 'tag-green'
  if (s === 'running') return 'tag-blue'
  if (s === 'pending') return 'tag-orange'
  // cancelled 是「人主动刹车」，不是故障：用中性灰，避免和真正的失败混为一谈
  if (s === 'cancelled') return 'tag-gray'
  return 'tag-red'
}

/** 【v1.13.1】未结束的任务（可停止、进度条实时跑） */
function isActiveRun(row: EvalRun): boolean {
  return row.status === 'pending' || row.status === 'running'
}

/**
 * 【v1.13.1】进度百分比。
 *
 * 分子用 processed_items（成功 + 失败 + 跳过），不是 success_items：
 * 用 success 的话「跳过」和「失败」的样本永远不计入，进度条会卡在 90% 不动，
 * 看起来像任务死住了。历史任务没有 processed_items（=0），退回 success 估算。
 */
function runPercent(row: EvalRun): number {
  if (!row.total_items) return 0
  const done = row.processed_items || row.success_items
  return Math.min(100, Math.round((done / row.total_items) * 100))
}

function progressColor(row: EvalRun): string {
  if (row.status === 'done') return 'var(--success)'
  if (row.status === 'failed') return 'var(--danger)'
  if (row.status === 'cancelled') return 'var(--text-3)'
  return 'var(--primary)'
}

/** 【P2】实验参数徽标：让「同一数据集、两组参数」在列表里一眼可辨，A/B 才有意义 */
function paramLabel(row: EvalRun): string {
  const cfg = row.rag_config || {}
  const topK = cfg.top_k as number | undefined
  const mode = cfg.search_mode as string | undefined
  if (topK == null && !mode) return '默认（KB 配置）'
  const modeText = mode === 'vector_only' ? '纯向量' : mode ? '混合' : ''
  const parts = [topK != null ? `top_k=${topK}` : '', modeText].filter(Boolean)
  return parts.join(' · ')
}

function runMetric(row: EvalRun, key: string): string {
  const v = row.metrics?.[key]
  return typeof v === 'number' ? v.toFixed(4) : '—'
}

// ---------------------------------------------------------------------------
// A/B 对比报告
// ---------------------------------------------------------------------------
// 后端 POST /eval/runs/compare 已经把「样本对齐 + 差值 + 阈值判定」全算完了，
// 前端只做渲染，不重复实现一遍算法（两端各算一次必然漂移）。
// 唯一在前端做的判断是「颜色」：Δ>0 绿 ↑，Δ<0 红 ↓（本项目内 green=好、red=坏，
// 与状态标签一致；注意这与股市红涨绿跌的惯例相反，是刻意的语义配色）。
const METRICS = [
  { key: 'faithfulness', label: 'Faithfulness', short: 'Faith.', desc: '答案是否有检索资料支撑' },
  { key: 'answer_relevancy', label: 'Answer Relevancy', short: 'Relev.', desc: '答案是否正面回答了问题' },
  { key: 'context_precision', label: 'Context Precision', short: 'Prec.', desc: '召回片段里有用信息的占比' },
  { key: 'context_recall', label: 'Context Recall', short: 'Recall', desc: '该召回的内容是否都召回了' },
] as const
type MetricKey = (typeof METRICS)[number]['key']

const selectedRuns = ref<EvalRun[]>([])
const compareVisible = ref(false)
const compareLoading = ref(false)
const compareResult = ref<EvalCompareResult | null>(null)
const compareError = ref('')
const onlyDiff = ref(false)

/** 只允许勾选「已完成」的 run，且最多两个、必须同数据集（后端对跨集会 400，提前挡掉） */
function selectableRun(row: EvalRun): boolean {
  if (row.status !== 'done') return false
  const sel = selectedRuns.value
  // 已勾中的行必须仍可点（否则取消不掉）
  if (sel.some((r) => r.id === row.id)) return true
  if (sel.length >= 2) return false
  if (!sel.length) return true
  return row.dataset_id === sel[0].dataset_id
}

function onSelectionChange(rows: EvalRun[]) {
  // selectable 已经把上限卡在 2，这里再 slice 一次兜住表格内部状态的滞后
  selectedRuns.value = rows.slice(0, 2)
}

function metricLabel(key: string): string {
  return METRICS.find((m) => m.key === key)?.label ?? key
}

function pick(score: EvalScore | null, key: MetricKey): number | null {
  if (!score) return null
  const v = score[key]
  return typeof v === 'number' ? v : null
}

function fmtVal(v: number | null | undefined): string {
  return typeof v === 'number' ? v.toFixed(4) : '—'
}

function fmtDelta(v: number | null | undefined): string {
  if (typeof v !== 'number') return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(4)}`
}

function deltaClass(v: number | null | undefined): string {
  if (typeof v !== 'number') return 'd-flat'
  if (v > 0) return 'd-up'
  if (v < 0) return 'd-down'
  return 'd-flat'
}

function deltaArrow(v: number | null | undefined): string {
  if (typeof v !== 'number') return ''
  if (v > 0) return '↑'
  if (v < 0) return '↓'
  return '='
}

/** run 基础信息第二行：参数 + 样本量 + 时间，A/B 两侧对称展示 */
function runBrief(row: EvalRun): string {
  const time = (row.created_at || '').slice(0, 19).replace('T', ' ')
  return `${row.success_items}/${row.total_items} 条样本 · ${time}`
}

const compareRows = computed(() => {
  const rows = compareResult.value?.rows ?? []
  return onlyDiff.value ? rows.filter((r) => r.is_diff) : rows
})

/** 一句话结论：先给「几升几降」，再点出变化最大的一项，避免只给一堆数字 */
const compareConclusion = computed(() => {
  const res = compareResult.value
  if (!res) return ''
  const valid = res.summary.filter((m) => typeof m.diff === 'number')
  if (!valid.length) return '两侧指标均无有效值，无法给出结论（检查样本是否都有参考答案）'
  const ups = valid.filter((m) => (m.diff as number) > 0).length
  const downs = valid.filter((m) => (m.diff as number) < 0).length
  const flat = valid.length - ups - downs
  const biggest = [...valid].sort(
    (x, y) => Math.abs(y.diff as number) - Math.abs(x.diff as number),
  )[0]
  const diffSamples = res.rows.filter((r) => r.is_diff).length
  return `B 相对 A：${ups} 项提升 / ${downs} 项下降 / ${flat} 项持平；变化最大的是 ${
    metricLabel(biggest.metric)
  } ${fmtDelta(biggest.diff)}；逐样本有 ${diffSamples} 条超过 Δ 阈值 ${res.diff_threshold}`
})

async function openCompare() {
  if (selectedRuns.value.length !== 2) {
    ElMessage.warning('请先勾选两个评测任务（同一数据集）')
    return
  }
  const [a, b] = selectedRuns.value
  if (a.dataset_id !== b.dataset_id) {
    ElMessage.warning('两个任务不属于同一数据集，样本无法对齐')
    return
  }
  compareVisible.value = true
  compareLoading.value = true
  compareError.value = ''
  compareResult.value = null
  onlyDiff.value = false
  try {
    compareResult.value = await compareRuns(a.id, b.id)
  } catch (err) {
    // 拦截器已弹提示，这里只留一份就地说明（弹窗不能空着）
    compareError.value =
      (err as { message?: string })?.message || '对比失败，请检查两个任务是否属于同一数据集'
  } finally {
    compareLoading.value = false
  }
}

async function loadStats() {
  // 【v1.13.2】单次聚合请求，不再拉列表自己算。
  // 失败时保留上一次数据（首屏失败则显示「—」）：大盘取数失败不该阻断主流程。
  try {
    runStats.value = await getRunStats()
  } catch {
    /* 错误已由拦截器提示，这里保持现状 */
  }
}

async function loadRuns() {
  loading.value = true
  selectedRuns.value = [] // 换页/刷新后勾选失效，避免「选中的 run 已不在表格里」却仍能点对比
  try {
    const res = await listRuns({ page: page.value, size: size.value })
    runs.value = res.items
    total.value = res.total
  } catch {
    /* ignore */
  } finally {
    loading.value = false
    startPollingIfNeeded()
  }
}

// ---------------------------------------------------------------------------
// 【v1.13.1】任务进度：有任务在跑时静默轮询
// ---------------------------------------------------------------------------
// 为什么需要：进度条（以及「成功/失败」计数）只有在数据刷新时才会动。原来全靠
// 用户手点「刷新」，进度显示等于摆设。这里在存在 pending/running 任务时每 5s
// 静默拉一次列表，全部结束后自动停 —- 不会无限轮询空跑接口。
//
// 关键：静默刷新**不能**走 loadRuns —— 它会清空 selectedRuns 并闪一次 loading 遮罩，
// 用户正在勾选两个 run 做 A/B 时会被反复打断。所以单独实现，并在数据替换后按 id
// 回填勾选（el-table 的勾选状态绑在行对象上，行对象换新就必须重新 toggle）。
const POLL_INTERVAL_MS = 5000
const runTableRef = ref<{ toggleRowSelection: (row: EvalRun, selected?: boolean) => void }>()
let pollTimer: ReturnType<typeof setInterval> | null = null
let polling = false

async function refreshRunsSilently() {
  if (polling) return // 上一轮还没回来，跳过（避免慢请求堆叠）
  polling = true
  const keepIds = selectedRuns.value.map((r) => r.id)
  try {
    const res = await listRuns({ page: page.value, size: size.value })
    runs.value = res.items
    total.value = res.total
    if (keepIds.length) {
      const rows = res.items.filter((r) => keepIds.includes(r.id))
      selectedRuns.value = rows
      await nextTick()
      rows.forEach((r) => runTableRef.value?.toggleRowSelection(r, true))
    }
    // 【v1.17】展开着的运行中任务，明细也要跟着涨 —— 原来只刷新列表，
    // 用户盯着「11/30」的进度条，展开区却一直是展开那一刻的旧数据。
    const activeExpanded = res.items.filter(
      (r) => isActiveRun(r) && expandedIds.value.includes(r.id),
    )
    if (activeExpanded.length) {
      await Promise.all(activeExpanded.map((r) => loadRunScores(r.id)))
    }
    await loadStats()
  } catch {
    /* 轮询失败静默：下一轮自然重试 */
  } finally {
    polling = false
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function startPollingIfNeeded() {
  const active = runs.value.some(isActiveRun)
  if (!active) {
    stopPolling()
    return
  }
  if (pollTimer) return
  pollTimer = setInterval(() => {
    if (!runs.value.some(isActiveRun)) {
      stopPolling()
      return
    }
    void refreshRunsSilently()
  }, POLL_INTERVAL_MS)
}

/** 【v1.13.1】删除评测任务（含级联得分明细），仅终态可删 */
async function onDeleteRun(row: EvalRun) {
  try {
    await ElMessageBox.confirm(
      `删除评测任务 #${row.id}？\n将同时删除该任务的 ${row.success_items} 条得分明细，不可恢复。\n（数据集与样本不受影响）`,
      '删除评测任务',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await deleteRun(row.id)
    delete scoresMap[row.id]
    ElMessage.success(`已删除任务 #${res.deleted_run_id}（含 ${res.deleted_scores} 条得分）`)
    // 当前页被删空时回退一页，避免停在空白尾页
    if (runs.value.length === 1 && page.value > 1) page.value -= 1
    await Promise.all([loadRuns(), loadStats()])
  } catch {
    /* 错误已由拦截器提示 */
  }
}

async function loadDatasets() {
  datasets.value = await listDatasets({ page: 1, size: 100 }).then((r) => r.items).catch(() => [])
}

// ---------------------------------------------------------------------------
// 【v1.13】数据集管理抽屉：补参考答案 / 删脏样本 / 删数据集
// ---------------------------------------------------------------------------
// 为什么需要：评测集是 RAGAS 的评分基准。点踩自动沉淀的 badcase 样本
// reference_answer 刻意留空（填 AI 的错误答案会让 RAGAS 把幻觉当标准答案），
// 但旧版只有「追加」没有「修改」，这批样本的 context_recall 永远算不出来 ——
// 对话页那句「可在评测页补充参考答案」的提示因此一直没有落点。
const dsDrawerVisible = ref(false)
const dsLoading = ref(false)
const dsRows = ref<EvalDataset[]>([])
const dsPage = ref(1)
const dsSize = ref(10)
const dsTotal = ref(0)

// null = 一级视图（数据集列表）；非空 = 二级视图（该数据集的样本列表）
const activeDataset = ref<EvalDataset | null>(null)
const itemsLoading = ref(false)
const dsItems = ref<EvalItem[]>([])
const itemPage = ref(1)
const itemSize = ref(10)
const itemTotal = ref(0)

const editVisible = ref(false)
const saving = ref(false)
const editForm = reactive({ id: 0, question: '', reference_answer: '', stratum: '' })

/** 参考答案是否缺失：后端 missing_reference 已按「空串/纯空白」口径统计 */
function missLabel(n: number | null | undefined): string {
  return n ? `${n} 条待补` : '已齐全'
}

function hasAnswer(text: string | null | undefined): boolean {
  return !!text && text.trim().length > 0
}

async function loadDatasetRows() {
  dsLoading.value = true
  try {
    const res = await listDatasets({ page: dsPage.value, size: dsSize.value })
    dsRows.value = res.items
    dsTotal.value = res.total
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    dsLoading.value = false
  }
}

function openDatasetDrawer() {
  dsDrawerVisible.value = true
  activeDataset.value = null
  dsItems.value = []
  dsPage.value = 1
  loadDatasetRows()
}

async function openItems(ds: EvalDataset) {
  activeDataset.value = ds
  itemPage.value = 1
  await loadItems()
}

async function loadItems() {
  if (!activeDataset.value) return
  itemsLoading.value = true
  try {
    const res = await listItems(activeDataset.value.id, { page: itemPage.value, size: itemSize.value })
    dsItems.value = res.items
    itemTotal.value = res.total
  } catch {
    dsItems.value = []
    itemTotal.value = 0
  } finally {
    itemsLoading.value = false
  }
}

function backToDatasets() {
  activeDataset.value = null
  dsItems.value = []
  loadDatasetRows()
}

function openEdit(row: EvalItem) {
  editForm.id = row.id
  editForm.question = row.question
  editForm.reference_answer = row.reference_answer || ''
  editForm.stratum = row.stratum || ''
  editVisible.value = true
}

async function doSaveItem() {
  if (!activeDataset.value) return
  const answer = editForm.reference_answer.trim()
  if (!answer) {
    // 与服务端口径一致：留空样本的 context_recall 无法计算，宁可挡住
    ElMessage.warning('参考答案不能为空，否则该样本的 context_recall 无法计算')
    return
  }
  const origin = dsItems.value.find((i) => i.id === editForm.id)
  const payload: { reference_answer?: string; stratum?: string | null } = {}
  if (answer !== (origin?.reference_answer || '')) payload.reference_answer = answer
  const nextStratum = editForm.stratum.trim() || null
  if (nextStratum !== (origin?.stratum ?? null)) payload.stratum = nextStratum
  if (!Object.keys(payload).length) {
    ElMessage.info('没有需要保存的改动')
    editVisible.value = false
    return
  }
  saving.value = true
  try {
    // PATCH 语义：只提交改动过的字段，未改的不会被回写成空值
    await updateItem(activeDataset.value.id, editForm.id, payload)
    ElMessage.success('已保存')
    editVisible.value = false
    await Promise.all([loadItems(), loadDatasetRows()])
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    saving.value = false
  }
}

async function onDeleteItem(row: EvalItem) {
  if (!activeDataset.value) return
  try {
    await ElMessageBox.confirm(
      `确定删除该样本？删除后不可恢复。\n\n问题：${row.question.slice(0, 60)}`,
      '删除样本',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await deleteItem(activeDataset.value.id, row.id)
    ElMessage.success('已删除')
    // 当前页被删空时回退一页，避免停在空白的尾页
    if (dsItems.value.length === 1 && itemPage.value > 1) itemPage.value -= 1
    await Promise.all([loadItems(), loadDatasetRows()])
  } catch {
    /* 错误已由拦截器提示 */
  }
}

async function onDeleteDataset(row: EvalDataset) {
  try {
    await ElMessageBox.confirm(
      `确定删除数据集「${row.name}」？\n将级联删除其 ${row.item_count} 条样本（历史评测运行记录保留）。`,
      '删除数据集',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteDataset(row.id)
    ElMessage.success('已删除数据集')
    if (dsRows.value.length === 1 && dsPage.value > 1) dsPage.value -= 1
    await Promise.all([loadDatasetRows(), loadDatasets()])
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 【v1.17】拉取单个任务的得分明细。
 *
 * 失败时只在「从来没有成功拉过」的情况下写空数组占位，绝不覆盖已有数据：
 * 原实现对 catch 一律 `scoresMap[id] = []`，一次网络抖动就会把已经展示出来的
 * 28 条明细清空，且因为下面 onExpand 的缓存判断，之后永远不会再重拉。
 */
async function loadRunScores(runId: number) {
  scoresLoading[runId] = true
  try {
    const res = await listScores(runId, { page: 1, size: 50 })
    scoresMap[runId] = res.items
  } catch {
    if (scoresMap[runId] === undefined) scoresMap[runId] = []
  } finally {
    scoresLoading[runId] = false
  }
}

/** 已展开的任务 id（轮询时用来刷新它们正在增长的明细）。 */
const expandedIds = ref<number[]>([])

async function onExpand(row: EvalRun, expandedRows: EvalRun[]) {
  const isOpen = expandedRows.some((r) => r.id === row.id)
  expandedIds.value = expandedRows.map((r) => r.id)
  if (!isOpen) return
  // 【v1.17】缓存判断从「有值」改成「终态 + 有值」。
  // 原判断 `if (scoresMap[row.id]) return` 有两个坑：
  //   1. 空数组是 truthy —— 展开瞬间还没落库（评测刚开跑）会拉到 []，
  //      从此被当成「已加载」，页面永远显示「该运行暂无得分记录」；
  //   2. 运行中的任务得分在涨，缓存命中后不再重拉，明细永远停在第一次的快照。
  if (!isActiveRun(row) && scoresMap[row.id] !== undefined) return
  await loadRunScores(row.id)
}

/** 明细存在但四项指标全为空 —— 判分链路故障的特征，必须显式提示而不是显示一片「-」。 */
function metricsAllEmpty(list: EvalScore[]): boolean {
  return (
    list.length > 0 &&
    list.every(
      (s) =>
        s.faithfulness == null &&
        s.answer_relevancy == null &&
        s.context_precision == null &&
        s.context_recall == null,
    )
  )
}

async function doCreateRun() {
  if (!newRunDatasetId.value) {
    ElMessage.warning('请选择数据集')
    return
  }
  creating.value = true
  try {
    // 【P2】rag_config 随任务持久化：top_k / search_mode 在评测执行时覆盖检索默认值
    const ragConfig: RagConfigPayload = {
      top_k: runTopK.value,
      search_mode: runSearchMode.value,
    }
    const run = await createRun(newRunDatasetId.value, ragConfig)
    const mode = runSearchMode.value === 'vector_only' ? '纯向量' : '混合检索'
    ElMessage.success(
      `已触发评测，run_id=${run.id}，状态=${run.status}（${mode} · top_k=${runTopK.value}）`,
    )
    newRunDialogVisible.value = false
    newRunDatasetId.value = null
    await Promise.all([loadRuns(), loadStats()])
  } catch {
    /* ignore */
  } finally {
    creating.value = false
  }
}

// ---------------------------------------------------------------------------
// 【v1.13】批量执行：从「全量无参」改为「弹窗勾选 + 参数与手动建任务一致」
// ---------------------------------------------------------------------------
// 改前的三个问题：
//   1) `datasets.filter(item_count>0)` 全量硬编码 —— 只想跑新加的那一个集也做不到；
//   2) `createRun(d.id)` 不传 rag_config —— 批量产出的 run 与手动建的 run 参数不同，
//      而任务表上看不出差异，A/B 时会把"参数差异"误读成"数据集差异"；
//   3) 一个按钮直接开跑 N 个任务，无确认无成本提示，与 P2「精细控制实验参数」自相矛盾。
const batchVisible = ref(false)
const batchRows = ref<EvalDataset[]>([])
const batchSelected = ref<number[]>([])
const batchTopK = ref(5)
const batchSearchMode = ref<'vector_only' | 'bm25_vector_hybrid'>('bm25_vector_hybrid')
const batchRunning = ref(false)
const batchDone = ref(0)
/** el-table 的多选状态要靠实例方法回填，不能只改 batchSelected（双向不会自动同步） */
const batchTableRef = ref<{ toggleRowSelection: (row: EvalDataset, selected: boolean) => void } | null>(null)

const batchPicked = computed(() => batchRows.value.filter((d) => batchSelected.value.includes(d.id)))
const batchSampleTotal = computed(() => batchPicked.value.reduce((sum, d) => sum + d.item_count, 0))
/** 单样本约 1 次问答生成 + 4 次 RAGAS 指标判定：给量级感，不是精确计费 */
const batchApiCalls = computed(() => batchSampleTotal.value * 5)
const batchModeLabel = computed(() => (batchSearchMode.value === 'vector_only' ? '纯向量' : '混合检索'))

function openBatchRun() {
  batchRows.value = datasets.value.filter((d) => d.item_count > 0)
  if (!batchRows.value.length) {
    ElMessage.warning('暂无可评测的数据集（样本数为 0）')
    return
  }
  // 默认全选：常见场景是「刚导入完想全跑一遍」。但保留取消能力 —— 能选才谈得上控制。
  batchSelected.value = batchRows.value.map((d) => d.id)
  batchDone.value = 0
  batchVisible.value = true
}

/**
 * 把 batchSelected 回填到 el-table 的复选框上。
 *
 * 时机必须是 dialog 的 `opened`（过渡动画结束、DOM 已挂载），不能用 nextTick：
 * el-dialog 默认懒渲染，首次打开时 nextTick 那一刻表格实例还没建出来，
 * 回填会静默失败 —— 表现为「按钮说已选 10 个但复选框全空」，用户会以为坏了。
 */
function syncBatchSelection() {
  batchRows.value.forEach((d) => batchTableRef.value?.toggleRowSelection(d, true))
}

function onBatchSelectionChange(rows: EvalDataset[]) {
  batchSelected.value = rows.map((r) => r.id)
}

async function doBatchRun() {
  const picked = batchPicked.value
  if (!picked.length) {
    ElMessage.warning('请至少勾选一个数据集')
    return
  }
  batchRunning.value = true
  batchDone.value = 0
  const ragConfig: RagConfigPayload = { top_k: batchTopK.value, search_mode: batchSearchMode.value }
  let ok = 0
  let failed = 0
  // 刻意串行触发、不用 Promise.all：后端有「同时运行任务数」上限，
  // 并发打过去只会让大部分请求撞 429，串行反而能稳定触发到上限为止。
  for (const d of picked) {
    try {
      await createRun(d.id, ragConfig)
      ok += 1
    } catch {
      // 错误提示由请求拦截器统一弹出（典型是并发上限 429）；继续尝试后面的
      failed += 1
    } finally {
      batchDone.value += 1
    }
  }
  batchRunning.value = false
  if (ok) {
    batchVisible.value = false
    ElMessage.success(
      `已触发 ${ok} 个评测任务（${batchModeLabel.value} · top_k=${batchTopK.value}）` +
        (failed ? `，${failed} 个未触发` : ''),
    )
  }
  await Promise.all([loadRuns(), loadStats()])
}

/** 停止任务：协作式取消，后端立即置 cancelled，后台在下一个样本检查点退出 */
async function onCancelRun(row: EvalRun) {
  try {
    await ElMessageBox.confirm(
      `停止任务 #${row.id}？已完成 ${row.success_items}/${row.total_items} 条的得分会保留，` +
        `但样本数不全，停止后的结果不能与完整运行做 A/B 对比。`,
      '停止评测任务',
      { type: 'warning', confirmButtonText: '停止', cancelButtonText: '继续跑' },
    )
  } catch {
    return // 用户点了「继续跑」
  }
  try {
    const res = await cancelRun(row.id)
    ElMessage.success(`已停止 #${row.id}，保留已完成 ${res.success_items}/${res.total_items} 条`)
    await Promise.all([loadRuns(), loadStats()])
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 清理僵尸任务 + 修复历史指标：
 * 1) 服务异常退出后，残留的 running 任务已无执行线程，却永久占着「同时运行任务数」
 *    名额 —— 表现为「新建任务一直 429，但界面上看不到在跑的任务」。
 * 2) 老版本中断的任务只落了得分、没落汇总指标，列表里四项全「—」但点开有明细；
 *    这里一并回填（只补 metrics，不改状态与计数）。
 * 后端启动时都会自动做一次，这里是运行期的显式出口。
 */
async function onCleanupStale() {
  try {
    await ElMessageBox.confirm(
      '只清理「超过 10 分钟没有任何进度」的任务，并回填历史任务的汇总指标。' +
        '正在正常执行但已跑超过 10 分钟的任务也会被标记失败，请先确认列表里确实没有正在跑的任务。',
      '清理僵尸任务 / 修复历史指标',
      { type: 'warning', confirmButtonText: '清理', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await cleanupStaleRuns()
    const parts: string[] = []
    if (res.cleared) parts.push(`清理 ${res.cleared} 个僵尸任务`)
    if (res.repaired) parts.push(`回填 ${res.repaired} 个任务的历史指标`)
    ElMessage.success(parts.length ? `已${parts.join('，')}` : '没有需要清理或修复的任务')
    await Promise.all([loadRuns(), loadStats()])
  } catch {
    /* 错误已由拦截器提示 */
  }
}

async function onDownload(row: EvalRun) {
  try {
    await exportRunScoresCsv(row.id)
    ElMessage.success(`已下载 ragas_run_${row.id}.csv`)
  } catch {
    /* 错误已由拦截器提示 */
  }
}

// ---------------------------------------------------------------------------
// 【P1】新建数据集 + 批量导入（JSON / CSV 粘贴）
// ---------------------------------------------------------------------------
// 闭环顺序：新建数据集 -> 导入样本 -> 补答案（P0 抽屉）-> 建评测任务（P2 参数）。
// 解析与逐行校验都在前端 utils/evalImport.ts 完成，后端只做最终兜底：
// 批量导入失败通常不是接口挂了，而是表头/空行/整列空答案，必须逐行报出来。
const kbs = ref<KnowledgeBase[]>([])

const dsDialogVisible = ref(false)
const dsSaving = ref(false)
const dsForm = reactive<{ name: string; kb_id: number | null; description: string }>({
  name: '',
  kb_id: null,
  description: '',
})

const importVisible = ref(false)
const importTarget = ref<EvalDataset | null>(null)
const importText = ref('')
const importing = ref(false)

const importOutcome = computed(() => parseEvalImport(importText.value))
const importSummary = computed(() => (importText.value.trim() ? describeOutcome(importOutcome.value) : ''))
const canImport = computed(
  () =>
    !!importTarget.value &&
    importOutcome.value.items.length > 0 &&
    importOutcome.value.errors.length === 0,
)

const IMPORT_SAMPLE = `question,reference_answer,stratum
差旅住宿标准是多少？,研发部一线城市住宿上限 500 元/晚,hard
出差审批人是谁？,技术总监,easy`

// ---- 文件直读：支持 .json / .csv / .txt 拖入或点选 ----
// 刻意**不走文件上传接口**：文件只在浏览器里读成文本，再喂给与"粘贴"完全相同的
// 解析器，所以逐行校验、错误文案、500 条上限判断全部复用，不会出现两套口径不一致。
const MAX_IMPORT_FILE_BYTES = 2 * 1024 * 1024
const importFileInput = ref<HTMLInputElement | null>(null)
const importFileName = ref('')
const importDragging = ref(false)

/** Excel 导出的 CSV 在中文 Windows 上默认是 GBK；直接用 UTF-8 硬读会整片乱码，故两段式解码 */
function decodeImportText(buf: ArrayBuffer): string {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buf)
  } catch {
    return new TextDecoder('gbk').decode(buf)
  }
}

async function readImportFile(file: File) {
  if (file.size > MAX_IMPORT_FILE_BYTES) {
    ElMessage.warning(`「${file.name}」超过 2MB，请拆分后再导入`)
    return
  }
  try {
    importText.value = decodeImportText(await file.arrayBuffer())
    importFileName.value = file.name
  } catch (err) {
    ElMessage.error(`读取文件失败：${(err as Error).message}`)
  }
}

function pickImportFile() {
  importFileInput.value?.click()
}

function onImportFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) void readImportFile(file)
  input.value = '' // 清空 value，否则再次选同一个文件不触发 change
}

function onImportDrop(e: DragEvent) {
  importDragging.value = false
  const file = e.dataTransfer?.files?.[0]
  if (file) void readImportFile(file)
}

async function openCreateDataset() {
  dsForm.name = ''
  dsForm.kb_id = null
  dsForm.description = ''
  dsDialogVisible.value = true
  if (!kbs.value.length) {
    kbs.value = await listKbs({ page: 1, size: 100 }).then((r) => r.items).catch(() => [])
  }
}

async function doCreateDataset() {
  const name = dsForm.name.trim()
  if (!name) {
    ElMessage.warning('请填写数据集名称')
    return
  }
  if (!dsForm.kb_id) {
    ElMessage.warning('请选择目标知识库')
    return
  }
  dsSaving.value = true
  try {
    const ds = await createDataset({
      kb_id: dsForm.kb_id,
      name,
      description: dsForm.description.trim() || null,
    })
    dsDialogVisible.value = false
    await Promise.all([loadDatasetRows(), loadDatasets()])
    ElMessage.success(`已创建「${ds.name}」，接着导入样本即可参与评测`)
    // 建完直接进导入：一条路走完「建集 -> 导入 -> 可评测」，不用回头找入口
    openImport(ds)
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    dsSaving.value = false
  }
}

function openImport(ds: EvalDataset) {
  importTarget.value = ds
  importText.value = ''
  importFileName.value = ''
  importVisible.value = true
}

function fillImportSample() {
  importText.value = IMPORT_SAMPLE
  importFileName.value = ''
}

async function doImport() {
  if (!importTarget.value) return
  const outcome = importOutcome.value
  if (outcome.errors.length) {
    ElMessage.warning('请先修正解析出来的问题')
    return
  }
  if (!outcome.items.length) {
    ElMessage.warning('没有可导入的样本')
    return
  }
  importing.value = true
  try {
    const res = await addItems(importTarget.value.id, outcome.items)
    importVisible.value = false
    ElMessage.success(`已导入 ${res.imported} 条，当前共 ${res.item_count} 条样本`)
    await Promise.all([loadDatasetRows(), loadDatasets()])
    if (activeDataset.value?.id === importTarget.value.id) await loadItems()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    importing.value = false
  }
}

onMounted(() => {
  loadRuns()
  loadStats()
  loadDatasets()
})

// 定时器必须在组件卸载时清掉：留着会在路由切走后继续打接口，且持有组件引用
onUnmounted(stopPolling)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title">RAGAS 评测管理</div>
        <div class="page-desc">数据集驱动的评测：faithfulness / answer_relevancy / context_precision / context_recall</div>
      </div>
      <div class="page-actions">
        <el-button @click="loadRuns(); loadStats()"><el-icon><Refresh /></el-icon>刷新</el-button>
        <el-button @click="openDatasetDrawer"><el-icon><Files /></el-icon>数据集管理</el-button>
        <el-button :disabled="selectedRuns.length !== 2" @click="openCompare">
          <el-icon><TrendCharts /></el-icon>对比 A/B{{ selectedRuns.length ? `（${selectedRuns.length}）` : '' }}
        </el-button>
        <el-button @click="openBatchRun"><el-icon><VideoPlay /></el-icon>批量执行</el-button>
        <el-button type="primary" @click="newRunDialogVisible = true"><el-icon><Plus /></el-icon>新建评测任务</el-button>
      </div>
    </div>

    <!-- 统计卡片 -->
    <!--
      统计卡片（v1.13.2）：一行 6 格 = 2 个运行态 + 4 个 RAGAS 指标。
      数据全部来自 GET /eval/runs/stats 的聚合结果，前端不做任何二次计算 ——
      口径只有后端一份实现，避免「界面一个数、A/B 报告另一个数」。
      指标为 null 时显示灰色「—」（不是 0）：「没测」和「测得差」是两件事。
    -->
    <div class="stat-grid stat-grid-6">
      <div class="stat-card">
        <div class="stat-label">评测任务总数</div>
        <div class="stat-value">{{ runStats ? runStats.total : '—' }}</div>
        <div class="stat-sub">
          完成 {{ runStats?.done ?? 0 }} · 失败 {{ runStats?.failed ?? 0 }} · 停止
          {{ runStats?.cancelled ?? 0 }}
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-label">运行中</div>
        <div class="stat-value" :style="runningCount ? { color: 'var(--primary)' } : undefined">
          {{ runStats ? runningCount : '—' }}
        </div>
        <div class="stat-sub">
          执行 {{ runStats?.running ?? 0 }} · 排队 {{ runStats?.pending ?? 0 }}
        </div>
      </div>
      <div v-for="m in METRICS" :key="m.key" class="stat-card">
        <div class="stat-label">
          {{ m.label }}
          <el-tooltip
            placement="top"
            :content="`${m.desc}。口径：仅统计已完成（done）任务，按成功样本数加权；该指标为空的任务不计入分母。`"
          >
            <span class="info-dot">?</span>
          </el-tooltip>
        </div>
        <div class="stat-value" :class="{ 'is-empty': statMetric(m.key) == null }">
          {{ fmtStat(statMetric(m.key)) }}
        </div>
        <div class="stat-sub">
          {{ statItems(m.key) ? `基于 ${statItems(m.key)} 条样本 · ${runStats?.metric_runs ?? 0} 个任务` : '暂无数据' }}
        </div>
      </div>
    </div>

    <!-- 任务表格 -->
    <div class="card">
      <el-table
        ref="runTableRef"
        v-loading="loading"
        :data="runs"
        style="width: 100%"
        row-key="id"
        @expand-change="onExpand"
        @selection-change="onSelectionChange"
      >
        <!-- 勾选两个已完成任务即可对比；未完成的 run 没有得分，勾了也没意义 -->
        <el-table-column type="selection" width="44" :selectable="selectableRun" />
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="detail-pad">
              <!-- 本次运行的汇总口径：四项指标 + 实际生效的实验参数，方便与另一组 A/B 对照 -->
              <div class="run-summary">
                <span class="sum-item"><em>实验参数</em>{{ paramLabel(row) }}</span>
                <span class="sum-item"><em>faithfulness</em>{{ runMetric(row, 'faithfulness') }}</span>
                <span class="sum-item"><em>answer_relevancy</em>{{ runMetric(row, 'answer_relevancy') }}</span>
                <span class="sum-item"><em>context_precision</em>{{ runMetric(row, 'context_precision') }}</span>
                <span class="sum-item"><em>context_recall</em>{{ runMetric(row, 'context_recall') }}</span>
              </div>
              <div v-if="scoresLoading[row.id]" class="small muted">加载得分明细…</div>
              <template v-else>
                <!-- 【v1.13.1】失败/停止的任务也可能有得分：中断前跑完的样本是真金白银算出来的 -->
                <el-alert
                  v-if="scoresMap[row.id]?.length && row.status !== 'done'"
                  type="warning"
                  :closable="false"
                  show-icon
                  class="partial-alert"
                >
                  <template #title>
                    该任务未完整跑完（{{ row.status === 'cancelled' ? '已停止' : '已失败' }}），
                    下列 {{ scoresMap[row.id].length }} 条是中断前已完成的样本得分。
                    指标只代表这部分样本，<span class="strong">不能与完整任务做 A/B 对比</span>。
                    <template v-if="row.error_msg">原因：{{ row.error_msg }}</template>
                  </template>
                </el-alert>
                <template v-if="scoresMap[row.id]?.length">
                  <!-- 【v1.17】样本跑完但指标全空：判分链路故障的特征。
                       以前这种情况只表现为一排「-」，看起来像「分数还没算出来」，
                       实际是每条样本都白跑了一遍检索+生成，必须点名。 -->
                  <el-alert
                    v-if="metricsAllEmpty(scoresMap[row.id])"
                    type="error"
                    :closable="false"
                    show-icon
                    style="margin-bottom: 8px"
                    title="这些样本跑完了，但四项指标全部为空 —— 判分模型没有产出结果"
                    description="通常是判分模型不可用（额度用尽、模型名不可用、接口地址不匹配）。请到「系统配置」核对判分模型后重跑；这些样本的指标不可用于 A/B 对比。"
                  />
                  <div class="strong" style="margin-bottom: 8px">评测指标得分明细（共 {{ scoresMap[row.id].length }} 条）</div>
                  <el-table :data="scoresMap[row.id]" size="small" border>
                    <el-table-column prop="item_id" label="样本ID" width="80" />
                    <el-table-column prop="question" label="问题" min-width="200" show-overflow-tooltip />
                    <el-table-column label="faithfulness" width="120">
                      <template #default="{ row: s }">{{ fmt(s.faithfulness) }}</template>
                    </el-table-column>
                    <el-table-column label="answer_relevancy" width="140">
                      <template #default="{ row: s }">{{ fmt(s.answer_relevancy) }}</template>
                    </el-table-column>
                    <el-table-column label="context_precision" width="140">
                      <template #default="{ row: s }">{{ fmt(s.context_precision) }}</template>
                    </el-table-column>
                    <el-table-column label="context_recall" width="130">
                      <template #default="{ row: s }">{{ fmt(s.context_recall) }}</template>
                    </el-table-column>
                  </el-table>
                </template>
                <EmptyState
                  v-else
                  :title="
                    row.status === 'failed' || row.status === 'cancelled'
                      ? '任务中断，未产生任何得分'
                      : isActiveRun(row)
                        ? '样本得分尚未落库'
                        : '该运行暂无得分记录'
                  "
                  :desc="
                    row.status === 'failed' || row.status === 'cancelled'
                      ? row.error_msg || '一条样本都没算完就中断了，没有可展示的指标'
                      : isActiveRun(row)
                        ? '评测进行中，得分按批次写入；本区域每 5 秒自动刷新'
                        : ''
                  "
                  icon="📈"
                />
              </template>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="dataset_id" label="数据集" width="90" />
        <el-table-column prop="kb_id" label="KB" width="70" />
        <el-table-column prop="model" label="模型" width="150" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><span class="ui-tag" :class="statusClass(row.status)">{{ row.status }}</span></template>
        </el-table-column>
        <!-- 【P2】参数与指标同屏：A/B 就是「同数据集、不同参数、对比这四列」 -->
        <el-table-column label="实验参数" width="150">
          <template #default="{ row }">
            <span class="param-tag" :class="{ 'param-default': !row.rag_config }">{{ paramLabel(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="Faithfulness" width="120">
          <template #default="{ row }">{{ runMetric(row, 'faithfulness') }}</template>
        </el-table-column>
        <el-table-column label="Context Recall" width="125">
          <template #default="{ row }">{{ runMetric(row, 'context_recall') }}</template>
        </el-table-column>
        <el-table-column label="进度" width="150">
          <template #default="{ row }">
            <div class="prog-cell">
              <!-- 结束后不再画条（没有「进行中」的语义），直接给最终计数，列表更安静 -->
              <el-progress
                v-if="isActiveRun(row)"
                :percentage="runPercent(row)"
                :stroke-width="6"
                :show-text="false"
                :color="progressColor(row)"
              />
              <div class="prog-text">
                <span :class="{ 'prog-active': isActiveRun(row) }">
                  {{ row.processed_items || row.success_items }}/{{ row.total_items }}
                </span>
                <span v-if="row.failed_items" class="prog-fail">失败 {{ row.failed_items }}</span>
                <span v-if="row.status === 'cancelled'" class="prog-muted">已停止</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="175" class-name="time-col">
          <template #default="{ row }">{{ (row.created_at || '').slice(0, 19).replace('T', ' ') }}</template>
        </el-table-column>
        <el-table-column label="操作" width="215" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :disabled="row.status !== 'done'" @click="onDownload(row)">
              <el-icon><Download /></el-icon>CSV
            </el-button>
            <!-- 停止只对未结束的任务出现：done/failed/cancelled 都没有可停的东西 -->
            <el-button
              v-if="isActiveRun(row)"
              link
              type="danger"
              size="small"
              @click="onCancelRun(row)"
            >停止</el-button>
            <!-- 删除只对终态出现：运行中的必须先停止（否则后台线程会往已删的 run 写孤儿得分） -->
            <el-button
              v-else
              link
              type="danger"
              size="small"
              @click="onDeleteRun(row)"
            >删除</el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyState title="暂无评测运行" desc="选择数据集后触发评测" icon="🚀" /></template>
      </el-table>
      <PagePagination :total="total" :page="page" :size="size" @update:page="(p) => { page = p; loadRuns() }" @update:size="(s) => { size = s; page = 1; loadRuns() }" />
    </div>

    <!-- 新建评测任务弹窗 -->
    <el-dialog v-model="newRunDialogVisible" title="新建评测任务" width="480px">
      <el-form label-position="top">
        <el-form-item label="选择数据集">
          <el-select v-model="newRunDatasetId" placeholder="请选择数据集" style="width: 100%">
            <el-option v-for="d in datasets" :key="d.id" :label="`#${d.id} ${d.name}（${d.item_count} 条样本）`" :value="d.id" />
          </el-select>
        </el-form-item>
        <!-- 【P2】A/B 实验参数：请求级覆盖检索，随任务持久化到 eval_runs.rag_config -->
        <el-form-item label="检索模式 search_mode">
          <el-select v-model="runSearchMode" style="width: 100%">
            <el-option label="混合检索（向量 + BM25，默认）" value="bm25_vector_hybrid" />
            <el-option label="纯向量（vector_only）" value="vector_only" />
          </el-select>
        </el-form-item>
        <el-form-item label="召回条数 top_k">
          <el-input-number v-model="runTopK" :min="1" :max="50" :step="1" controls-position="right" style="width: 160px" />
          <span class="form-tip">同配置重复跑无意义；A/B 请分两次、只改其中一个参数</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="newRunDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="doCreateRun">开始评测</el-button>
      </template>
    </el-dialog>

    <!-- 【v1.13】批量执行：勾选数据集 + 与手动建任务完全一致的实验参数 -->
    <el-dialog v-model="batchVisible" title="批量执行评测" width="760px" @opened="syncBatchSelection">
      <div class="batch-head">
        <span class="import-target">
          已选 <b>{{ batchPicked.length }}</b> / {{ batchRows.length }} 个数据集
        </span>
        <span class="batch-cost">
          合计 {{ batchSampleTotal }} 条样本 · 约 {{ batchApiCalls }} 次模型调用
        </span>
      </div>
      <el-table
        ref="batchTableRef"
        :data="batchRows"
        size="small"
        border
        max-height="280"
        row-key="id"
        @selection-change="onBatchSelectionChange"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column prop="id" label="#" width="58" />
        <el-table-column prop="name" label="数据集" min-width="200" show-overflow-tooltip />
        <el-table-column prop="item_count" label="样本数" width="80" align="right" />
        <el-table-column label="参考答案" width="100">
          <template #default="{ row }">
            <span v-if="row.missing_reference" class="batch-tag is-warn">待补 {{ row.missing_reference }}</span>
            <span v-else class="batch-tag is-ok">齐全</span>
          </template>
        </el-table-column>
        <template #empty><EmptyState title="暂无可评测的数据集" desc="样本数为 0 的数据集不会出现在这里" icon="📚" /></template>
      </el-table>
      <el-form label-position="top" style="margin-top: 12px">
        <el-form-item label="检索模式 search_mode（对本次勾选的全部数据集统一生效）">
          <el-select v-model="batchSearchMode" style="width: 100%">
            <el-option label="混合检索（向量 + BM25，默认）" value="bm25_vector_hybrid" />
            <el-option label="纯向量（vector_only）" value="vector_only" />
          </el-select>
        </el-form-item>
        <el-form-item label="召回条数 top_k">
          <el-input-number v-model="batchTopK" :min="1" :max="50" :step="1" controls-position="right" style="width: 160px" />
          <span class="form-tip">与「新建评测任务」同一套参数，保证批量跑出的 run 与手动跑的 run 可比</span>
        </el-form-item>
      </el-form>
      <el-alert type="info" :closable="false" show-icon>
        <template #title>
          任务异步执行，触发后可随时在下方任务列表点「停止」中止（已完成的得分会保留）。
          当前已有 {{ runningCount }} 个任务在跑；系统对同时运行的任务数有上限，超出的会被拒绝，
          已触发的不会回滚。
        </template>
      </el-alert>
      <div class="batch-foot-tip">
        <el-button link type="primary" size="small" @click="onCleanupStale">清理僵尸任务</el-button>
        <span class="form-tip">
          仅在「新建任务一直报上限、但列表里看不到在跑的任务」时才需要 —— 这是服务异常退出留下的残留任务占着名额。
        </span>
      </div>
      <template #footer>
        <el-button @click="batchVisible = false">取消</el-button>
        <el-button type="primary" :loading="batchRunning" :disabled="!batchPicked.length" @click="doBatchRun">
          {{ batchRunning ? `触发中 ${batchDone}/${batchPicked.length}` : `触发 ${batchPicked.length} 个任务` }}
        </el-button>
      </template>
    </el-dialog>

    <!-- A/B 对比报告：数据全部来自后端 compare 接口，前端只负责着色与排版 -->
    <el-dialog v-model="compareVisible" title="A/B 对比报告" width="1080px" top="5vh">
      <div v-if="compareLoading" class="cmp-state">正在对齐样本并计算差值…</div>
      <div v-else-if="compareError" class="cmp-state cmp-error">{{ compareError }}</div>
      <template v-else-if="compareResult">
        <!-- 顶部：两侧基础信息 -->
        <div class="cmp-head">
          <div class="cmp-side">
            <span class="cmp-badge cmp-badge-a">A</span>
            <div class="cmp-info">
              <div class="cmp-line"><b>run #{{ compareResult.run_a.id }}</b> · {{ compareResult.run_a.model }}</div>
              <div class="cmp-line">数据集 #{{ compareResult.run_a.dataset_id }} · {{ runBrief(compareResult.run_a) }}</div>
              <div class="cmp-line cmp-param">{{ paramLabel(compareResult.run_a) }}</div>
            </div>
          </div>
          <div class="cmp-vs">VS</div>
          <div class="cmp-side">
            <span class="cmp-badge cmp-badge-b">B</span>
            <div class="cmp-info">
              <div class="cmp-line"><b>run #{{ compareResult.run_b.id }}</b> · {{ compareResult.run_b.model }}</div>
              <div class="cmp-line">数据集 #{{ compareResult.run_b.dataset_id }} · {{ runBrief(compareResult.run_b) }}</div>
              <div class="cmp-line cmp-param">{{ paramLabel(compareResult.run_b) }}</div>
            </div>
          </div>
        </div>

        <!-- 结论：先给一句话，再给表；只给表的话对方要自己算 -->
        <div class="cmp-conclusion">{{ compareConclusion }}</div>

        <!-- 中间：四项指标均值对比 -->
        <div class="cmp-metrics">
          <div class="cmp-mrow cmp-mhead">
            <span class="c-name">指标</span>
            <span class="c-val">A</span>
            <span class="c-val">B</span>
            <span class="c-delta">Δ（B − A）</span>
          </div>
          <div v-for="m in compareResult.summary" :key="m.metric" class="cmp-mrow">
            <span class="c-name">
              {{ metricLabel(m.metric) }}
              <em class="c-desc">{{ METRICS.find((x) => x.key === m.metric)?.desc }}</em>
            </span>
            <span class="c-val">{{ fmtVal(m.avg_a) }}</span>
            <span class="c-val">{{ fmtVal(m.avg_b) }}</span>
            <span class="c-delta" :class="deltaClass(m.diff)">
              {{ deltaArrow(m.diff) }} {{ fmtDelta(m.diff) }}
            </span>
          </div>
        </div>

        <!-- 底部：逐样本明细 -->
        <div class="cmp-rows-head">
          <span class="strong">
            逐样本对比（{{ compareRows.length }} / {{ compareResult.rows.length }} 条）
          </span>
          <el-switch v-model="onlyDiff" active-text="只看差异样本" />
        </div>
        <el-table :data="compareRows" size="small" border max-height="320" style="width: 100%">
          <el-table-column label="问题" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.is_diff" class="diff-chip">差异</span>
              <span :class="{ 'q-dim': !row.a || !row.b }">{{ row.question }}</span>
            </template>
          </el-table-column>
          <el-table-column v-for="m in METRICS" :key="m.key" :label="m.label" width="170">
            <template #default="{ row }">
              <span class="ab-pair">{{ fmtVal(pick(row.a, m.key)) }} → {{ fmtVal(pick(row.b, m.key)) }}</span>
              <span class="ab-delta" :class="deltaClass(row.diffs[m.key])">
                {{ deltaArrow(row.diffs[m.key]) }} {{ fmtDelta(row.diffs[m.key]) }}
              </span>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState
              :title="onlyDiff ? '没有超过阈值的差异样本' : '没有可对比的样本'"
              desc="未勾选「只看差异样本」时展示全部对齐结果"
              icon="🔍"
            />
          </template>
        </el-table>
        <div class="cmp-foot">
          Δ 阈值 {{ compareResult.diff_threshold }}：任一指标 |Δ| 超过该值即标记为「差异」样本。
          灰色显示的行表示该问题只在一侧出现（另一侧无得分）。
        </div>
      </template>
      <template #footer>
        <el-button @click="compareVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 【P1】新建数据集 -->
    <el-dialog v-model="dsDialogVisible" title="新建评测数据集" width="480px">
      <el-form label-position="top">
        <el-form-item label="数据集名称" required>
          <el-input v-model="dsForm.name" maxlength="128" show-word-limit placeholder="如：差旅制度回归集-30题" />
        </el-form-item>
        <el-form-item label="目标知识库" required>
          <el-select v-model="dsForm.kb_id" placeholder="选择该数据集评测哪个知识库" style="width: 100%">
            <el-option v-for="k in kbs" :key="k.id" :label="`#${k.id} ${k.name}（${k.doc_count} 文档）`" :value="k.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="dsForm.description" maxlength="512" placeholder="可选：这批样本覆盖什么场景" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dsDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="dsSaving" @click="doCreateDataset">创建并导入样本</el-button>
      </template>
    </el-dialog>

    <!-- 【P1】批量导入样本：JSON / CSV 粘贴 -->
    <el-dialog v-model="importVisible" title="批量导入评测样本" width="720px">
      <div class="import-head">
        <span class="import-target">
          目标：<b>{{ importTarget ? `#${importTarget.id} ${importTarget.name}` : '—' }}</b>
        </span>
        <span class="import-sum" :class="{ 'is-bad': importOutcome.errors.length }">{{ importSummary }}</span>
      </div>
      <div
        class="import-drop"
        :class="{ 'is-drag': importDragging }"
        @dragover.prevent="importDragging = true"
        @dragleave.prevent="importDragging = false"
        @drop.prevent="onImportDrop"
      >
        <el-input
          v-model="importText"
          type="textarea"
          :rows="12"
          resize="vertical"
          placeholder="把 .json / .csv 文件拖到这里，或直接粘贴文本（列：question, reference_answer, stratum 可选）"
        />
      </div>
      <input
        ref="importFileInput"
        class="import-file-input"
        type="file"
        accept=".json,.csv,.txt,application/json,text/csv,text/plain"
        @change="onImportFileChange"
      />
      <div class="import-foot">
        <el-button size="small" @click="pickImportFile">
          <el-icon><Upload /></el-icon>选择文件
        </el-button>
        <el-button link type="primary" size="small" @click="fillImportSample">填入示例</el-button>
        <span v-if="importFileName" class="import-file" :title="importFileName">已载入 {{ importFileName }}</span>
      </div>
      <div class="form-tip import-tip">
        支持 JSON 数组 / CSV（首行可带表头，中英文列名均可，UTF-8 与 GBK 自动识别）；单次上限
        {{ MAX_IMPORT_ITEMS }} 条；reference_answer 必须是人工确认过的标准答案，整列为空会被逐行拦下。
      </div>
      <el-alert
        v-if="importOutcome.errors.length"
        type="warning"
        :closable="false"
        show-icon
        title="以下问题需要先修正"
        style="margin-top: 10px"
      >
        <ul class="err-list">
          <li v-for="(msg, i) in importOutcome.errors.slice(0, 8)" :key="i">{{ msg }}</li>
          <li v-if="importOutcome.errors.length > 8">…另有 {{ importOutcome.errors.length - 8 }} 条问题</li>
        </ul>
      </el-alert>
      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" :loading="importing" :disabled="!canImport" @click="doImport">
          导入 {{ importOutcome.items.length }} 条
        </el-button>
      </template>
    </el-dialog>

    <!-- 数据集管理抽屉（v1.13）：补参考答案 / 删脏样本 / 删数据集 -->
    <el-drawer v-model="dsDrawerVisible" size="860px" :with-header="false">
      <div class="drawer-head">
        <div class="drawer-head-left">
          <el-button v-if="activeDataset" link @click="backToDatasets">
            <el-icon><ArrowLeft /></el-icon>返回数据集
          </el-button>
          <span class="drawer-title">{{ activeDataset ? activeDataset.name : '评测数据集' }}</span>
          <span class="drawer-sub">
            {{ activeDataset ? `共 ${itemTotal} 条样本` : `共 ${dsTotal} 个数据集` }}
          </span>
        </div>
        <el-button v-if="!activeDataset" type="primary" size="small" @click="openCreateDataset">
          <el-icon><Plus /></el-icon>新建数据集
        </el-button>
      </div>

      <!-- 一级：数据集列表 -->
      <template v-if="!activeDataset">
        <div class="ds-hint">
          参考答案是 RAGAS 的评分基准：<b>缺答案的样本无法计算 context_recall</b>。
          点「查看样本」补全后再跑评测；点踩沉淀的 badcase 默认都是待补状态。
        </div>
        <el-table v-loading="dsLoading" :data="dsRows" size="small" border style="width: 100%">
          <el-table-column prop="id" label="ID" width="64" />
          <el-table-column prop="name" label="数据集" min-width="220" show-overflow-tooltip />
          <el-table-column prop="kb_id" label="KB" width="64" />
          <el-table-column label="样本" width="80">
            <template #default="{ row }">{{ row.item_count }}</template>
          </el-table-column>
          <el-table-column label="参考答案" width="120">
            <template #default="{ row }">
              <span class="miss-tag" :class="row.missing_reference ? 'miss-warn' : 'miss-ok'">
                {{ missLabel(row.missing_reference) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="190" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openItems(row)">查看样本</el-button>
              <el-button link type="primary" size="small" @click="openImport(row)">导入</el-button>
              <el-button link type="danger" size="small" @click="onDeleteDataset(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState title="暂无评测数据集" desc="新建数据集后导入样本，或在对话页点踩自动沉淀 badcase" icon="📚" />
          </template>
        </el-table>
        <PagePagination
          :total="dsTotal"
          :page="dsPage"
          :size="dsSize"
          @update:page="(p) => { dsPage = p; loadDatasetRows() }"
          @update:size="(s) => { dsSize = s; dsPage = 1; loadDatasetRows() }"
        />
      </template>

      <!-- 二级：样本列表 -->
      <template v-else>
        <el-table v-loading="itemsLoading" :data="dsItems" size="small" border style="width: 100%">
          <el-table-column prop="id" label="ID" width="64" />
          <el-table-column prop="question" label="问题" min-width="200" show-overflow-tooltip />
          <el-table-column label="参考答案" min-width="240">
            <template #default="{ row }">
              <span v-if="hasAnswer(row.reference_answer)" class="ans-text">{{ row.reference_answer }}</span>
              <span v-else class="ans-missing">未填写（context_recall 不可算）</span>
            </template>
          </el-table-column>
          <el-table-column label="分层" width="90">
            <template #default="{ row }">{{ row.stratum || '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
              <el-button link type="danger" size="small" @click="onDeleteItem(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState title="该数据集暂无样本" desc="badcase 集在对话页点踩后自动追加样本" icon="📝" />
          </template>
        </el-table>
        <PagePagination
          :total="itemTotal"
          :page="itemPage"
          :size="itemSize"
          @update:page="(p) => { itemPage = p; loadItems() }"
          @update:size="(s) => { itemSize = s; itemPage = 1; loadItems() }"
        />
      </template>
    </el-drawer>

    <!-- 编辑样本：只改参考答案 / 分层标签，问题不可改（改了等于换了一道题） -->
    <el-dialog v-model="editVisible" title="编辑评测样本" width="620px">
      <el-form label-width="96px" label-position="left">
        <el-form-item label="问题">
          <div class="ro-text">#{{ editForm.id }}　{{ editForm.question }}</div>
        </el-form-item>
        <el-form-item label="参考答案" required>
          <el-input
            v-model="editForm.reference_answer"
            type="textarea"
            :rows="7"
            maxlength="4000"
            show-word-limit
            placeholder="填写标准答案。RAGAS 用它计算 context_recall，留空则该指标无法计算。"
          />
        </el-form-item>
        <el-form-item label="分层标签">
          <el-input v-model="editForm.stratum" placeholder="可选，如 easy / hard / 业务分层名" style="width: 240px" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="doSaveItem">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.detail-pad { padding: 16px 20px; }
:deep(.time-col .cell) { white-space: nowrap; }

/* ---------------------------------------------------------------------------
   大盘 6 格（v1.13.2）：2 个运行态 + 4 个 RAGAS 指标。
   全局 .stat-grid 是 4 列（TraceLog 等页面在用），这里只叠加不覆写。
   用 .stat-grid.stat-grid-6 提高特异性，避免被全局规则压回去。
   --------------------------------------------------------------------------- */
.stat-grid.stat-grid-6 { grid-template-columns: repeat(6, minmax(0, 1fr)); }
/* 窄屏退化成 3 列 × 2 行，避免 4 位小数的数值被挤到换行 */
@media (max-width: 1440px) {
  .stat-grid.stat-grid-6 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
/* 副标：口径说明（完成/失败/停止、基于多少条样本），弱化但可读 */
.stat-sub {
  margin-top: 4px; font-size: 11px; color: var(--text-3, #909399);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
/* 空值降级：null 显示灰色「—」，与真实数值在视觉上明确区分 */
.stat-value.is-empty { color: var(--text-3, #909399); font-weight: 600; }
/* 指标口径提示问号：hover 出 tooltip，不占布局（inline，不影响 6 列网格） */
.info-dot {
  display: inline-flex; align-items: center; justify-content: center;
  width: 13px; height: 13px; margin-left: 4px; vertical-align: middle;
  border: 1px solid var(--border-2, #dcdfe6); border-radius: 50%;
  font-size: 9px; line-height: 1; color: var(--text-3, #909399);
  cursor: help;
}

/* run 展开区的汇总条（A/B 对照口径） */
.run-summary {
  display: flex; flex-wrap: wrap; gap: 8px 18px; margin-bottom: 12px;
  padding: 10px 12px; border-radius: 6px; background: #fafbfc; border: 1px solid var(--border-2, #ebeef5);
}
.sum-item { font-size: 12px; color: var(--text-2, #606266); }
.sum-item em { display: inline-block; margin-right: 6px; font-style: normal; color: var(--text-3, #909399); }
.param-tag { font-size: 12px; padding: 1px 8px; border-radius: 10px; background: #eef4ff; color: #2b5fbf; white-space: nowrap; }
.param-tag.param-default { background: #f4f4f5; color: #909399; }

/* 【v1.13.1】进度列：进度条 + 计数 + 失败/停止标注 */
.prog-cell { display: flex; flex-direction: column; gap: 4px; padding-right: 8px; }
.prog-text { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-3, #909399); }
.prog-text .prog-active { color: var(--primary, #165dff); font-weight: 600; }
.prog-text .prog-fail { color: var(--danger, #f53f3f); }
.prog-text .prog-muted { color: var(--text-3, #909399); }
.prog-cell :deep(.el-progress) { margin-bottom: 0; }
.partial-alert { margin-bottom: 10px; }
.partial-alert :deep(.el-alert__title) { font-size: 12px; line-height: 1.7; font-weight: 400; }

/* 数据集管理抽屉 */
.drawer-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 20px 24px 12px; border-bottom: 1px solid var(--border-2, #ebeef5);
}
.drawer-head-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.drawer-title { font-size: 16px; font-weight: 600; color: var(--text-1, #303133); }
.drawer-sub { font-size: 12px; color: var(--text-3, #909399); }
.ds-hint {
  margin: 12px 24px; padding: 10px 12px; border-radius: 6px;
  background: #f4f8ff; border: 1px solid #d9e7ff;
  font-size: 12px; line-height: 1.7; color: var(--text-2, #606266);
}
.miss-tag { font-size: 12px; padding: 1px 8px; border-radius: 10px; white-space: nowrap; }
.miss-warn { background: #fff4e6; color: #d46b08; border: 1px solid #ffd8a8; }
.miss-ok { background: #eefaf0; color: #2f9e44; border: 1px solid #c3f0ca; }
.ans-text {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; font-size: 12px; color: var(--text-2, #606266); line-height: 1.6;
}
.ans-missing { font-size: 12px; color: #d46b08; }
.ro-text { font-size: 13px; line-height: 1.7; color: var(--text-1, #303133); word-break: break-all; }
:deep(.el-drawer__body) { padding: 0 24px 20px; overflow-y: auto; }

/* 批量导入弹窗 */
.form-tip { margin-left: 8px; font-size: 12px; color: var(--text-3, #909399); line-height: 1.6; }
.import-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px; font-size: 13px; color: var(--text-2, #606266);
}
.import-target b { color: var(--text-1, #303133); }
.import-sum { font-size: 12px; color: #2f9e44; }
.import-sum.is-bad { color: #d46b08; }
.import-foot { display: flex; align-items: flex-start; gap: 8px; margin-top: 8px; }
.import-foot .form-tip { margin-left: 0; }
.import-drop { border-radius: 4px; }
.import-drop.is-drag { outline: 2px dashed #409eff; outline-offset: 2px; }
.import-file-input { display: none; }
.import-file {
  align-self: center; max-width: 280px; font-size: 12px; color: var(--text-3, #909399);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.import-tip { display: block; margin: 6px 0 0; }
.err-list { margin: 4px 0 0; padding-left: 18px; font-size: 12px; line-height: 1.8; }

/* 批量执行弹窗 */
.batch-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px; font-size: 13px; color: var(--text-2, #606266);
}
.batch-cost { font-size: 12px; color: var(--text-3, #909399); }
.batch-tag { font-size: 12px; }
.batch-tag.is-warn { color: #d46b08; }
.batch-tag.is-ok { color: #2f9e44; }
.batch-foot-tip { display: flex; align-items: baseline; gap: 6px; margin-top: 6px; }

/* ---------------- A/B 对比报告 ---------------- */
/* 配色约定：提升=绿、下降=红（本项目 green=好/red=坏，与状态标签一致）。
   注意这与「股市红涨绿跌」相反，是刻意选的语义色而非行情色。 */
.cmp-state { padding: 40px 0; text-align: center; font-size: 13px; color: var(--text-3, #909399); }
.cmp-state.cmp-error { color: #e03131; }

.cmp-head {
  display: grid; grid-template-columns: 1fr 48px 1fr; align-items: center; gap: 8px;
  padding: 12px 14px; border: 1px solid var(--border-2, #ebeef5); border-radius: 8px; background: #fafbfc;
}
.cmp-side { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
.cmp-badge {
  flex: none; width: 22px; height: 22px; border-radius: 50%; font-size: 12px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center; color: #fff;
}
.cmp-badge-a { background: #4c6ef5; }
.cmp-badge-b { background: #f08c00; }
.cmp-info { min-width: 0; }
.cmp-line { font-size: 12px; line-height: 1.9; color: var(--text-2, #606266); }
.cmp-param { color: var(--text-3, #909399); }
.cmp-vs { text-align: center; font-size: 12px; font-weight: 700; color: var(--text-3, #909399); }

.cmp-conclusion {
  margin: 12px 0; padding: 10px 12px; border-radius: 6px;
  background: #f4f8ff; border: 1px solid #d9e7ff;
  font-size: 13px; line-height: 1.7; color: var(--text-2, #606266);
}

.cmp-metrics { border: 1px solid var(--border-2, #ebeef5); border-radius: 8px; overflow: hidden; }
.cmp-mrow {
  display: grid; grid-template-columns: 1fr 120px 120px 140px;
  align-items: center; padding: 10px 14px; font-size: 13px;
  border-bottom: 1px solid var(--border-2, #ebeef5);
}
.cmp-mrow:last-child { border-bottom: none; }
.cmp-mhead { background: #fafbfc; font-size: 12px; color: var(--text-3, #909399); font-weight: 600; }
.cmp-mrow .c-name { color: var(--text-1, #303133); }
.cmp-mrow .c-desc { display: block; font-style: normal; font-size: 11px; color: var(--text-3, #909399); }
.cmp-mrow .c-val { font-variant-numeric: tabular-nums; color: var(--text-2, #606266); }
.cmp-mrow .c-delta { font-variant-numeric: tabular-nums; font-weight: 600; }

.d-up { color: #2f9e44; }
.d-down { color: #e03131; }
.d-flat { color: var(--text-3, #909399); }

.cmp-rows-head { display: flex; align-items: center; justify-content: space-between; margin: 16px 0 8px; font-size: 13px; }
.diff-chip {
  display: inline-block; margin-right: 6px; padding: 0 6px; border-radius: 8px;
  background: #fff4e6; border: 1px solid #ffd8a8; color: #d46b08; font-size: 11px;
}
.q-dim { color: var(--text-3, #909399); }
.ab-pair { font-size: 12px; color: var(--text-2, #606266); font-variant-numeric: tabular-nums; }
.ab-delta { margin-left: 6px; font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
.cmp-foot { margin-top: 8px; font-size: 12px; line-height: 1.7; color: var(--text-3, #909399); }
</style>
