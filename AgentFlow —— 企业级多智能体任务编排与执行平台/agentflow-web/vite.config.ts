import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv, type ProxyOptions } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 构造一条开发期代理规则。
 *
 * 后端未启动 / 进程被杀时，http-proxy 默认会抛出一个**裸 500**（无响应体、无业务信封），
 * 前端 axios 只能拿到 status=500 且读不到 `code`，最终提示「请求失败（HTTP 500）」——排查困难。
 * 这里统一接管 proxy error，回一个带 ApiResponse 信封的 503，前端即可显示可读原因。
 */
function proxyTo(target: string): ProxyOptions {
  return {
    target,
    changeOrigin: true,
    configure: (proxy) => {
      proxy.on('error', (err: Error, _req: unknown, res: unknown) => {
        const response = res as { headersSent?: boolean; writeHead?: (s: number, h: Record<string, string>) => void; end?: (b: string) => void } | undefined
        if (!response || response.headersSent || typeof response.writeHead !== 'function' || typeof response.end !== 'function') {
          return
        }
        response.writeHead(503, { 'Content-Type': 'application/json; charset=utf-8' })
        response.end(
          JSON.stringify({
            code: 'SERVICE_UNAVAILABLE',
            message: `后端服务不可用：代理无法连接 ${target}，请确认后端已启动后重试（${err.code || err.message}）`,
            data: null,
            trace_id: null,
          }),
        )
      })
    },
  }
}

/**
 * Vite 配置
 * - 开发期通过 /api 代理到后端，避免浏览器跨域（后端默认 8000 端口）
 * - 生产构建产物输出到 dist/，可直接交由 Nginx 托管
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  // 后端 address 与 API 前缀分离，便于 dev 代理只代理 path 前缀
  const backend = env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000'
  const apiPrefix = env.VITE_API_PREFIX || '/api/v1'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: 5173,
      host: '127.0.0.1',
      proxy: {
        // 仅开发期生效；生产由 VITE_API_BASE_URL 直连网关
        [apiPrefix]: proxyTo(backend),
        '/healthz': proxyTo(backend),
        '/readyz': proxyTo(backend),
        '/static': proxyTo(backend),
        '/mcp': proxyTo(backend),
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      chunkSizeWarningLimit: 1500,
      rollupOptions: {
        output: {
          // 把体积最大的两个依赖单独拆包，避免首屏 chunk 过大
          manualChunks: {
            arco: ['@arco-design/web-vue'],
            x6: ['@antv/x6'],
          },
        },
      },
    },
  }
})
