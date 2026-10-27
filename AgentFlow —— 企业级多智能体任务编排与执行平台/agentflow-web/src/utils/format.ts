/**
 * 时间 / 数值格式化。
 *
 * 后端规则（务必遵守）：
 *  - 所有时间字段是 ISO8601 字符串（UTC），展示前统一转本地时区；
 *  - **没有 duration 字段**，耗时一律前端按 finished_at - created_at 计算；
 *  - token 为整数，大数按 K/M 缩写展示。
 */
import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(utc)
dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

/**
 * 解析后端 ISO 时间为本地 dayjs 对象；无效时间返回 null。
 *
 * 时区口径（2026-10-22 起全库统一 UTC，务必遵守）：
 *  - 后端时间字段落库均为 **UTC naive**（如 `2026-10-22T03:15:11`，无偏移标记），
 *    直接 `dayjs()` 会按本地时区理解，展示比真实时间慢 8 小时（UTC+8）；
 *  - 因此无偏移的日期时间一律 `dayjs.utc(...).local()`；
 *  - 已带偏移（`Z` / `+00:00` / `+08:00`）交由 dayjs 自行换算，不重复处理；
 *  - 纯日期（`2026-10-22`，趋势图日桶）无时间语义，按本地日期原样解析，避免整体偏移。
 */
export function parseTime(value?: string | null): dayjs.Dayjs | null {
  if (!value) return null
  const text = String(value).trim()
  const isDateOnly = /^\d{4}-\d{2}-\d{2}$/.test(text)
  const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text)
  const parsed = isDateOnly || hasOffset ? dayjs(text) : dayjs.utc(text).local()
  return parsed.isValid() ? parsed : null
}

/** `MM-DD HH:mm` —— 表格里的紧凑时间 */
export function formatShortTime(value?: string | null): string {
  const time = parseTime(value)
  return time ? time.format('MM-DD HH:mm') : '—'
}

/** `YYYY-MM-DD HH:mm:ss` —— 详情元信息 */
export function formatFullTime(value?: string | null): string {
  const time = parseTime(value)
  return time ? time.format('YYYY-MM-DD HH:mm:ss') : '—'
}

/** `HH:mm:ss` —— Trace 时间线 */
export function formatClock(value?: string | null): string {
  const time = parseTime(value)
  return time ? time.format('HH:mm:ss') : '—'
}

/**
 * 计算耗时文本。
 * 运行中的任务没有 finished_at，传第二个参数为"当前时间"即可得到实时耗时。
 */
export function formatDuration(start?: string | null, end?: string | null): string {
  const from = parseTime(start)
  if (!from) return '—'
  const to = end ? parseTime(end) : dayjs()
  if (!to) return '—'
  const ms = to.diff(from)
  if (ms < 0) return '—'
  const totalSeconds = Math.floor(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h${String(minutes).padStart(2, '0')}m`
  if (minutes > 0) return `${minutes}m${String(seconds).padStart(2, '0')}s`
  return `${seconds}s`
}

/** `mm:ss` 计时器（运行中任务实时跳动） */
export function formatTimer(start?: string | null): string {
  const from = parseTime(start)
  if (!from) return '00:00'
  const totalSeconds = Math.max(0, dayjs().diff(from, 'second'))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

/** token 缩写：1.24M / 8.2K */
export function formatTokens(value?: number | null): string {
  if (value === null || value === undefined) return '—'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

/** 毫秒 → `1.2s` / `840ms` */
export function formatLatency(ms?: number | null): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

/**
 * 审批倒计时。
 * 后端用 `approvals.expires_at` 表示截止时间；没有剩余时间字段，需前端计算。
 * level 供 UI 决定颜色：normal（>1h）/ warning（<=1h）/ danger（<=10min 或已过期）
 */
export function formatCountdown(expiresAt?: string | null): { text: string; level: 'normal' | 'warning' | 'danger' } {
  const deadline = parseTime(expiresAt)
  if (!deadline) return { text: '—', level: 'normal' }
  const diff = deadline.diff(dayjs(), 'second')
  if (diff <= 0) return { text: '已过期', level: 'danger' }
  const hours = Math.floor(diff / 3600)
  const minutes = Math.floor((diff % 3600) / 60)
  const seconds = diff % 60
  const text =
    hours > 0
      ? `${hours}h ${String(minutes).padStart(2, '0')}m`
      : `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  const level = diff <= 600 ? 'danger' : diff <= 3600 ? 'warning' : 'normal'
  return { text, level }
}

/** 百分数（保留 1 位） */
export function formatPercent(value?: number | null, digits = 1): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/** 截断长文本 */
export function truncate(text?: string | null, max = 80): string {
  if (!text) return ''
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/**
 * eval_score 可读化。后端 reviewer 节点产出的是对象
 * `{total, verdict, confidence, ...}`（reviewer.py::eval_score），旧任务可能是裸数字；
 * 直接渲染对象会变成一坨 JSON（2026-10-27 评测页单任务查询实锤）。
 */
export function formatEvalScore(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') return `${value}`
  if (typeof value === 'object') {
    const rec = value as Record<string, unknown>
    const total = rec.total
    const verdict = typeof rec.verdict === 'string' ? rec.verdict : ''
    if (typeof total === 'number') return verdict ? `${total} / 5（${verdict}）` : `${total} / 5`
    return '—'
  }
  return String(value)
}

/** 评审三态判定中文映射（PASS / REVISE / REJECT；decision 为旧字段兜底） */
export function formatReviewVerdict(
  review: { verdict?: string; decision?: string } | null | undefined,
): string {
  const verdict = String(review?.verdict ?? review?.decision ?? '').toUpperCase()
  if (verdict === 'PASS') return '通过（PASS）'
  if (verdict === 'REJECT') return '不通过（REJECT）'
  if (verdict === 'REVISE') return '需修改（REVISE）'
  return verdict || '—'
}

/** 审阅得分（5 分制均分，后端 review.total；score 为旧字段兜底） */
export function formatReviewScore(
  review: { total?: number; score?: number } | null | undefined,
): string {
  const total = review?.total ?? review?.score
  return typeof total === 'number' ? `${total} / 5` : '—'
}
