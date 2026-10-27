/**
 * 数据集文件解析（纯前端，不调后端）：CSV / JSON / JSONL 三格式统一出口。
 *
 * 为什么需要 JSON：
 *   - CSV 只有 3 列（question / ground_truth / stratum），表达不了 `reference_contexts`、
 *     `difficulty`、`item_type` —— 而这些后端 `ItemCreate` 本来就支持；
 *   - 从项目一 / 项目三导出的评测集本来就是 JSON 数组，先转 CSV 再上传是多余的一道手工活。
 *
 * 三格式的口径**必须一致**：question 与 ground_truth 都是必填（缺 ground_truth 会让
 * context_recall 永远算不出来，属于"能导入但评不了"的坑，前置拒绝比事后排查便宜）。
 */
import type { ItemInput } from '@/api/types'
import { MAX_DATASET_ROWS, MAX_STRATUM_LENGTH, parseDatasetCsv } from '@/utils/csv'

export { MAX_DATASET_ROWS }

/** 后端 ItemCreate.item_type 的合法取值 */
const ITEM_TYPES = ['rag_qa', 'tool_call', 'multi_turn', 'agent_task'] as const

export interface DatasetParseResult {
  ok: boolean
  /** 阻断性问题（有任意一条就不允许导入） */
  errors: string[]
  /** 非阻断提示（如「difficulty 越界已忽略」），不影响导入 */
  warnings: string[]
  items: ItemInput[]
}

/** 报错上限：500 行全错时不要把整个文件刷到界面上 */
const MAX_ERRORS = 50

// ---------------------------------------------------------------- 入口
export function parseDatasetFile(text: string, filename: string): DatasetParseResult {
  const lower = filename.toLowerCase()
  if (lower.endsWith('.jsonl') || lower.endsWith('.ndjson')) return parseDatasetJsonl(text, true)
  if (lower.endsWith('.json')) return parseDatasetJson(text)
  if (lower.endsWith('.csv')) {
    const csv = parseDatasetCsv(text)
    return { ok: csv.ok, errors: csv.errors, warnings: [], items: csv.items }
  }
  return {
    ok: false,
    errors: ['仅支持 .csv / .json / .jsonl 文件（UTF-8 编码）'],
    warnings: [],
    items: [],
  }
}

// ---------------------------------------------------------------- JSON
/**
 * 支持的顶层形态（三种都常见，全部兼容）：
 *   1. 数组             [{...}, {...}]
 *   2. 带包装的对象      {"name": "...", "items": [{...}]}   （items / samples / data / list / rows 任一）
 *   3. 单个样本对象      {"question": "...", "ground_truth": "..."}
 */
export function parseDatasetJson(text: string): DatasetParseResult {
  const raw = stripBom(text).trim()
  if (!raw) return { ok: false, errors: ['JSON 文件为空'], warnings: [], items: [] }

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch (err) {
    // 常见手误：把 JSONL 存成了 .json
    const asJsonl = parseDatasetJsonl(raw, false)
    if (asJsonl.ok) {
      asJsonl.warnings.unshift('文件内容是「一行一个 JSON」，已按 JSONL 解析')
      return asJsonl
    }
    return {
      ok: false,
      errors: [`JSON 解析失败：${err instanceof Error ? err.message : '格式非法'}`],
      warnings: [],
      items: [],
    }
  }

  const rows = toRows(parsed)
  if (rows === null) {
    return {
      ok: false,
      errors: [
        'JSON 结构不支持：请用「样本数组」，或 {"items": [...]} 这类带数组字段的对象',
      ],
      warnings: [],
      items: [],
    }
  }
  return buildItems(rows)
}

/** 一行一个 JSON 对象（JSONL / NDJSON，适合几千条的大集） */
export function parseDatasetJsonl(text: string, reportBadLines = true): DatasetParseResult {
  const lines = stripBom(text)
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0)
  if (lines.length === 0) return { ok: false, errors: ['JSONL 文件为空'], warnings: [], items: [] }

  const rows: unknown[] = []
  const errors: string[] = []
  lines.forEach((line, i) => {
    try {
      rows.push(JSON.parse(line))
    } catch {
      if (reportBadLines) errors.push(`第 ${i + 1} 行不是合法 JSON，已跳过`)
    }
  })
  if (rows.length === 0) {
    return { ok: false, errors: errors.length ? errors : ['未解析到任何 JSON 对象'], warnings: [], items: [] }
  }
  const built = buildItems(rows)
  return {
    ok: built.ok && errors.length === 0,
    errors: [...errors, ...built.errors],
    warnings: built.warnings,
    items: built.items,
  }
}

// ---------------------------------------------------------------- 内部
function stripBom(text: string): string {
  return text.replace(/^\uFEFF/, '')
}

const CONTAINER_KEYS = ['items', 'samples', 'data', 'list', 'rows', 'records']

function toRows(parsed: unknown): unknown[] | null {
  if (Array.isArray(parsed)) return parsed
  if (parsed && typeof parsed === 'object') {
    const obj = parsed as Record<string, unknown>
    for (const key of CONTAINER_KEYS) {
      if (Array.isArray(obj[key])) return obj[key] as unknown[]
    }
    // 单个样本对象（有 question 就算）
    if (pickField(obj, FIELDS.question) !== '') return [obj]
  }
  return null
}

/** 字段别名：大小写、下划线、驼峰、中英文都认（各家导出的表头五花八门） */
const FIELDS: Record<string, string[]> = {
  question: ['question', 'query', 'q', '问题', '提问'],
  ground_truth: [
    'ground_truth', 'groundtruth', 'groundTruth', 'reference_answer', 'referenceanswer',
    'answer', 'gold', '标准答案', '参考答案', '答案',
  ],
  contexts: ['reference_contexts', 'referencecontexts', 'contexts', 'gold_contexts', '参考上下文'],
  stratum: ['stratum', 'layer', 'category', '分层', '类别'],
  difficulty: ['difficulty', '难度'],
  item_type: ['item_type', 'itemtype', 'type', '样本类型'],
}

function normalizeKey(key: string): string {
  return key.toLowerCase().replace(/[_\-\s]/g, '')
}

function pickRaw(obj: Record<string, unknown>, aliases: string[]): unknown {
  const wanted = new Set(aliases.map(normalizeKey))
  for (const key of Object.keys(obj)) {
    if (wanted.has(normalizeKey(key))) return obj[key]
  }
  return undefined
}

/** 取字符串结果：数组取第一项；对象/数字转字符串（防止 description 里放了数字） */
function pickField(obj: Record<string, unknown>, aliases: string[]): string {
  const value = pickRaw(obj, aliases)
  if (value === undefined || value === null) return ''
  if (Array.isArray(value)) return value.length ? String(value[0]).trim() : ''
  return String(value).trim()
}

function buildItems(rows: unknown[]): DatasetParseResult {
  const items: ItemInput[] = []
  const errors: string[] = []
  const warnings: string[] = []

  rows.forEach((row, index) => {
    const seq = index + 1
    if (!row || typeof row !== 'object' || Array.isArray(row)) {
      errors.push(`第 ${seq} 条不是 JSON 对象，已跳过`)
      return
    }
    const obj = row as Record<string, unknown>
    const question = pickField(obj, FIELDS.question)
    const groundTruth = pickField(obj, FIELDS.ground_truth)

    if (!question) {
      errors.push(`第 ${seq} 条 question 为空，已拒绝`)
      return
    }
    if (!groundTruth) {
      errors.push(`第 ${seq} 条 ground_truth 为空，已拒绝`)
      return
    }

    const item: ItemInput = { question, reference_answer: groundTruth }

    // ---- 可选：参考上下文（数组或单个字符串都收）----
    const ctxRaw = pickRaw(obj, FIELDS.contexts)
    if (Array.isArray(ctxRaw)) {
      const contexts = ctxRaw.map((c) => String(c ?? '').trim()).filter((c) => c.length > 0)
      if (contexts.length) item.reference_contexts = contexts
    } else if (typeof ctxRaw === 'string' && ctxRaw.trim()) {
      item.reference_contexts = [ctxRaw.trim()]
    }

    // ---- 可选：分层标签（超长整条拒绝，后端 max_length=64 会 422）----
    const stratum = pickField(obj, FIELDS.stratum)
    if (stratum) {
      if (stratum.length > MAX_STRATUM_LENGTH) {
        errors.push(`第 ${seq} 条 stratum 超过 ${MAX_STRATUM_LENGTH} 字符上限，已拒绝`)
        return
      }
      item.stratum = stratum
    }

    // ---- 可选：难度 1~5（越界忽略并提示，不整条拒绝：难度不影响评测能否跑）----
    const diffRaw = pickRaw(obj, FIELDS.difficulty)
    if (diffRaw !== undefined && diffRaw !== null && String(diffRaw).trim() !== '') {
      const difficulty = Number(diffRaw)
      if (Number.isInteger(difficulty) && difficulty >= 1 && difficulty <= 5) {
        item.difficulty = difficulty
      } else {
        warnings.push(`第 ${seq} 条 difficulty=${String(diffRaw)} 不在 1~5 内，已忽略该字段`)
      }
    }

    // ---- 可选：样本类型（非法值忽略并提示，默认 rag_qa 由后端补）----
    const typeRaw = pickField(obj, FIELDS.item_type)
    if (typeRaw) {
      if ((ITEM_TYPES as readonly string[]).includes(typeRaw)) {
        item.item_type = typeRaw
      } else {
        warnings.push(`第 ${seq} 条 item_type=${typeRaw} 非法（可选 ${ITEM_TYPES.join(' / ')}），已忽略该字段`)
      }
    }

    items.push(item)
  })

  if (items.length > MAX_DATASET_ROWS) {
    errors.push(`样本数 ${items.length} 超过单数据集上限 ${MAX_DATASET_ROWS} 条`)
    items.length = 0
  }
  if (items.length === 0 && errors.length === 0) {
    errors.push('未解析到有效样本')
  }
  if (errors.length > MAX_ERRORS) {
    const total = errors.length
    errors.length = MAX_ERRORS
    errors.push(`…… 共 ${total} 处问题，仅显示前 ${MAX_ERRORS} 条`)
  }

  return { ok: items.length > 0 && errors.length === 0, errors, warnings, items }
}
