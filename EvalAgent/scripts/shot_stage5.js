/**
 * 截取设置页「裁判模型配置」真实面板（无 mock 数据，读的是 8200 真实接口）。
 * 依赖：前端 5174、后端 8200、项目一 8010 都在线。
 */
const path = require('path')
const fs = require('fs')
const puppeteer = require('puppeteer-core')

const FRONT = 'http://127.0.0.1:5174'
const UPSTREAM = 'http://127.0.0.1:8010/api/v1'
const OUT = path.resolve(__dirname, '..', 'docs', 'evidence')

async function loginToken() {
  const res = await fetch(`${UPSTREAM}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'agentflow_svc', password: 'Af-svc-2026!kb' }),
  })
  const body = await res.json()
  const data = body.data || {}
  return { token: data.access_token, refresh: data.refresh_token, user: data.user }
}

;(async () => {
  const auth = await loginToken()
  if (!auth.token) throw new Error('登录失败，无法截图')

  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--no-sandbox', '--lang=zh-CN'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1560, height: 1100, deviceScaleFactor: 1.5 })

  const net = []
  page.on('response', (r) => {
    const req = r.request()
    const type = req.resourceType()
    if (type === 'xhr' || type === 'fetch') {
      net.push({ method: req.method(), url: req.url(), status: r.status() })
    }
  })

  // 注入真实凭据后进设置页（先访问一次同源页面，才能写 localStorage）
  await page.goto(`${FRONT}/login`, { waitUntil: 'networkidle2' })
  await page.evaluate((a) => {
    localStorage.setItem('evalagent_token', a.token)
    if (a.refresh) localStorage.setItem('evalagent_refresh_token', a.refresh)
    localStorage.setItem('evalagent_user', JSON.stringify(a.user))
    localStorage.removeItem('evalagent_api_base')
  }, auth)

  await page.goto(`${FRONT}/settings`, { waitUntil: 'networkidle2', timeout: 40000 })
  await page.waitForFunction(() => /裁判模型配置/.test(document.body.innerText), { timeout: 30000 })
  await new Promise((r) => setTimeout(r, 2500))

  const text = await page.evaluate(() => document.body.innerText)
  console.log('卡片文案检查：')
  console.log('  含「裁判模型配置」:', /裁判模型配置/.test(text))
  console.log('  含「考官」:', /考官/.test(text))
  console.log('  含「考生」:', /考生/.test(text))
  console.log('  含「温度强制锁定」:', /温度强制锁定/.test(text))
  console.log('  含「保存并探活」:', /保存并探活/.test(text))
  console.log('  含 Mock 字样:', /mock|模拟数据/i.test(text))

  const judgeCalls = net.filter((n) => n.url.includes('/system/judge-config'))
  console.log('实际打到 8200 的裁判配置请求：')
  judgeCalls.forEach((n) => console.log(`  ${n.status} ${n.method} ${n.url}`))

  const file = path.join(OUT, '10-stage5-judge-config.png')
  await page.screenshot({ path: file, fullPage: true })
  console.log('saved', file)

  fs.writeFileSync(
    path.resolve(__dirname, '..', 'evalagent_backend', 'logs', 'stage5_network.json'),
    JSON.stringify(net, null, 1),
    'utf8',
  )
  await browser.close()
})().catch((e) => {
  console.error('FAILED:', e.message)
  process.exit(1)
})
