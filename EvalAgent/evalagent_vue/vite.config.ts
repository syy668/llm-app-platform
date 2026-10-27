import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 独立仓库 evalagent_vue；端口 5174，避开项目一 frontend_vue 的 5173
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // 依赖拆包：Element Plus / ECharts 独立成块（与项目一前端同策略）
        manualChunks(id: string) {
          if (id.includes('node_modules')) {
            if (id.includes('element-plus') || id.includes('@element-plus')) return 'element-plus'
            if (id.includes('echarts')) return 'echarts'
            return 'vendor'
          }
        },
      },
    },
  },
  server: {
    port: 5174,
    host: true,
    // 后端已开启 CORS(allow_origins=*)，前端直连上游 API，不走代理
  },
})
