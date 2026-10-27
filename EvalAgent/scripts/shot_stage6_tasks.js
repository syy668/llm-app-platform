/**
 * 第六步证据截图：任务列表大盘四卡片 + 执行中行的「停止」按钮 + 二次确认 + 停止后状态。
 *
 * 全程真实：登录走项目一 8010 真账号，页面读 8200 真接口（/eval/runs、/eval/runs/stats），
 * 「停止」按钮点下去真调 POST /eval/runs/{id}/cancel，最后回查 8200 确认状态真的变了。
 *
 * 依赖：前端 5174、后端 8200、项目一 8010 在线；且列表里存在一条 running 任务（先跑一次评测）。
 * 运行（在 EvalAgent/ 下）：
 *   NODE_PATH=C:/Users/丁思宇/.workbuddy/binaries/node/workspace/node_modules \
 *     node scripts/shot_stage6_tasks.js
 */
const path = require('path')
const puppeteer = require('puppeteer-core')

const FRONT = 'http://127.0.0.1:5174'
const UPSTREAM = 'http://127.0.0.1:8010/api/v1'
const EVAL = 'http://127.0.0.1:8200/api/v1'
const OUT = path.resolve(__dirname, '..', 'docs', 'evidence')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function login() {
  const res = await fetch(`${UPSTREAM}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'agentflow_svc', password: 'Af-svc-2026!kb' }),
  })
  const body = await res.json()
  const data = body.data || {}
  if (!data.access_token) throw new Error(`登录失败：${body.message}`)
  return { token: data.access_token, refresh: data.refresh_token, user: data.user }
}

/** 找到「执行中」那一行，返回它的 DOM 句柄（每次重新查，避免轮询重渲染导致句柄失效） */
async function findRunningRow(page) {
  return page.evaluateHandle(() => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    const row = rows.find((tr) => tr.innerText.includes('执行中'))
    return row || null
  })
}

;(async () => {
  const auth = await login()
  console.log(`登录成功：${auth.user.username}（role=${auth.user.role}）`)

  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--no-sandbox', '--lang=zh-CN', '--font-render-hinting=none'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1560, height: 1120, deviceScaleFactor: 1.5 })

  const net = []
  page.on('response', (r) => {
    const req = r.request()
    if (['xhr', 'fetch'].includes(req.resourceType())) {
      net.push({ method: req.method(), url: req.url(), status: r.status() })
    }
  })

  // 注入真实凭据（先落到同源页面才能写 localStorage）
  await page.goto(`${FRONT}/login`, { waitUntil: 'networkidle2' })
  await page.evaluate((a) => {
    localStorage.setItem('evalagent_token', a.token)
    if (a.refresh) localStorage.setItem('evalagent_refresh_token', a.refresh)
    localStorage.setItem('evalagent_user', JSON.stringify(a.user))
    localStorage.removeItem('evalagent_api_base')
  }, auth)

  await page.goto(`${FRONT}/task`, { waitUntil: 'networkidle2', timeout: 45000 })
  await page.waitForFunction(
    () => ['任务总数', '任务成功率', '全局平均 Faithfulness', '累计 Token 消耗'].every(
      (t) => document.body.innerText.includes(t),
    ),
    { timeout: 30000 },
  )
  await page.waitForFunction(
    () => document.body.innerText.includes('执行中'),
    { timeout: 30000 },
  )
  await sleep(1500)

  // ---------- 1) 全景：顶部四卡片 + 列表 ----------
  await page.screenshot({ path: path.join(OUT, '11-stage6-dashboard.png') })
  console.log('已保存 11-stage6-dashboard.png')

  // ---------- 2) 执行中那一行（证明「停止」出现、且没有「删除」）----------
  const rowInfo = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    const tr = rows.find((r) => r.innerText.includes('执行中'))
    if (!tr) return null
    const btns = [...tr.querySelectorAll('button')].map((b) => b.innerText.trim())
    return { text: tr.innerText.replace(/\s+/g, ' ').slice(0, 160), buttons: btns }
  })
  console.log('执行中行：', rowInfo && rowInfo.text)
  console.log('该行按钮：', rowInfo && rowInfo.buttons)
  if (!rowInfo || !rowInfo.buttons.includes('停止')) throw new Error('执行中行没有「停止」按钮')
  if (rowInfo.buttons.includes('删除')) throw new Error('执行中行不该出现「删除」按钮')

  const handle = await findRunningRow(page)
  const el = handle.asElement()
  if (!el) throw new Error('取不到执行中行元素')
  await el.screenshot({ path: path.join(OUT, '12-stage6-stop-button.png') })
  console.log('已保存 12-stage6-stop-button.png')

  // ---------- 3) 二次确认弹窗 ----------
  await page.evaluate(() => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    const tr = rows.find((r) => r.innerText.includes('执行中'))
    const btn = [...tr.querySelectorAll('button')].find((b) => b.innerText.trim() === '停止')
    btn.click()
  })
  await page.waitForFunction(() => !!document.querySelector('.el-message-box'), { timeout: 10000 })
  await sleep(600)
  const dialogText = await page.evaluate(() => {
    const box = document.querySelector('.el-message-box')
    return box ? box.innerText.replace(/\s+/g, ' ').slice(0, 200) : ''
  })
  console.log('确认弹窗文案：', dialogText)
  await page.screenshot({ path: path.join(OUT, '13-stage6-stop-confirm.png') })
  console.log('已保存 13-stage6-stop-confirm.png')

  // ---------- 4) 真点「停止任务」，验状态落定 ----------
  const runningId = Number((rowInfo.text.match(/^(\d+)/) || [])[1])
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('.el-message-box__btns button')]
    const target = btns.find((b) => b.innerText.trim() === '停止任务')
    target.click()
  })

  await page.waitForFunction(
    () => !document.body.innerText.includes('执行中') || document.body.innerText.includes('已取消'),
    { timeout: 30000 },
  )
  await sleep(2500)

  const after = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    const tr = rows.find((r) => r.innerText.includes('已取消'))
    if (!tr) return null
    return {
      text: tr.innerText.replace(/\s+/g, ' ').slice(0, 160),
      buttons: [...tr.querySelectorAll('button')].map((b) => b.innerText.trim()),
    }
  })
  console.log('停止后行：', after && after.text)
  console.log('停止后按钮：', after && after.buttons)

  const handle2 = await page.evaluateHandle(() => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    return rows.find((r) => r.innerText.includes('已取消')) || null
  })
  const el2 = handle2.asElement()
  if (el2) {
    await el2.screenshot({ path: path.join(OUT, '14-stage6-cancelled-row.png') })
    console.log('已保存 14-stage6-cancelled-row.png')
  }

  await page.screenshot({ path: path.join(OUT, '15-stage6-dashboard-after-stop.png') })
  console.log('已保存 15-stage6-dashboard-after-stop.png')

  // ---------- 5) 回查后端，确认不是「前端假绿」----------
  const check = await fetch(`${EVAL}/eval/runs/${runningId}`).then((r) => r.json())
  console.log(`后端复查 run #${runningId}: status=${check.data.status} processed=${check.data.processed_items}/${check.data.total_items} metrics=${JSON.stringify(check.data.metrics)}`)

  console.log('\n打到的 8200 关键请求：')
  net
    .filter((n) => n.url.includes('/eval/runs/stats') || n.url.includes('/cancel'))
    .forEach((n) => console.log(`  ${n.status} ${n.method} ${n.url.replace(FRONT, '')}`))

  await browser.close()
})().catch(async (err) => {
  console.error('截图失败：', err.message)
  process.exit(1)
})
