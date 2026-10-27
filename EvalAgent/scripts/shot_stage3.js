/**
 * 第三步验收截图：端到端控制台输出 + 落库实据。
 * 运行：
 *   NODE_PATH=C:/Users/<user>/.workbuddy/binaries/node/workspace/node_modules
 *   <managed node> EvalAgent/scripts/shot_stage3.js
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

const CSS = `
  * { box-sizing: border-box; }
  body { margin:0; padding:26px 30px; font-family:"Microsoft YaHei","Segoe UI",system-ui,sans-serif;
         background:#f6f7f9; color:#1f2328; }
  .card { background:#fff; border:1px solid #e3e6ea; border-radius:10px; overflow:hidden; }
  .hd { padding:14px 18px; border-bottom:1px solid #e3e6ea; background:#fbfcfd; }
  .hd h1 { margin:0 0 4px; font-size:16px; font-weight:700; }
  .hd p { margin:0; font-size:12px; color:#656d76; }
  .meta { display:flex; gap:16px; flex-wrap:wrap; margin-top:8px; font-size:12px; color:#43474d; }
  .meta b { font-weight:600; color:#1f2328; }
  .pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px;
          font-weight:600; background:#e7f6ec; color:#1a7f37; border:1px solid #b7e0c4; }
  .term { background:#0f172a; padding:14px 16px; }
  .term pre { margin:0; font-family:"Cascadia Mono",Consolas,monospace; font-size:11.5px;
              line-height:1.5; color:#e2e8f0; white-space:pre; }
  .term .g{color:#7ee787} .term .b{color:#79c0ff} .term .y{color:#f2cc60}
  .term .d{color:#7d8590} .term .m{color:#d2a8ff} .term .o{color:#ffa657}
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th { text-align:left; padding:8px 10px; background:#f6f8fa; border-bottom:1px solid #e3e6ea;
       font-weight:600; color:#43474d; white-space:nowrap; }
  td { padding:6px 10px; border-bottom:1px solid #eef1f4; }
  tr:last-child td { border-bottom:none; }
  .mono { font-family:"Cascadia Mono",Consolas,monospace; }
  .muted { color:#8c959f; }
  .ok { color:#1a7f37; font-weight:600; }
  .bad { color:#cf222e; font-weight:600; }
  .low { color:#cf222e; }
  .node { font-family:"Cascadia Mono",Consolas,monospace; font-weight:700; font-size:13px; }
  .sec { padding:10px 18px; background:#f2f5f8; border-top:1px solid #e3e6ea;
         border-bottom:1px solid #e3e6ea; font-size:12px; font-weight:600; color:#0b3d91; }
`

function num(v, digits = 3) {
  if (v === null || v === undefined) return '<span class="muted">NULL</span>'
  const n = Number(v)
  const cls = n < 0.5 ? 'low' : ''
  return `<span class="${cls}">${n.toFixed(digits)}</span>`
}

function e2eHtml(raw) {
  const lines = raw.split(/\r?\n/).filter((l, i, arr) => l.length > 0 || (i > 0 && arr[i - 1]))
  const html = lines
    .map((line) => {
      let cls = ''
      if (/^\[/.test(line)) cls = 'b'
      else if (/状态=done|通过|EXIT/.test(line)) cls = 'g'
      else if (/failed|失败|FAIL/.test(line)) cls = 'y'
      else if (/^\s+#\d/.test(line)) cls = 'm'
      else if (/^={10,}/.test(line)) cls = 'd'
      else if (/Δ/.test(line)) cls = 'o'
      return `<span class="${cls}">${esc(line)}</span>`
    })
    .join('\n')
  return `<!doctype html><html><head><meta charset="utf-8"><style>${CSS}</style></head><body>
  <div class="card" id="shot">
    <div class="hd">
      <h1>第三步端到端验证：真实链路控制台输出</h1>
      <p>POST /eval/runs → httpx 调项目一取答案 → 本仓库 RAGAS 打分 → 落库 → 聚合 → A/B 对比 → CSV 导出</p>
      <div class="meta">
        <div><b>被测系统</b> 项目一 127.0.0.1:8010</div>
        <div><b>裁判</b> deepseek-v4.1-flash-1017</div>
        <div><b>样本</b> 7 条 / 4 分层</div>
        <div><b>结果</b> <span class="pill">EXIT=0</span></div>
      </div>
    </div>
    <div class="term"><pre>${html}</pre></div>
  </div></body></html>`
}

function dbHtml(data) {
  const runs = data.runs
    .map((r) => {
      const m = r.metrics || {}
      return `<tr>
        <td class="mono"><b>${r.id}</b></td>
        <td class="mono">${esc(r.run_no)}</td>
        <td>${esc(r.variant || '-')}</td>
        <td class="${r.status === 'done' ? 'ok' : 'bad'}">${esc(r.status)}</td>
        <td>${r.total_items}</td>
        <td>${r.success_items}</td>
        <td>${r.failed_items}</td>
        <td class="mono muted">${esc(JSON.stringify(r.rag_config || {}))}</td>
        <td>${num(m.faithfulness)}</td>
        <td>${num(m.answer_relevancy)}</td>
        <td>${num(m.context_precision)}</td>
        <td>${num(m.context_recall)}</td>
        <td class="mono">${r.total_tokens}</td>
      </tr>`
    })
    .join('')

  const scores = data.scores
    .map(
      (s) => `<tr>
        <td class="mono">${s.run_id}</td>
        <td class="mono">${s.item_seq}</td>
        <td>${esc(s.stratum || '<span class="muted">NULL</span>')}</td>
        <td>${esc(s.question.slice(0, 30))}</td>
        <td>${num(s.faithfulness)}</td>
        <td>${num(s.answer_relevancy)}</td>
        <td>${num(s.context_precision)}</td>
        <td>${num(s.context_recall)}</td>
        <td class="mono">${s.latency_ms}</td>
        <td class="${s.status === 'ok' ? 'ok' : 'bad'}">${esc(s.status)}</td>
        <td class="muted">${esc((s.error_msg || '').slice(0, 42))}</td>
      </tr>`
    )
    .join('')

  return `<!doctype html><html><head><meta charset="utf-8"><style>${CSS}</style></head><body>
  <div class="card" id="shot">
    <div class="hd">
      <h1>eval_agent_db 落库实据</h1>
      <p>指标 NULL 表示"未算出"，与 0.0（真实得零分）严格区分；失败样本保留 error_msg 不静默</p>
      <div class="meta">
        <div><b>runs</b> ${data.runs.length}</div>
        <div><b>scores</b> ${data.scores.length}</div>
        <div><b>裁判</b> deepseek-v4.1-flash-1017</div>
      </div>
    </div>
    <div class="sec">eval_runs（任务主表：配置快照 + 汇总指标 + token）</div>
    <table>
      <thead><tr><th>id</th><th>run_no</th><th>variant</th><th>status</th><th>total</th>
      <th>ok</th><th>fail</th><th>rag_config</th><th>F</th><th>AR</th><th>CP</th><th>CR</th><th>tokens</th></tr></thead>
      <tbody>${runs}</tbody>
    </table>
    <div class="sec">eval_scores（单样本明细：四项指标 + 延迟 + 失败原因）</div>
    <table>
      <thead><tr><th>run</th><th>seq</th><th>分层</th><th>问题</th><th>F</th><th>AR</th>
      <th>CP</th><th>CR</th><th>ms</th><th>status</th><th>error</th></tr></thead>
      <tbody>${scores}</tbody>
    </table>
  </div></body></html>`
}

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const logRaw = fs.readFileSync(path.join(BACKEND, 'logs', 'e2e_stage3.log'), 'utf8')
  const data = JSON.parse(fs.readFileSync(path.join(BACKEND, 'logs', 'stage3_db.json'), 'utf8'))

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--hide-scrollbars', '--force-device-scale-factor=2'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1240, height: 900, deviceScaleFactor: 2 })

  const shots = [
    ['04-stage3-e2e-console', e2eHtml(logRaw)],
    ['05-stage3-db-evidence', dbHtml(data)],
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
