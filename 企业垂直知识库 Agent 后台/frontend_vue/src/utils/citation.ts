// 溯源渲染工具：把答案里的引用信息统一渲染成 chip。
//
// 两种形态：
//   ① 结构化序号  [来源2]              —— 对应后端 sources 的 index
//   ② 纯文本溯源  来源：《文档》第4块    —— 模型经常不输出 ①，只写文档名 + 块号
//
// 【v1.11】块号统一 1-based；且**没有 sources 时不渲染成可点击 chip**。
// 老消息（v1.10 之前产生的）库里没有来源快照，可点击等于诱导用户白点一次。
//
// 抽成纯函数便于单测：组件里 `v-html` 的内容无法绑定事件，也因此更需要确定性的产出。

/** 与 Chat.vue 的 SourceItem 结构保持一致（只依赖这几个字段） */
export interface CiteSource {
  index: number
  document_title?: string
  /** 存储序号（0-based），仅用于排查 */
  chunk_index?: number
  /** 展示序号（1-based），后端下发，前端一律用它展示与匹配 */
  chunk_no?: number
  content?: string
  score?: number
  group?: 'A' | 'B'
}

/** 取展示用块号：优先后端的 chunk_no，兜底用 chunk_index + 1（兼容旧数据） */
export function sourceChunkNo(s: CiteSource | null | undefined): number {
  if (!s) return 0
  if (typeof s.chunk_no === 'number') return s.chunk_no
  if (typeof s.chunk_index === 'number') return s.chunk_index + 1
  return 0
}

/**
 * 纯文本溯源正则：
 *   「来源：《X》第4块」「来源3：《X》第2块」「《X》 第 4 块」「《X》第2页」
 * - 前缀「来源[:：]? 数字?」可有可无；
 * - 单位同时接受「块」和「页」——实测模型会把块号写成页码，按块号尽力匹配；
 * - 文档名限制 1~80 字且不含换行/右书名号，避免贪婪跨行误吞整段。
 */
const PLAIN_CITE_RE = /(?:来源\s*[:：]?\s*\d*\s*[:：]?\s*)?《([^》\n]{1,80})》\s*第\s*(\d{1,4})\s*(块|页)/g

/** 小文档图标（内联 SVG，避免额外依赖与 emoji 字体差异） */
const CITE_ICON =
  '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true" ' +
  'style="flex:none;margin-right:3px">' +
  '<path d="M4 1.8h5l3 3v9.4H4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>' +
  '<path d="M9 1.8v3h3" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>' +
  '</svg>'

/** HTML 属性转义（用于 title 等需要完整转义的场景） */
export function escAttr(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

/** 只在非 <pre> 区域做替换，避免把代码块里的示例文本也变成标签 */
function replaceOutsidePre(html: string, fn: (seg: string) => string): string {
  return html
    .split(/(<pre>[\s\S]*?<\/pre>)/)
    .map((seg, i) => (i % 2 === 1 ? seg : fn(seg)))
    .join('')
}

/** 文档名比对归一化：去空白、忽略大小写、忽略扩展名（模型常漏写 .pdf/.md） */
export function normalizeDocName(s: string): string {
  return String(s ?? '')
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '')
    .replace(/\.(pdf|docx?|md|txt|xlsx?)$/i, '')
}

/**
 * 把纯文本溯源替换成 chip。
 * @param clickable 本次回答是否带来源快照。false 时产出静态样式：
 *                  不加 data-*、不加 pointer 光标、不提示「点击查看」——
 *                  让用户一眼看出「这条没有原文可看」，而不是点了才弹一句提示。
 */
export function chipifyPlainCitations(html: string, clickable = true): string {
  return replaceOutsidePre(html, (seg) =>
    seg.replace(PLAIN_CITE_RE, (_all, doc: string, no: string, unit: string) => {
      const title = doc.trim()
      const chunk = Number(no)
      const label = `<span class="plain-cite-t">《${title}》第 ${chunk} ${unit}</span>`
      if (!clickable) {
        return `<span class="plain-cite is-static" title="本条回答未保留来源快照，无法展开原文">${CITE_ICON}${label}</span>`
      }
      // data-doc 用完整转义：文档名里可能含引号，会撞坏属性
      return (
        `<span class="plain-cite" data-doc="${escAttr(title)}" ` +
        `data-chunk="${chunk}" data-unit="${unit}" title="点击查看该片段原文">` +
        `${CITE_ICON}${label}</span>`
      )
    }),
  )
}

/**
 * 渲染答案：markdown HTML -> 纯文本溯源 chip -> [来源N] chip。
 *
 * 顺序不能反：先把纯文本转成标签，我们生成的标签里就不再有`《》第N块`裸文本，
 * 后续 [来源N] 替换不会误伤；反之 title 属性会被二次匹配。
 *
 * 无 sources 时两种形态都降级为静态样式（不可点），避免「点了才发现没有」。
 */
export function renderAnswerHtml(content: string, sources?: CiteSource[]): string {
  const clickable = !!sources?.length
  let html = chipifyPlainCitations(content, clickable)

  html = html.replace(/\[来源(\d+)\]/g, (_all, d: string) => {
    const idx = Number(d)
    if (!clickable) {
      return `<span class="cite-chip is-static" title="本条回答未保留来源快照，无法展开原文">来源${idx}</span>`
    }
    const src = sources?.find((s) => s.index === idx)
    const no = sourceChunkNo(src)
    const tip = src?.document_title
      ? no > 0
        ? `《${src.document_title}》第 ${no} 块`
        : `《${src.document_title}》`
      : '查看引用原文'
    return `<span class="cite-chip" data-cite="${idx}" title="${escAttr(tip)}">来源${idx}</span>`
  })
  return html
}

/**
 * 按「文档名 + 块号」在本次回答的 sources 里匹配片段。
 * 先精确匹配，再按归一化文档名匹配（模型常漏写扩展名），两者都要求块号一致。
 * 块号一律用展示号（1-based）比较，和后端下发、模型抄写保持一致。
 * 返回 { source: 命中的片段 | null, sameDoc: 该文档是否存在但块号对不上 }
 */
export function matchSourceByDocChunk(
  sources: CiteSource[] | undefined,
  doc: string,
  chunk: number,
): { source: CiteSource | null; sameDoc: boolean } {
  const list = sources ?? []
  const target = String(doc ?? '').trim()
  const want = Number(chunk)

  const exact =
    list.find((s) => (s.document_title || '').trim() === target && sourceChunkNo(s) === want) ?? null
  if (exact) return { source: exact, sameDoc: true }

  const loose =
    list.find(
      (s) => normalizeDocName(s.document_title || '') === normalizeDocName(target) && sourceChunkNo(s) === want,
    ) ?? null
  if (loose) return { source: loose, sameDoc: true }

  const sameDoc = list.some((s) => normalizeDocName(s.document_title || '') === normalizeDocName(target))
  return { source: null, sameDoc }
}

/**
 * 同一文档在本次 sources 里**实际可展开**的块号（升序去重，1-based）。
 *
 * 用途：块号对不上时，把「本文档这次到底能看哪几块」明确告诉用户。
 * 之前只有一句「没有第 2 块」，用户既不知道有没有别的块，也没有入口 —— 拦得对但帮不上忙。
 */
export function siblingChunkNos(sources: CiteSource[] | undefined, doc: string): number[] {
  const target = normalizeDocName(doc)
  if (!target) return []
  const nos = (sources ?? [])
    .filter((s) => normalizeDocName(s.document_title || '') === target)
    .map(sourceChunkNo)
    .filter((n) => n > 0)
  return [...new Set(nos)].sort((a, b) => a - b)
}

/**
 * 块号对不上时的兜底片段：同文档里块号**最接近**的那个。
 *
 * 取舍：距离相同时取更小的块号（更靠前的原文通常覆盖定义与主条款，
 * 例如「人事制度手册」第 2 块找不到时，第 1 块比第 3 块更可能包含答案）。
 * 只在同文档确有片段时返回，绝不跨文档猜。
 */
export function nearestSourceByDoc(
  sources: CiteSource[] | undefined,
  doc: string,
  chunk: number,
): CiteSource | null {
  const target = normalizeDocName(doc)
  const want = Number(chunk)
  if (!target || !Number.isFinite(want)) return null
  const same = (sources ?? []).filter(
    (s) => normalizeDocName(s.document_title || '') === target && sourceChunkNo(s) > 0,
  )
  if (!same.length) return null
  return same.reduce((best, cur) => {
    const dCur = Math.abs(sourceChunkNo(cur) - want)
    const dBest = Math.abs(sourceChunkNo(best) - want)
    if (dCur !== dBest) return dCur < dBest ? cur : best
    return sourceChunkNo(cur) < sourceChunkNo(best) ? cur : best
  })
}

/** 去掉答案里的引用标注，只留正文：`（来源：《X》第2块）`、`[来源1]` */
export function stripCitationMarks(answer: string): string {
  return (answer || '')
    .replace(/[（(]\s*来源\s*[:：][^）)]*[）)]/g, ' ')
    .replace(/\[来源\s*\d+\]/g, ' ')
}

/**
 * 内容重合度（0~1）：把答案正文切成 n-gram，看有多少落在候选片段里。
 *
 * 为什么用 8-gram：中文 8 字基本能锁住一个完整语义片段，既能容忍模型改写
 * 个别虚词，又不会像单字/双字那样被「工资条」这类高频词刷高。
 * 实测《人事制度手册.md》真实案例：答案与正确块重合 73.7%，另两块均 0%。
 */
export function chunkOverlap(answerBody: string, chunkText: string, n = 8): number {
  const a = (answerBody || '').replace(/\s+/g, '')
  const b = (chunkText || '').replace(/\s+/g, '')
  if (a.length < n + 8 || !b) return 0
  const grams = new Set<string>()
  for (let i = 0; i + n <= a.length; i += 1) grams.add(a.slice(i, i + n))
  if (grams.size < 5) return 0
  let hit = 0
  for (const g of grams) if (b.includes(g)) hit += 1
  return hit / grams.size
}

/**
 * 块号对不上时，**按内容**定位答案真正出自哪一块。
 *
 * 比 `nearestSourceByDoc` 的块号距离可靠得多：模型把「## 2. 工资条查询」的章节号
 * 写成「第2块」时，块号距离会把第 2 块判成离第 1、3 块一样近（并列取小号 → 第1块），
 * 只是碰巧蒙对；换成内容重合度则是 73.7% 对 0% 的明确差距。
 *
 * 重合度为 0（答案全是模型自己的措辞、与原文无字面交集）时返回 null，
 * 由调用方退回块号距离兜底。
 */
export function pickSourceByContent(
  sources: CiteSource[] | undefined,
  doc: string,
  answerText: string,
): { source: CiteSource; overlap: number } | null {
  const target = normalizeDocName(doc)
  if (!target) return null
  const body = stripCitationMarks(answerText)
  const same = (sources ?? []).filter(
    (s) => normalizeDocName(s.document_title || '') === target && sourceChunkNo(s) > 0,
  )
  if (!same.length) return null

  let best: { source: CiteSource; overlap: number } | null = null
  for (const s of same) {
    const overlap = chunkOverlap(body, s.content || '')
    if (
      !best ||
      overlap > best.overlap ||
      (overlap === best.overlap && sourceChunkNo(s) < sourceChunkNo(best.source))
    ) {
      best = { source: s, overlap }
    }
  }
  return best && best.overlap > 0 ? best : null
}
