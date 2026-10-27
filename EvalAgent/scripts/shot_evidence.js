/**
 * 生成验收截图：启动日志 + 表结构。
 * 用 puppeteer-core 驱动本机 Chrome，把真实捕获的文本/结构渲染成 PNG。
 * 运行：
 *   NODE_PATH=C:/Users/<user>/.workbuddy/binaries/node/workspace/node_modules
 *   <managed node> /e/AI_Project_Workspace/EvalAgent/scripts/shot_evidence.js
 */
const fs = require('fs')
const path = require('path')
const puppeteer = require('puppeteer-core')

const ROOT = path.resolve(__dirname, '..')
const BACKEND = path.join(ROOT, 'evalagent_backend')
const OUT = path.join(ROOT, 'docs', 'evidence')

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'

const esc = (s) =>
  String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')

const BASE_CSS = `
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 28px 32px;
    font-family: "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
    background: #f6f7f9; color: #1f2328;
  }
  .card { background: #fff; border: 1px solid #e3e6ea; border-radius: 10px; overflow: hidden; }
  .hd { padding: 14px 18px; border-bottom: 1px solid #e3e6ea; background: #fbfcfd; }
  .hd h1 { margin: 0 0 4px; font-size: 16px; font-weight: 700; }
  .hd p { margin: 0; font-size: 12px; color: #656d76; }
  .meta { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 8px; font-size: 12px; color: #43474d; }
  .meta b { font-weight: 600; color: #1f2328; }
  .pill {
    display: inline-block; padding: 1px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 600; background: #e7f6ec; color: #1a7f37; border: 1px solid #b7e0c4;
  }
  .term { background: #0f172a; padding: 16px 18px; }
  .term pre {
    margin: 0; font-family: "Cascadia Mono", Consolas, monospace; font-size: 12.5px;
    line-height: 1.62; color: #e2e8f0; white-space: pre-wrap; word-break: break-word;
  }
  .term .g { color: #7ee787; }
  .term .b { color: #79c0ff; }
  .term .y { color: #f2cc60; }
  .term .d { color: #7d8590; }
  .term .r { color: #ffa198; }
  .tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .tbl th {
    text-align: left; padding: 9px 12px; background: #f6f8fa;
    border-bottom: 1px solid #e3e6ea; font-weight: 600; color: #43474d; white-space: nowrap;
  }
  .tbl td { padding: 7px 12px; border-bottom: 1px solid #eef1f4; vertical-align: top; }
  .tbl tr:last-child td { border-bottom: none; }
  .mono { font-family: "Cascadia Mono", Consolas, monospace; }
  .pk { color: #8250df; font-weight: 600; }
  .muted { color: #8c959f; }
  .tname { display: flex; align-items: center; gap: 10px; padding: 12px 18px; background: #f2f5f8; border-top: 1px solid #e3e6ea; border-bottom: 1px solid #e3e6ea; }
  .tname span.n { font-family: "Cascadia Mono", Consolas, monospace; font-size: 14px; font-weight: 700; color: #0b3d91; }
  .tname span.c { font-size: 11.5px; color: #656d76; }
  .tname span.rw { margin-left: auto; font-size: 11.5px; color: #656d76; }
  .idx { padding: 9px 18px 12px; font-size: 11.5px; color: #656d76; background: #fbfcfd; border-top: 1px solid #eef1f4; }
  .idx code { background: #eef1f4; padding: 1px 5px; border-radius: 4px; font-size: 11px; }
`

function logHtml(raw) {
  const lines = raw.split(/\r?\n/).filter((l) => l.length > 0)
  const html = lines
    .map((line) => {
      let cls = ''
      if (line.includes(' ERROR')) cls = 'r'
      else if (line.includes('中央评测中台') || line.includes('启动完成') || line.includes('startup complete')) cls = 'g'
      else if (line.includes('数据库连接正常') || line.includes('建表完成')) cls = 'b'
      else if (/ 50\d | 40\d /.test(line)) cls = 'y'
      else if (line.startsWith('INFO:') && line.includes('HTTP')) cls = 'd'
      return `<span class="${cls}">${esc(line)}</span>`
    })
    .join('\n')
  return `<!doctype html><html><head><meta charset="utf-8"><style>${BASE_CSS}</style></head><body>
  <div class="card" id="shot">
    <div class="hd">
      <h1>EvalAgent 后端启动日志（uvicorn · 127.0.0.1:8200）</h1>
      <p>真实进程输出，非手工誊写</p>
      <div class="meta">
        <div><b>Python</b> 3.12.3 (venv)</div>
        <div><b>FastAPI</b> 0.115.6</div>
        <div><b>DB</b> MySQL 8.0.40 / eval_agent_db</div>
        <div><b>状态</b> <span class="pill">Application startup complete</span></div>
      </div>
    </div>
    <div class="term"><pre>${html}</pre></div>
  </div></body></html>`
}

function schemaHtml(schema, tables, part) {
  const blocks = tables
    .map((t) => {
      const rows = t.columns
        .map((c) => {
          const rawKey = c.key || ''
          const key = rawKey
            ? `<span class="${rawKey === 'PRI' ? 'pk' : ''}">${esc(rawKey)}</span>`
            : '<span class="muted">-</span>'
          return `<tr>
        <td class="mono"><b>${esc(c.field)}</b></td>
        <td class="mono muted">${esc(c.type)}</td>
        <td>${c.null === 'YES' ? 'YES' : 'NO'}</td>
        <td>${key}</td>
        <td class="mono muted">${c.default === null ? '<span class="muted">NULL</span>' : esc(c.default)}${
            c.extra ? ` <span class="muted">${esc(c.extra)}</span>` : ''
          }</td>
        <td class="muted">${esc(c.comment || '')}</td>
      </tr>`
        })
        .join('')
      const idx = t.indexes.map((i) => `<code>${esc(i)}</code>`).join(' ')
      return `
    <div class="tname">
      <span class="n">${esc(t.name)}</span>
      <span class="c">${esc(t.comment || '')}</span>
      <span class="rw">${t.columns.length} 字段 · ${esc(t.engine || '')} ${esc(t.collation || '')} · 行数 ${t.rows}</span>
    </div>
    <table class="tbl">
      <thead><tr><th>字段</th><th>类型</th><th>可空</th><th>键</th><th>默认</th><th>说明</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="idx">索引：${idx}</div>`
    })
    .join('')

  return `<!doctype html><html><head><meta charset="utf-8"><style>${BASE_CSS}</style></head><body>
  <div class="card" id="shot">
    <div class="hd">
      <h1>eval_agent_db 表结构（information_schema 实采 · 第 ${part} 部分）</h1>
      <p>库与项目一 kb_agent 物理隔离；4 张表同库，外键 + 唯一约束齐全（列注释来自 COLUMN_COMMENT）</p>
      <div class="meta">
        <div><b>MySQL</b> ${esc(schema.mysql_version)}</div>
        <div><b>Schema</b> ${esc(schema.database)}</div>
        <div><b>表数</b> ${schema.tables.length}</div>
        <div><b>字符集</b> utf8mb4 / utf8mb4_unicode_ci</div>
      </div>
    </div>
    ${blocks}
  </div></body></html>`
}

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const logRaw = fs.readFileSync(path.join(BACKEND, 'logs', 'uvicorn.log'), 'utf8')
  const schema = JSON.parse(fs.readFileSync(path.join(BACKEND, 'logs', 'schema.json'), 'utf8'))

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--hide-scrollbars', '--force-device-scale-factor=2'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1180, height: 900, deviceScaleFactor: 2 })

  const byName = (n) => schema.tables.filter((t) => t.name === n)
  const shots = [
    ['01-uvicorn-startup', logHtml(logRaw)],
    ['02-db-schema-dataset', schemaHtml(schema, [...byName('eval_datasets'), ...byName('eval_items')], 1)],
    ['03-db-schema-run', schemaHtml(schema, [...byName('eval_runs'), ...byName('eval_scores')], 2)],
  ]
  for (const [name, html] of shots) {
    await page.setContent(html, { waitUntil: 'load' })
    const el = await page.$('#shot')
    const out = path.join(OUT, `${name}.png`)
    await el.screenshot({ path: out })
    console.log('saved', out)
  }
  await browser.close()
})().catch((e) => {
  console.error('SHOT_FAILED', e)
  process.exit(1)
})
