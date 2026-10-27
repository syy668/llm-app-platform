// 极简 Markdown 渲染（自包含，无第三方依赖）。
// 覆盖后端 report 路由输出格式：标题(#) / 表格(|) / 代码块(```) / 引用(>) / 列表(-)。
// 先做 HTML 转义，避免 LLM 输出注入任意 HTML。

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function inline(s: string): string {
  return s
    .replace(/`([^`]+)`/g, '<code style="background:#F2F3F5;padding:1px 6px;border-radius:4px">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
}

export function renderMarkdown(text: string): string {
  const lines = String(text ?? '').split('\n')
  const out: string[] = []
  let inTable = false
  let inCode = false
  let codeBuf: string[] = []

  for (const raw of lines) {
    const line = raw.replace(/\r$/, '')
    const trimmed = line.trim()

    if (trimmed.startsWith('```')) {
      if (inCode) {
        out.push('<pre><code>' + escapeHtml(codeBuf.join('\n')) + '</code></pre>')
        codeBuf = []
        inCode = false
      } else {
        if (inTable) {
          out.push('</table>')
          inTable = false
        }
        inCode = true
      }
      continue
    }
    if (inCode) {
      codeBuf.push(line)
      continue
    }

    // 标题
    if (/^#{1,6}\s/.test(trimmed)) {
      if (inTable) {
        out.push('</table>')
        inTable = false
      }
      const level = trimmed.match(/^#+/)![0].length
      const h = inline(escapeHtml(trimmed.replace(/^#+\s*/, '')))
      out.push(`<h${level} style="margin:8px 0 6px;font-weight:700">${h}</h${level}>`)
      continue
    }

    // 表格
    if (trimmed.includes('|')) {
      const cells = trimmed.split('|').map((c) => c.trim())
      if (cells[0] === '') cells.shift()
      if (cells[cells.length - 1] === '') cells.pop()
      if (cells.every((c) => /^-{2,}$/.test(c))) continue // 分隔行
      if (!inTable) {
        out.push('<table class="md-table">')
        inTable = true
      }
      out.push('<tr>' + cells.map((c) => `<td>${inline(escapeHtml(c))}</td>`).join('') + '</tr>')
      continue
    }

    if (inTable) {
      out.push('</table>')
      inTable = false
    }

    if (trimmed === '') {
      out.push('<div style="height:8px"></div>')
      continue
    }
    // 引用
    if (/^>\s?/.test(trimmed)) {
      out.push(
        '<blockquote style="border-left:3px solid #E5E6EB;margin:8px 0;padding-left:10px;color:#86909C">' +
          inline(escapeHtml(trimmed.replace(/^>\s?/, ''))) +
          '</blockquote>',
      )
      continue
    }
    // 列表
    if (/^[-*]\s/.test(trimmed)) {
      out.push('<div style="padding-left:8px">• ' + inline(escapeHtml(trimmed.replace(/^[-*]\s/, ''))) + '</div>')
      continue
    }
    out.push('<div>' + inline(escapeHtml(trimmed)) + '</div>')
  }

  if (inTable) out.push('</table>')
  if (inCode) out.push('<pre><code>' + escapeHtml(codeBuf.join('\n')) + '</code></pre>')
  return out.join('')
}
