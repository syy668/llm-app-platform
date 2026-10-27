/**
 * 第十步证据：「追加样本」真实 UI 走一遍。
 *
 *   1. 用 API 建一个只有 2 条样本的数据集（模拟"已经用了很久"的数据集）
 *   2. 打开数据集管理 → 找到该行 → 点「追加样本」
 *   3. 截弹窗（当前 2 / 上限 500 / 本次最多可加 498）
 *   4. 塞一个 2 条的 JSON → 截解析态 → 点「追加 2 条」
 *   5. 回查后端：seq 必须是 3、4（接续），item_count=4
 *   6. 清理
 *
 * 运行（在 EvalAgent/ 下）：
 *   NODE_PATH=C:/Users/丁思宇/.workbuddy/binaries/node/workspace/node_modules \
 *     node scripts/shot_stage10_append_items.js
 */
const fs = require('fs')
const path = require('path')
const puppeteer = require('puppeteer-core')

const FRONT = 'http://127.0.0.1:5174'
const UPSTREAM = 'http://127.0.0.1:8010/api/v1'
const EVAL = 'http://127.0.0.1:8200/api/v1'
const OUT = path.resolve(__dirname, '..', 'docs', 'evidence')
const TMP = path.resolve(__dirname, '.tmp_append_items.json')
const DS_NAME = `追加样本验证-${new Date().toISOString().slice(11, 19).replace(/:/g, '')}`

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function login() {
  const res = await fetch(`${UPSTREAM}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'agentflow_svc', password: 'Af-svc-2026!kb' }),
  })
  const body = await res.json()
  if (!body.data?.access_token) throw new Error(`登录失败：${body.message}`)
  return body.data
}

async function api(pathname, init) {
  const res = await fetch(`${EVAL}${pathname}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  return res.json()
}

;(async () => {
  const auth = await login()

  // 1) 造一个已有 2 条样本的数据集
  const created = await api('/eval/datasets', {
    method: 'POST',
    body: JSON.stringify({ kb_id: 6, name: DS_NAME }),
  })
  if (created.code !== 0) throw new Error(`建数据集失败：${created.message}`)
  const dsId = created.data.id
  const seeded = await api(`/eval/datasets/${dsId}/items`, {
    method: 'POST',
    body: JSON.stringify({
      items: [
        { question: '老样本 Q1', reference_answer: '老答案 A1', stratum: 'easy' },
        { question: '老样本 Q2', reference_answer: '老答案 A2' },
      ],
    }),
  })
  console.log(`已建数据集 #${dsId}「${DS_NAME}」，现有 ${seeded.data.item_count} 条`)

  // 2) 准备要追加的文件
  fs.writeFileSync(
    TMP,
    JSON.stringify(
      [
        { question: '追加 Q3', ground_truth: '追加答案 A3', stratum: 'hard', difficulty: 4 },
        { question: '追加 Q4', ground_truth: '追加答案 A4' },
      ],
      null,
      2,
    ),
    'utf-8',
  )

  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--no-sandbox', '--lang=zh-CN'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1400, height: 1000, deviceScaleFactor: 1.5 })

  await page.goto(`${FRONT}/login`, { waitUntil: 'networkidle2' })
  await page.evaluate((a) => {
    localStorage.setItem('evalagent_token', a.access_token)
    if (a.refresh_token) localStorage.setItem('evalagent_refresh_token', a.refresh_token)
    localStorage.setItem('evalagent_user', JSON.stringify(a.user))
    localStorage.removeItem('evalagent_api_base')
  }, auth)

  await page.goto(`${FRONT}/dataset`, { waitUntil: 'networkidle2', timeout: 45000 })
  await page.waitForFunction((name) => document.body.innerText.includes(name), { timeout: 30000 }, DS_NAME)
  await sleep(800)

  const rowButtons = await page.evaluate((name) => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    const tr = rows.find((r) => r.innerText.includes(name))
    return tr ? [...tr.querySelectorAll('button')].map((b) => b.innerText.trim()) : null
  }, DS_NAME)
  console.log('该行按钮：', rowButtons)
  if (!rowButtons || !rowButtons.includes('追加样本')) throw new Error('操作列没有「追加样本」按钮')

  // 3) 打开追加弹窗
  await page.evaluate((name) => {
    const rows = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
    const tr = rows.find((r) => r.innerText.includes(name))
    const btn = [...tr.querySelectorAll('button')].find((b) => b.innerText.trim() === '追加样本')
    btn.click()
  }, DS_NAME)
  await page.waitForFunction(() => !!document.querySelector('.el-dialog'), { timeout: 10000 })
  await sleep(700)
  const dialogText = await page.evaluate(
    () => document.querySelector('.el-dialog')?.innerText.replace(/\s+/g, ' ').slice(0, 240) || '',
  )
  console.log('弹窗文案：', dialogText)
  await page.screenshot({ path: path.join(OUT, '23-stage10-append-dialog.png') })
  console.log('已保存 23-stage10-append-dialog.png')

  // 4) 塞文件 → 解析
  const fileInput = await page.$('.el-dialog input[type=file]')
  if (!fileInput) throw new Error('弹窗里找不到 file input')
  await fileInput.uploadFile(TMP)
  await page.waitForFunction(
    () => /已解析\s*2\s*条有效样本/.test(document.body.innerText),
    { timeout: 20000 },
  )
  console.log('弹窗已确认解析 2 条')
  await sleep(600)
  await page.screenshot({ path: path.join(OUT, '24-stage10-append-parsed.png') })
  console.log('已保存 24-stage10-append-parsed.png')

  // 5) 提交
  await page.evaluate(() => {
    const dlg = document.querySelector('.el-dialog')
    const btn = [...dlg.querySelectorAll('.el-dialog__footer button')].find((b) =>
      /追加\s*2\s*条/.test(b.innerText),
    )
    if (btn) btn.click()
  })
  await page.waitForFunction(
    () => /追加\s*2\s*条，当前共\s*4\s*条/.test(document.body.innerText),
    { timeout: 30000 },
  )
  console.log('UI 已确认：追加 2 条，当前共 4 条')
  await sleep(1500)
  await page.screenshot({ path: path.join(OUT, '25-stage10-append-done.png') })
  console.log('已保存 25-stage10-append-done.png')

  // 6) 回查后端：seq 必须接续、条数对得上
  const items = await api(`/eval/datasets/${dsId}/items?page=1&size=10`)
  const rows = items.data.items
  console.log('后端样本：')
  for (const it of rows) {
    console.log(`  seq=${it.seq} ${it.question}  stratum=${it.stratum ?? '—'} difficulty=${it.difficulty ?? '—'}`)
  }
  const seqs = rows.map((r) => r.seq)
  if (JSON.stringify(seqs) !== JSON.stringify([1, 2, 3, 4])) throw new Error(`seq 不对：${seqs}`)
  if (items.data.total !== 4) throw new Error(`总数不对：${items.data.total}`)
  console.log('seq 接续校验通过（1,2,3,4，老样本未重排）')

  const del = await api(`/eval/datasets/${dsId}`, { method: 'DELETE' })
  console.log(`已清理测试数据集：${JSON.stringify(del.data)}`)
  fs.unlinkSync(TMP)
  await browser.close()
})().catch((err) => {
  console.error('失败：', err.message)
  process.exit(1)
})
