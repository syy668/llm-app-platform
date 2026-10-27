#!/usr/bin/env node
/**
 * Vite dev server 缓存清理 / 体检。
 *
 * 背景：本机出现过「dev server 返回 200 但内容为 0 字节」的模块（nav.ts），
 * 浏览器侧表现为 `does not provide an export named 'XXX'`，页面白屏。
 * 根因是 Vite 在文件被连续快速写入时把一条空 transform 结果写进了内存模块图，
 * 之后该 URL 一直返回空，靠 HMR 自己也恢复不了，只能重启（或清缓存）。
 *
 * 用法：
 *   node scripts/clean-vite-cache.mjs          清理依赖预构建缓存（node_modules/.vite 等）
 *   node scripts/clean-vite-cache.mjs --check   只体检：列出缓存状态 + 探测运行中的 dev server
 */
import { existsSync, readdirSync, rmSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const CHECK_ONLY = process.argv.includes('--check')
const BASE_URL = process.env.VITE_DEV_URL || 'http://127.0.0.1:5173'

/** 参与探测的模块：入口 + 最容易踩坑的聚合模块 */
const PROBE_MODULES = [
  '/',
  '/src/main.ts',
  '/src/router/nav.ts',
  '/src/router/index.ts',
  '/src/layouts/BasicLayout.vue',
]

const CACHE_DIRS = [
  join(root, 'node_modules', '.vite'),
  join(root, '..', '.vite'),
  join(root, 'node_modules', '.vite-temp'),
]

function dirSize(dir) {
  let total = 0
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    try {
      total += entry.isDirectory() ? dirSize(full) : statSync(full).size
    } catch {
      /* 并发删除时忽略 */
    }
  }
  return total
}

const kb = (n) => `${(n / 1024).toFixed(1)} KB`

async function probe(url) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 8000)
  try {
    const res = await fetch(BASE_URL + url, { signal: controller.signal })
    const body = await res.text()
    return { status: res.status, bytes: Buffer.byteLength(body) }
  } catch (err) {
    return { status: 0, bytes: 0, error: err.name === 'AbortError' ? 'timeout' : String(err.message || err) }
  } finally {
    clearTimeout(timer)
  }
}

async function main() {
  console.log(`[vite-cache] 工程目录：${root}`)

  if (CHECK_ONLY) {
    for (const dir of CACHE_DIRS) {
      if (existsSync(dir)) console.log(`  缓存存在  ${dir}  (${kb(dirSize(dir))})`)
      else console.log(`  缓存缺失  ${dir}`)
    }
  } else {
    let removed = 0
    for (const dir of CACHE_DIRS) {
      if (!existsSync(dir)) continue
      rmSync(dir, { recursive: true, force: true })
      console.log(`  已清理    ${dir}`)
      removed += 1
    }
    console.log(removed ? `[vite-cache] 共清理 ${removed} 个目录，下次启动会自动重新预构建` : '[vite-cache] 无需清理')
  }

  console.log(`\n[vite-cache] 探测 dev server：${BASE_URL}`)
  let bad = 0
  for (const url of PROBE_MODULES) {
    const { status, bytes, error } = await probe(url)
    if (status !== 200) {
      console.log(`  [FAIL] ${url.padEnd(28)} ${error ? `连不上（${error}）` : `HTTP ${status}`}`)
      bad += 1
    } else if (bytes === 0) {
      console.log(`  [EMPTY] ${url.padEnd(27)} 200 但 0 字节 —— 模块图缓存已损坏`)
      bad += 1
    } else {
      console.log(`  [OK]   ${url.padEnd(28)} ${status} ${bytes} bytes`)
    }
  }

  if (bad === 0) {
    console.log('\n[vite-cache] 结论：全部正常。')
    return
  }

  // 连不上属于「没启动」，不算缓存损坏
  const unreachable = bad === PROBE_MODULES.length
  if (unreachable) {
    console.log('\n[vite-cache] 结论：dev server 未启动，跳过缓存判定。')
    return
  }
  console.log('\n[vite-cache] 结论：存在空模块/异常模块，请执行 `npm run dev:clean` 重启 dev server，')
  console.log('            然后浏览器 Ctrl+Shift+R 强制刷新（旧的空模块已被浏览器 HTTP 缓存）。')
  process.exitCode = 1
}

main()
