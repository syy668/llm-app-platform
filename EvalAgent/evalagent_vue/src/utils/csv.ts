/**
 * CSV 解析与校验（纯前端，不调后端）：
 * - 表头 question,ground_truth（UTF-8）；可选第三列 stratum（分层难例标签，B2-4）
 * - 校验：空文件 / 表头缺失 / question 为空，全部拒绝
 * - ground_truth 映射为上游 reference_answer；stratum 非空时随样本透传后端
 * - 单数据集上限 500 条（与上游 ItemsImport max_length=500 对齐）
 */
import type { ItemInput } from '@/api/types'

export const MAX_DATASET_ROWS = 500

/** 分层标签长度上限（与后端 ItemInput.stratum Field(max_length=64) 对齐） */
export const MAX_STRATUM_LENGTH = 64

export interface CsvParseResult {
  ok: boolean
  errors: string[]
  items: ItemInput[]
}

/** 支持 RFC4180 双引号转义的极简 CSV 行解析 */
function splitCsvLine(line: string): string[] {
  const cells: string[] = []
  let cur = ''
  let inQuote = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (inQuote) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          cur += '"'
          i += 1
        } else {
          inQuote = false
        }
      } else {
        cur += ch
      }
    } else if (ch === '"') {
      inQuote = true
    } else if (ch === ',') {
      cells.push(cur)
      cur = ''
    } else {
      cur += ch
    }
  }
  cells.push(cur)
  return cells.map((c) => c.trim())
}

export function parseDatasetCsv(text: string): CsvParseResult {
  const errors: string[] = []
  const items: ItemInput[] = []

  const lines = text
    .replace(/^\uFEFF/, '') // 去除 BOM
    .split(/\r?\n/)
    .filter((l) => l.trim().length > 0)

  if (lines.length === 0) {
    return { ok: false, errors: ['CSV 文件为空'], items }
  }

  const headers = splitCsvLine(lines[0]).map((h) => h.toLowerCase())
  const qIdx = headers.indexOf('question')
  const gIdx = headers.indexOf('ground_truth')
  // stratum 为可选列：存在即解析，空值视为未分层（后端存 NULL）
  const sIdx = headers.indexOf('stratum')
  if (qIdx === -1 || gIdx === -1) {
    return {
      ok: false,
      errors: [`表头必须为 question,ground_truth（可选追加 stratum 列，当前为：${headers.join(',')}）`],
      items,
    }
  }

  for (let i = 1; i < lines.length; i++) {
    const cells = splitCsvLine(lines[i])
    const question = cells[qIdx] ?? ''
    const groundTruth = cells[gIdx] ?? ''
    if (!question) {
      errors.push(`第 ${i + 1} 行 question 为空，已拒绝`)
      continue
    }
    if (!groundTruth) {
      errors.push(`第 ${i + 1} 行 ground_truth 为空，已拒绝`)
      continue
    }
    // 分层标签：空串 = 未分层；超长整行拒绝（后端 max_length=64 会 422，前置拦截给出友好提示）
    const item: ItemInput = { question, reference_answer: groundTruth }
    if (sIdx !== -1) {
      const stratum = cells[sIdx] ?? ''
      if (stratum) {
        if (stratum.length > MAX_STRATUM_LENGTH) {
          errors.push(`第 ${i + 1} 行 stratum 超过 ${MAX_STRATUM_LENGTH} 字符上限，已拒绝`)
          continue
        }
        item.stratum = stratum
      }
    }
    items.push(item)
  }

  if (items.length > MAX_DATASET_ROWS) {
    errors.push(`样本数 ${items.length} 超过单数据集上限 ${MAX_DATASET_ROWS} 条`)
    items.length = 0
  }
  if (items.length === 0 && errors.length === 0) {
    errors.push('未解析到有效样本行')
  }

  return { ok: items.length > 0 && errors.length === 0, errors, items }
}
