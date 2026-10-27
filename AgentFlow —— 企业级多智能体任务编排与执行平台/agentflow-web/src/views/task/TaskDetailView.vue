<script setup lang="ts">
/**
 * P04 任务详情（核心页）。
 *
 * 数据来源（全部为真实接口）：
 *  - 详情/子任务：`GET /tasks/{id}`（子任务状态**只能**从这里拿到，SSE 不下发子任务事件）
 *  - 轨迹：`GET /tasks/{id}/trace`
 *  - 挂起上下文：`GET /tasks/{id}/interrupt`
 *  - 实时事件：`GET /tasks/{id}/stream`（SSE 四类事件）
 *  - 产物：详情里的 `artifacts[]`（url 形如 `/static/charts/xxx.png`，需拼 API Base）
 *  - 记忆：`GET /memories?task_id=`（仅 admin 可见全量，其他角色看本人）
 *
 * 实时策略：
 *  - SSE 负责"当前跑到哪个节点"以及工具调用的即时反馈；
 *  - 子任务状态、token 累计等仍然每 4s 轮询一次详情（接口粒度决定的，不是偷懒）；
 *  - 收到 interrupt 终帧 → 拉 interrupt 上下文并自动滑出审批抽屉。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Message, Modal } from '@arco-design/web-vue'
import dayjs from 'dayjs'

import {
  cancelTask,
  downloadTaskExport,
  downloadTraceExport,
  getInterrupt,
  getSnapshot,
  getTaskDetail,
  getTrace,
  resumeTask,
} from '@/api/tasks'
import { listMemories } from '@/api/memories'
import { TaskStreamClient } from '@/api/stream'
import { isApiError, resolveAssetUrl } from '@/api/http'
import { downloadText } from '@/utils/download'
import { useAuthStore } from '@/stores/auth'
import { useApprovalStore } from '@/stores/approval'
import { rememberTask, updateRecentStatus } from '@/utils/recentTasks'
import { APPROVAL_TYPE_TEXT, NODE_LABEL, ERROR_CODE_TEXT, toolNameOf } from '@/utils/status'
import { formatDuration, formatFullTime, formatLatency, formatTokens, formatEvalScore } from '@/utils/format'

import DagGraph from '@/components/DagGraph.vue'
import TraceTimeline from '@/components/TraceTimeline.vue'
import JsonViewer from '@/components/JsonViewer.vue'
import TaskStatusTag from '@/components/TaskStatusTag.vue'
import ApprovalDrawer from '@/components/ApprovalDrawer.vue'
import StateBlock from '@/components/StateBlock.vue'
import MemoryContentCell from '@/components/MemoryContentCell.vue'

import type { InterruptContext } from '@/types/approval'
import type { NodeStatus } from '@/types/dag'
import type { TableData } from '@arco-design/web-vue'
import type { TaskDetail, Subtask, ArtifactItem } from '@/types/task'
import type { CheckpointStep, SnapshotData, TraceStep } from '@/types/trace'
import type { MemoryItem } from '@/types/memory'
import type { CompletePayload, InterruptPayload, StateUpdatePayload, ToolCallPayload } from '@/types/sse'

const props = defineProps<{ taskId: string }>()

const router = useRouter()
const auth = useAuthStore()
const approvalStore = useApprovalStore()

const taskId = computed(() => Number(props.taskId))

const detail = ref<TaskDetail | null>(null)
const loading = ref(false)
const exporting = ref(false)
const detailError = ref<{ code: string; message: string; traceId?: string | null } | null>(null)

const steps = ref<TraceStep[]>([])
const checkpoints = ref<CheckpointStep[]>([])
const traceLoading = ref(false)

const snapshot = ref<SnapshotData | null>(null)
const interruptContext = ref<InterruptContext | null>(null)
const memories = ref<MemoryItem[]>([])
const memoryLoading = ref(false)
const memoryError = ref<string | null>(null)

const activeTab = ref('subtasks')
const selectedSubtask = ref<Subtask | null>(null)

/** SSE 实时节点状态 */
const liveNodes = reactive<Record<string, NodeStatus>>({})
/** 实时工具调用（用于 Tab1 的"实时事件"尾部） */
const liveToolCalls = ref<Array<{ seq: number; tool: string; phase: string; ok?: boolean; latency?: number; ts: string }>>([])
const streamConnected = ref(false)
const streamError = ref(false)

const awaiting = computed(() => detail.value?.status === 'AWAITING_APPROVAL')
const isRunning = computed(() =>
  ['PLANNING', 'RUNNING', 'SUMMARIZING'].includes(detail.value?.status ?? ''),
)
const isTerminal = computed(() => ['COMPLETED', 'FAILED', 'CANCELLED'].includes(detail.value?.status ?? ''))

const drawerVisible = ref(false)
const drawerLoading = ref(false)
const submittingDecision = ref(false)

const isCreator = computed(() => detail.value?.creator_id === auth.user?.id)
const canDecide = computed(() => (auth.isAdmin || auth.isApprover) && awaiting.value)

/**
 * v1.1：评审 REJECT 的人工批注闸门。
 *
 * 后端 review_gate 节点挂起时，interrupt 的 type 为 `REVIEW_INTERVENTION`（不是审批单），
 * 因此不能走 ApprovalDrawer（会去查 approval_id）。这里改为在详情页内嵌一张批注卡片：
 *  - 提交批注（decision=REJECTED + comment）→ 批注作为硬约束注入 Planner 重规划；
 *  - 人工放行（decision=APPROVED）→ 跳过重规划，按当前结论降级交付。
 */
const REVIEW_INTERVENTION_TYPE = 'REVIEW_INTERVENTION'

interface ReviewInterventionView {
  reason: string
  total: number | null
  confidence: number | null
  confidenceReason: string
  replanCount: number
  excerpt: string
  suggestions: Array<{ seq: number | string; advice: string }>
}

const reviewIntervention = computed<ReviewInterventionView | null>(() => {
  const ctx = interruptContext.value
  if (!ctx?.paused) return null
  if (String(ctx.type || '').toUpperCase() !== REVIEW_INTERVENTION_TYPE) return null
  const preview = (ctx.preview || {}) as Record<string, unknown>
  const suggestions = Array.isArray(preview.suggestions)
    ? preview.suggestions.map((item) => {
        const row = (item ?? {}) as Record<string, unknown>
        return { seq: (row.seq as number | string) ?? '-', advice: String(row.advice ?? '') }
      })
    : []
  return {
    reason: ctx.reason ?? '',
    total: typeof preview.total === 'number' ? preview.total : null,
    confidence: typeof preview.confidence === 'number' ? preview.confidence : null,
    confidenceReason: typeof preview.confidence_reason === 'string' ? preview.confidence_reason : '',
    replanCount: typeof preview.replan_count === 'number' ? preview.replan_count : 0,
    excerpt: typeof preview.report_excerpt === 'string' ? preview.report_excerpt : '',
    suggestions,
  }
})

const annotation = ref('')
const submittingAnnotation = ref(false)
const canCancel = computed(
  () => (auth.isAdmin || auth.isApprover) && ['PLANNING', 'RUNNING', 'AWAITING_APPROVAL', 'SUMMARIZING'].includes(detail.value?.status ?? ''),
)

const timeRangeText = computed(() => {
  const range = detail.value?.time_range as { start?: string; end?: string } | undefined
  if (!range?.start && !range?.end) return '—'
  return `${range.start ?? '—'} ~ ${range.end ?? '—'}`
})

const durationText = computed(() => formatDuration(detail.value?.created_at, detail.value?.finished_at))

const errorBanner = computed(() => {
  const error = detail.value?.error
  if (!error) return null
  const code = String(error.code ?? '')
  return {
    code,
    title: ERROR_CODE_TEXT[code] ?? (code || '任务异常'),
    message: String(error.message ?? ''),
    spanId: error.span_id ?? null,
  }
})

const artifacts = computed(() => detail.value?.artifacts ?? [])
const imageArtifacts = computed(() =>
  artifacts.value.filter((item) => {
    const url = String(item.url ?? '')
    return /\.(png|jpe?g|svg|webp)$/i.test(url)
  }),
)
const fileArtifacts = computed(() => artifacts.value.filter((item) => !imageArtifacts.value.includes(item)))

const tokenLedgerRows = computed(() => {
  const ledger = detail.value?.token_ledger ?? {}
  return Object.entries(ledger).map(([node, tokens]) => ({ node, tokens }))
})

/** 产物可点击的链接：url 为空的产物（如 summarizer 的内嵌报告 ref）返回 null，渲染为非链接卡片 */
function artifactHref(item: ArtifactItem): string | null {
  const url = String(item.url ?? '').trim()
  return url ? resolveAssetUrl(url) : null
}

/** token_ledger 单条目文案：后端每节点是 {input, output, calls} 对象，兜底兼容数字 */
function ledgerEntryText(entry: unknown): string {
  if (typeof entry === 'number') return formatTokens(entry)
  if (!entry || typeof entry !== 'object') return '—'
  const e = entry as Record<string, unknown>
  const parts: string[] = []
  if (typeof e.input === 'number') parts.push(`入 ${formatTokens(e.input)}`)
  if (typeof e.output === 'number') parts.push(`出 ${formatTokens(e.output)}`)
  if (typeof e.calls === 'number') parts.push(`${e.calls} 次调用`)
  return parts.length ? parts.join(' · ') : '—'
}

const reviewText = computed(() => {
  const review = detail.value?.review
  if (!review) return '—'
  // 后端 review 的字段是 total / pass / verdict / confidence（decision、score 是早期占位字段，后端从不下发）
  const parts: string[] = []
  if (review.verdict) parts.push(String(review.verdict))
  if (typeof review.total === 'number') parts.push(`${review.total}/5`)
  if (typeof review.confidence === 'number') parts.push(`可信度 ${Math.round(review.confidence * 100)}%`)
  if (!parts.length && review.decision) parts.push(String(review.decision))
  return parts.length ? parts.join(' · ') : '—'
})

/** 评审判定对应的标签色（PASS 绿 / REVISE 橙 / REJECT 红） */
const reviewVerdictColor = computed(() => {
  const verdict = String(detail.value?.review?.verdict || '').toUpperCase()
  if (verdict === 'PASS') return 'green'
  if (verdict === 'REVISE') return 'orange'
  if (verdict === 'REJECT') return 'red'
  return 'gray'
})

/**
 * 与所选子任务关联的工具调用。
 *
 * G7 已闭环：TraceStep 出参 `subtask_id`，优先按它精确归属；
 * 旧数据 `subtask_id` 为 null 时回落到「tool_name 匹配 + 时间窗」的近似策略。
 */
const relatedSteps = computed(() => {
  const task = selectedSubtask.value
  if (!task) return []
  const precise = steps.value.filter((step) => step.subtask_id === task.id)
  if (precise.length > 0) return precise.slice(-10)
  const toolName = toolNameOf(task.tool_hint)
  if (toolName === '—') return []
  return steps.value.filter((step) => step.tool_name === toolName).slice(-10)
})

/** 关联是否来自 subtask_id 精确归属（false = 走了 tool_name 兜底） */
const relatedIsPrecise = computed(() => {
  const task = selectedSubtask.value
  if (!task) return false
  return steps.value.some((step) => step.subtask_id === task.id)
})

// ---------------------------------------------------------------------------
// 关联工具调用的「人话摘要」：先给结论，原始 JSON 默认折叠（研发需要时展开）
// ---------------------------------------------------------------------------

/** 工具层错误码 → 中文（对应后端 app/schemas/mcp.py::McpErrorCode + 业务护栏码） */
const TOOL_ERROR_LABELS: Record<string, string> = {
  // sql-query
  SQL_SYNTAX: 'SQL 语法错误',
  SQL_TIMEOUT: 'SQL 执行超时',
  SQL_TOO_LARGE: '查询结果过大',
  DB_UNAVAILABLE: '数据库不可用',
  // chart-render
  CHART_DATA_INVALID: '图表数据不合法',
  RENDER_TIMEOUT: '图表渲染超时',
  RENDER_FAILED: '图表渲染失败',
  // kb-search
  KB_TIMEOUT: '知识库检索超时',
  KB_UNAVAILABLE: '知识库暂不可用',
  KB_AUTH_FAILED: '知识库认证失败',
  // notify
  APPROVAL_MISSING: '前置审批未通过，已阻断发送',
  NOTIFY_INVALID: '消息内容不合法',
  NOTIFY_FAILED: '消息发送失败',
  // 客户端 / 执行器
  TOOL_LOOP_LIMIT: '工具调用次数超限',
  TOOL_NOT_FOUND: '工具不存在',
  MCP_PROTOCOL_ERROR: '工具通信协议错误',
  MCP_TIMEOUT: '工具调用超时',
  TOOL_CIRCUIT_OPEN: '连续失败已触发熔断',
  // 业务护栏 / 资源
  INVALID_INPUT: '输入内容被安全护栏拦截',
  INTENT_BLOCKED: '输入疑似攻击意图，被拦截',
  TOKEN_BUDGET_EXCEEDED: '任务 Token 预算超限',
}

/** 执行节点 → 中文 */
const NODE_LABELS: Record<string, string> = {
  planner: '规划',
  executor: '执行',
  human_gate: '人工审批',
  reviewer: '审阅',
  replan: '重规划',
  summarizer: '总结',
  notifier: '通知',
  memory_recall: '记忆召回',
  context_compress: '上下文压缩',
}

function payloadOf(step: TraceStep): Record<string, unknown> {
  return (step.payload ?? {}) as Record<string, unknown>
}

/** 该条调用的健康度：成功绿 / 失败红 / 过程蓝 */
function stepTone(step: TraceStep): 'ok' | 'fail' | 'info' {
  const payload = payloadOf(step)
  const errorCode = payload.error_code
  if (
    step.tool_error ||
    payload.ok === false ||
    (typeof errorCode === 'string' && errorCode && errorCode !== 'null')
  ) {
    return 'fail'
  }
  if (step.event === 'tool.result') return 'ok'
  return 'info'
}

/** 把原始报错压成一句话：优先查错误码字典，否则取首行去前缀截断 */
function friendlyError(step: TraceStep): string {
  const payload = payloadOf(step)
  const code = typeof payload.error_code === 'string' ? payload.error_code : ''
  if (code && TOOL_ERROR_LABELS[code]) return TOOL_ERROR_LABELS[code]
  const raw = step.tool_error || (code ? code : '')
  if (raw) {
    const first = raw.split('\n')[0].replace(/^[A-Z_]+:\s*/, '').trim()
    return first.length > 60 ? `${first.slice(0, 60)}…` : first || '未知原因'
  }
  return '未知原因'
}

/** 一句话中文摘要（面向业务/管理层；工程师展开原始数据看细节） */
function stepSummary(step: TraceStep): string {
  const payload = payloadOf(step)
  const tool = step.tool_name || (typeof payload.server === 'string' ? payload.server : '')
  const toolSuffix = tool ? `（${tool}）` : ''
  switch (step.event) {
    case 'tool.call':
      return `调用工具${toolSuffix}`
    case 'tool.result':
      return stepTone(step) === 'fail'
        ? `工具执行失败${toolSuffix}：${friendlyError(step)}`
        : `工具执行成功${toolSuffix}`
    case 'llm.call': {
      const provider = typeof payload.provider === 'string' ? payload.provider : ''
      return `调用大模型${provider ? `（${provider}）` : ''}`
    }
    case 'human_gate':
    case 'human_edit':
      return '等待人工审批 / 人工介入'
    case 'replan':
      return '计划需调整，触发重规划'
    case 'error':
      return `执行出错：${friendlyError(step)}`
    default: {
      const node = step.node ? (NODE_LABELS[step.node] ?? step.node) : ''
      return node ? `${node}节点记录（${step.event}）` : step.event
    }
  }
}

/** 原始 JSON 折叠状态（Set 重建以保持响应式） */
const rawOpenIds = ref<Set<number>>(new Set())
function toggleRaw(id: number): void {
  const next = new Set(rawOpenIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  rawOpenIds.value = next
}
const rawExpanded = (id: number): boolean => rawOpenIds.value.has(id)

/** 依赖展示：depends_on 存的是 seq */
const dependsText = computed(() => {
  const task = selectedSubtask.value
  if (!task) return '—'
  return task.depends_on.length > 0 ? task.depends_on.map((seq) => `#${seq}`).join(' → ') : '无（起始节点）'
})

// ---------------------------------------------------------------------------
// 数据加载
// ---------------------------------------------------------------------------
let pollTimer: number | null = null
let traceTimer: number | null = null

async function loadDetail(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    const data = await getTaskDetail(taskId.value)
    detail.value = data
    detailError.value = null
    rememberTask({ taskId: data.task_id, taskNo: data.task_no, goal: data.goal, status: data.status })
    updateRecentStatus(data.task_id, data.status, data.goal)
    if (!selectedSubtask.value && data.subtasks.length > 0) {
      selectedSubtask.value = data.subtasks[0]
    } else if (selectedSubtask.value) {
      selectedSubtask.value = data.subtasks.find((item) => item.id === selectedSubtask.value?.id) ?? data.subtasks[0] ?? null
    }
  } catch (error) {
    if (isApiError(error)) {
      detailError.value = { code: error.code, message: error.message, traceId: error.traceId }
    } else {
      detailError.value = { code: 'NETWORK_ERROR', message: '无法连接后端服务' }
    }
  } finally {
    loading.value = false
  }
}

async function loadTrace(silent = true): Promise<void> {
  if (!silent) traceLoading.value = true
  try {
    const data = await getTrace(taskId.value)
    steps.value = data.steps
    checkpoints.value = data.checkpoints
  } catch {
    // 轨迹拉取失败不阻塞主视图（例如角色权限不足）
  } finally {
    traceLoading.value = false
  }
}

const snapshotLoading = ref(false)

async function loadSnapshot(): Promise<void> {
  snapshotLoading.value = true
  try {
    const data = await getSnapshot(taskId.value)
    snapshot.value = data
    Message.info(`快照已刷新（step ${data.step ?? '—'}）`)
  } catch {
    snapshot.value = null
    Message.warning('快照加载失败：该任务可能尚未产生 checkpoint')
  } finally {
    snapshotLoading.value = false
  }
}

async function loadInterrupt(): Promise<void> {
  // 非挂起态直接清空：否则恢复执行后仍会残留上一轮的挂起上下文
  // （表现为"批注卡片 / 审批抽屉在任务已继续跑之后还挂着"）
  if (!awaiting.value) {
    interruptContext.value = null
    return
  }
  if (!canDecide.value) return
  drawerLoading.value = true
  try {
    interruptContext.value = await getInterrupt(taskId.value)
  } catch {
    interruptContext.value = null
  } finally {
    drawerLoading.value = false
  }
}

async function loadMemories(): Promise<void> {
  if (!auth.isAdmin) {
    memoryError.value = '当前角色无权查看任务关联记忆（写操作与全量查看仅限管理员）'
    return
  }
  memoryLoading.value = true
  memoryError.value = null
  try {
    const data = await listMemories({ task_id: taskId.value, page: 1, size: 20 })
    memories.value = data.items
    if (!data.enabled) memoryError.value = '分层记忆未启用（ENABLE_LAYERED_MEMORY=false）'
  } catch (error) {
    memoryError.value = isApiError(error) ? error.message : '记忆加载失败'
  } finally {
    memoryLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------
let client: TaskStreamClient | null = null
/** SSE 游标（最后消费的事件 id）。断开/重连都必须沿用，否则后端会从头重放旧帧 */
let streamCursor: string | null = null

function scheduleTraceRefresh(): void {
  if (traceTimer !== null) return
  traceTimer = window.setTimeout(() => {
    traceTimer = null
    void loadTrace()
  }, 1200)
}

function handleStateUpdate(payload: StateUpdatePayload): void {
  const node = payload.node ?? ''
  if (node && ['planner', 'human_gate', 'reviewer', 'summarizer', 'notifier'].includes(node)) {
    if (payload.phase === 'enter') liveNodes[node] = 'run'
    else if (payload.phase === 'exit') liveNodes[node] = payload.status === 'FAILED' ? 'err' : 'ok'
    else if (payload.phase === 'error') liveNodes[node] = 'err'
  }
  if (payload.status && detail.value && payload.phase !== 'enter') {
    // 服务端推来的状态优先，但 END 之前的 RUNNING 不必覆盖终态
    if (payload.status !== detail.value.status && !isTerminal.value) {
      detail.value.status = payload.status as TaskDetail['status']
    }
  }
  if (payload.phase === 'error') streamError.value = true
  scheduleTraceRefresh()
  void loadDetail(true)
}

function handleToolCall(payload: ToolCallPayload): void {
  liveToolCalls.value.push({
    seq: payload.seq,
    tool: payload.tool,
    phase: payload.phase,
    ok: payload.ok,
    latency: payload.latency_ms,
    ts: payload.ts,
  })
  if (liveToolCalls.value.length > 30) liveToolCalls.value.shift()
  scheduleTraceRefresh()
}

function handleInterrupt(payload: InterruptPayload): void {
  streamConnected.value = false
  const isReviewGate = String(payload.type || '').toUpperCase() === REVIEW_INTERVENTION_TYPE
  void (async () => {
    await loadDetail(true)
    void loadSnapshot()
    await loadInterrupt()
    // 评审批注闸门不弹审批抽屉（没有 approval_id），由页内卡片承接
    if (isReviewGate) return
    // 兜底：重连或刷新时后端会重放历史 interrupt 帧，
    // 任务若已恢复/终态、或拿不到挂起上下文，都不要弹出一个空抽屉
    if (!awaiting.value || !interruptContext.value) return
    if (auth.isAdmin || auth.isApprover) {
      drawerVisible.value = true
      void approvalStore.refresh()
    }
  })()
}

function handleComplete(payload: CompletePayload): void {
  streamConnected.value = false
  void loadDetail(true)
  void loadTrace()
  if (payload.status === 'COMPLETED') {
    Message.success('任务已完成')
  } else if (payload.status === 'FAILED') {
    Message.error('任务执行失败，请查看异常横幅与 Trace')
  } else if (payload.status === 'CANCELLED') {
    Message.info('任务已终止')
  }
  stopPolling()
}

function connectStream(): void {
  if (client) return
  client = new TaskStreamClient({
    taskId: taskId.value,
    // 复用上次连接留下的游标：后端 sse_event_frames 的 START_ID 是 "0-0"，
    // 不带游标会从第一条事件重放，把已消费过的 interrupt 帧再投一次
    lastEventId: streamCursor,
    handlers: {
      onOpen: () => {
        streamConnected.value = true
        streamError.value = false
      },
      onStateUpdate: handleStateUpdate,
      onToolCall: handleToolCall,
      onInterrupt: handleInterrupt,
      onComplete: handleComplete,
      onError: () => {
        streamConnected.value = false
        streamError.value = true
      },
    },
  })
  client.connect()
}

function disconnectStream(): void {
  // 先记下游标再关闭：handleResume 会断开重连，丢了游标就会触发历史帧重放
  if (client?.cursor) streamCursor = client.cursor
  client?.close()
  client = null
  streamConnected.value = false
}

function startPolling(): void {
  stopPolling()
  if (!isRunning.value) return
  pollTimer = window.setInterval(() => {
    void loadDetail(true)
  }, 4000)
}

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

// ---------------------------------------------------------------------------
// 操作
// ---------------------------------------------------------------------------
async function copyTraceId(): Promise<void> {
  if (!detail.value?.trace_id) return
  try {
    await navigator.clipboard.writeText(detail.value.trace_id)
    Message.success('trace_id 已复制')
  } catch {
    Message.error('复制失败')
  }
}

async function handleResume(payload: { decision: 'APPROVED' | 'REJECTED'; comment: string }): Promise<void> {
  submittingDecision.value = true
  try {
    const result = await resumeTask(taskId.value, {
      decision: payload.decision,
      comment: payload.comment,
    })
    Message.success(result.resumed ? '已提交决策，任务继续执行' : '决策已提交（任务未恢复执行）')
    drawerVisible.value = false
    await loadDetail(true)
    await loadTrace()
    await approvalStore.refresh()
    // 恢复后重新建立 SSE 连接
    disconnectStream()
    if (isRunning.value || awaiting.value) connectStream()
    startPolling()
  } catch (error) {
    if (isApiError(error)) {
      Message.error(error.code === 'FORBIDDEN' ? `${error.message}（APPROVAL_ALLOW_SELF_DECIDE=false 时不能审批本人任务）` : error.message)
    } else {
      Message.error('提交失败，请重试')
    }
  } finally {
    submittingDecision.value = false
  }
}

function handleCancel(): void {
  Modal.prompt({
    title: '终止任务',
    content: '终止后任务置为 CANCELLED 且不再执行，已产生的轨迹保留。请填写终止原因（最多 255 字）。',
    placeholder: '例如：口径错误，重新提交',
    okText: '终止',
    cancelText: '取消',
    onOk: async (value: string) => {
      try {
        await cancelTask(taskId.value, { reason: value || '' })
        Message.success('任务已终止')
        await loadDetail(true)
        await loadTrace()
        disconnectStream()
        stopPolling()
      } catch (error) {
        Message.error(isApiError(error) ? error.message : '终止失败')
      }
    },
  })
}

/** 导出：后端无导出接口（G10），这里在浏览器侧组装详情 + 轨迹 */
/**
 * 导出（服务端生成文件）。
 *
 * 变更说明：此前是"浏览器侧把已加载的数据拼成 JSON"，只能导出当前页面已拉到的部分，
 * 且不包含数据库里的完整轨迹/评审/审批。v1.1 起后端提供 `GET /tasks/{id}/export`
 * 与 `GET /tasks/{id}/trace/export`，这里改为直接下载服务端归档文件。
 */
async function handleExport(value: string | number | Record<string, unknown> | undefined): Promise<void> {
  if (!detail.value || exporting.value) return
  const kind = value === undefined ? 'task' : String(value)
  exporting.value = true
  try {
    const filename =
      kind === 'trace'
        ? await downloadTraceExport(detail.value.task_id)
        : await downloadTaskExport(detail.value.task_id)
    Message.success(`已导出 ${filename}`)
  } catch (error) {
    Message.error(isApiError(error) ? error.message : '导出失败，请稍后重试')
  } finally {
    exporting.value = false
  }
}

/**
 * 剥掉提示词侧的内容隔离标签。
 *
 * 后端 `app/agents/prompts.py` 用 `<untrusted_document>…</untrusted_document>` 把外部
 * 不可信内容（检索片段、工具结果）物理隔离，防间接注入。LLM 偶尔会把这些边界标记
 * 原样抄进 summary / 子任务结论，于是它们会跟着进 Markdown 交付物 —— 对读文档的人
 * 毫无意义，而且不少 Markdown 渲染器会把尖括号当 HTML 标签吞掉正文。
 *
 * 只剥标签本身、保留内部正文；未闭合的残留也一并清掉。
 */
function stripPromptTags(text: string): string {
  if (!text) return ''
  return text
    // [^>]* 同时吃掉属性变体（<untrusted_document type="kb">），i 兼顾大小写
    .replace(/<\/?untrusted_document[^>]*>/gi, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/**
 * 导出「结论摘要 + 审阅 + 子任务 + 产物」为 Markdown 交付文档。
 *
 * 为什么不复用后端 `/tasks/{id}/export`：
 * 那份是**整任务归档 JSON**（给系统间流转、复盘用），字段平铺且体积大；
 * 而"产物与报告"这个 Tab 面向的是人（业务方要一份能贴进周报/邮件的东西），
 * 所以这份 Markdown 在前端拼装：摘要原文照搬、审阅与子任务转成可读表格、产物给绝对链接。
 */
function exportReportMarkdown(): void {
  const d = detail.value
  if (!d) return
  const lines: string[] = []
  const range = d.time_range as { start?: string; end?: string }

  lines.push(`# ${d.task_no} 任务报告`)
  lines.push('')
  lines.push(`> 目标：${d.goal}`)
  lines.push('')
  lines.push('## 一、任务概况')
  lines.push('')
  lines.push('| 项目 | 内容 |')
  lines.push('| --- | --- |')
  lines.push(`| 任务编号 | ${d.task_no}（#${d.task_id}） |`)
  lines.push(`| 状态 | ${d.status} |`)
  lines.push(`| 业务时间 | ${range.start ?? '—'} ~ ${range.end ?? '—'} |`)
  lines.push(`| 创建时间 | ${formatFullTime(d.created_at)} |`)
  lines.push(`| 结束时间 | ${formatFullTime(d.finished_at)} |`)
  lines.push(`| 耗时 | ${durationText.value} |`)
  lines.push(`| Token | ${formatTokens(d.token_usage)} |`)
  lines.push(`| 重规划次数 | ${d.replan_count} |`)
  lines.push(`| trace_id | \`${d.trace_id}\` |`)
  lines.push('')

  lines.push('## 二、结论摘要')
  lines.push('')
  lines.push(stripPromptTags(d.summary ?? '') || '（摘要尚未生成）')
  lines.push('')

  if (d.review) {
    lines.push('## 三、审阅结果')
    lines.push('')
    if (d.review.verdict) lines.push(`- 评审结论：**${d.review.verdict}**`)
    if (typeof d.review.total === 'number') lines.push(`- 总分：${d.review.total}`)
    if (typeof d.review.confidence === 'number') {
      lines.push(`- 可信度：${(d.review.confidence * 100).toFixed(1)}%`)
    }
    if (d.review.confidence_reason) lines.push(`- 可信度构成：${stripPromptTags(d.review.confidence_reason)}`)
    if (d.review.skip_reason) lines.push(`- 放行原因：${stripPromptTags(d.review.skip_reason)}`)
    if (d.eval_score !== null && d.eval_score !== undefined) {
      // 可读化：对象形态 {total, verdict, ...} 取总分，不再吐 JSON（与详情卡口径一致）
      lines.push(`- 评测分：${formatEvalScore(d.eval_score)}`)
    }
    if (d.review_intervention?.status) {
      lines.push(`- 人工介入：${d.review_intervention.status}${d.review_intervention.comment ? `（${d.review_intervention.comment}）` : ''}`)
    }
    if (d.review.suggestions && d.review.suggestions.length > 0) {
      lines.push('')
      lines.push('评审建议：')
      lines.push('')
      for (const item of d.review.suggestions) {
        lines.push(`- ${stripPromptTags(typeof item === 'string' ? item : JSON.stringify(item))}`)
      }
    }
    lines.push('')
  }

  if (d.subtasks.length > 0) {
    lines.push('## 四、子任务执行')
    lines.push('')
    lines.push('| # | 子任务 | 工具 | 状态 | 结果摘要 |')
    lines.push('| --- | --- | --- | --- | --- |')
    for (const task of d.subtasks) {
      const summary = stripPromptTags(task.result_summary ?? '')
        .replace(/\s+/g, ' ')
        .replace(/\|/g, '\\|')
        .slice(0, 80)
      lines.push(`| ${task.seq} | ${task.name} | ${task.tool_hint} | ${task.status} | ${summary || '—'} |`)
    }
    lines.push('')
  }

  if (artifacts.value.length > 0) {
    lines.push('## 五、产物清单')
    lines.push('')
    for (const item of artifacts.value) {
      const title = item.title || item.name || item.type || '产物'
      const href = item.url ? resolveAssetUrl(String(item.url)) : ''
      lines.push(href ? `- [${title}](${href})` : `- ${title}（内嵌产物，内容见结论摘要）`)
    }
    lines.push('')
  }

  lines.push('---')
  lines.push('')
  lines.push(`导出时间：${formatFullTime(new Date().toISOString())}`)
  lines.push('')

  downloadText(`${d.task_no}-report.md`, lines.join('\n'))
  Message.success('报告已导出为 Markdown')
}

function openApproval(): void {
  // 评审批注闸门没有审批单，只刷新上下文，不打开审批抽屉
  if (reviewIntervention.value) {
    void loadInterrupt()
    return
  }
  drawerVisible.value = true
  void loadInterrupt()
  if (!snapshot.value) void loadSnapshot()
}

/**
 * 提交评审批注（v1.1）。
 *  - REJECTED：批注必填，作为硬约束注入 Planner 重规划（replan_context.human_comment）；
 *  - APPROVED：人工放行，跳过重规划直接汇总，报告标注"人工放行"。
 */
async function submitReviewIntervention(decision: 'APPROVED' | 'REJECTED'): Promise<void> {
  const comment = annotation.value.trim()
  if (decision === 'REJECTED' && comment.length === 0) {
    Message.warning('请填写批注内容：它会作为硬约束注入重新规划')
    return
  }
  submittingAnnotation.value = true
  try {
    await resumeTask(taskId.value, { decision, comment })
    Message.success(decision === 'APPROVED' ? '已人工放行，任务继续汇总' : '批注已提交，任务将按批注重规划')
    annotation.value = ''
    await loadDetail(true)
    await loadTrace()
    await loadInterrupt()
    disconnectStream()
    if (isRunning.value || awaiting.value) connectStream()
    startPolling()
  } catch (error) {
    Message.error(isApiError(error) ? error.message : '提交失败，请重试')
  } finally {
    submittingAnnotation.value = false
  }
}

function selectSubtask(record: Subtask): void {
  selectedSubtask.value = record
}

watch(activeTab, (tab) => {
  if (tab === 'memory' && memories.value.length === 0) void loadMemories()
  if (tab === 'artifacts' && !snapshot.value) void loadSnapshot()
  if (tab === 'trace' && steps.value.length === 0) void loadTrace(false)
})

watch(
  () => detail.value?.status,
  (status) => {
    if (!status) return
    if (['PLANNING', 'RUNNING', 'SUMMARIZING'].includes(status)) {
      startPolling()
      if (!client) connectStream()
    } else if (status === 'AWAITING_APPROVAL') {
      stopPolling()
      if (!client) connectStream()
      // 挂起原因可能是 human_gate 也可能是 review_gate，统一刷新上下文交给页面判定
      if (auth.isAdmin || auth.isApprover) void loadInterrupt()
    } else {
      stopPolling()
      disconnectStream()
    }
  },
)

onMounted(async () => {
  await loadDetail()
  if (!detail.value) return
  await loadTrace()
  if (isRunning.value) {
    startPolling()
    connectStream()
  } else if (awaiting.value) {
    connectStream()
    if (auth.isAdmin || auth.isApprover) {
      await loadInterrupt()
      // 评审批注闸门用页内卡片承接，不弹审批抽屉
      if (!reviewIntervention.value) drawerVisible.value = true
    }
  }
  if (auth.isAdmin) void loadMemories()
})

onBeforeUnmount(() => {
  disconnectStream()
  stopPolling()
  if (traceTimer !== null) window.clearTimeout(traceTimer)
})
</script>

<template>
  <div class="af-page">
    <!-- 加载 / 错误 -->
    <a-card v-if="loading && !detail" :bordered="true">
      <a-skeleton :animation="true">
        <a-space direction="vertical" :style="{ width: '100%' }" size="large">
          <a-skeleton-line :rows="2" />
          <a-skeleton-line :rows="6" />
        </a-space>
      </a-skeleton>
    </a-card>

    <a-card v-else-if="detailError" :bordered="true">
      <StateBlock
        state="error"
        title="任务无法加载"
        :description="detailError.message"
        :code="detailError.code"
        :trace-id="detailError.traceId"
        @retry="loadDetail()"
      />
    </a-card>

    <template v-else-if="detail">
      <!-- 头部 -->
      <a-card :bordered="true" class="head-card">
        <div class="head-top">
          <div class="head-title">
            <span class="af-mono head-no">{{ detail.task_no }}</span>
            <TaskStatusTag :status="detail.status" size="medium" />
            <a-tag v-if="detail.replan_count > 0" size="small" color="orange">replan ×{{ detail.replan_count }}</a-tag>
            <!-- G8：本任务实际启用的高级分支（字段来自 tasks 表同名三列） -->
            <a-tag v-if="detail.use_memory" size="small" color="purple">记忆召回</a-tag>
            <a-tag v-if="detail.compress" size="small" color="cyan">上下文压缩</a-tag>
            <a-tag v-if="detail.eval_tag" size="small" color="gold">评测 {{ detail.eval_tag }}</a-tag>
            <!-- v1.1：人工批注记录（REJECT 后是否已介入） -->
            <a-tag v-if="detail.review_intervention" size="small" color="orangered">
              {{ detail.review_intervention.status === 'APPROVED' ? '人工放行' : '人工批注' }}
            </a-tag>
            <a-tag v-if="streamConnected" size="small" color="green">实时</a-tag>
            <a-tag v-else-if="isRunning" size="small" color="gray">未连接</a-tag>
          </div>
          <a-space :size="8">
            <a-button size="small" @click="loadDetail()">刷新</a-button>
            <a-dropdown trigger="click" @select="handleExport">
              <a-button size="small" :loading="exporting">
                导出
                <icon-down />
              </a-button>
              <template #content>
                <a-doption value="task">任务归档 JSON（含评审/审批/轨迹）</a-doption>
                <a-doption value="trace">轨迹 JSON（steps + checkpoints）</a-doption>
              </template>
            </a-dropdown>
            <a-button v-if="canDecide" size="small" type="primary" @click="openApproval">审批</a-button>
            <a-button v-if="canCancel" size="small" status="danger" @click="handleCancel">终止</a-button>
          </a-space>
        </div>

        <div class="goal-bar">
          <span class="goal-label">目标</span>
          <span class="goal-text">{{ detail.goal }}</span>
        </div>

        <a-descriptions :column="4" size="small" class="meta">
          <a-descriptions-item label="任务 ID">{{ detail.task_id }}</a-descriptions-item>
          <a-descriptions-item label="trace_id">
            <span class="af-mono">{{ detail.trace_id }}</span>
            <a-link class="copy-link" @click="copyTraceId">复制</a-link>
          </a-descriptions-item>
          <a-descriptions-item label="创建人">{{ detail.creator_id }}</a-descriptions-item>
          <a-descriptions-item label="业务时间">{{ timeRangeText }}</a-descriptions-item>
          <a-descriptions-item label="创建时间">{{ formatFullTime(detail.created_at) }}</a-descriptions-item>
          <a-descriptions-item label="结束时间">{{ formatFullTime(detail.finished_at) }}</a-descriptions-item>
          <a-descriptions-item label="耗时">{{ durationText }}</a-descriptions-item>
          <a-descriptions-item label="Token">{{ formatTokens(detail.token_usage) }}</a-descriptions-item>
        </a-descriptions>

        <a-alert v-if="errorBanner" type="error" class="error-banner">
          <template #title>
            {{ errorBanner.title }}
            <span class="af-mono error-code">{{ errorBanner.code }}</span>
          </template>
          <div class="error-body">
            <div>{{ errorBanner.message || '后端未返回具体错误信息' }}</div>
            <div v-if="errorBanner.spanId" class="af-muted af-mono">
              出错 span：{{ errorBanner.spanId }}（可到 Trace 面板定位）
            </div>
          </div>
        </a-alert>

        <!-- v1.1：评审判定 REJECT → 人工批注闸门（review_gate） -->
        <a-card v-if="reviewIntervention" :bordered="true" class="block-card review-gate-card">
          <template #title>
            <span class="block-title">评审驳回 · 需要人工批注</span>
            <a-tag size="small" color="red" class="block-tag">REJECT</a-tag>
          </template>
          <template #extra>
            <span class="af-muted">批注会作为硬约束注入 Planner 重新生成方案</span>
          </template>

          <div class="review-gate-body">
            <div class="review-gate-metrics">
              <div class="metric">
                <span class="metric-label">总分</span>
                <span class="metric-value af-num">{{ reviewIntervention.total ?? '—' }}<em v-if="reviewIntervention.total !== null">/5</em></span>
              </div>
              <div class="metric">
                <span class="metric-label">可信度</span>
                <span class="metric-value af-num">
                  {{ reviewIntervention.confidence !== null ? `${Math.round(reviewIntervention.confidence * 100)}%` : '—' }}
                </span>
              </div>
              <div class="metric">
                <span class="metric-label">已重规划</span>
                <span class="metric-value af-num">{{ reviewIntervention.replanCount }} 次</span>
              </div>
            </div>

            <div class="review-gate-reason">{{ reviewIntervention.reason || '评审判定不可交付，需人工介入' }}</div>
            <div v-if="reviewIntervention.confidenceReason" class="af-muted review-gate-hint">
              可信度构成：{{ reviewIntervention.confidenceReason }}
            </div>

            <div v-if="reviewIntervention.suggestions.length" class="review-gate-suggestions">
              <div class="suggest-title">评审建议</div>
              <div v-for="(item, index) in reviewIntervention.suggestions" :key="index" class="suggest-row">
                · #{{ item.seq }} {{ item.advice }}
              </div>
            </div>

            <div v-if="reviewIntervention.excerpt" class="review-gate-excerpt">
              <div class="suggest-title">当前结论摘要（待修补）</div>
              <pre class="excerpt-text">{{ reviewIntervention.excerpt }}</pre>
            </div>

            <a-textarea
              v-model="annotation"
              class="review-gate-input"
              placeholder="填写批注：口径 / 必须补充的证据 / 需要排除的子任务…（例如：把 GMV 口径改为环比，并单独列出断货 SKU 的库存周转天数）"
              :auto-size="{ minRows: 3, maxRows: 6 }"
              :max-length="500"
              show-word-limit
              :disabled="submittingAnnotation || !canDecide"
            />

            <a-space>
              <a-button type="primary" :loading="submittingAnnotation" :disabled="!canDecide" @click="submitReviewIntervention('REJECTED')">
                提交批注并重规划
              </a-button>
              <a-button :loading="submittingAnnotation" :disabled="!canDecide" @click="submitReviewIntervention('APPROVED')">
                人工放行（降级交付）
              </a-button>
              <span v-if="!canDecide" class="af-muted">当前角色无批注权限，请联系管理员 / 审批人</span>
            </a-space>
          </div>
        </a-card>

        <a-alert v-else-if="awaiting" type="warning" class="wait-banner">
          <template #title>
            任务停在 human_gate，等待审批（{{ interruptContext?.type ? APPROVAL_TYPE_TEXT[interruptContext.type] ?? interruptContext.type : '高危操作' }}）
          </template>
          <div class="error-body">
            <div>{{ interruptContext?.reason || '存在需人工确认的高危操作' }}</div>
            <a-space v-if="canDecide">
              <a-button size="mini" type="primary" @click="openApproval">去审批</a-button>
            </a-space>
            <span v-else class="af-muted">当前角色无审批权限，请联系审批人处理</span>
          </div>
        </a-alert>
      </a-card>

      <!-- DAG 独占整行：横向最多 8 列节点，需要足够宽度；配合组件内缩放/全屏使用 -->
      <a-card :bordered="true" class="block-card dag-card">
        <template #title>
          <span class="block-title">执行计划（DAG）</span>
          <a-tag size="small" color="gray" class="block-tag">只读</a-tag>
        </template>
        <template #extra>
          <div class="legend">
            <span><i class="dot is-ok" />完成</span>
            <span><i class="dot is-run" />执行中</span>
            <span><i class="dot is-wait" />等待</span>
            <span><i class="dot is-err" />失败</span>
            <span><i class="dot is-idle" />待执行</span>
          </div>
        </template>
        <DagGraph :detail="detail" :live-nodes="liveNodes" :awaiting="awaiting" :height="460" />
      </a-card>

      <a-row :gutter="12">
        <a-col :span="17">
          <!-- Tabs -->
          <a-card :bordered="true" class="block-card tabs-card">
            <a-tabs v-model:active-key="activeTab" type="line" size="medium">
              <!-- Tab1 子任务 -->
              <a-tab-pane key="subtasks" title="子任务与工具调用">
                <div class="subtask-wrap">
                  <div class="subtask-table">
                    <a-table
                      :data="detail.subtasks"
                      row-key="id"
                      size="small"
                      :pagination="false"
                      :bordered="false"
                      :row-class="(record: Subtask) => (record.id === selectedSubtask?.id ? 'is-selected' : '')"
                      @row-click="(record: TableData) => selectSubtask(record as unknown as Subtask)"
                    >
                      <template #columns>
                        <a-table-column title="#" :width="52">
                          <template #cell="{ record }">
                            <span class="af-mono">#{{ record.seq }}</span>
                          </template>
                        </a-table-column>
                        <a-table-column title="子任务" data-index="name" :ellipsis="true" />
                        <a-table-column title="工具" :width="120">
                          <template #cell="{ record }">
                            <span class="af-mono">{{ toolNameOf(record.tool_hint) }}</span>
                          </template>
                        </a-table-column>
                        <a-table-column title="风险" :width="80">
                          <template #cell="{ record }">
                            <TaskStatusTag kind="risk" :status="record.risk_level" />
                          </template>
                        </a-table-column>
                        <a-table-column title="状态" :width="96">
                          <template #cell="{ record }">
                            <TaskStatusTag kind="subtask" :status="record.status" />
                          </template>
                        </a-table-column>
                        <a-table-column title="重试" :width="64" align="right">
                          <template #cell="{ record }">
                            <span class="af-num">{{ record.retry_count }}</span>
                          </template>
                        </a-table-column>
                      </template>
                    </a-table>

                    <div v-if="liveToolCalls.length" class="live-tools">
                      <div class="live-tools-title">
                        <span class="af-pulse live-dot" />
                        实时工具调用（SSE）
                      </div>
                      <div v-for="call in liveToolCalls.slice(-5).reverse()" :key="call.seq" class="live-tool-row">
                        <span class="af-mono">{{ call.tool }}</span>
                        <a-tag size="small" :color="call.phase === 'call' ? 'arcoblue' : call.ok ? 'green' : 'red'">
                          {{ call.phase === 'call' ? '调用' : call.ok ? '成功' : '失败' }}
                        </a-tag>
                        <span v-if="call.latency" class="af-muted af-num">{{ formatLatency(call.latency) }}</span>
                        <span class="af-muted af-mono">{{ dayjs(call.ts).format('HH:mm:ss') }}</span>
                      </div>
                    </div>
                  </div>

                  <!-- 选中子任务详情 -->
                  <div class="subtask-detail">
                    <template v-if="selectedSubtask">
                      <div class="sd-title">
                        <span class="af-mono">#{{ selectedSubtask.seq }}</span>
                        <span>{{ selectedSubtask.name }}</span>
                      </div>
                      <div class="sd-desc">{{ selectedSubtask.description }}</div>
                      <div class="sd-kv"><span>工具</span><b class="af-mono">{{ toolNameOf(selectedSubtask.tool_hint) }}</b></div>
                      <div class="sd-kv"><span>依赖</span><b>{{ dependsText }}</b></div>
                      <div class="sd-kv"><span>状态</span><TaskStatusTag kind="subtask" :status="selectedSubtask.status" /></div>
                      <div class="sd-kv"><span>结果</span><b>{{ selectedSubtask.result_summary || '—' }}</b></div>

                      <a-divider :margin="10" />
                      <div class="sd-sub-title">
                        关联工具调用
                        <a-tag v-if="relatedIsPrecise" size="small" color="green">subtask_id 精确归属</a-tag>
                        <a-tag v-else size="small" color="gray">按 tool_name 近似</a-tag>
                        <a-tooltip>
                          <template #content>
                            优先用 TraceStep.subtask_id 精确归属（G7 已闭环）；旧数据该列为空时，
                            才退回 tool_name + 时间窗的近似匹配。
                          </template>
                          <icon-info-circle class="af-muted" />
                        </a-tooltip>
                      </div>
                      <div v-if="relatedSteps.length === 0" class="af-muted">暂无关联的工具调用记录</div>
                      <div v-for="step in relatedSteps" :key="step.id" class="sd-step">
                        <div class="sd-step-head">
                          <span class="sd-tone" :class="`tone-${stepTone(step)}`"></span>
                          <span class="sd-summary" :class="{ 'is-fail': stepTone(step) === 'fail' }">
                            {{ stepSummary(step) }}
                          </span>
                          <span class="af-mono af-muted sd-event">{{ step.event }}</span>
                          <span class="af-mono af-muted">#{{ step.id }}</span>
                          <span class="af-muted af-num">{{ formatLatency(step.latency_ms) }}</span>
                        </div>
                        <div v-if="step.payload" class="sd-raw-toggle" @click="toggleRaw(step.id)">
                          {{ rawExpanded(step.id) ? '收起原始数据 ▲' : '查看原始数据 ▼' }}
                        </div>
                        <JsonViewer
                          v-if="step.payload && rawExpanded(step.id)"
                          :value="step.payload"
                          :max-height="140"
                          empty-text="无 payload"
                        />
                      </div>
                    </template>
                    <div v-else class="af-muted">选择左侧子任务查看详情</div>
                  </div>
                </div>
              </a-tab-pane>

              <!-- Tab2 Trace -->
              <a-tab-pane key="trace" title="Trace 回放">
                <TraceTimeline :steps="steps" :checkpoints="checkpoints" :loading="traceLoading" />
              </a-tab-pane>

              <!-- Tab3 产物 -->
              <a-tab-pane key="artifacts" title="产物与报告">
                <div class="export-bar">
                  <span class="af-muted export-bar-label">交付物导出</span>
                  <a-space :size="8" wrap>
                    <a-button size="small" @click="exportReportMarkdown">
                      <template #icon><icon-download /></template>
                      报告 Markdown
                    </a-button>
                    <a-button size="small" :loading="exporting" @click="handleExport('task')">完整归档 JSON</a-button>
                    <a-button size="small" :loading="exporting" @click="handleExport('trace')">轨迹 JSON</a-button>
                  </a-space>
                  <span class="af-muted export-bar-hint">
                    Markdown 含摘要/审阅/子任务/产物，可直接贴进周报；归档 JSON 给系统间流转与复盘
                  </span>
                </div>

                <div class="artifact-grid">
                  <div class="artifact-main">
                    <div class="sd-sub-title">结论摘要（summary）</div>
                    <div class="summary-box">{{ detail.summary || '摘要尚未生成（任务未到 summarizer 节点）' }}</div>

                    <div class="sd-sub-title mt">审阅结果（review）</div>
                    <div class="sd-kv">
                      <span>结论</span>
                      <b>
                        <a-tag v-if="detail.review?.verdict" size="small" :color="reviewVerdictColor">
                          {{ detail.review.verdict }}
                        </a-tag>
                        <span class="review-text">{{ reviewText }}</span>
                      </b>
                    </div>
                    <div class="sd-kv"><span>评测分</span><b>{{ formatEvalScore(detail.eval_score) }}</b></div>
                    <div v-if="detail.review?.confidence_reason" class="sd-kv">
                      <span>可信度构成</span><b class="af-muted">{{ detail.review.confidence_reason }}</b>
                    </div>
                    <div v-if="detail.review?.skip_reason" class="sd-kv">
                      <span>放行原因</span><b>{{ detail.review.skip_reason }}</b>
                    </div>
                    <div v-if="detail.review?.suggestions?.length" class="mt">
                      <JsonViewer :value="detail.review.suggestions" title="review.suggestions" show-header :max-height="160" />
                    </div>

                    <div class="sd-sub-title mt">产物列表（artifacts）</div>
                    <div v-if="artifacts.length === 0" class="af-muted">后端未返回产物（artifacts 为空或缺省）</div>
                    <div class="artifact-list">
                      <template v-for="(item, index) in fileArtifacts" :key="index">
                        <!-- 有 url：可点击新窗口打开 -->
                        <a
                          v-if="artifactHref(item)"
                          class="artifact-item"
                          :href="artifactHref(item) ?? '#'"
                          target="_blank"
                          rel="noreferrer"
                        >
                          <icon-file class="artifact-icon" />
                          <div class="artifact-meta">
                            <div class="af-ellipsis">{{ item.title || item.name || item.type || `产物 ${index + 1}` }}</div>
                            <div class="af-mono af-muted af-ellipsis">{{ item.url || '—' }}</div>
                          </div>
                        </a>
                        <!-- 无 url（如 report ref）：内嵌报告，不渲染成链接，避免点击跳回工作台 -->
                        <div v-else class="artifact-item is-inline-report">
                          <icon-file class="artifact-icon" />
                          <div class="artifact-meta">
                            <div class="af-ellipsis">{{ item.title || item.name || item.type || `产物 ${index + 1}` }}</div>
                            <div class="af-muted af-ellipsis">内嵌报告 · 内容见上方「结论摘要」</div>
                          </div>
                        </div>
                      </template>
                    </div>

                    <div v-if="imageArtifacts.length" class="artifact-thumbs">
                      <a-image
                        v-for="(item, index) in imageArtifacts"
                        :key="index"
                        :src="resolveAssetUrl(String(item.url ?? ''))"
                        :alt="String(item.title ?? '图表产物')"
                        width="168"
                        fit="contain"
                        hide-footer
                      />
                    </div>
                    <div class="af-muted mt-sm">
                      产物 url 是 <span class="af-mono">/static/...</span> 相对路径，已拼接 API Base；
                      若 404 需确认后端 <span class="af-mono">/static</span> 已挂载。
                    </div>
                  </div>

                  <div class="artifact-side">
                    <div class="sd-sub-title">状态快照（snapshot）</div>
                    <a-spin :loading="snapshotLoading" class="snapshot-spin">
                      <JsonViewer :value="snapshot" :max-height="300" empty-text="点击下方按钮加载快照" />
                    </a-spin>
                    <a-button size="small" class="mt-sm" :loading="snapshotLoading" @click="loadSnapshot">
                      加载/刷新快照
                    </a-button>

                    <div class="sd-sub-title mt">上下文记账（token_ledger）</div>
                    <div v-if="tokenLedgerRows.length === 0" class="af-muted">无记账数据</div>
                    <div v-for="row in tokenLedgerRows" :key="row.node" class="sd-kv">
                      <span>{{ NODE_LABEL[row.node] ?? row.node }}</span>
                      <b class="af-num ledger-text">{{ ledgerEntryText(row.tokens) }}</b>
                    </div>
                  </div>
                </div>
              </a-tab-pane>

              <!-- Tab4 记忆 -->
              <a-tab-pane key="memory" title="记忆面板">
                <div v-if="memoryError" class="af-muted memory-error">{{ memoryError }}</div>
                <a-spin :loading="memoryLoading" class="memory-spin">
                  <a-table
                    v-if="memories.length"
                    :data="memories"
                    row-key="id"
                    size="small"
                    :pagination="false"
                    :bordered="false"
                  >
                    <template #columns>
                      <a-table-column title="ID" :width="70">
                        <template #cell="{ record }"><span class="af-mono">{{ record.id }}</span></template>
                      </a-table-column>
                      <a-table-column title="层级" :width="80">
                        <template #cell="{ record }">
                          <a-tag size="small" :color="record.scope === 'long' ? 'purple' : 'arcoblue'">{{ record.scope }}</a-tag>
                        </template>
                      </a-table-column>
                      <a-table-column title="内容" data-index="content" :ellipsis="true" :tooltip="false">
                        <template #cell="{ record }">
                          <!-- 正文最长 4000 字，表格自带 tooltip 会溢出视口被裁；改用限高浮层 + 抽屉 -->
                          <MemoryContentCell :item="record" />
                        </template>
                      </a-table-column>
                      <a-table-column title="重要度" :width="80" align="right">
                        <template #cell="{ record }"><span class="af-num">{{ record.importance }}</span></template>
                      </a-table-column>
                      <a-table-column title="命中" :width="70" align="right">
                        <template #cell="{ record }"><span class="af-num">{{ record.hit_count }}</span></template>
                      </a-table-column>
                      <a-table-column title="创建时间" :width="156">
                        <template #cell="{ record }"><span class="af-muted af-num memory-time">{{ formatFullTime(record.created_at) }}</span></template>
                      </a-table-column>
                    </template>
                  </a-table>
                  <StateBlock
                    v-else-if="!memoryError && !memoryLoading"
                    state="empty"
                    title="该任务暂无关联记忆"
                    description="执行过程中产生的 mid/long 记忆会挂到 task_id 上"
                  />
                </a-spin>
                <div class="af-muted mt-sm">
                  数据源：<span class="af-mono">GET /memories?task_id={{ detail.task_id }}</span>（仅管理员可见全量）
                </div>
              </a-tab-pane>
            </a-tabs>
          </a-card>
        </a-col>

        <!-- 右侧栏 -->
        <a-col :span="7">
          <a-card :bordered="true" class="side-card" title="执行摘要">
            <div class="side-kv"><span>子任务</span><b>{{ detail.subtasks.filter((s) => s.status === 'DONE').length }} / {{ detail.subtasks.length }}</b></div>
            <div class="side-kv"><span>失败</span><b>{{ detail.subtasks.filter((s) => s.status === 'FAILED').length }}</b></div>
            <div class="side-kv"><span>轨迹步骤</span><b>{{ steps.length }}</b></div>
            <div class="side-kv"><span>Checkpoint</span><b>{{ checkpoints.length }}</b></div>
            <div class="side-kv"><span>重规划</span><b>{{ detail.replan_count }}</b></div>
            <div class="side-kv"><span>Token</span><b class="af-num">{{ formatTokens(detail.token_usage) }}</b></div>
            <div class="side-kv"><span>耗时</span><b>{{ durationText }}</b></div>
          </a-card>

          <a-card v-if="awaiting" :bordered="true" class="side-card wait-card">
            <template #title>待审批</template>
            <div class="side-kv"><span>审批单</span><b>#{{ interruptContext?.approval_id ?? '—' }}</b></div>
            <div class="side-kv"><span>类型</span><b>{{ interruptContext?.type ?? '—' }}</b></div>
            <div class="side-kv"><span>挂起时间</span><b>{{ formatFullTime(interruptContext?.interrupted_at) }}</b></div>
            <div class="side-kv"><span>可恢复</span><b>{{ interruptContext?.resumable ? '是' : '否' }}</b></div>
            <a-button
              v-if="canDecide"
              type="primary"
              long
              size="small"
              class="mt-sm"
              @click="openApproval"
            >
              审批决策
            </a-button>
          </a-card>

          <a-card :bordered="true" class="side-card" title="接入说明">
            <div class="af-muted note">
              工具入参与返回来自 <span class="af-mono">task_executions.payload</span>（已脱敏），
              原样展示不做二次加工；LLM 原始 prompt 若存在于 payload 中会一并展示。
            </div>
          </a-card>
        </a-col>
      </a-row>

      <!-- 审批抽屉 -->
      <ApprovalDrawer
        v-model:visible="drawerVisible"
        :task-id="detail.task_id"
        :context="interruptContext"
        :loading="drawerLoading"
        :submitting="submittingDecision"
        :is-creator="isCreator"
        @submit="handleResume"
      />
    </template>
  </div>
</template>

<style scoped>
.head-card {
  margin-bottom: 12px;
}

.head-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.head-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.head-no {
  font-size: 15px;
  font-weight: 500;
  color: var(--color-text-1);
}

.goal-bar {
  display: flex;
  gap: 10px;
  margin: 10px 0;
  padding: 8px 10px;
  background: var(--color-fill-1, #fafbfc);
  border-left: 3px solid rgb(var(--primary-6));
  border-radius: 2px;
}

.goal-label {
  flex: none;
  font-size: 12px;
  color: var(--color-text-3);
}

.goal-text {
  font-size: 13px;
  line-height: 20px;
  color: var(--color-text-1);
}

.meta :deep(.arco-descriptions-item-label) {
  color: var(--color-text-3);
  font-size: 12px;
}

.meta :deep(.arco-descriptions-item-value) {
  font-size: 12.5px;
}

.copy-link {
  margin-left: 6px;
  font-size: 11px;
}

.error-banner,
.wait-banner {
  margin-top: 10px;
}

.error-code {
  margin-left: 8px;
  font-size: 11px;
  opacity: 0.8;
}

.error-body {
  font-size: 12px;
  line-height: 20px;
}

.block-card {
  margin-bottom: 12px;
}

.block-title {
  font-size: 14px;
  font-weight: 500;
}

/* DAG 卡片收紧内边距，把宽度让给画布 */
.dag-card :deep(.arco-card-body) {
  padding: 10px 12px 12px;
}

.block-tag {
  margin-left: 8px;
}

.legend {
  display: flex;
  gap: 12px;
  font-size: 11px;
  color: var(--color-text-3);
}

.legend span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.dot.is-ok {
  background: #00b42a;
}

.dot.is-run {
  background: #165dff;
}

.dot.is-wait {
  background: #ff7d00;
}

.dot.is-err {
  background: #f53f3f;
}

.dot.is-idle {
  background: #c9cdd4;
}

.tabs-card :deep(.arco-card-body) {
  padding-top: 4px;
}

.subtask-wrap {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr);
  gap: 14px;
}

.subtask-table :deep(.is-selected td) {
  background: #f2f7ff;
}

.subtask-table :deep(.arco-table-tr) {
  cursor: pointer;
}

.live-tools {
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px dashed var(--color-border-2, #e5e6eb);
  border-radius: 2px;
}

.live-tools-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--color-text-2);
  margin-bottom: 6px;
}

.live-dot {
  color: #165dff;
}

.live-tool-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  line-height: 22px;
}

.subtask-detail {
  padding: 10px;
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  background: var(--color-fill-1, #fafbfc);
  min-height: 200px;
  /* 数据多时固定高度 + 内部滑轮，不再把中间栏撑到页面底部 */
  max-height: min(560px, 58vh);
  overflow-y: auto;
}

.sd-title {
  display: flex;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 6px;
}

.sd-desc {
  font-size: 12px;
  line-height: 19px;
  color: var(--color-text-2);
  margin-bottom: 8px;
}

.sd-kv {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 22px;
}

.sd-kv span {
  flex: none;
  min-width: 46px;
  white-space: nowrap;
  color: var(--color-text-3);
}

.sd-kv b {
  font-weight: 400;
  color: var(--color-text-1);
  word-break: break-all;
}

.sd-sub-title {
  font-size: 11.5px;
  color: var(--color-text-3);
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  gap: 4px;
}

.sd-step {
  margin-bottom: 8px;
}

.sd-step-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  margin-bottom: 2px;
}

/* 人话摘要：状态色点 + 中文结论，事件码/序号/耗时弱化靠右 */
.sd-tone {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.tone-ok {
  background: #00b42a;
}

.tone-fail {
  background: #f53f3f;
}

.tone-info {
  background: #165dff;
}

.sd-summary {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sd-summary.is-fail {
  color: #f53f3f;
  font-weight: 500;
}

.sd-event {
  flex: none;
  font-size: 11px;
}

.sd-raw-toggle {
  margin: 2px 0 4px;
  font-size: 11px;
  color: var(--color-text-3);
  cursor: pointer;
  user-select: none;
}

.sd-raw-toggle:hover {
  color: rgb(var(--primary-6));
}

.mt {
  margin-top: 12px;
}

/* v1.1：评审驳回 · 人工批注卡片 */
.review-gate-card :deep(.arco-card-header) {
  border-bottom-color: var(--color-border-1);
}

.review-gate-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.review-gate-metrics {
  display: flex;
  gap: 32px;
}

.review-gate-metrics .metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.review-gate-metrics .metric-label {
  font-size: 12px;
  color: var(--color-text-3);
}

.review-gate-metrics .metric-value {
  font-size: 22px;
  font-weight: 600;
}

.review-gate-metrics .metric-value em {
  font-size: 13px;
  font-style: normal;
  color: var(--color-text-3);
  margin-left: 2px;
}

.review-gate-reason {
  font-size: 13.5px;
  color: var(--color-text-1);
}

.review-gate-hint {
  font-size: 12px;
  margin-top: -6px;
}

.review-gate-suggestions .suggest-title,
.review-gate-excerpt .suggest-title {
  font-size: 12px;
  color: var(--color-text-3);
  margin-bottom: 4px;
}

.suggest-row {
  font-size: 13px;
  color: var(--color-text-2);
  line-height: 1.7;
}

.excerpt-text {
  margin: 0;
  padding: 8px 10px;
  max-height: 160px;
  overflow: auto;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--color-fill-1);
  border-radius: 4px;
  color: var(--color-text-2);
}

.review-gate-input {
  width: 100%;
}

.mt-sm {
  margin-top: 8px;
}

/* 交付物导出条：产物 Tab 顶部，一眼能看到"这份报告能带走" */
.export-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  padding: 8px 10px;
  border: 1px dashed var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  background: var(--color-fill-1, #f7f8fa);
}

.export-bar-label {
  font-size: 12px;
  white-space: nowrap;
}

.export-bar-hint {
  font-size: 11px;
  line-height: 16px;
  flex: 1 1 220px;
  min-width: 0;
}

.artifact-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
  gap: 14px;
}

.summary-box {
  padding: 10px;
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  background: #fff;
  font-size: 12.5px;
  line-height: 21px;
  min-height: 64px;
}

.artifact-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.artifact-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  color: var(--color-text-1);
  text-decoration: none;
}

.artifact-item:hover {
  border-color: rgb(var(--primary-5));
}

/* 无 url 的内嵌报告（report ref）：非链接卡片，不响应 hover 变色 */
.artifact-item.is-inline-report {
  cursor: default;
}

.artifact-item.is-inline-report:hover {
  border-color: var(--color-border-2, #e5e6eb);
}

/* token 记账文案比纯数字长，缩小一号防换行挤压；右对齐拉开与长标签（Summarizer）的距离 */
.ledger-text {
  font-size: 12px;
  font-weight: 400;
  text-align: right;
  margin-left: auto;
  white-space: nowrap;
}

.artifact-icon {
  flex: none;
  color: var(--color-text-3);
}

.artifact-meta {
  min-width: 0;
  flex: 1;
  font-size: 12px;
}

.artifact-thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.artifact-thumbs img {
  width: 168px;
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  background: #fff;
}

/* a-image 预览：缩略图容器横排；hover 出现「预览」提示由组件自带 */
.artifact-thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

/* 记忆面板：创建时间一行显示不折行 */
.memory-time {
  white-space: nowrap;
}

.artifact-side {
  min-width: 0;
}

.memory-spin {
  width: 100%;
}

.memory-error {
  padding: 20px 0;
  text-align: center;
}

.side-card {
  margin-bottom: 12px;
}

.side-kv {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
  line-height: 24px;
}

.side-kv span {
  color: var(--color-text-3);
}

.side-kv b {
  font-weight: 400;
  color: var(--color-text-1);
}

.wait-card {
  border-color: rgb(var(--warning-3));
}

.note {
  font-size: 11.5px;
  line-height: 19px;
}

.review-text {
  margin-left: 6px;
}
</style>
