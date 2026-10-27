/**
 * 第七步证据截图：换成 qwen3.8-flash 后的真实出分。
 *
 * 一条链路拍三张：
 *   16 大盘四卡片（真实任务在跑）
 *   17 执行中行（红「停止」按钮）—— 这一轮**不点停止**，让它跑完
 *   18 跑完后点「详情」，抽屉里的四项指标 / 雷达图 / 逐条得分
 *
 * 全程真数据：登录走 8010、页面读 8200、评测真实打 8010 + dashscope 裁判。
 * 运行（在 EvalAgent/ 下）：
 *   NODE_PATH=C:/Users/丁思宇/.workbuddy/binaries/node/workspace/node_modules \
 *     node scripts/shot_stage7_guard.js
 */
const path = require('path')
const puppeteer = require('puppeteer-core')

const FRONT = 'http://127.0.0.1:5174'
const UPSTREAM = 'http://127.0.0.1:8010/api/v1'
const EVAL = 'http://127.0.0.1:8200/api/v1'
const OUT = path.resolve(__dirname, '..', 'docs', 'evidence')

const DATASET_ID = 19
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function login() {
  const res = await fetch(`${UPSTREAM}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'agentflow_svc', password: 'Af-svc-2026!kb' }),
  })
  const body = await res.json()
  if (!body.data?.access_token) throw new Error(`登录失败：${body.message}`)
  return { token: body.data.access_token, refresh: body.data.refresh_token, user: body.data.user }
}

async function getRun(runId) {
  const res = await fetch(`${EVAL}/eval/runs/${runId}`)
  return (await res.json()).data
}

;(async () => {
  const auth = await login()
  console.log(`登录成功：${auth.user.username}（role=${auth.user.role}）`)

  // ---------- 触发一次真实评测（7 条样本）----------
  const createRes = await fetch(`${EVAL}/eval/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      dataset_id: DATASET_ID,
      variant: 'baseline',
      rag_config: { top_k: 5, search_mode: 'vector_only', chunk_size: 500 },
    }),
  })
  const created = await createRes.json()
  if (created.code !== 0) throw new Error(`触发评测失败：${created.message}`)
  const runId = created.data.id
  console.log(`已触发任务 #${runId}（${created.data.run_no}，${created.data.total_items} 条样本）`)

  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--no-sandbox', '--lang=zh-CN'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1560, height: 1120, deviceScaleFactor: 1.5 })

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
  // 等这一条跑到「执行中」（后端刚入队是 pending，几秒内变 running）
  await page.waitForFunction(
    (id) => {
      const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
      const tr = rows.find((r) => r.innerText.trim().startsWith(String(id)))
      return !!tr && /执行中|等待中/.test(tr.innerText)
    },
    { timeout: 40000 },
    runId,
  )
  await sleep(1200)

  await page.screenshot({ path: path.join(OUT, '16-stage7-dashboard-running.png') })
  console.log('已保存 16-stage7-dashboard-running.png')

  const rowInfo = await page.evaluate((id) => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    const tr = rows.find((r) => r.innerText.trim().startsWith(String(id)))
    if (!tr) return null
    return {
      text: tr.innerText.replace(/\s+/g, ' ').slice(0, 150),
      buttons: [...tr.querySelectorAll('button')].map((b) => b.innerText.trim()),
    }
  }, runId)
  console.log('执行中行：', rowInfo && rowInfo.text)
  console.log('该行按钮：', rowInfo && rowInfo.buttons)

  const handle = await page.evaluateHandle((id) => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    return rows.find((r) => r.innerText.trim().startsWith(String(id))) || null
  }, runId)
  const el = handle.asElement()
  if (el) {
    await el.screenshot({ path: path.join(OUT, '17-stage7-stop-button.png') })
    console.log('已保存 17-stage7-stop-button.png')
  }

  // ---------- 不停止，等它真跑完（RAGAS 7 条样本通常 3~7 分钟）----------
  const deadline = Date.now() + 11 * 60 * 1000
  let run = null
  while (Date.now() < deadline) {
    run = await getRun(runId)
    if (['done', 'failed', 'cancelled'].includes(run.status)) break
    await sleep(10000)
  }
  if (!run || !['done', 'failed', 'cancelled'].includes(run.status)) {
    throw new Error(`等待超时，任务仍为 ${run && run.status}`)
  }
  console.log(
    `任务终态：status=${run.status} processed=${run.processed_items}/${run.total_items} metrics=${JSON.stringify(run.metrics)}`,
  )
  if (run.status !== 'done') {
    console.log(`!!! 未成功：error_msg=${run.error_msg}`)
  }

  // ---------- 回到列表，打开这一条的详情 ----------
  await page.goto(`${FRONT}/task`, { waitUntil: 'networkidle2', timeout: 45000 })
  await page.waitForFunction(
    (id) => {
      const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
      return rows.some((r) => r.innerText.trim().startsWith(String(id)))
    },
    { timeout: 30000 },
    runId,
  )
  await sleep(1000)
  await page.screenshot({ path: path.join(OUT, '18-stage7-dashboard-done.png') })
  console.log('已保存 18-stage7-dashboard-done.png')

  await page.evaluate((id) => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    const tr = rows.find((r) => r.innerText.trim().startsWith(String(id)))
    const btn = [...tr.querySelectorAll('button')].find((b) => b.innerText.trim() === '详情')
    btn.click()
  }, runId)

  await page.waitForFunction(
    () => !!document.querySelector('.el-drawer') && /评测明细/.test(document.body.innerText),
    { timeout: 20000 },
  )
  await page.waitForFunction(
    () => document.querySelectorAll('.el-drawer .el-table__body-wrapper tbody tr').length > 0,
    { timeout: 30000 },
  )
  await sleep(3000) // 等雷达图（echarts canvas）画完

  const drawerText = await page.evaluate(
    () => document.querySelector('.el-drawer')?.innerText.replace(/\s+/g, ' ').slice(0, 600) || '',
  )
  console.log('详情抽屉文案：', drawerText)
  await page.screenshot({ path: path.join(OUT, '19-stage7-detail-scores.png') })
  console.log('已保存 19-stage7-detail-scores.png')

  const scores = await fetch(`${EVAL}/eval/runs/${runId}/scores?page=1&size=10`).then((r) => r.json())
  const okRows = (scores.data?.items || []).filter((s) => s.status === 'ok')
  console.log(`明细复核：${scores.data.total} 行，其中 ok=${okRows.length}`)
  if (okRows[0]) {
    console.log(
      `第 1 条真实得分：F=${okRows[0].faithfulness} AR=${okRows[0].answer_relevancy} CP=${okRows[0].context_precision} CR=${okRows[0].context_recall}`,
    )
  }

  await browser.close()
})().catch((err) => {
  console.error('截图失败：', err.message)
  process.exit(1)
})
