<script setup lang="ts">
/**
 * DAG 只读画布（AntV X6）。
 *
 * 数据从哪来（不虚构）：
 *  - 节点：固定流水线节点（planner / human_gate / reviewer / summarizer / notifier）
 *          + `GET /tasks/{id}` 返回的 `subtasks[]`；
 *  - 依赖边：`subtasks[].depends_on`（存的是 seq，不是 id）；
 *  - 状态：子任务用 `subtask.status`，流水线节点由 `task.status` 推导，
 *          运行期再被 SSE 的 `state_update`（enter/exit/error）实时覆盖；
 *  - replan 回边：仅当 `replan_count > 0` 时绘制，标注次数。
 *
 * 只读：interacting 全部关闭，仅保留平移/缩放/"适应画布"，禁止拖动节点/连线。
 *
 * 视觉对齐 `agentflow-ui-prototype/p04a-detail.html` 的 dag_card：
 *  - 节点 = 左侧品牌色图标块（P/E/H/R/S/N/M/C）+ 标题/副标题 + 右上角状态徽标；
 *  - human_gate 用虚线描边，运行/等待/失败态加粗描边 + 淡色底；
 *  - 卡片右上浮层提供 缩放（−/%/+）、适应画布、全屏，Ctrl+滚轮等比缩放。
 * 模型计算（哪些节点、哪些边、什么状态）与改造前完全一致，仅替换绘制与交互层。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Graph } from '@antv/x6'
import type { TaskDetail, Subtask } from '@/types/task'
import type { NodeStatus } from '@/types/dag'
import { NODE_LABEL, toolNameOf } from '@/utils/status'

const props = withDefaults(
  defineProps<{
    detail: TaskDetail | null
    /** SSE 实时覆盖：node 名 → 状态 */
    liveNodes?: Record<string, NodeStatus>
    /** 是否处于待审批（human_gate 高亮） */
    awaiting?: boolean
    height?: number
  }>(),
  { liveNodes: () => ({}), awaiting: false, height: 460 },
)

const containerRef = ref<HTMLDivElement | null>(null)
/** 画布渲染异常（防御性兜底，避免整页白屏且问题不可见） */
const renderError = ref<string | null>(null)
const tooltip = ref<{ visible: boolean; x: number; y: number; title: string; rows: Array<[string, string]> }>({
  visible: false,
  x: 0,
  y: 0,
  title: '',
  rows: [],
})

let graph: Graph | null = null

// ---------------------------------------------------------------------------
// 节点模型
// ---------------------------------------------------------------------------
/** 节点种类 —— 决定左侧图标块的字母与品牌色（对齐原型 dnode 的 kind 映射） */
type NodeKind = 'memory' | 'planner' | 'compress' | 'human' | 'executor' | 'reviewer' | 'summarizer' | 'notifier'

interface DagNode {
  id: string
  column: number
  kind: NodeKind
  title: string
  sub: string
  status: NodeStatus
  /** 悬浮卡内容 */
  rows: Array<[string, string]>
}

interface DagEdge {
  source: string
  target: string
  dashed?: boolean
  label?: string
  active?: boolean
}

const NODE_W = 196
const NODE_H = 68
const COL_GAP = 48
const ROW_GAP = 20
const PAD = 28

/** 缩放上下限（与 mousewheel / 按钮共用，避免各写一份） */
const MIN_SCALE = 0.3
const MAX_SCALE = 2.5
/** 默认视图的最小缩放：宁可横向溢出可平移，也不要把 13px 文字压成 5px */
const FIT_MIN_SCALE = 0.8
/** 浮层工具栏高度，避免 fit 时节点钻到工具栏下面 */
const TOOLBAR_PAD = 46
/** 溢出时对齐到左上的边距 */
const EDGE_PAD = 16
/** 标题/副标题可用宽度（左侧 48px 图标区 + 右侧 16px 留白） */
const TITLE_MAX_PX = NODE_W - 48 - 16
const SUB_MAX_PX = TITLE_MAX_PX

const scaled = ref(1)
const isFullscreen = ref(false)

/** 子任务状态 → 画布色标 */
function subtaskNodeStatus(status: Subtask['status']): NodeStatus {
  switch (status) {
    case 'DONE':
      return 'ok'
    case 'RUNNING':
      return 'run'
    case 'READY':
      return 'wait'
    case 'FAILED':
      return 'err'
    default:
      return 'idle'
  }
}

/** 是否完成到某一阶段（用于推导流水线节点状态） */
function passed(status: string, ...candidates: string[]): boolean {
  return candidates.includes(status)
}

const model = computed(() => {
  const detail = props.detail
  const nodes: DagNode[] = []
  const edges: DagEdge[] = []
  if (!detail) return { nodes, edges, columns: 1, height: props.height }

  const subtasks = [...(detail.subtasks ?? [])].sort((a, b) => a.seq - b.seq)
  const allDone = subtasks.length > 0 && subtasks.every((s) => s.status === 'DONE')
  const hasHighRisk = subtasks.some((s) => s.risk_level === 'high')
  const hasNotifier = subtasks.some((s) => s.tool_hint === 'notify')
  const status = detail.status

  // ---- 流水线节点状态推导 ----
  const plannerStatus: NodeStatus =
    props.liveNodes.planner ??
    (status === 'PLANNING' ? 'run' : status === 'FAILED' && subtasks.length === 0 ? 'err' : 'ok')

  const showGate = hasHighRisk || props.awaiting || Boolean(detail.approvals && Object.keys(detail.approvals).length)
  const gateStatus: NodeStatus =
    props.liveNodes.human_gate ??
    (props.awaiting || status === 'AWAITING_APPROVAL'
      ? 'wait'
      : passed(status, 'RUNNING', 'SUMMARIZING', 'COMPLETED')
        ? 'ok'
        : 'idle')

  const reviewerStatus: NodeStatus =
    props.liveNodes.reviewer ??
    (passed(status, 'SUMMARIZING', 'COMPLETED') ? 'ok' : status === 'RUNNING' && allDone ? 'run' : 'idle')

  const summarizerStatus: NodeStatus =
    props.liveNodes.summarizer ?? (status === 'COMPLETED' ? 'ok' : status === 'SUMMARIZING' ? 'run' : 'idle')

  const notifierStatus: NodeStatus = props.liveNodes.notifier ?? (status === 'COMPLETED' ? 'ok' : 'idle')

  // ---- 列分配 ----
  let column = 0

  // G8：use_memory 打开时，START 之后先跑 memory_recall，再进 planner
  const showMemory = Boolean(detail.use_memory)
  const showCompress = Boolean(detail.compress)
  if (showMemory) {
    const memoryStatus: NodeStatus = props.liveNodes.memory_recall ?? (status === 'PLANNING' ? 'run' : 'ok')
    nodes.push({
      id: 'memory_recall',
      kind: 'memory',
      column,
      title: NODE_LABEL.memory_recall,
      sub: '召回长期记忆',
      status: memoryStatus,
      rows: [
        ['节点', 'memory_recall'],
        ['状态', memoryStatus === 'run' ? '召回中' : '已召回'],
        ['说明', '命中项并入 planner prompt'],
      ],
    })
    column += 1
  }

  nodes.push({
    id: 'planner',
    kind: 'planner',
    column,
    title: NODE_LABEL.planner,
    sub: status === 'PLANNING' ? '生成子任务计划' : '计划已生成',
    status: plannerStatus,
    rows: [
      ['节点', 'planner'],
      ['状态', plannerStatus === 'run' ? '规划中' : '已完成'],
      ['子任务数', String(subtasks.length)],
    ],
  })

  // G8：compress 打开时，planner 之后插入 context_compress，产出摘要供下游复用
  if (showCompress) {
    column += 1
    const compressStatus: NodeStatus =
      props.liveNodes.context_compress ?? (status === 'PLANNING' ? 'idle' : 'ok')
    nodes.push({
      id: 'context_compress',
      kind: 'compress',
      column,
      title: NODE_LABEL.context_compress,
      sub: '压缩上下文摘要',
      status: compressStatus,
      rows: [
        ['节点', 'context_compress'],
        ['状态', compressStatus === 'idle' ? '待执行' : compressStatus === 'run' ? '压缩中' : '已压缩'],
        ['说明', 'context_digest 供下游节点复用'],
      ],
    })
  }

  // G8 分支边：memory_recall → planner → context_compress
  if (showMemory) edges.push({ source: 'memory_recall', target: 'planner' })
  if (showCompress) edges.push({ source: 'planner', target: 'context_compress' })

  if (showGate) {
    column += 1
    nodes.push({
      id: 'human_gate',
      kind: 'human',
      column,
      title: NODE_LABEL.human_gate,
      sub: props.awaiting ? '等待审批决策' : '已通过',
      status: gateStatus,
      rows: [
        ['节点', 'human_gate'],
        ['触发原因', '存在高危子任务'],
        ['状态', gateStatus === 'wait' ? '挂起中' : gateStatus === 'ok' ? '已审批' : '未触发'],
      ],
    })
    // 审批节点的前驱：有压缩节点时接在它后面，否则直接接 planner
    edges.push({ source: showCompress ? 'context_compress' : 'planner', target: 'human_gate' })
  }

  column += 1
  const subtaskColumn = column
  const rootSeqs = subtasks.filter((s) => !s.depends_on || s.depends_on.length === 0).map((s) => s.seq)

  subtasks.forEach((task) => {
    const status = props.liveNodes[`subtask-${task.seq}`] ?? subtaskNodeStatus(task.status)
    nodes.push({
      id: `s-${task.seq}`,
      kind: 'executor',
      column: subtaskColumn,
      title: `#${task.seq} ${task.name}`,
      sub: `${toolNameOf(task.tool_hint)}${task.retry_count > 0 ? ` · 重试 ${task.retry_count}` : ''}`,
      status,
      rows: [
        ['子任务', `#${task.seq} ${task.name}`],
        ['工具', toolNameOf(task.tool_hint)],
        ['状态', task.status],
        ['风险', task.risk_level === 'high' ? '高危（需审批）' : '常规'],
        ['重试', String(task.retry_count)],
        ['结果', task.result_summary ? String(task.result_summary).slice(0, 60) : '—'],
      ],
    })
    if (task.depends_on?.length) {
      task.depends_on.forEach((depSeq) => {
        // depends_on 存的是 seq；只连存在的节点，避免脏数据画出悬空边
        if (subtasks.some((s) => s.seq === depSeq)) {
          edges.push({ source: `s-${depSeq}`, target: `s-${task.seq}` })
        }
      })
    }
  })

  // planner / gate / 压缩节点 → 根子任务（按实际启用的分支取最靠近子任务的上游）
  const upstream = showGate ? 'human_gate' : showCompress ? 'context_compress' : 'planner'
  if (subtasks.length === 0) {
    // 计划尚未生成
  } else {
    rootSeqs.forEach((seq) => edges.push({ source: upstream, target: `s-${seq}` }))
  }

  column += 1
  nodes.push({
    id: 'reviewer',
    kind: 'reviewer',
    column,
    title: NODE_LABEL.reviewer,
    sub: passed(status, 'SUMMARIZING', 'COMPLETED') ? '审核通过' : '等待执行结果',
    status: reviewerStatus,
    rows: [
      ['节点', 'reviewer'],
      ['状态', status],
      ['重规划次数', String(detail.replan_count)],
    ],
  })

  column += 1
  nodes.push({
    id: 'summarizer',
    kind: 'summarizer',
    column,
    title: NODE_LABEL.summarizer,
    sub: status === 'SUMMARIZING' ? '生成结论摘要' : status === 'COMPLETED' ? '摘要已生成' : '待执行',
    status: summarizerStatus,
    rows: [
      ['节点', 'summarizer'],
      ['状态', status],
      ['Token 消耗', String(detail.token_usage)],
    ],
  })

  if (hasNotifier) {
    column += 1
    nodes.push({
      id: 'notifier',
      kind: 'notifier',
      column,
      title: NODE_LABEL.notifier,
      sub: '推送通知',
      status: notifierStatus,
      rows: [
        ['节点', 'notifier'],
        ['说明', '含 notify 工具调用'],
      ],
    })
  }

  // 汇聚边：叶子子任务 → reviewer；无子任务时 planner/gate 直接连 reviewer
  const leafSeqs = subtasks
    .filter((s) => !subtasks.some((other) => other.depends_on?.includes(s.seq)))
    .map((s) => s.seq)
  if (subtasks.length === 0) {
    edges.push({ source: upstream, target: 'reviewer' })
  } else {
    leafSeqs.forEach((seq) => edges.push({ source: `s-${seq}`, target: 'reviewer' }))
  }
  edges.push({ source: 'reviewer', target: 'summarizer' })
  if (hasNotifier) edges.push({ source: 'summarizer', target: 'notifier' })

  // replan 回边：reviewer → 第一个子任务（虚线），次数取自 replan_count
  if (detail.replan_count > 0 && subtasks.length > 0) {
    edges.push({
      source: 'reviewer',
      target: `s-${subtasks[0].seq}`,
      dashed: true,
      label: `replan ×${detail.replan_count}`,
    })
  }

  // 高亮"正在流转"的边：从运行中节点出发的边
  const runningIds = new Set(nodes.filter((n) => n.status === 'run').map((n) => n.id))
  edges.forEach((edge) => {
    edge.active = runningIds.has(edge.source)
  })

  return { nodes, edges, columns: column + 1, height: props.height }
})

// ---------------------------------------------------------------------------
// 布局与渲染
// ---------------------------------------------------------------------------
/** 状态 → 描边/底色/字色/文案/右上角徽标（对齐原型 dnode 的 style + 徽标表） */
function statusStyle(status: NodeStatus): {
  stroke: string
  fill: string
  label: string
  subColor: string
  titleColor: string
  strokeWidth: number
  badgeBg: string
  badgeFg: string
  badgeChar: string
} {
  switch (status) {
    case 'ok':
      return {
        stroke: '#E5E6EB',
        fill: '#FFFFFF',
        label: '完成',
        subColor: '#86909C',
        titleColor: '#1D2129',
        strokeWidth: 1,
        badgeBg: '#E8FFEA',
        badgeFg: '#00B42A',
        badgeChar: '✓',
      }
    case 'run':
      return {
        stroke: '#165DFF',
        fill: '#F7FAFF',
        label: '执行中',
        subColor: '#165DFF',
        titleColor: '#1D2129',
        strokeWidth: 2,
        badgeBg: '#165DFF',
        badgeFg: '#FFFFFF',
        badgeChar: '▶',
      }
    case 'wait':
      return {
        stroke: '#FF7D00',
        fill: '#FFF7E8',
        label: '等待中',
        subColor: '#FF7D00',
        titleColor: '#1D2129',
        strokeWidth: 2,
        badgeBg: '#FF7D00',
        badgeFg: '#FFFFFF',
        badgeChar: 'Ⅱ',
      }
    case 'err':
      return {
        stroke: '#F53F3F',
        fill: '#FFF5F3',
        label: '失败',
        subColor: '#F53F3F',
        titleColor: '#1D2129',
        strokeWidth: 2,
        badgeBg: '#FFECE8',
        badgeFg: '#F53F3F',
        badgeChar: '✕',
      }
    default:
      return {
        stroke: '#E5E6EB',
        fill: '#FFFFFF',
        label: '待执行',
        subColor: '#86909C',
        titleColor: '#4E5969',
        strokeWidth: 1,
        badgeBg: '#F7F8FA',
        badgeFg: '#C9CDD4',
        badgeChar: '○',
      }
  }
}

/** 节点种类 → 左侧图标块的字母与品牌色 */
const KIND_STYLE: Record<NodeKind, { letter: string; color: string }> = {
  memory: { letter: 'M', color: '#722ED1' },
  planner: { letter: 'P', color: '#722ED1' },
  compress: { letter: 'C', color: '#13C2C2' },
  human: { letter: 'H', color: '#FF7D00' },
  executor: { letter: 'E', color: '#165DFF' },
  reviewer: { letter: 'R', color: '#0F6E56' },
  summarizer: { letter: 'S', color: '#1677FF' },
  notifier: { letter: 'N', color: '#86909C' },
}

/** 估算文本像素宽（X6 不做自动换行，CJK 按 1em、其余按 0.56em 近似） */
function textWidth(text: string, fontPx: number): number {
  let width = 0
  for (const ch of text) {
    width += (ch.codePointAt(0) ?? 0) > 0x2e80 ? fontPx : fontPx * 0.56
  }
  return width
}

/** 按可用像素宽截断并补省略号 */
function fit(text: string, maxPx: number, fontPx: number): string {
  if (textWidth(text, fontPx) <= maxPx) return text
  let out = ''
  for (const ch of text) {
    if (textWidth(`${out}${ch}…`, fontPx) > maxPx) break
    out += ch
  }
  return `${out}…`
}

/** 丢掉「· 重试 N」尾缀（重试次数在悬浮卡与子任务表里另有展示） */
function trimRetry(text: string): string {
  return text.replace(/\s*·\s*重试\s*\d+\s*$/, '')
}

/**
 * 副标题：优先保留状态词；空间紧张时先丢重试尾缀，再截断。
 */
function subText(sub: string, label: string): string {
  const full = `${sub} · ${label}`
  if (textWidth(full, 11.5) <= SUB_MAX_PX) return full
  return fit(`${trimRetry(sub)} · ${label}`, SUB_MAX_PX, 11.5)
}

function render(): void {
  const instance = graph
  if (!instance) return
  try {
    renderCells(instance)
    renderError.value = null
  } catch (error) {
    renderError.value = error instanceof Error ? `${error.name}: ${error.message}` : String(error)
    console.error('[DagGraph] 渲染失败', error)
  }
}

function renderCells(instance: Graph): void {
  const { nodes, edges, columns } = model.value
  if (nodes.length === 0) {
    instance.clearCells()
    return
  }

  const byColumn = new Map<number, DagNode[]>()
  nodes.forEach((node) => {
    const list = byColumn.get(node.column) ?? []
    list.push(node)
    byColumn.set(node.column, list)
  })
  const maxRows = Math.max(...[...byColumn.values()].map((list) => list.length), 1)
  const canvasHeight = Math.max(maxRows * NODE_H + (maxRows - 1) * ROW_GAP + PAD * 2, 240)

  const position = new Map<string, { x: number; y: number }>()
  byColumn.forEach((list, col) => {
    const stackHeight = list.length * NODE_H + (list.length - 1) * ROW_GAP
    const startY = (canvasHeight - stackHeight) / 2
    list.forEach((node, index) => {
      position.set(node.id, {
        x: PAD + col * (NODE_W + COL_GAP),
        y: startY + index * (NODE_H + ROW_GAP),
      })
    })
  })

  const nodeMetas: Array<Record<string, unknown>> = []
  const edgeMetas: Array<Record<string, unknown>> = []

  nodes.forEach((node) => {
    const pos = position.get(node.id)!
    const style = statusStyle(node.status)
    const kind = KIND_STYLE[node.kind]
    // 运行/等待/失败态图标块更亮，完成/待执行态更淡（对齐原型 opacity 0.16 / 0.12）
    const iconOpacity = node.status === 'ok' || node.status === 'idle' ? 0.12 : 0.16
    nodeMetas.push({
      id: node.id,
      shape: 'af-dag-node',
      x: pos.x,
      y: pos.y,
      width: NODE_W,
      height: NODE_H,
      attrs: {
        body: {
          stroke: style.stroke,
          strokeWidth: style.strokeWidth,
          fill: style.fill,
          // 人工审批节点用虚线框区分「需要人参与」
          strokeDasharray: node.kind === 'human' ? '5 4' : undefined,
        },
        iconBg: { fill: kind.color, fillOpacity: iconOpacity },
        iconText: { text: kind.letter, fill: kind.color },
        title: { text: fit(node.title, TITLE_MAX_PX, 13), fill: style.titleColor },
        // 副标题空间紧张时先丢「重试 N」，保证状态词不被截断
        sub: { text: subText(node.sub, style.label), fill: style.subColor },
        badge: { fill: style.badgeBg },
        badgeText: { text: style.badgeChar, fill: style.badgeFg },
      },
      data: { rows: node.rows, title: node.title },
    })
  })

  edges.forEach((edge, index) => {
    const edgeColor = edge.active ? '#165DFF' : '#C9CDD4'
    edgeMetas.push({
      id: `e-${index}`,
      shape: 'edge',
      source: { cell: edge.source, anchor: { name: 'right' } },
      target: { cell: edge.target, anchor: { name: 'left' } },
      attrs: {
        line: {
          stroke: edgeColor,
          strokeWidth: edge.active ? 1.8 : 1.3,
          // 流转中的边用流动虚线，让"当前跑到哪一步"一眼可见
          strokeDasharray: edge.active ? '6 4' : edge.dashed ? '5 4' : undefined,
          targetMarker: { name: 'classic', size: 7, fill: edgeColor, stroke: edgeColor },
          class: edge.active ? 'af-edge-active' : undefined,
        },
      },
      labels: edge.label
        ? [
            {
              attrs: {
                label: { text: edge.label, fontSize: 10.5, fill: '#FF7D00' },
                body: { fill: '#FFF7E8', stroke: '#FFCF8B', rx: 4, ry: 4 },
              },
              position: 0.5,
            },
          ]
        : [],
      router: { name: 'er', args: { offset: 18 } },
      connector: { name: 'rounded', args: { radius: 10 } },
      zIndex: 0,
    })
  })

  // 先清空再批量添加：X6 v2 的 resetCells 只接受 Cell 实例，metadata 必须走 addNodes/addEdges
  instance.clearCells()
  instance.addNodes(nodeMetas as never[])
  instance.addEdges(edgeMetas as never[])
  fitToContent()
}

/** 适应画布：限制最小缩放，避免图宽时把文字压成不可读 */
function fitToContent(): void {
  if (!graph || graph.getNodes().length === 0) return
  graph.zoomToFit({
    padding: { top: TOOLBAR_PAD, right: 16, bottom: 16, left: 16 },
    minScale: FIT_MIN_SCALE,
    maxScale: 1,
  })
  alignAfterFit()
  syncScale()
}

/**
 * 溢出时把内容对齐到左上，而不是居中裁切。
 * 否则 8 列的图会被裁掉首列（"记忆召回/Planner"直接看不见）。
 */
function alignAfterFit(): void {
  if (!graph || !containerRef.value) return
  // getContentBBox 返回的是画布坐标（已含缩放与平移），不能再乘 scale
  const content = graph.getContentBBox()
  const matrix = graph.matrix()
  const viewportW = containerRef.value.clientWidth
  const viewportH = containerRef.value.clientHeight
  let tx = matrix.e
  let ty = matrix.f
  if (content.width > viewportW - EDGE_PAD * 2) {
    tx = matrix.e + (EDGE_PAD - content.x)
  }
  if (content.height > viewportH - EDGE_PAD * 2) {
    ty = matrix.f + (EDGE_PAD - content.y)
  }
  if (Math.abs(tx - matrix.e) > 0.5 || Math.abs(ty - matrix.f) > 0.5) {
    graph.translate(tx, ty)
  }
}

/** 同步当前缩放比到工具栏百分比 */
function syncScale(): void {
  if (!graph) return
  scaled.value = graph.zoom()
}

// ---------------------------------------------------------------------------
// 悬浮卡
// ---------------------------------------------------------------------------
function showTooltip(nodeId: string): void {
  if (!graph) return
  const node = graph.getCellById(nodeId)
  if (!node || !containerRef.value) return
  const data = node.getData() as { rows?: Array<[string, string]>; title?: string } | undefined
  const bbox = node.getBBox()
  const point = graph.localToClient(bbox.x + bbox.width / 2, bbox.y)
  const rect = containerRef.value.getBoundingClientRect()
  tooltip.value = {
    visible: true,
    x: Math.min(Math.max(point.x - rect.left, 120), rect.width - 120),
    y: point.y - rect.top - 8,
    title: data?.title ?? '',
    rows: data?.rows ?? [],
  }
}

function hideTooltip(): void {
  tooltip.value.visible = false
}

onMounted(() => {
  if (!containerRef.value) return

  Graph.registerNode(
    'af-dag-node',
    {
      inherit: 'rect',
      width: NODE_W,
      height: NODE_H,
      markup: [
        { tagName: 'rect', selector: 'body' },
        { tagName: 'rect', selector: 'iconBg' },
        { tagName: 'text', selector: 'iconText' },
        { tagName: 'text', selector: 'title' },
        { tagName: 'text', selector: 'sub' },
        { tagName: 'circle', selector: 'badge' },
        { tagName: 'text', selector: 'badgeText' },
      ],
      attrs: {
        body: { rx: 6, ry: 6, strokeWidth: 1, fill: '#FFFFFF' },
        // 左侧 26×26 品牌色图标块（垂直居中于 68 高节点）
        iconBg: { x: 12, y: 21, width: 26, height: 26, rx: 6, ry: 6, stroke: 'none' },
        // 注意：X6 的 text 定位必须用 refX/refY（裸 x/y 会被中心 transform 覆盖）
        iconText: {
          refX: 25,
          refY: 34,
          textAnchor: 'middle',
          textVerticalAnchor: 'middle',
          fontSize: 12,
          fontWeight: 600,
          fontFamily: 'inherit',
        },
        title: {
          refX: 48,
          refY: 28,
          textAnchor: 'start',
          textVerticalAnchor: 'middle',
          fontSize: 13,
          fill: '#1D2129',
          fontFamily: 'inherit',
        },
        sub: {
          refX: 48,
          refY: 48,
          textAnchor: 'start',
          textVerticalAnchor: 'middle',
          fontSize: 11.5,
          fill: '#86909C',
          fontFamily: 'ui-monospace, Consolas, monospace',
        },
        // 右上角状态徽标（circle 用原生 cx/cy，不受 text 的 ref 定位影响）
        badge: { cx: NODE_W - 17, cy: 17, r: 7.5, stroke: 'none' },
        badgeText: {
          refX: NODE_W - 17,
          refY: 21,
          textAnchor: 'middle',
          textVerticalAnchor: 'middle',
          fontSize: 9,
          fontFamily: 'inherit',
        },
      },
    },
    true,
  )

  graph = new Graph({
    container: containerRef.value,
    autoResize: true,
    background: { color: '#FCFCFD' },
    grid: { visible: true, type: 'dot', size: 10, args: { color: '#E9EBEF' } },
    // 只读：禁止节点/边拖动与连线（key 必须落在 X6 的 InteractionMap 内）
    interacting: {
      nodeMovable: false,
      edgeMovable: false,
      edgeLabelMovable: false,
      arrowheadMovable: false,
      vertexMovable: false,
      vertexAddable: false,
      vertexDeletable: false,
      magnetConnectable: false,
      toolsAddable: false,
    },
    // 左键拖拽平移；滚轮平移（沿用原交互，避免改变页面滚动习惯）
    panning: { enabled: true, eventTypes: ['leftMouseDown', 'mouseWheel'] },
    // Ctrl/⌘ + 滚轮在鼠标位置等比缩放（不占用普通滚轮）
    mousewheel: {
      enabled: true,
      modifiers: ['ctrl', 'meta'],
      factor: 1.12,
      minScale: MIN_SCALE,
      maxScale: MAX_SCALE,
      zoomAtMousePosition: true,
    },
    connecting: { snap: false },
  })

  graph.on('node:mouseenter', ({ node }) => showTooltip(node.id))
  graph.on('node:mouseleave', () => hideTooltip())
  graph.on('blank:click', () => hideTooltip())
  graph.on('scale', () => syncScale())
  // 容器尺寸变化（含窗口缩放、全屏切换）后重算缩放，否则停留在旧比例
  graph.on('resize', () => fitToContent())

  window.addEventListener('keydown', onKeydown)
  render()
})

// ---------------------------------------------------------------------------
// 缩放 / 全屏（纯展示层交互，不改数据）
// ---------------------------------------------------------------------------
function zoomIn(): void {
  graph?.zoom(0.15, { maxScale: MAX_SCALE })
  syncScale()
}

function zoomOut(): void {
  graph?.zoom(-0.15, { minScale: MIN_SCALE })
  syncScale()
}

function resetZoom(): void {
  graph?.zoomTo(1)
  graph?.centerContent()
  syncScale()
}

function toggleFullscreen(): void {
  isFullscreen.value = !isFullscreen.value
  void nextTick(() => {
    graph?.resize()
    fitToContent()
  })
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && isFullscreen.value) {
    toggleFullscreen()
  }
}

const zoomPercent = computed(() => `${Math.round(scaled.value * 100)}%`)

/** 全屏时按视口高度铺开（12px 内边距 ×2） */
const canvasStyle = computed(() =>
  isFullscreen.value ? { height: 'calc(100vh - 24px)' } : { height: `${props.height}px` },
)

watch(
  () => [props.detail, props.liveNodes, props.awaiting],
  () => render(),
  { deep: true },
)

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  graph?.dispose()
  graph = null
})

defineExpose({
  /** 适应画布（供父组件按钮调用） */
  fit: () => fitToContent(),
  /** 等比缩放到指定比例 */
  zoomTo: (factor: number) => {
    graph?.zoomTo(Math.min(Math.max(factor, MIN_SCALE), MAX_SCALE))
    syncScale()
  },
  zoomIn,
  zoomOut,
  toggleFullscreen,
})
</script>

<template>
  <div class="af-dag" :class="{ 'is-fullscreen': isFullscreen }">
    <div ref="containerRef" class="af-dag-canvas" :style="canvasStyle" />

    <!-- 浮层工具栏：缩放 / 适应画布 / 全屏 -->
    <div class="af-dag-toolbar">
      <div class="af-dag-zoom">
        <button type="button" class="af-dag-btn" title="缩小" @click="zoomOut">−</button>
        <button type="button" class="af-dag-pct" title="恢复 100%" @click="resetZoom">{{ zoomPercent }}</button>
        <button type="button" class="af-dag-btn" title="放大" @click="zoomIn">+</button>
      </div>
      <span class="af-dag-sep" />
      <button type="button" class="af-dag-btn is-text" title="缩放至完整可见" @click="fitToContent">适应画布</button>
      <button type="button" class="af-dag-btn is-text" title="全屏查看（Esc 退出）" @click="toggleFullscreen">
        {{ isFullscreen ? '退出全屏' : '全屏' }}
      </button>
    </div>

    <div class="af-dag-hint">Ctrl/⌘ + 滚轮缩放 · 按住拖拽平移</div>

    <div v-if="renderError" class="af-dag-error">画布渲染失败：{{ renderError }}</div>

    <div v-if="!detail || (detail.subtasks?.length ?? 0) === 0" class="af-dag-placeholder">
      <a-empty description="计划尚未生成，DAG 将在 Planner 输出后出现" />
    </div>

    <!-- 节点悬浮卡 -->
    <div
      v-if="tooltip.visible"
      class="af-dag-tip"
      :style="{ left: `${tooltip.x}px`, top: `${tooltip.y}px` }"
    >
      <div class="af-dag-tip-title">{{ tooltip.title }}</div>
      <div v-for="[key, value] in tooltip.rows" :key="key" class="af-dag-tip-row">
        <span class="af-dag-tip-key">{{ key }}</span>
        <span class="af-dag-tip-val">{{ value }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.af-dag {
  position: relative;
}

.af-dag-canvas {
  width: 100%;
  border-radius: 4px;
  overflow: hidden;
  background: #fcfcfd;
}

/* 全屏：脱离卡片容器铺满视口，画布按视口高度铺开 */
.af-dag.is-fullscreen {
  position: fixed;
  inset: 0;
  z-index: 1000;
  padding: 12px;
  background: #fff;
  box-shadow: 0 8px 32px rgba(29, 33, 41, 0.18);
}

.af-dag.is-fullscreen .af-dag-canvas {
  border: 1px solid var(--color-border-2, #e5e6eb);
}

/* ---- 浮层工具栏 ---- */
.af-dag-toolbar {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 3px 4px;
  background: rgba(255, 255, 255, 0.94);
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 6px;
  box-shadow: 0 2px 8px rgba(29, 33, 41, 0.08);
  backdrop-filter: blur(4px);
}

.af-dag-zoom {
  display: flex;
  align-items: center;
  gap: 1px;
}

.af-dag-btn {
  min-width: 24px;
  height: 24px;
  padding: 0 6px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-2, #4e5969);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s;
}

.af-dag-btn:hover {
  background: var(--color-fill-2, #f2f3f5);
  color: var(--color-text-1, #1d2129);
}

.af-dag-btn.is-text {
  font-size: 12px;
}

.af-dag-pct {
  min-width: 42px;
  height: 24px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-2, #4e5969);
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  cursor: pointer;
}

.af-dag-pct:hover {
  background: var(--color-fill-2, #f2f3f5);
}

.af-dag-sep {
  width: 1px;
  height: 14px;
  margin: 0 4px;
  background: var(--color-border-2, #e5e6eb);
}

.af-dag-hint {
  position: absolute;
  left: 10px;
  bottom: 6px;
  z-index: 9;
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.82);
  color: var(--color-text-3, #86909c);
  font-size: 11px;
  pointer-events: none;
}

/* 流转中的边：虚线流动，直观体现"当前执行到哪条链路" */
.af-dag-canvas :deep(.af-edge-active) {
  animation: af-dash-flow 0.9s linear infinite;
}

@keyframes af-dash-flow {
  to {
    stroke-dashoffset: -20;
  }
}

.af-dag-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #fff;
  opacity: 0.92;
  pointer-events: none;
}

.af-dag-error {
  position: absolute;
  left: 12px;
  top: 12px;
  right: 12px;
  padding: 8px 10px;
  background: #fff1f0;
  border: 1px solid #fbaca3;
  border-radius: 4px;
  color: #cb2634;
  font-size: 12px;
  z-index: 5;
}

.af-dag-tip {
  position: absolute;
  transform: translate(-50%, -100%);
  z-index: 20;
  min-width: 200px;
  max-width: 300px;
  padding: 8px 10px;
  background: #fff;
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 6px;
  box-shadow: 0 6px 20px rgba(29, 33, 41, 0.14);
  pointer-events: none;
}

.af-dag-tip-title {
  font-size: 12px;
  font-weight: 500;
  margin-bottom: 6px;
  color: #1d2129;
}

.af-dag-tip-row {
  display: flex;
  gap: 8px;
  font-size: 11.5px;
  line-height: 18px;
}

.af-dag-tip-key {
  flex: none;
  width: 56px;
  color: var(--color-text-3);
}

.af-dag-tip-val {
  flex: 1;
  color: var(--color-text-2);
  word-break: break-all;
}
</style>
