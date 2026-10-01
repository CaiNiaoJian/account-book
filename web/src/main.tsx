/**
 * 前端入口。
 *
 * 顺序很重要：**先加载样式，再挂载 React**。
 * 若反过来，首帧会以浏览器默认样式渲染一次（白底黑字、无布局），
 * 用户会看到一次明显的闪烁 —— 这是桌面应用"廉价感"的主要来源之一。
 *
 * `StrictMode` 保持开启：它会在开发模式下双重执行 effect，
 * 从而暴露"忘记清理副作用"的问题（本项目里出现了多个订阅/定时器，
 * 这类保护很有价值）。生产构建中它不产生任何开销。
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import './styles/app.css'
import { App } from './App'
import { boot } from './lib/boot'

// 窗口标题是**跨层契约**：后端用它确认"界面窗口是否真的出现"
// （见 accountbook/shell/window.py 的 _wait_for_app_window，
// 以及 accountbook/__init__.py 的 APP_WINDOW_TITLE）。
// 因此这里统一由 boot 数据推导，而不是散落在各处硬编码字符串。
// 注意：刻意**不**跟随界面语言变化 —— 标记必须稳定，否则窗口探测会失效。
document.title = `${boot.appName} · ${boot.appNameEn}`

const container = document.getElementById('root')
if (!container) {
  // 只有在 index.html 被破坏时才可能发生；给出明确信息而不是静默白屏
  throw new Error('找不到 #root 挂载点，请检查 index.html 是否完整')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
