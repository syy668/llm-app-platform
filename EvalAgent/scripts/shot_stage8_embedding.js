/**
 * 第八步证据：裁判配置卡片新增的「向量模型独立连接」三项 + 探活结果。
 * 运行（在 EvalAgent/ 下）：
 *   NODE_PATH=C:/Users/丁思宇/.workbuddy/binaries/node/workspace/node_modules \
 *     node scripts/shot_stage8_embedding.js
 */
const path = require('path')
const puppeteer = require('puppeteer-core')

const FRONT = 'http://127.0.0.1:5174'
const UPSTREAM = 'http://127.0.0.1:8010/api/v1'
const OUT = path.resolve(__dirname, '..', 'docs', 'evidence')
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  const login = await fetch(`${UPSTREAM}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'agentflow_svc', password: 'Af-svc-2026!kb' }),
  }).then((r) => r.json())
  const auth = login.data
  if (!auth?.access_token) throw new Error(`登录失败：${login.message}`)

  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--no-sandbox', '--lang=zh-CN'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1480, height: 1500, deviceScaleFactor: 1.5 })

  await page.goto(`${FRONT}/login`, { waitUntil: 'networkidle2' })
  await page.evaluate((a) => {
    localStorage.setItem('evalagent_token', a.access_token)
    if (a.refresh_token) localStorage.setItem('evalagent_refresh_token', a.refresh_token)
    localStorage.setItem('evalagent_user', JSON.stringify(a.user))
    localStorage.removeItem('evalagent_api_base')
  }, auth)

  await page.goto(`${FRONT}/settings`, { waitUntil: 'networkidle2', timeout: 45000 })
  await page.waitForFunction(() => /裁判模型配置/.test(document.body.innerText), { timeout: 30000 })
  await page.waitForFunction(() => /向量接口地址/.test(document.body.innerText), { timeout: 20000 })
  await sleep(1500)

  // 点一次「探活」，把结构 + 向量两条结果都拍进去
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button')].filter((b) => b.innerText.trim() === '探活')
    if (btns[0]) btns[0].click()
  })
  await sleep(4000)

  const text = await page.evaluate(() => document.body.innerText.replace(/\s+/g, ' '))
  console.log('含「向量接口地址」：', /向量接口地址/.test(text))
  console.log('含 BAAI/bge-m3：', /BAAI\/bge-m3/.test(text))
  console.log('含「与裁判同源」标记：', /与裁判同源/.test(text))
  console.log('含 siliconflow 地址：', /api\.siliconflow\.cn/.test(text))
  const probe = text.match(/探活[^。]{0,80}/g) || []
  console.log('探活提示：', probe.slice(0, 3))

  // 只截裁判卡片
  const card = await page.evaluateHandle(() => {
    const titles = [...document.querySelectorAll('.ea-card-title')]
    const t = titles.find((x) => x.innerText.includes('裁判模型配置'))
    return t ? t.closest('.ea-card') : null
  })
  const el = card.asElement()
  if (el) {
    await el.screenshot({ path: path.join(OUT, '20-stage8-embedding-config.png') })
    console.log('已保存 20-stage8-embedding-config.png')
  } else {
    await page.screenshot({ path: path.join(OUT, '20-stage8-embedding-config.png') })
    console.log('未定位到卡片，已保存整页 20-stage8-embedding-config.png')
  }

  await browser.close()
})().catch((err) => {
  console.error('截图失败：', err.message)
  process.exit(1)
})
