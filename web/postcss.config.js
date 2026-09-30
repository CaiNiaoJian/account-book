/**
 * PostCSS 配置 —— Tailwind 与 Autoprefixer。
 *
 * 说明：WebView2 是 Chromium 内核，理论上不需要 autoprefixer；
 * 但保留它是为了未来「浏览器外壳」与导出 HTML 报告在旧内核上也能正确渲染。
 */
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
