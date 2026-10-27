<script setup lang="ts">
/**
 * P02 工作台大盘。
 *
 * 接入策略（W6 文档 §4 映射表 + 对接清单 G1/G2）——**直连 + 异常回落**：
 *  - 指标卡：`GET /dashboards/summary`（W6 §2.2，字段与五卡一一对应）；
 *    请求失败时：待审批数量继续用 `GET /approvals` 的 total，其余 4 卡显示「—」占位。
 *  - 工具分布：`GET /dashboards/tools`（W6 §2.3，task_executions 聚合）；失败回落 StateBlock。
 *  - 任务列表：`GET /tasks` 分页列表（W6 §1，viewer 由服务端强制本人 + 脱敏 creator_name）；
 *    失败回落「本机最近访问」（localStorage + 逐条 GET /tasks/{id} 刷新，真实数据非 mock）。
 *  - 任务完成趋势：`GET /dashboards/trend`（缺口 G4，按天聚合 + 空桶补齐），
 *    折线只画后端返回的 task_total / completed，完成率在悬停数值卡中展示。
 *  - 环比对比：追加拉取**昨日窗口**的 summary（同一接口不同时段），只做展示对比，
 *    失败即不显示环比，不影响任何主功能；颜色语义照原型（绿=好 / 红=差）。
 *
 * 所有回落均为显式占位，不填充任何模拟数据。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Message, Modal } from '@arco-design/web-vue'
import { useAuthStore } from '@/stores/auth'
import { useApprovalStore } from '@/stores/approval'
import { archiveTask, getTaskDetail, listTasks } from '@/api/tasks'
import { getDashboardSummary, getDashboardTools, getDashboardTrend, getDashboardFailureReasons } from '@/api/dashboards'
import { isMissingEndpoint } from '@/api/http'
import { readRecentTasks, clearRecentTasks, type RecentTask } from '@/utils/recentTasks'
import { formatDuration, formatShortTime, formatTokens, formatPercent } from '@/utils/format'
import { TASK_STATUS_META } from '@/utils/status'
import TaskStatusTag from '@/components/TaskStatusTag.vue'
import StateBlock from '@/components/StateBlock.vue'
import TrendChart from '@/components/TrendChart.vue'
import DonutChart from '@/components/DonutChart.vue'
import type { DashboardSummary, ToolUsageItem, TrendPoint, DashboardFailures } from '@/types/dashboard'
import type { TaskListItem } from '@/types/task'
import type { TaskStatus } from '@/types/enums'

const router = useRouter()
const auth = useAuthStore()
const approvals = useApprovalStore()

const loading = ref(false)
const today = new Date()

const pad2 = (n: number): string => String(n).padStart(2, '0')

// ---------------------------------------------------------------------------
// W6 接入状态：true = 后端接口已上线并返回成功；false = 404 回落占位
// ---------------------------------------------------------------------------
const summaryOn = ref(false)
const toolsOn = ref(false)
const listOn = ref(false)

// ---------------------------------------------------------------------------
// 指标卡：优先 W6 summary，回落旧逻辑
// ---------------------------------------------------------------------------
const summary = ref<DashboardSummary | null>(null)

const greeting = computed(() => {
  const hour = today.getHours()
  if (hour < 6) return '凌晨好'
  if (hour < 12) return '上午好'
  if (hour < 14) return '中午好'
  if (hour < 18) return '下午好'
  return '晚上好'
})

/** 今日窗口（本地时区零点 → 24 点），与 W6 summary 的 start/end 参数一致 */
function todayWindow(): { start: string; end: string } {
  const day = `${today.getFullYear()}-${pad2(today.getMonth() + 1)}-${pad2(today.getDate())}`
  return { start: `${day}T00:00:00`, end: `${day}T23:59:59` }
}

/** 近 N 天窗口（含今日，本地时区），供趋势卡使用 */
function recentWindow(days: number): { start: string; end: string } {
  const from = new Date(today.getFullYear(), today.getMonth(), today.getDate() - (days - 1))
  const to = today
  const fmt = (d: Date): string => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
  return { start: `${fmt(from)}T00:00:00`, end: `${fmt(to)}T23:59:59` }
}

/** 秒数 → 紧凑可读文本（「平均端到端耗时」卡用；服务端已算好 duration） */
function formatSeconds(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value < 60) return `${Math.round(value)}s`
  const m = Math.floor(value / 60)
  const s = Math.round(value % 60)
  return `${m}m${s.toString().padStart(2, '0')}s`
}

// ---------------------------------------------------------------------------
// 环比（较昨日）：额外拉一次昨日窗口 summary，仅用于对比展示，失败即静默不显示
// ---------------------------------------------------------------------------
const summaryYesterday = ref<DashboardSummary | null>(null)

/** 昨日窗口（本地时区） */
function yesterdayWindow(): { start: string; end: string } {
  const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1)
  const day = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
  return { start: `${day}T00:00:00`, end: `${day}T23:59:59` }
}

interface Delta {
  /** 「↑ 8.6%」前缀箭头文本 */
  text: string
  /** good=利好（绿）/ bad=利空（红）/ flat=持平（灰），语义照原型而非方向 */
  tone: 'good' | 'bad' | 'flat'
  /** 「较昨日」 */
  hint: string
}

/** 数值差 → 环比文案；goodWhen='down' 表示下降是利好（如耗时、Token 消耗） */
function deltaOf(
  cur: number | null | undefined,
  prev: number | null | undefined,
  format: (v: number) => string,
  goodWhen: 'up' | 'down' = 'up',
  opts: { pct?: boolean; absFormat?: (v: number) => string; flatEps?: number } = {},
): Delta | null {
  const { pct = false, absFormat, flatEps = 0.5 } = opts
  if (cur == null || prev == null) return null
  const diff = cur - prev
  const up = diff > 0
  const toneOf = (isUp: boolean): Delta['tone'] => (isUp === (goodWhen === 'up') ? 'good' : 'bad')
  if (pct) {
    if (prev === 0) {
      // 昨日为 0 时百分比无意义 → 退化为绝对增量（如「↑ 130.7K 较昨日」）
      if (diff === 0) return { text: '— 持平', tone: 'flat', hint: '较昨日' }
      const render = absFormat ?? format
      return { text: `${up ? '↑' : '↓'} ${render(Math.abs(diff))}`, tone: toneOf(up), hint: '较昨日' }
    }
    const rate = (diff / prev) * 100
    if (Math.abs(rate) < 0.05) return { text: '— 持平', tone: 'flat', hint: '较昨日' }
    return {
      text: `${up ? '↑' : '↓'} ${format(Math.abs(rate))}`,
      tone: toneOf(up),
      hint: '较昨日',
    }
  }
  if (Math.abs(diff) < flatEps) return { text: '— 持平', tone: 'flat', hint: '较昨日' }
  return {
    text: `${up ? '↑' : '↓'} ${format(Math.abs(diff))}`,
    tone: toneOf(up),
    hint: '较昨日',
  }
}

/** 大数字 + 单位分排（原型 `1.24<small>M</small>`）：拆出尾部 K/M/% 等单位 */
function splitUnit(text: string): { value: string; unit: string } {
  const m = /^(.+?)([KM%]+)$/.exec(text)
  return m ? { value: m[1], unit: m[2] } : { value: text, unit: '' }
}

const kpis = computed(() => {
  const s = summary.value
  const y = summaryYesterday.value
  const total = s ? (s.token_usage_total == null ? '已脱敏' : formatTokens(s.token_usage_total)) : '—'
  const rate = s ? formatPercent(s.completion_rate) : '—'
  return [
    {
      title: '今日任务总量',
      ...splitUnit(s ? String(s.task_total) : '—'),
      delta: s ? deltaOf(s.task_total, y?.task_total, (v) => String(Math.round(v))) : null,
      footer: s ? '按创建时间统计' : '接口待接入（G2）',
      tone: s ? ('normal' as const) : ('pending' as const),
      clickable: false,
    },
    {
      title: '任务完成率',
      ...splitUnit(rate),
      delta: s
        ? deltaOf(s.completion_rate, y?.completion_rate, (v) => `${(v * 100).toFixed(1)}pt`, 'up', {
            flatEps: 0.005, // 0~1 小数：0.5pt 以内视为持平
          })
        : null,
      footer: s ? '已完成 / 终态任务' : '接口待接入（G2）',
      tone: s ? ('normal' as const) : ('pending' as const),
      clickable: false,
    },
    {
      title: '待审批数量',
      ...splitUnit(String(s ? s.pending_approvals : approvals.total)),
      delta: null,
      footer: (s ? s.pending_approvals : approvals.total) > 0 ? '需处理 · 去收件箱' : '暂无待办',
      tone: (s ? s.pending_approvals : approvals.total) > 0 ? ('warn' as const) : ('normal' as const),
      clickable: true,
    },
    {
      title: '平均端到端耗时',
      ...splitUnit(s ? formatSeconds(s.avg_duration_s) : '—'),
      delta: s ? deltaOf(s.avg_duration_s, y?.avg_duration_s, formatSeconds, 'down') : null,
      footer: s ? '窗口内终态任务均值' : '接口待接入（G2）',
      tone: s ? ('normal' as const) : ('pending' as const),
      clickable: false,
    },
    {
      title: '总 Token 消耗',
      ...splitUnit(total),
      delta: s
        ? deltaOf(s.token_usage_total, y?.token_usage_total, (v) => `${v.toFixed(1)}%`, 'down', {
            pct: true,
            absFormat: formatTokens, // 昨日为 0 时退化为绝对增量
          })
        : null,
      footer: s ? (s.token_usage_total == null ? '当前角色不可见' : '窗口内任务累计') : '接口待接入（G2）',
      tone: s ? ('normal' as const) : ('pending' as const),
      clickable: false,
    },
  ]
})

async function loadSummary(): Promise<void> {
  try {
    const { start, end } = todayWindow()
    summary.value = await getDashboardSummary(start, end)
    summaryOn.value = true
  } catch (error) {
    if (isMissingEndpoint(error)) {
      summaryOn.value = false // 接口不可用 → 保持占位（KPI 卡的「—」与脚注）
    } else {
      // 路由存在但请求失败（500 等）：提示但不阻断页面
      Message.error(`大盘指标加载失败：${error instanceof Error ? error.message : String(error)}`)
    }
    return
  }
  // 环比补充请求：失败静默（对比行不显示即可），不打扰主流程
  try {
    const { start, end } = yesterdayWindow()
    summaryYesterday.value = await getDashboardSummary(start, end)
  } catch {
    summaryYesterday.value = null
  }
}

// ---------------------------------------------------------------------------
// 工具调用分布：优先 W6 tools，回落 StateBlock；渲染为环形占比图（原型 P02）
// ---------------------------------------------------------------------------
const tools = ref<ToolUsageItem[]>([])

/** 环形图数据：占比按 call_count；副行保留次数与成功率（原有信息不丢） */
const toolDonutItems = computed(() =>
  tools.value.map((tool) => ({
    name: tool.tool_name,
    value: tool.call_count,
    sub: `${tool.call_count} 次 · 成功率 ${formatPercent(tool.success_rate)}`,
  })),
)

const toolTotal = computed(() => tools.value.reduce((sum, tool) => sum + tool.call_count, 0))

/** 状态分布 chips（summary.status_breakdown，六键恒齐）——照原型过滤条的计数 chips */
const STATUS_CHIP_ORDER: TaskStatus[] = [
  'COMPLETED',
  'RUNNING',
  'PLANNING',
  'SUMMARIZING',
  'AWAITING_APPROVAL',
  'FAILED',
  'CANCELLED',
]

const statusChips = computed(() => {
  const breakdown = summary.value?.status_breakdown
  if (!breakdown) return []
  return STATUS_CHIP_ORDER.map((key) => ({
    key,
    text: TASK_STATUS_META[key].text,
    count: breakdown[key] ?? 0,
    tone: TASK_STATUS_META[key].color,
    highlight: key === 'AWAITING_APPROVAL' && (breakdown[key] ?? 0) > 0,
  })).filter((chip) => chip.count > 0)
})

async function loadTools(): Promise<void> {
  try {
    const { start, end } = todayWindow()
    const data = await getDashboardTools(start, end)
    tools.value = data.items
    toolsOn.value = true
  } catch (error) {
    toolsOn.value = false // 404 回落占位
    if (!isMissingEndpoint(error)) console.error('[dashboard] 工具分布加载失败', error)
  }
}

// ---------------------------------------------------------------------------
// 失败原因 Top 5：tasks.error.code 聚合（原型 P02）；缺码归入 UNKNOWN
// ---------------------------------------------------------------------------
const failuresOn = ref(false)
const failureData = ref<DashboardFailures | null>(null)

/** 条形宽度按 max 归一（原型同款：最长条 100%） */
const failureBars = computed(() => {
  const items = failureData.value?.items ?? []
  const max = Math.max(...items.map((item) => item.count), 1)
  return items.map((item) => ({
    ...item,
    widthPct: Math.max((item.count / max) * 100, 6),
  }))
})

async function loadFailures(): Promise<void> {
  try {
    const { start, end } = todayWindow()
    failureData.value = await getDashboardFailureReasons(start, end)
    failuresOn.value = true
  } catch (error) {
    failuresOn.value = false // 404 回落占位
    if (!isMissingEndpoint(error)) console.error('[dashboard] 失败原因加载失败', error)
  }
}

// ---------------------------------------------------------------------------
// 任务完成趋势：GET /dashboards/trend（缺口 G4，按天聚合；窗口内空桶由后端补齐）
// ---------------------------------------------------------------------------
const trendOn = ref(false)
const trendLoading = ref(false)
const trendPoints = ref<TrendPoint[]>([])
const trendWindow = ref<{ start: string; end: string } | null>(null)

/** 卡头区间文案：接口返回窗口优先，未就绪时退回「近 7 天」 */
const trendRangeText = computed(() => {
  const win = trendWindow.value
  if (!win) return '近 7 天'
  return `${win.start.slice(5, 10)} ~ ${win.end.slice(5, 10)}`
})

/** 窗口内合计（直接累加后端返回的点，不做推算） */
const trendTotals = computed(() => ({
  total: trendPoints.value.reduce((sum, p) => sum + p.task_total, 0),
  done: trendPoints.value.reduce((sum, p) => sum + p.completed, 0),
}))

/** 全部为 0 时不画一条贴底折线，改走空状态说明 */
const trendAllZero = computed(() => trendPoints.value.every((p) => p.task_total === 0 && p.completed === 0))

async function loadTrend(): Promise<void> {
  trendLoading.value = true
  try {
    const { start, end } = recentWindow(7)
    const data = await getDashboardTrend(start, end)
    trendPoints.value = data.points
    trendWindow.value = data.window
    trendOn.value = true
  } catch (error) {
    trendOn.value = false // 404 回落占位
    if (!isMissingEndpoint(error)) console.error('[dashboard] 任务完成趋势加载失败', error)
  } finally {
    trendLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// 任务列表：优先 GET /tasks（W6 §1），404 回落「本机最近访问」
// ---------------------------------------------------------------------------
const listPage = ref(1)
const listSize = 10
const listTotal = ref(0)
const taskRows = ref<TaskListItem[]>([])
/** 列表口径：active=全部任务（默认，排除已归档）；archived=只看已归档 */
const listFilter = ref<'active' | 'archived'>('active')
/** 行级归档中的任务 id（避免连点，也让按钮有自己的 loading） */
const archivingId = ref<number | null>(null)

/** 回落路径：本机最近访问 + 逐条详情刷新（真实数据，非 mock） */
const recent = ref<RecentTask[]>([])
/** 回落路径的详情行补充字段：耗时与 token（来自 GET /tasks/{id}） */
const detailMap = ref<Record<number, { createdAt: string | null; finishedAt: string | null; tokenUsage: number }>>({})

/** 终态集合：只有终态任务能归档（与后端 ARCHIVABLE_STATUSES 保持一致） */
const TERMINAL_STATUSES = ['COMPLETED', 'FAILED', 'CANCELLED']

/**
 * 是否可以归档。
 *
 * 刻意与详情页权限不对齐：`GET /tasks/{id}` 对 ADMIN 是"仅本人"，那是**读隐私**防线；
 * 归档是**全局治理动作** —— 管理员在「全部任务」里看得到所有人的任务，按钮却点不了，
 * 这个能力就是摆设。归档不读正文、不改数据、可随时取消，所以 ADMIN / APPROVER 都不限。
 */
function canArchive(record: TaskListItem): boolean {
  if (!TERMINAL_STATUSES.includes(String(record.status))) return false
  return auth.isAdmin || auth.isApprover
}

async function handleArchive(record: TaskListItem): Promise<void> {
  const archiving = !record.archived_at
  const action = archiving ? '归档' : '取消归档'
  Modal.confirm({
    title: `${action}任务 ${record.task_no}？`,
    content: archiving
      ? '归档后该任务从「全部任务」列表隐藏，任务数据、审批单、轨迹与产物全部保留，可随时取消归档。'
      : '取消归档后该任务会重新出现在「全部任务」列表中。',
    okText: action,
    cancelText: '取消',
    onOk: async () => {
      archivingId.value = record.task_id
      try {
        await archiveTask(record.task_id, archiving)
        Message.success(`${action}成功`)
        await loadTaskList()
      } catch (error) {
        // 409 TASK_STATE_CONFLICT：任务在列表刷新的间隙又开始跑了
        Message.error(error instanceof Error ? error.message : `${action}失败，请稍后重试`)
      } finally {
        archivingId.value = null
      }
    },
  })
}

function changeListFilter(value: string | number | boolean): void {
  listFilter.value = value === 'archived' ? 'archived' : 'active'
  listPage.value = 1
  void loadTaskList()
}

async function loadTaskList(): Promise<void> {
  loading.value = true
  try {
    const data = await listTasks({
      page: listPage.value,
      size: listSize,
      // 默认口径不传 archived（后端默认排除归档）；只看归档时传 true
      ...(listFilter.value === 'archived' ? { archived: true } : {}),
    })
    taskRows.value = data.items
    listTotal.value = data.meta.total
    listOn.value = true
  } catch (error) {
    if (isMissingEndpoint(error)) {
      listOn.value = false
      await loadRecent() // 回落：本机最近访问
    } else {
      console.error('[dashboard] 任务列表加载失败', error)
      Message.error('任务列表加载失败，请稍后重试')
    }
  } finally {
    loading.value = false
  }
}

function changeListPage(page: number): void {
  listPage.value = page
  loadTaskList()
}

/** 回落路径：拉取本机最近任务的真实状态 */
async function loadRecent(): Promise<void> {
  recent.value = readRecentTasks()
  if (recent.value.length === 0) return
  const results = await Promise.allSettled(recent.value.map((item) => getTaskDetail(item.taskId)))
  results.forEach((result, index) => {
    const item = recent.value[index]
    if (result.status !== 'fulfilled') return
    const detail = result.value
    item.status = detail.status
    item.goal = detail.goal
    detailMap.value[detail.task_id] = {
      createdAt: detail.created_at,
      finishedAt: detail.finished_at,
      tokenUsage: detail.token_usage,
    }
  })
}

function openTask(taskId: number): void {
  router.push(`/tasks/${taskId}`)
}

function clearRecent(): void {
  clearRecentTasks()
  recent.value = []
  detailMap.value = {}
  Message.success('已清空本机访问记录')
}

function goCreate(): void {
  if (!auth.isAdmin) {
    Message.warning('仅管理员可创建任务')
    return
  }
  router.push('/tasks/new')
}

function goApprovals(): void {
  router.push('/approvals')
}

/** 刷新全部数据源（W6 四路 + 失败原因 + 审批计数）；失败互不影响 */
function refreshAll(): void {
  approvals.refresh()
  loadSummary()
  loadTools()
  loadTrend()
  loadFailures()
  loadTaskList()
}

onMounted(() => {
  refreshAll()
})
</script>

<template>
  <div class="af-page">
    <div class="af-page-head">
      <div>
        <h1>工作台</h1>
        <div class="af-sub">
          {{ greeting }}，{{ auth.displayName }} · {{ today.toLocaleDateString('zh-CN') }} ·
          当前角色 {{ auth.roleText }}
        </div>
      </div>
      <a-space>
        <a-button @click="refreshAll">刷新</a-button>
        <a-button v-if="auth.isAdmin" type="primary" @click="goCreate">+ 新建任务</a-button>
      </a-space>
    </div>

    <!-- 指标卡：W6 summary 就绪时为真值，否则占位；delta 行为较昨日环比（失败静默） -->
    <div class="kpis">
      <a-card v-for="kpi in kpis" :key="kpi.title" class="kpi" :class="`is-${kpi.tone}`" :bordered="true">
        <div class="kpi-title">{{ kpi.title }}</div>
        <div class="kpi-value af-num">
          {{ kpi.value }}<small v-if="kpi.unit">{{ kpi.unit }}</small>
        </div>
        <div class="kpi-foot">
          <span v-if="kpi.delta" class="kpi-delta" :class="`is-${kpi.delta.tone}`">
            {{ kpi.delta.text }}<em>{{ kpi.delta.hint }}</em>
          </span>
          <span
            v-else
            class="kpi-note"
            :class="{ 'is-link': kpi.clickable }"
            @click="kpi.clickable && goApprovals()"
          >{{ kpi.footer }}</span>
        </div>
      </a-card>
    </div>

    <!-- 状态分布 chips（summary.status_breakdown，窗口内真实计数） -->
    <div v-if="statusChips.length" class="status-chips">
      <span v-for="chip in statusChips" :key="chip.key" class="st-chip" :class="{ 'is-hi': chip.highlight }">
        {{ chip.text }} <b class="af-num">{{ chip.count }}</b>
      </span>
    </div>

    <a-row :gutter="12" class="dash-mid">
      <a-col :span="10">
        <a-card class="af-card" :bordered="true">
          <template #title>任务完成趋势</template>
          <template #extra><span class="af-muted af-num">{{ trendRangeText }}</span></template>

          <!-- 首次加载：骨架占位（不闪空图） -->
          <StateBlock v-if="trendLoading && !trendOn" state="loading" :min-height="228" />

          <!-- 接口不可用：显式占位，不伪造数据 -->
          <StateBlock
            v-else-if="!trendOn"
            state="pending"
            description="趋势折线依赖按天聚合的任务量接口（缺口 G4），当前请求未成功。"
            expected-api="GET /dashboards/trend?start&end"
            :min-height="228"
          />

          <!-- 窗口内确实没有任务：空状态（比画一条贴底直线更诚实） -->
          <StateBlock
            v-else-if="trendAllZero"
            state="empty"
            title="近 7 天暂无任务"
            description="窗口内没有任务记录；产生任务后，折线与完成率会自动出现在这里。"
            :min-height="228"
          />

          <!-- 就绪：真实折线（新建任务 / 已完成，完成率见悬停卡） -->
          <div v-else class="trend-body">
            <div class="trend-meta">
              <div class="trend-legend">
                <span class="lgd"><i class="lgd-dot is-total" />新建任务</span>
                <span class="lgd"><i class="lgd-dot is-done" />已完成</span>
              </div>
              <span class="af-muted af-num">
                新建 {{ trendTotals.total }} · 完成 {{ trendTotals.done }}
              </span>
            </div>
            <TrendChart :points="trendPoints" :height="228" />
          </div>
        </a-card>
      </a-col>
      <a-col :span="7">
        <a-card class="af-card" :bordered="true">
          <template #title>工具调用分布</template>
          <template #extra><span class="af-muted af-mono">tool.result 口径</span></template>

          <!-- 接口不可用或窗口内无数据：占位 -->
          <StateBlock
            v-if="!toolsOn"
            state="pending"
            description="工具维度统计依赖 task_executions 聚合接口（W6 §2.3 已定稿）。"
            expected-api="GET /dashboards/tools?start&end"
            :min-height="200"
          />

          <!-- W6 就绪：环形占比图 + 图例（不引入图表库，纯 SVG） -->
          <div v-else class="tool-donut">
            <div v-if="tools.length === 0" class="af-muted" style="padding: 24px 0; text-align: center">
              窗口内暂无工具调用记录
            </div>
            <DonutChart
              v-else
              :items="toolDonutItems"
              :center-value="String(toolTotal)"
              center-label="次工具调用"
            />
          </div>
        </a-card>
      </a-col>
      <a-col :span="7">
        <a-card class="af-card" :bordered="true">
          <template #title>失败原因 Top 5</template>
          <template #extra><span class="af-muted af-mono">tasks.error.code</span></template>

          <!-- 接口不可用：占位，不伪造数据 -->
          <StateBlock
            v-if="!failuresOn"
            state="pending"
            description="失败归因依赖 tasks.error.code 聚合接口。"
            expected-api="GET /dashboards/failure-reasons?start&end"
            :min-height="200"
          />

          <div v-else class="fail-list">
            <div v-if="failureBars.length === 0" class="af-muted fail-empty">
              窗口内没有失败任务
            </div>
            <div v-for="bar in failureBars" :key="bar.reason_code" class="fail-row">
              <a-tooltip :content="bar.sample_message || bar.reason_code" position="top">
                <span class="fail-code af-mono">{{ bar.reason_code }}</span>
              </a-tooltip>
              <div class="fail-bar-wrap">
                <div class="fail-bar" :style="{ width: `${bar.widthPct}%` }" />
              </div>
              <span class="fail-count af-num">{{ bar.count }}</span>
            </div>
            <div class="fail-foot af-muted">
              窗口内失败 {{ failureData?.failed_total ?? 0 }} 个 · 缺错误码归入 UNKNOWN
            </div>
          </div>
        </a-card>
      </a-col>
    </a-row>

    <!-- 任务列表：W6 GET /tasks 就绪 → 服务端分页真数据；404 → 本机最近访问（同样真实） -->
    <a-card class="af-card af-table-card" :bordered="true">
      <template #title>
        <template v-if="listOn">{{ listFilter === 'archived' ? '已归档任务' : '全部任务' }}</template>
        <template v-else>
          最近访问的任务
          <a-tooltip>
            <template #content>
              未能获取 <span class="af-mono">GET /tasks</span> 分页列表（接口异常或角色受限），
              此处仅展示本机打开过的任务，并用 <span class="af-mono">GET /tasks/{{ '{id}' }}</span> 刷新状态。
            </template>
            <icon-info-circle class="af-muted" />
          </a-tooltip>
        </template>
      </template>
      <template #extra>
        <a-space>
          <!-- 归档是「列表整理」而不是删除，所以用同一张表切换口径，不做二级页面 -->
          <a-radio-group
            v-if="listOn"
            :model-value="listFilter"
            type="button"
            size="mini"
            @change="changeListFilter"
          >
            <a-radio value="active">全部任务</a-radio>
            <a-radio value="archived">已归档</a-radio>
          </a-radio-group>
          <span class="af-muted">共 {{ listOn ? listTotal : recent.length }} 条</span>
          <a-button v-if="!listOn" size="mini" :disabled="recent.length === 0" @click="clearRecent">清空</a-button>
        </a-space>
      </template>

      <!-- W6 路径：服务端分页列表 -->
      <a-table
        v-if="listOn"
        :data="taskRows"
        :loading="loading"
        row-key="task_id"
        :pagination="{ pageSize: listSize, current: listPage, total: listTotal, showTotal: true }"
        @page-change="changeListPage"
        size="small"
        :bordered="false"
      >
        <template #columns>
          <a-table-column title="任务编号" :width="168">
            <template #cell="{ record }">
              <a-link class="af-mono" @click="openTask(record.task_id)">{{ record.task_no }}</a-link>
            </template>
          </a-table-column>
          <a-table-column title="目标" data-index="goal" :ellipsis="true" :tooltip="true" />
          <a-table-column title="状态" :width="110">
            <template #cell="{ record }">
              <TaskStatusTag :status="record.status" />
              <a-tag v-if="record.archived_at" size="small" color="gray" class="archived-tag">已归档</a-tag>
            </template>
          </a-table-column>
          <a-table-column v-if="!auth.isViewer" title="创建人" :width="130">
            <template #cell="{ record }">
              <span class="af-mono">{{ record.creator_name ?? '—' }}</span>
            </template>
          </a-table-column>
          <a-table-column title="耗时" :width="96" align="right">
            <template #cell="{ record }">
              <span class="af-num">{{ formatSeconds(record.duration_s) }}</span>
            </template>
          </a-table-column>
          <a-table-column title="Token" :width="86" align="right">
            <template #cell="{ record }">
              <span class="af-num">{{ formatTokens(record.token_usage) }}</span>
            </template>
          </a-table-column>
          <a-table-column title="创建时间" :width="110">
            <template #cell="{ record }">
              <span class="af-muted af-num">{{ formatShortTime(record.created_at) }}</span>
            </template>
          </a-table-column>
          <a-table-column title="操作" :width="140">
            <template #cell="{ record }">
              <a-space :size="8">
                <a-link @click="openTask(record.task_id)">查看</a-link>
                <a-link
                  v-if="canArchive(record)"
                  :loading="archivingId === record.task_id"
                  :status="record.archived_at ? undefined : 'warning'"
                  @click="handleArchive(record)"
                >
                  {{ record.archived_at ? '取消归档' : '归档' }}
                </a-link>
              </a-space>
            </template>
          </a-table-column>
        </template>
      </a-table>

      <!-- 回落路径：本机最近访问 -->
      <template v-else>
        <StateBlock
          v-if="recent.length === 0"
          state="empty"
          title="还没有访问记录"
          :description="auth.isAdmin ? '新建一个任务，或从审批收件箱进入任务详情' : '从审批收件箱进入任务详情后，这里会出现记录'"
          :min-height="180"
        />

        <a-table
          v-else
          :data="recent"
          :loading="loading"
          row-key="taskId"
          :pagination="false"
          size="small"
          :bordered="false"
        >
          <template #columns>
            <a-table-column title="任务编号" data-index="taskId" :width="168">
              <template #cell="{ record }">
                <a-link class="af-mono" @click="openTask(record.taskId)">{{ record.taskNo }}</a-link>
              </template>
            </a-table-column>
            <a-table-column title="目标" data-index="goal" :ellipsis="true" :tooltip="true" />
            <a-table-column title="状态" :width="110">
              <template #cell="{ record }">
                <TaskStatusTag :status="record.status" />
              </template>
            </a-table-column>
            <a-table-column title="耗时" :width="96" align="right">
              <template #cell="{ record }">
                <span class="af-num">
                  {{ detailMap[record.taskId] ? formatDuration(detailMap[record.taskId].createdAt, detailMap[record.taskId].finishedAt) : '—' }}
                </span>
              </template>
            </a-table-column>
            <a-table-column title="Token" :width="86" align="right">
              <template #cell="{ record }">
                <span class="af-num">{{ detailMap[record.taskId] ? formatTokens(detailMap[record.taskId].tokenUsage) : '—' }}</span>
              </template>
            </a-table-column>
            <a-table-column title="最近打开" :width="110">
              <template #cell="{ record }">
                <span class="af-muted af-num">{{ formatShortTime(record.openedAt) }}</span>
              </template>
            </a-table-column>
            <a-table-column title="操作" :width="90">
              <template #cell="{ record }">
                <a-link @click="openTask(record.taskId)">查看</a-link>
              </template>
            </a-table-column>
          </template>
        </a-table>
      </template>
    </a-card>
  </div>
</template>

<style scoped>
.kpis {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

.kpi :deep(.arco-card-body) {
  padding: 14px 16px;
}

.kpi-title {
  font-size: 12px;
  color: var(--color-text-2);
}

.kpi-value {
  margin-top: 6px;
  font-size: 26px;
  font-weight: 600;
  line-height: 34px;
  letter-spacing: -0.5px;
}

.kpi-value small {
  margin-left: 4px;
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-3);
}

.kpi-foot {
  margin-top: 4px;
  font-size: 11px;
  color: var(--color-text-3);
  display: flex;
  align-items: center;
  gap: 5px;
  min-height: 16px;
}

/* 环比行：颜色语义照原型（绿=利好 / 红=利空），与涨跌方向解耦 */
.kpi-delta {
  display: inline-flex;
  align-items: baseline;
  gap: 3px;
  font-weight: 500;
  white-space: nowrap;
}

.kpi-delta em {
  font-style: normal;
  font-weight: 400;
  color: var(--color-text-3);
}

.kpi-delta.is-good {
  color: #00b42a;
}

.kpi-delta.is-bad {
  color: #f53f3f;
}

.kpi-delta.is-flat {
  color: var(--color-text-3);
}

.kpi-note.is-link {
  color: rgb(var(--primary-6));
  cursor: pointer;
}

.kpi.is-warn {
  border-color: rgb(var(--warning-3));
  background: #fffdf9;
}

.kpi.is-pending .kpi-value {
  color: var(--color-text-3);
}

/* 状态分布 chips（原型过滤条的计数样式） */
.status-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: -2px 0 12px;
}

.st-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 22px;
  padding: 0 8px;
  border-radius: 2px;
  background: var(--color-fill-2, #f2f3f5);
  color: var(--color-text-2, #4e5969);
  font-size: 12px;
}

.st-chip b {
  font-weight: 500;
  color: var(--color-text-1, #1d2129);
}

.st-chip.is-hi {
  background: #ffece8;
  color: #f53f3f;
}

.st-chip.is-hi b {
  color: #f53f3f;
}

.af-card {
  margin-bottom: 12px;
}

/* 趋势卡与工具分布卡并排：等高对齐，避免底部参差 */
.dash-mid {
  align-items: stretch;
}

.dash-mid :deep(.arco-col) {
  display: flex;
}

.dash-mid .af-card {
  flex: 1;
  width: 100%;
}

/* 任务完成趋势：图例 + 合计（数据全部来自 /dashboards/trend） */
.trend-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 11px;
}

.trend-legend {
  display: flex;
  align-items: center;
  gap: 14px;
  color: var(--color-text-2);
}

.lgd {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.lgd-dot {
  width: 8px;
  height: 2px;
  border-radius: 1px;
}

.lgd-dot.is-total {
  background: rgb(var(--primary-6));
}

.lgd-dot.is-done {
  background: rgb(var(--success-6));
}

.af-table-card :deep(.arco-card-body) {
  padding: 0;
}

.af-table-card :deep(.arco-card-header) {
  display: flex;
  align-items: center;
}

/* 工具调用分布：环形占比图（原型 P02 donut） */
.tool-donut {
  padding: 4px 0 8px;
}

/* 失败原因 Top 5：横向条形（原型 P02，红条=失败语义） */
.fail-list {
  padding: 10px 2px 6px;
}

.fail-empty {
  padding: 28px 0;
  text-align: center;
}

.fail-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}

.fail-code {
  flex: none;
  width: 132px;
  font-size: 11px;
  color: var(--color-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fail-bar-wrap {
  flex: 1;
  height: 12px;
  border-radius: 2px;
  background: var(--color-fill-2, #f7f8fa);
  overflow: hidden;
}

.fail-bar {
  height: 100%;
  border-radius: 2px;
  background: #f53f3f;
  opacity: 0.85;
}

.fail-count {
  flex: none;
  width: 22px;
  text-align: right;
  font-size: 12px;
}

.fail-foot {
  margin-top: 2px;
  padding-top: 8px;
  border-top: 1px dashed var(--color-border-2, #e5e6eb);
  font-size: 11px;
}

/* 归档标记：紧贴状态标签，弱化处理（归档只是整理，不是异常状态） */
.archived-tag {
  margin-left: 6px;
}
</style>
