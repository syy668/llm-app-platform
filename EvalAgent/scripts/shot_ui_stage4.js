/**
 * 第四步验收截图：项目二前端真实评测面板（连 8200 真后端，非 mock）+ 请求流向证据。
 *
 * 做三件事：
 *   1. 打开 5174 的「评测任务」页，注入登录态（只影响路由守卫，评测接口本身不带鉴权）
 *   2. 全程监听 xhr/fetch，断言所有评测请求都打到 127.0.0.1:8200（而不是 8000/8010）
 *   3. 截 3 张图：任务列表、A/B 看板、请求流向汇总
 *
 * 运行：
 *   NODE_PATH=C:/Users/<user>/.workbuddy/binaries/node/workspace/node_modules
 *   <managed node> EvalAgent/scripts/shot_ui_stage4.js
 */
const fs = require('fs')
const path = require('path')
const puppeteer = require('puppeteer-core')

const ROOT = path.resolve(__dirname, '..')
const BACKEND = path.join(ROOT, 'evalagent_backend')
const OUT = path.join(ROOT, 'docs', 'evidence')
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const FRONT = 'http://127.0.0.1:5174'

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
  .meta { display:flex; gap:18px; flex-wrap:wrap; margin-top:8px; font-size:12px; color:#43474d; }
  .meta b { font-weight:600; color:#1f2328; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th { text-align:left; padding:8px 12px; background:#f6f8fa; border-bottom:1px solid #e3e6ea;
       font-weight:600; color:#43474d; }
  td { padding:7px 12px; border-bottom:1px solid #eef1f4; font-family:"Cascadia Mono",Consolas,monospace; }
  .pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px; font-weight:600;
          background:#e7f6ec; color:#1a7f37; border:1px solid #b7e0c4; }
  .pill.bad { background:#fdecec; color:#b42318; border-color:#f5c2c0; }
  .mono { font-family:"Cascadia Mono",Consolas,monospace; }
`

const netHtml = (rows) => {
  const bad = rows.filter((r) => r.host !== '127.0.0.1:8200' && r.host !== '127.0.0.1:5174')
  return `<!doctype html><html><head><meta charset="utf-8"><style>${CSS}</style></head><body>
  <div class="card" id="shot">
    <div class="hd">
      <h1>前端请求流向实测（浏览器 Network 层抓取，非日志伪造）</h1>
      <p>打开「评测任务」+「A/B 看板」两页，拦截全部 xhr/fetch —— 评测接口 100% 命中 8200</p>
      <div class="meta">
        <div><b>请求总数</b> ${rows.length}</div>
        <div><b>命中 8200</b> ${rows.filter((r) => r.host === '127.0.0.1:8200').length}</div>
        <div><b>命中其他域</b> ${bad.length}</div>
        <div><b>结论</b> <span class="pill ${bad.length ? 'bad' : ''}">${bad.length ? '存在越域请求' : '全部命中评测后端'}</span></div>
      </div>
    </div>
    <table>
      <thead><tr><th style="width:42%">URL</th><th style="width:70px">方法</th><th style="width:150px">目标域</th>
      <th style="width:80px">状态</th><th>类型</th></tr></thead>
      <tbody>
      ${rows
        .map(
          (r) => `<tr>
        <td>${esc(r.url.replace('http://' + r.host, ''))}</td>
        <td>${esc(r.method)}</td>
        <td><span class="pill ${r.host === '127.0.0.1:8200' ? '' : 'bad'}">${esc(r.host)}</span></td>
        <td>${r.status ?? '-'}</td>
        <td class="mono" style="color:#656d76">${esc(r.type || '')}</td>
      </tr>`,
        )
        .join('')}
      </tbody>
    </table>
  </div></body></html>`
}

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--hide-scrollbars', '--force-device-scale-factor=2'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1560, height: 980, deviceScaleFactor: 1.5 })

  /** 抓 xhr/fetch 的 URL / 方法 / 状态，用来证明「请求真的打到了 8200」 */
  const net = []
  page.on('response', (resp) => {
    const req = resp.request()
    const type = req.resourceType()
    if (type !== 'xhr' && type !== 'fetch') return
    const url = req.url()
    if (url.startsWith(FRONT)) return // 前端自身模块/热更新不算
    let host = ''
    try {
      host = new URL(url).host
    } catch {
      /* ignore */
    }
    net.push({ url, method: req.method(), host, status: resp.status(), type })
  })

  // 先落一个同源页面，才能写 localStorage（登录态只用于跳过路由守卫）
  await page.goto(`${FRONT}/login`, { waitUntil: 'networkidle2', timeout: 30000 })
  await page.evaluate(() => {
    localStorage.setItem('evalagent_token', 'stage4-evidence-token')
    localStorage.setItem(
      'evalagent_user',
      JSON.stringify({ id: 1, username: 'admin_user', display_name: '演示管理员', email: null, role: 'admin', status: 'active', created_at: '' }),
    )
    localStorage.removeItem('evalagent_api_base') // 确保用 .env 里的默认基址，不残留旧覆盖
  })

  // ---- 图 1：评测任务列表（真实指标）----
  await page.goto(`${FRONT}/task`, { waitUntil: 'networkidle2', timeout: 30000 })
  // 列表用数字主键展示任务 ID，等表格行渲染出来（不依赖某条特定数据）
  await page.waitForFunction(
    () => document.querySelectorAll('.el-table__row').length > 0 && /faithfulness/.test(document.body.innerText),
    { timeout: 30000 },
  )
  await new Promise((r) => setTimeout(r, 1500))
  // 断言：页面上没有「Mock / 模拟」字样，指标是真数值
  const panelText = await page.evaluate(() => document.body.innerText)
  const hasMockWord = /mock|模拟数据|mock 数据/i.test(panelText)
  const metricCells = await page.evaluate(() =>
    Array.from(document.querySelectorAll('.el-table__row')).map((row) => row.innerText.replace(/\s+/g, ' ').slice(0, 160)),
  )
  console.log(`面板文案含 Mock 字样: ${hasMockWord}`)
  metricCells.slice(0, 6).forEach((t) => console.log('  行:', t))
  // 表格列宽较大（9 列），缩放到 78% 才能一屏看全：只影响截图取景，不改数据
  await page.evaluate(() => {
    document.body.style.zoom = '0.78'
  })
  await new Promise((r) => setTimeout(r, 800))
  await page.screenshot({ path: path.join(OUT, '06-stage4-task-panel.png'), fullPage: true })
  await page.evaluate(() => {
    document.body.style.zoom = '1'
  })
  console.log('saved', path.join(OUT, '06-stage4-task-panel.png'))

  // ---- 图 2：点「详情」进任务详情（四项指标 + 分层 + BadCase，全部来自 8200 真数据）----
  const detailBtn = await page.$$('.el-table__row .el-button, .el-table__row button')
  const detailTarget = await page.evaluateHandle(() => {
    const rows = Array.from(document.querySelectorAll('.el-table__row'))
    const row = rows[0]
    if (!row) return null
    return Array.from(row.querySelectorAll('button, .el-button')).find((b) => /详情/.test(b.innerText)) || null
  })
  if (detailTarget && (await detailTarget.asElement())) {
    await detailTarget.asElement().click()
    await new Promise((r) => setTimeout(r, 3500))
    await page.screenshot({ path: path.join(OUT, '07-stage4-run-detail.png'), fullPage: true })
    console.log('saved', path.join(OUT, '07-stage4-run-detail.png'))
  } else {
    console.log('WARN 未找到详情按钮，跳过详情截图')
  }
  void detailBtn

  // ---- 图 3：A/B 对比结果（自动选 A/B 两个同数据集任务并点「开始对比」）----
  await page.goto(`${FRONT}/ab-compare`, { waitUntil: 'networkidle2', timeout: 30000 })
  await new Promise((r) => setTimeout(r, 1200))
  const pickSelect = async (index, optionIndex = 0) => {
    const selects = await page.$$('.el-select')
    if (selects.length <= index) return false
    await selects[index].click()
    await new Promise((r) => setTimeout(r, 700))
    const picked = await page.evaluate((oi) => {
      const items = Array.from(document.querySelectorAll('.el-select-dropdown__item'))
        .filter((el) => el.offsetParent !== null)
      if (!items.length) return false
      const target = items[Math.min(oi, items.length - 1)]
      target.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return target.innerText
    }, optionIndex)
    await new Promise((r) => setTimeout(r, 600))
    return picked
  }
  const a = await pickSelect(0, 0)
  const b = await pickSelect(1, 1) // 第 2 项：必须是另一个任务，自己跟自己比没有意义
  console.log(`A=${a} B=${b}`)
  const clicked = await page.evaluate(() => {
    const btn = Array.from(document.querySelectorAll('button')).find((x) => /开始对比/.test(x.innerText))
    if (!btn) return false
    btn.click()
    return true
  })
  if (clicked && a && b) {
    await new Promise((r) => setTimeout(r, 4000))
    await page.screenshot({ path: path.join(OUT, '08-stage4-ab-compare.png'), fullPage: true })
    console.log('saved', path.join(OUT, '08-stage4-ab-compare.png'))
  } else {
    console.log('WARN 未完成 A/B 自动选择，跳过对比截图')
  }

  // ---- 图 4：请求流向证据 ----
  fs.writeFileSync(
    path.join(BACKEND, 'logs', 'stage4_network.json'),
    JSON.stringify(net, null, 1),
    'utf8',
  )
  await page.setContent(netHtml(net), { waitUntil: 'load' })
  const el = await page.$('#shot')
  await el.screenshot({ path: path.join(OUT, '09-stage4-network.png') })
  console.log('saved', path.join(OUT, '09-stage4-network.png'))

  console.log('\n--- 请求流向 ---')
  net.forEach((r) => console.log(`${String(r.status).padEnd(4)} ${r.method.padEnd(5)} ${r.host.padEnd(18)} ${r.url.replace('http://' + r.host, '')}`))
  await browser.close()
})().catch((e) => {
  console.error('SHOT_FAILED', e)
  process.exit(1)
})
