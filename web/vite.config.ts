/**
 * Vite 配置 —— 前端构建与开发服务器。
 *
 * 关键决策
 * --------
 * 1. **产物直接输出到 Python 包内**（`src/accountbook/resources/web_dist`）：
 *    这样 PyInstaller 只需收集一个目录，不存在"忘记拷贝前端"的经典事故。
 * 2. **base: './'**：使用相对路径引用资源，使页面在任意挂载路径下都能工作。
 * 3. **开发代理**：开发模式下后端固定监听 8787，Vite 把 `/api` 与 `/health`
 *    代理过去。于是浏览器看到的是**同源**请求，CORS、Cookie、令牌三条链路
 *    全部与生产一致 —— 避免"开发能用、打包就坏"。
 * 4. **手动分包**：把 react / framer-motion / echarts 拆开，
 *    既改善首屏并行加载，也让后续增量升级单个库时不至于整包失效缓存。
 */

import path from 'node:path'
import { fileURLToPath } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const rootDir = path.dirname(fileURLToPath(import.meta.url))

/** 后端在开发模式下的固定端口，必须与 accountbook/config.py 的 dev_backend_port 一致 */
const DEV_BACKEND = 'http://127.0.0.1:8787'

export default defineConfig({
  plugins: [react()],

  // 根路径基址：页面由本进程的本地服务挂在 `/` 上，
  // 使用绝对基址可避免 SPA 深链（如 /transactions）下相对资源解析到错误层级。
  base: '/',

  resolve: {
    alias: {
      '@': path.resolve(rootDir, 'src'),
    },
  },

  build: {
    outDir: path.resolve(rootDir, '../src/accountbook/resources/web_dist'),
    emptyOutDir: true,
    // 目标内核由 WebView2（Chromium 内核）决定，无需兼容旧浏览器
    target: 'chrome114',
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        // 拆包策略：把体积大、变更频率低的库独立出来，
        // 使后续升级单个库时不必让用户重新下载全部资源。
        // 说明：echarts 将在 P2 首次被真正 import；此时手动分包会生成空 chunk 并告警，
        // 因此等 P2 接入图表后再把 vendor_charts 加回（计划已记录在 docs/PLAN.md）。
        manualChunks: {
          vendor_react: ['react', 'react-dom', 'react-router-dom'],
          vendor_motion: ['framer-motion'],
        },
      },
    },
  },

  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: DEV_BACKEND, changeOrigin: false },
      '/health': { target: DEV_BACKEND, changeOrigin: false },
    },
  },
})
