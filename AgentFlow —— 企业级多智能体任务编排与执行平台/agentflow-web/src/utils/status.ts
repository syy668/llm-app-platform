/**
 * 状态 → 文案 / 颜色 / 图标的唯一映射表。
 *
 * 严格遵守原型规范 §1.6 的五种语义色 + SUMMARIZING 专用紫色：
 *  成功 #00B42A（green）、危险 #F53F3F（red）、警告 #FF7D00（orange）、
 *  信息 #1677FF（arcoblue）、进行中 #165DFF（arcoblue）、
 *  规划 #1677FF 浅（blue）、摘要生成 #722ED1（purple）、中性 #86909C（gray）。
 *
 * Arco Tag 的颜色名与上述色值一一对应，禁止在页面里另造颜色。
 */
import type { SubtaskRiskLevel, SubtaskStatus, TaskStatus, UserRole } from '@/types/enums'

export type ArcoTagColor = 'arcoblue' | 'blue' | 'green' | 'red' | 'orange' | 'purple' | 'gray' | 'gold' | 'cyan'

export interface StatusMeta {
  text: string
  color: ArcoTagColor
  /** 是否需要在标签上显示脉冲动效（进行中类状态） */
  pulse?: boolean
}

/** 任务状态（7 个值，易漏 SUMMARIZING） */
export const TASK_STATUS_META: Record<TaskStatus, StatusMeta> = {
  PLANNING: { text: '规划中', color: 'blue', pulse: true },
  RUNNING: { text: '执行中', color: 'arcoblue', pulse: true },
  AWAITING_APPROVAL: { text: '待审批', color: 'orange' },
  SUMMARIZING: { text: '生成摘要', color: 'purple', pulse: true },
  COMPLETED: { text: '已完成', color: 'green' },
  FAILED: { text: '失败', color: 'red' },
  CANCELLED: { text: '已取消', color: 'gray' },
}

/** 子任务状态 */
export const SUBTASK_STATUS_META: Record<SubtaskStatus, StatusMeta> = {
  PENDING: { text: '待执行', color: 'gray' },
  READY: { text: '就绪', color: 'blue' },
  RUNNING: { text: '执行中', color: 'arcoblue', pulse: true },
  DONE: { text: '完成', color: 'green' },
  FAILED: { text: '失败', color: 'red' },
  SKIPPED: { text: '跳过', color: 'gray' },
  CANCELLED: { text: '已取消', color: 'gray' },
}

/** 节点中文名（`task_executions.node` / Graph 节点） */
export const NODE_LABEL: Record<string, string> = {
  planner: 'Planner',
  executor: 'Executor',
  human_gate: '人工审批',
  reviewer: 'Reviewer',
  replan: 'Replan',
  summarizer: 'Summarizer',
  notifier: 'Notifier',
  // G8 任务级可选节点（开关打开才出现）
  memory_recall: '记忆召回',
  context_compress: '上下文压缩',
}

/** 轨迹事件中文名（`task_executions.event`） */
export const EVENT_LABEL: Record<string, string> = {
  enter: '进入节点',
  exit: '节点输出',
  'tool.call': '工具调用',
  'tool.result': '工具返回',
  replan: '重新规划',
  human_edit: '人工干预',
  error: '异常',
}

/** 审批类型文案 */
export const APPROVAL_TYPE_TEXT: Record<string, string> = {
  HIGH_RISK_TOOL: '高危工具调用',
  LARGE_SCAN: '大范围扫描',
}

/** 审批单状态文案（已处理 Tab 用；PENDING 不出现在已处理列表） */
export const APPROVAL_STATUS_TEXT: Record<string, string> = {
  PENDING: '待处理',
  APPROVED: '已通过',
  REJECTED: '已驳回',
  EXPIRED: '已过期',
}

/** 审批单状态对应的 Arco 标签色（中国习惯：通过=绿、驳回=红、过期=灰） */
export const APPROVAL_STATUS_COLOR: Record<string, string> = {
  PENDING: 'orange',
  APPROVED: 'green',
  REJECTED: 'red',
  EXPIRED: 'gray',
}

/** 只读展示用的失败原因文案（`tasks.error.code`） */
export const ERROR_CODE_TEXT: Record<string, string> = {
  TASK_EXECUTION_ERROR: '执行异常',
  TOOL_TIMEOUT: '工具调用超时',
  PLAN_INVALID: '计划校验失败',
  REPLAN_EXHAUSTED: '重规划次数耗尽',
  APPROVAL_MISSING: '审批单缺失',
  TASK_CANCELED: '任务被终止',
}

/** 角色文案与能力边界（与后端 require_role 一致） */
export const ROLE_META: Record<UserRole, { text: string; scope: string; canWrite: boolean }> = {
  admin: { text: '管理员', scope: '本人任务的全生命周期 + 记忆写操作', canWrite: true },
  approver: { text: '审批人', scope: '全部待审批任务的决策，可查看任务详情', canWrite: false },
  viewer: { text: '访客', scope: '仅可查看 COMPLETED 任务，字段被裁剪', canWrite: false },
}

/** 风险等级 */
export const RISK_META: Record<SubtaskRiskLevel, StatusMeta> = {
  normal: { text: '常规', color: 'gray' },
  high: { text: '高危', color: 'red' },
}

/** tool_hint（下划线）→ MCP 工具名（连字符） */
export function toolNameOf(toolHint?: string | null): string {
  if (!toolHint || toolHint === 'none') return '—'
  return toolHint.replace(/_/g, '-')
}

/** 安全取状态元信息，未知状态回落到中性灰 */
export function statusMeta(map: object, status?: string | null): StatusMeta {
  if (!status) return { text: '—', color: 'gray' }
  return (map as Record<string, StatusMeta | undefined>)[status] ?? { text: status, color: 'gray' }
}
