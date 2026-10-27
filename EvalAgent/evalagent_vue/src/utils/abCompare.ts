/**
 * A/B 对比核心算法（纯前端，无后端参与）：
 * 1) question 文本对齐两份 run 的全量 scores
 * 2) 计算四项指标的差值
 * 3) |差值| > 0.1 的样本标记为差异样本（表格标黄）
 */
import type { EvalRun, RunMetrics, RunScore } from '@/api/types'

export const DIFF_THRESHOLD = 0.1

export const METRIC_KEYS = ['faithfulness', 'answer_relevancy', 'context_precision', 'context_recall'] as const
export type MetricKey = (typeof METRIC_KEYS)[number]

export const METRIC_LABELS: Record<MetricKey, string> = {
  faithfulness: '忠实度 faithfulness',
  answer_relevancy: '答案相关性 answer_relevancy',
  context_precision: '上下文精确率 context_precision',
  context_recall: '上下文召回率 context_recall',
}

export interface AlignedRow {
  question: string
  a: RunScore | null
  b: RunScore | null
  /** 每项指标差值（b - a），缺失为 null */
  diffs: Record<MetricKey, number | null>
  /** 任一指标 |差值| > 0.1 即标黄 */
  isDiff: boolean
}

export interface AbSummary {
  metric: MetricKey
  avgA: number | null
  avgB: number | null
  diff: number | null
}

function avg(values: Array<number | null>): number | null {
  const valid = values.filter((v): v is number => v !== null && Number.isFinite(v))
  if (valid.length === 0) return null
  return valid.reduce((s, v) => s + v, 0) / valid.length
}

/** 按 question（trim 后）对齐两条 run 的得分记录 */
export function alignScores(scoresA: RunScore[], scoresB: RunScore[]): AlignedRow[] {
  const mapB = new Map<string, RunScore>()
  for (const s of scoresB) {
    mapB.set(s.question.trim(), s)
  }

  const usedB = new Set<number>()
  const rows: AlignedRow[] = []

  for (const sa of scoresA) {
    const key = sa.question.trim()
    const sb = mapB.get(key) ?? null
    if (sb) usedB.add(sb.id)
    rows.push(buildRow(sa, sb))
  }
  // 仅在 B 中出现的 question（理论上同数据集不应出现，兜底展示）
  for (const sb of scoresB) {
    if (!usedB.has(sb.id)) rows.push(buildRow(null, sb))
  }
  return rows
}

function buildRow(a: RunScore | null, b: RunScore | null): AlignedRow {
  const diffs = {} as Record<MetricKey, number | null>
  let isDiff = false
  for (const k of METRIC_KEYS) {
    const va = a ? a[k] : null
    const vb = b ? b[k] : null
    if (va === null || vb === null) {
      diffs[k] = null
    } else {
      diffs[k] = vb - va
      if (Math.abs(diffs[k]) > DIFF_THRESHOLD) isDiff = true
    }
  }
  return {
    question: a?.question ?? b?.question ?? '',
    a,
    b,
    diffs,
    isDiff,
  }
}

/** 汇总指标：优先使用后端 run.metrics，缺省时从前端全量 scores 计算 */
export function computeSummary(runA: EvalRun, runB: EvalRun, rows: AlignedRow[]): AbSummary[] {
  const sourceA = runA.metrics ?? metricsFromRows(rows, 'a')
  const sourceB = runB.metrics ?? metricsFromRows(rows, 'b')
  return METRIC_KEYS.map((k) => {
    const avgA = sourceA[k]
    const avgB = sourceB[k]
    return {
      metric: k,
      avgA,
      avgB,
      diff: avgA !== null && avgB !== null ? avgB - avgA : null,
    }
  })
}

function metricsFromRows(rows: AlignedRow[], side: 'a' | 'b'): RunMetrics {
  const result = {} as RunMetrics
  for (const k of METRIC_KEYS) {
    result[k] = avg(rows.map((r) => (side === 'a' ? r.a?.[k] ?? null : r.b?.[k] ?? null)))
  }
  return result
}

/** 差异样本列表（任一指标 |Δ|>0.1），按最大差值绝对值降序 */
export function diffRows(rows: AlignedRow[]): AlignedRow[] {
  return rows
    .filter((r) => r.isDiff)
    .sort((x, y) => maxAbsDiff(y) - maxAbsDiff(x))
}

function maxAbsDiff(row: AlignedRow): number {
  return Math.max(0, ...METRIC_KEYS.map((k) => Math.abs(row.diffs[k] ?? 0)))
}
