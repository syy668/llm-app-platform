/** A/B 实验 Markdown 报告生成（纯前端拼装文本 + Blob 下载，不需要后端新增导出接口）。 */
import { METRIC_KEYS, METRIC_LABELS, type AbSummary, type AlignedRow, type MetricKey } from '@/utils/abCompare'
import { formatExpLabel } from '@/utils/expParams'
import type { Dataset, EvalRun } from '@/api/types'

export interface AbReportContext {
  runA: EvalRun
  runB: EvalRun
  dataset: Dataset | null
  summary: AbSummary[]
  rows: AlignedRow[]
  diffList: AlignedRow[]
  generatedAt: string
}

function fmt(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(4)
}

export function buildAbReport(ctx: AbReportContext): string {
  const { runA, runB, dataset, summary, diffList, generatedAt } = ctx
  const lines: string[] = []

  lines.push('# EvalAgent A/B 对比实验报告')
  lines.push('')
  lines.push(`> 生成时间：${generatedAt}（由 EvalAgent 前端自动生成，计算过程不依赖后端）`)
  lines.push('')
  lines.push('## 1. 实验配置说明')
  lines.push('')
  lines.push('| 项目 | A 组 | B 组 |')
  lines.push('| --- | --- | --- |')
  lines.push(`| 评测任务 ID | ${runA.id} | ${runB.id} |`)
  lines.push(`| 数据集 | ${dataset ? `${dataset.name}（ID=${dataset.id}）` : `dataset_id=${runA.dataset_id}`} | 同左（前端已校验同源数据集） |`)
  lines.push(`| 评测模型 | ${runA.model} | ${runB.model} |`)
  lines.push(`| RAG 实验参数（本地记录） | ${formatExpLabel(runA)} | ${formatExpLabel(runB)} |`)
  lines.push(`| 样本总数 | ${runA.total_items}（成功 ${runA.success_items}） | ${runB.total_items}（成功 ${runB.success_items}） |`)
  lines.push(`| 创建时间 | ${runA.created_at} | ${runB.created_at} |`)
  lines.push('')

  lines.push('## 2. 汇总指标对比')
  lines.push('')
  lines.push('| 指标 | A 组平均 | B 组平均 | 差值（B - A） | 结论 |')
  lines.push('| --- | --- | --- | --- | --- |')
  for (const s of summary) {
    const verdict =
      s.diff === null
        ? '数据不足'
        : s.diff > 0.1
          ? 'B 显著更优'
          : s.diff < -0.1
            ? 'A 显著更优'
            : Math.abs(s.diff) <= 0.01
              ? '基本持平'
              : '略有差异'
    lines.push(`| ${METRIC_LABELS[s.metric as MetricKey]} | ${fmt(s.avgA)} | ${fmt(s.avgB)} | ${fmt(s.diff)} | ${verdict} |`)
  }
  lines.push('')

  lines.push(`## 3. 差异样本列表（任一指标 |Δ| > 0.1，共 ${diffList.length} 条）`)
  lines.push('')
  if (diffList.length === 0) {
    lines.push('两组结果高度一致，无显著差异样本。')
  } else {
    lines.push('| # | question | 指标 | A 分数 | B 分数 | Δ（B-A） |')
    lines.push('| --- | --- | --- | --- | --- | --- |')
    diffList.forEach((row, idx) => {
      for (const k of METRIC_KEYS) {
        const d = row.diffs[k]
        if (d === null || Math.abs(d) <= 0.1) continue
        const q = row.question.replace(/\|/g, '\\|').slice(0, 80)
        lines.push(
          `| ${idx + 1} | ${q} | ${METRIC_LABELS[k]} | ${fmt(row.a?.[k])} | ${fmt(row.b?.[k])} | ${fmt(d)} |`,
        )
      }
    })
  }
  lines.push('')

  lines.push('## 4. 样本级明细（对齐后前 50 条）')
  lines.push('')
  lines.push('| # | question | A faithfulness | A context_recall | B faithfulness | B context_recall |')
  lines.push('| --- | --- | --- | --- | --- | --- |')
  ctx.rows.slice(0, 50).forEach((row, idx) => {
    const q = row.question.replace(/\|/g, '\\|').slice(0, 80)
    lines.push(
      `| ${idx + 1} | ${q} | ${fmt(row.a?.faithfulness)} | ${fmt(row.a?.context_recall)} | ${fmt(row.b?.faithfulness)} | ${fmt(row.b?.context_recall)} |`,
    )
  })
  lines.push('')
  return lines.join('\n')
}
