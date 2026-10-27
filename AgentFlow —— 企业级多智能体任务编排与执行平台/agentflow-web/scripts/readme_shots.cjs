/* AgentFlow README 配图截图脚本（headless Edge 驱动，登录态真实 UI） */
const puppeteer = require('puppeteer-core');
const path = require('path');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const BASE = 'http://127.0.0.1:5173';
const OUT = path.resolve(__dirname, '../../agentflow-platform/docs/images');
const USER = 'admin_user';
const PASS = 'Passw0rd!';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: 'new',
    args: ['--window-size=1600,900', '--lang=zh-CN'],
    defaultViewport: { width: 1600, height: 900, deviceScaleFactor: 1.5 },
  });
  const page = await browser.newPage();
  const log = (...a) => console.log('[shot]', ...a);

  // ---- 登录 ----
  await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle2', timeout: 30000 });
  await page.waitForSelector('input', { timeout: 15000 });
  await page.type('input[placeholder="请输入用户名"]', USER, { delay: 20 });
  await page.type('input[placeholder="请输入密码"]', PASS, { delay: 20 });
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('button')].find((b) => /登\s*录/.test(b.textContent));
    if (btn) btn.click();
  });
  await page.waitForFunction(() => !location.hash.includes('login'), { timeout: 20000 });
  log('logged in');

  // ---- 图1：任务 50 DAG 全绿 ----
  await page.goto(`${BASE}/#/tasks/50`, { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(4500); // 等表格/DAG/图表渲染
  await page.screenshot({ path: path.join(OUT, 'flow_dag.png'), fullPage: true });
  log('flow_dag.png saved');

  // ---- 图2：任务 53 挂起审批（优先抽屉，退回收件箱） ----
  await page.goto(`${BASE}/#/approvals`, { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(3500);
  let drawerShot = false;
  const approveLink = await page.evaluateHandle(() => {
    const cell = [...document.querySelectorAll('a, button, span')].find((el) => el.textContent.trim() === '审批');
    return cell || null;
  });
  if (approveLink && (await approveLink.asElement())) {
    await approveLink.asElement().click();
    await sleep(3000); // 等抽屉拉取审批详情 + 高危动作 preview
    if (await page.$('.arco-drawer')) drawerShot = true;
  }
  if (!drawerShot) log('drawer failed, keep inbox view');
  await page.screenshot({ path: path.join(OUT, 'flow_approval.png'), fullPage: !drawerShot });
  log(`flow_approval.png saved (drawer=${drawerShot})`);

  await browser.close();
})().catch((e) => {
  console.error('[shot] FAILED:', e.message);
  process.exit(1);
});
