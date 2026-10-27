import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    // element-plus 为全量注册架构（app.use(ElementPlus)），单块 ~823KB 属已知情况，
    // 按需引入（unplugin-vue-components）列为后续优化；其余主包已拆分至 <500KB
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // 依赖拆包：Element Plus / 图标 / Markdown 渲染独立成块，避免主包超 500KB（ISSUE-02）
        manualChunks(id: string) {
          if (id.includes('node_modules')) {
            if (id.includes('element-plus') || id.includes('@element-plus')) return 'element-plus'
            if (id.includes('echarts')) return 'echarts'
            if (id.includes('marked') || id.includes('dompurify') || id.includes('highlight.js')) return 'markdown'
            return 'vendor'
          }
        },
      },
    },
  },
  server: {
    port: 5173,
    host: true,
    // 后端已开启 CORS(allow_origins=*)，故前端直连；如需代理可取消注释：
    // proxy: {
    //   '/api/v1': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    // },
  },
})
