/**
 * 第九步证据：数据集上传支持 JSON（真实 UI 走一遍）。
 *
 * 做的事：
 *   1. 生成一个带扩展字段的 .json 评测集文件
 *   2. 打开数据集管理页 → 选知识库 + 填名称 → 把文件塞进 el-upload 的 input
 *   3. 等「已解析 N 条有效样本」出现 → 截图（表单态）
 *   4. 点「创建数据集并导入样本」→ 截图（成功态 / 列表里出现新数据集）
 *   5. 回查 8200，确认样本真的带上了 stratum / difficulty / reference_contexts
 *   6. 清理：删掉这次建的数据集，不留脏数据
 *
 * 运行（在 EvalAgent/ 下）：
 *   NODE_PATH=C:/Users/丁思宇/.workbuddy/binaries/node/workspace/node_modules \
 *     node scripts/shot_stage9_json_dataset.js
 */
const fs = require('fs')
const path = require('path')
const puppeteer = require('puppeteer-core')

const FRONT = 'http://127.0.0.1:5174'
const UPSTREAM = 'http://127.0.0.1:8010/api/v1'
const EVAL = 'http://127.0.0.1:8200/api/v1'
const OUT = path.resolve(__dirname, '..', 'docs', 'evidence')
const TMP = path.resolve(__dirname, '.tmp_json_dataset.json')
const DS_NAME = `JSON 导入验证-${new Date().toISOString().slice(11, 19).replace(/:/g, '')}`

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const SAMPLE = [
  {
    question: '2026 年 Q3 华东区销售额是多少？',
    ground_truth: '2026 年第三季度华东区销售额为 1200 万元',
    stratum: 'easy',
    difficulty: 1,
    reference_contexts: ['2026 年第三季度华东区销售额为 1200 万元，同比增长 18%'],
  },
  {
    question: '哪些 SKU 的退货率超过 5%？',
    ground_truth: 'SKU-1023 与 SKU-2087 的退货率分别为 7.2% 与 6.1%',
    stratum: 'hard',
    difficulty: 4,
  },
  {
    question: '上周广告投放的 ROI 是多少？',
    ground_truth: '上周广告投放 ROI 为 2.8',
  },
]

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

;(async () => {
  fs.writeFileSync(TMP, JSON.stringify(SAMPLE, null, 2), 'utf-8')
  console.log(`已生成 JSON 文件：${TMP}（${SAMPLE.length} 条，含 stratum/difficulty/reference_contexts）`)

  const auth = await login()
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--no-sandbox', '--lang=zh-CN'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1400, height: 1150, deviceScaleFactor: 1.5 })

  await page.goto(`${FRONT}/login`, { waitUntil: 'networkidle2' })
  await page.evaluate((a) => {
    localStorage.setItem('evalagent_token', a.access_token)
    if (a.refresh_token) localStorage.setItem('evalagent_refresh_token', a.refresh_token)
    localStorage.setItem('evalagent_user', JSON.stringify(a.user))
    localStorage.removeItem('evalagent_api_base')
  }, auth)

  await page.goto(`${FRONT}/dataset`, { waitUntil: 'networkidle2', timeout: 45000 })
  await page.waitForFunction(() => /上传评测数据集/.test(document.body.innerText), { timeout: 30000 })
  await sleep(1200)

  // 展开「支持的两种格式」示例
  await page.evaluate(() => {
    const heads = [...document.querySelectorAll('.el-collapse-item__header')]
    const t = heads.find((h) => h.innerText.includes('支持的两种格式'))
    if (t) t.click()
  })
  await sleep(600)
  const fmtText = await page.evaluate(() => document.body.innerText)
  console.log('含 JSON 示例 ground_truth：', /"ground_truth"/.test(fmtText))
  console.log('含 reference_contexts 说明：', /reference_contexts/.test(fmtText))

  // 知识库：选第一项
  await page.click('.el-form-item:nth-of-type(1) .el-select')
  await page.waitForSelector('.el-select-dropdown__item', { visible: true, timeout: 10000 })
  const kbLabel = await page.evaluate(() => {
    const item = document.querySelector('.el-select-dropdown__item')
    if (!item) return ''
    item.click()
    return item.innerText.trim()
  })
  console.log('已选知识库：', kbLabel)

  // 数据集名称
  const nameInput = await page.$('.el-form-item:nth-of-type(2) input')
  await nameInput.click({ clickCount: 3 })
  await nameInput.type(DS_NAME)

  // 上传文件（el-upload 的 input 是隐藏的，直接塞）
  const fileInput = await page.$('.el-upload input[type=file]')
  if (!fileInput) throw new Error('找不到 file input')
  await fileInput.uploadFile(TMP)

  await page.waitForFunction(
    () => /已解析\s*3\s*条有效样本/.test(document.body.innerText),
    { timeout: 20000 },
  )
  console.log('UI 已确认解析出 3 条有效样本')
  await sleep(800)
  await page.screenshot({ path: path.join(OUT, '21-stage9-json-parsed.png') })
  console.log('已保存 21-stage9-json-parsed.png')

  // 提交
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button')]
    const b = btns.find((x) => x.innerText.trim() === '创建数据集并导入样本')
    if (b) b.click()
  })
  await page.waitForFunction(
    () => /创建成功，已导入\s*3\s*条样本/.test(document.body.innerText),
    { timeout: 30000 },
  )
  console.log('UI 已确认：创建成功，已导入 3 条样本')
  await sleep(1500)
  await page.screenshot({ path: path.join(OUT, '22-stage9-json-created.png') })
  console.log('已保存 22-stage9-json-created.png')

  // 回查后端：样本字段是否真的带过去了
  const list = await fetch(`${EVAL}/eval/datasets?page=1&size=100`).then((r) => r.json())
  const ds = (list.data?.items || []).find((d) => d.name === DS_NAME)
  if (!ds) throw new Error('后端没查到刚建的数据集')
  console.log(`后端数据集：#${ds.id} ${ds.name} item_count=${ds.item_count}`)

  const items = await fetch(`${EVAL}/eval/datasets/${ds.id}/items?page=1&size=10`).then((r) => r.json())
  for (const it of items.data.items) {
    console.log(
      `  seq=${it.seq} question=${it.question.slice(0, 14)}… stratum=${it.stratum ?? '—'} ` +
        `difficulty=${it.difficulty ?? '—'} contexts=${(it.reference_contexts || []).length}`,
    )
  }
  const first = items.data.items[0]
  if (first.stratum !== 'easy' || first.difficulty !== 1 || (first.reference_contexts || []).length !== 1) {
    throw new Error('扩展字段没有正确落库（stratum/difficulty/reference_contexts）')
  }
  console.log('扩展字段落库校验通过（stratum / difficulty / reference_contexts）')

  // 清理
  const del = await fetch(`${EVAL}/eval/datasets/${ds.id}`, { method: 'DELETE' }).then((r) => r.json())
  console.log(`已清理测试数据集：${JSON.stringify(del.data)}`)
  fs.unlinkSync(TMP)

  await browser.close()
})().catch((err) => {
  console.error('失败：', err.message)
  process.exit(1)
})
