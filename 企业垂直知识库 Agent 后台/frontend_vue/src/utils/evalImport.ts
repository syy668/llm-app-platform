// 评测集批量导入的纯文本解析（不依赖 Vue / DOM，纯函数，便于复用与验证）。
//
// 支持两种输入：
//   1) JSON：`[{question, reference_answer, stratum?}, ...]` 或 `{"items": [...]}`
//   2) CSV ：首行可为表头（question,reference_answer,stratum），也可省略表头按列序解析；
//            支持双引号包裹、字段内逗号/换行、`""` 转义、CRLF 与 UTF-8 BOM。
//
// 设计取舍：**解析放前端、校验也放前端**，后端只做最终兜底。
// 理由：批量导入最常见的失败不是接口挂了，而是用户从 Excel 复制出来带了表头、
// 空行、全角逗号，或者参考答案列整列空着（空答案会让该样本的 context_recall
// 永远算不出来）。这些必须在提交前就逐行报出来，否则用户只看到一个 422。

/** 与后端 `ItemsImport.items` 的 max_length=500 对齐，超限直接挡在提交前 */
export const MAX_IMPORT_ITEMS = 500

export interface ImportRow {
  question: string
  reference_answer: string
  stratum: string | null
}

export type ImportFormat = 'json' | 'csv' | 'unknown'

export interface ParseOutcome {
  format: ImportFormat
  items: ImportRow[]
  /** 逐行错误，形如「第 3 行：question 为空」；非空时禁止提交 */
  errors: string[]
}

type RawRow = Record<string, string>

/** 表头别名：中英文都认，避免用户把 Excel 中文表头直接粘进来就报错 */
const HEADER_ALIASES: Record<string, keyof ImportRow> = {
  question: 'question',
  问题: 'question',
  提问: 'question',
  q: 'question',
  reference_answer: 'reference_answer',
  referenceanswer: 'reference_answer',
  answer: 'reference_answer',
  参考答案: 'reference_answer',
  标准答案: 'reference_answer',
  答案: 'reference_answer',
  stratum: 'stratum',
  layer: 'stratum',
  分层: 'stratum',
  分层标签: 'stratum',
}

function normalizeHeader(cell: string): keyof ImportRow | null {
  const key = cell.trim().toLowerCase().replace(/\s+/g, '')
  return HEADER_ALIASES[key] ?? null
}

/** CSV 全文字符级扫描：能正确处理引号内的逗号与换行（按行 split 会撕坏多行答案） */
function parseCsvRows(text: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cell += '"'
          i += 1
        } else {
          quoted = false
        }
      } else {
        cell += ch
      }
      continue
    }
    if (ch === '"') {
      quoted = true
    } else if (ch === ',') {
      row.push(cell)
      cell = ''
    } else if (ch === '\n') {
      row.push(cell)
      rows.push(row)
      row = []
      cell = ''
    } else if (ch !== '\r') {
      cell += ch
    }
  }
  if (cell.length || row.length) {
    row.push(cell)
    rows.push(row)
  }
  return rows
}

function rowsFromCsv(text: string): RawRow[] {
  const grid = parseCsvRows(text).filter((r) => r.some((c) => c.trim().length > 0))
  if (!grid.length) return []

  const first = grid[0]
  const mapped = first.map((c) => normalizeHeader(c))
  const hasHeader = mapped.some((m) => m !== null)
  const body = hasHeader ? grid.slice(1) : grid
  // 无表头时按列序约定：[question, reference_answer, stratum]；有表头时未识别的列按列序兜底（超过 3 列则忽略）
  const defaultOrder: Array<keyof ImportRow> = ['question', 'reference_answer', 'stratum']
  const keys: Array<keyof ImportRow | null> = hasHeader
    ? first.map((_, idx) => mapped[idx] ?? (idx < defaultOrder.length ? defaultOrder[idx] : null))
    : defaultOrder

  return body.map((cells) => {
    const record: RawRow = {}
    keys.forEach((key, idx) => {
      if (!key) return
      record[key] = (cells[idx] ?? '').trim()
    })
    return record
  })
}

function rowsFromJson(text: string): RawRow[] {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (err) {
    throw new Error(`JSON 解析失败：${(err as Error).message}`)
  }
  const list = Array.isArray(parsed)
    ? parsed
    : parsed && typeof parsed === 'object' && Array.isArray((parsed as { items?: unknown }).items)
      ? ((parsed as { items: unknown[] }).items)
      : null
  if (!list) {
    throw new Error('JSON 必须是对象数组，或形如 {"items": [...]} 的对象')
  }
  return list.map((entry) => {
    const record: RawRow = {}
    if (entry && typeof entry === 'object') {
      Object.entries(entry as Record<string, unknown>).forEach(([k, v]) => {
        const key = normalizeHeader(k)
        if (key) record[key] = v == null ? '' : String(v).trim()
      })
    }
    return record
  })
}

/** 逐行校验并产出可直接提交 ItemsImport 的 items */
function buildOutcome(rows: RawRow[], format: ImportFormat): ParseOutcome {
  const items: ImportRow[] = []
  const errors: string[] = []

  rows.forEach((row, idx) => {
    const lineNo = idx + 1
    const question = (row.question ?? '').trim()
    const answer = (row.reference_answer ?? '').trim()
    const stratum = (row.stratum ?? '').trim()
    if (!question && !answer) {
      // 整行空白（Excel 复制常见）直接跳过，不当作错误
      return
    }
    if (!question) {
      errors.push(`第 ${lineNo} 行：question 为空`)
      return
    }
    if (!answer) {
      // 与服务端口径一致：空答案的样本 context_recall 无法计算，不允许导入
      errors.push(`第 ${lineNo} 行：reference_answer 为空（缺标准答案的样本无法评测）`)
      return
    }
    items.push({ question, reference_answer: answer, stratum: stratum || null })
  })

  if (items.length > MAX_IMPORT_ITEMS) {
    errors.push(`单次最多导入 ${MAX_IMPORT_ITEMS} 条，当前有效 ${items.length} 条，请分批导入`)
  }
  return { format, items, errors }
}

export function parseEvalImport(raw: string): ParseOutcome {
  const text = (raw || '').replace(/^\uFEFF/, '').trim()
  if (!text) {
    return { format: 'unknown', items: [], errors: ['内容为空，请粘贴 JSON 或 CSV'] }
  }
  const format: ImportFormat = /^[[{]/.test(text) ? 'json' : 'csv'
  try {
    const rows = format === 'json' ? rowsFromJson(text) : rowsFromCsv(text)
    return buildOutcome(rows, format)
  } catch (err) {
    return { format, items: [], errors: [(err as Error).message] }
  }
}

/** 解析结果的一句话摘要，供 UI 回显 */
export function describeOutcome(outcome: ParseOutcome): string {
  const tag = outcome.format === 'json' ? 'JSON' : outcome.format === 'csv' ? 'CSV' : '未识别'
  if (outcome.errors.length) {
    return `${tag} · ${outcome.errors.length} 处问题待修正`
  }
  return `${tag} · 可导入 ${outcome.items.length} 条`
}
