/**
 * Tailwind 配置 —— 把「设计令牌」映射为工具类。
 *
 * 核心约定（REQ-12 Apple 美学 / REQ-7 日夜切换）
 * ----------------------------------------------
 * 所有颜色都指向 CSS 变量（`rgb(var(--ab-*))`），**不在本文件里写死任何色值**。
 * 于是：
 *   * 日夜主题切换只是换一套变量，无需改任何组件；
 *   * 未来「自定义主题编辑器」（P9）只需覆盖变量；
 *   * 图表库也用同一套变量，界面与图表永不脱节。
 *
 * 变量定义见 `src/styles/tokens.css`。
 */

/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ['class', '.dark'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],

  theme: {
    extend: {
      colors: {
        // ---- 底层背景与容器 ----
        bg: 'rgb(var(--ab-bg) / <alpha-value>)',
        elevated: 'rgb(var(--ab-bg-elevated) / <alpha-value>)',
        surface: 'rgb(var(--ab-surface) / <alpha-value>)',
        'surface-2': 'rgb(var(--ab-surface-2) / <alpha-value>)',
        'surface-3': 'rgb(var(--ab-surface-3) / <alpha-value>)',

        // ---- 文本层级（Apple 的 label / secondaryLabel / tertiaryLabel 三级） ----
        label: 'rgb(var(--ab-text) / <alpha-value>)',
        'label-2': 'rgb(var(--ab-text-2) / <alpha-value>)',
        'label-3': 'rgb(var(--ab-text-3) / <alpha-value>)',

        // ---- 分隔线 ----
        separator: 'rgb(var(--ab-separator) / <alpha-value>)',
        // 比 separator 更淡的"发丝线"：用于悬停填充与徽标底色。
        // 注意：这个键必须与 tokens.css 中的 --ab-hairline 成对出现，
        // 只定义变量而忘记在此注册，会导致 @apply bg-hairline 在构建期报错。
        hairline: 'rgb(var(--ab-hairline) / <alpha-value>)',

        // ---- 语义色 ----
        accent: 'rgb(var(--ab-accent) / <alpha-value>)',
        'accent-hover': 'rgb(var(--ab-accent-hover) / <alpha-value>)',
        positive: 'rgb(var(--ab-positive) / <alpha-value>)',
        negative: 'rgb(var(--ab-negative) / <alpha-value>)',
        warning: 'rgb(var(--ab-warning) / <alpha-value>)',
        info: 'rgb(var(--ab-info) / <alpha-value>)',
        purple: 'rgb(var(--ab-purple) / <alpha-value>)',
        pink: 'rgb(var(--ab-pink) / <alpha-value>)',
        indigo: 'rgb(var(--ab-indigo) / <alpha-value>)',
        teal: 'rgb(var(--ab-teal) / <alpha-value>)',
        mint: 'rgb(var(--ab-mint) / <alpha-value>)',
        orange: 'rgb(var(--ab-orange) / <alpha-value>)',
        yellow: 'rgb(var(--ab-yellow) / <alpha-value>)',
      },

      // ---- 圆角：Apple 的连续圆角尺度（卡片 20 / 控件 10 / 弹层 28） ----
      borderRadius: {
        'ab-xs': '6px',
        'ab-sm': '10px',
        'ab-md': '14px',
        'ab-lg': '20px',
        'ab-xl': '28px',
      },

      // ---- 阴影：三档，全部极低透明度，追求"悬浮感"而非"重投影" ----
      boxShadow: {
        'ab-1': '0 1px 2px rgb(0 0 0 / 0.04), 0 1px 1px rgb(0 0 0 / 0.03)',
        'ab-2': '0 4px 16px rgb(0 0 0 / 0.07), 0 1px 3px rgb(0 0 0 / 0.04)',
        'ab-3': '0 16px 48px rgb(0 0 0 / 0.14), 0 2px 8px rgb(0 0 0 / 0.06)',
        'ab-inset': 'inset 0 0 0 1px rgb(var(--ab-separator) / 0.7)',
      },

      fontFamily: {
        // 与后端 DWM 美化配合：优先系统字体，中文回落到苹方 / 雅黑
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          'SF Pro Text',
          'Segoe UI Variable Text',
          'Segoe UI',
          'PingFang SC',
          'Microsoft YaHei UI',
          'Microsoft YaHei',
          'Source Han Sans SC',
          'Noto Sans SC',
          'sans-serif',
        ],
        // 金额与数据使用等宽表格数字，避免跳动
        mono: ['SF Mono', 'Cascadia Mono', 'Consolas', 'Menlo', 'monospace'],
      },

      fontSize: {
        // Apple 的排版尺度（pt → px 近似）
        'ab-caption2': ['10px', { lineHeight: '13px', letterSpacing: '0.01em' }],
        'ab-caption': ['11px', { lineHeight: '14px', letterSpacing: '0.006em' }],
        'ab-footnote': ['12px', { lineHeight: '16px' }],
        'ab-subhead': ['13px', { lineHeight: '18px' }],
        'ab-callout': ['14px', { lineHeight: '19px' }],
        'ab-body': ['15px', { lineHeight: '21px' }],
        'ab-headline': ['15px', { lineHeight: '21px', fontWeight: '600' }],
        'ab-title3': ['17px', { lineHeight: '23px', fontWeight: '600', letterSpacing: '-0.01em' }],
        'ab-title2': ['20px', { lineHeight: '26px', fontWeight: '600', letterSpacing: '-0.015em' }],
        'ab-title1': ['26px', { lineHeight: '32px', fontWeight: '600', letterSpacing: '-0.02em' }],
        'ab-large': ['34px', { lineHeight: '40px', fontWeight: '700', letterSpacing: '-0.025em' }],
      },

      transitionTimingFunction: {
        // Apple 风格的标准缓动（先快后慢，收尾柔和）
        'ab-standard': 'cubic-bezier(0.32, 0.72, 0, 1)',
        'ab-emphasized': 'cubic-bezier(0.2, 0, 0, 1)',
        'ab-spring': 'cubic-bezier(0.34, 1.56, 0.64, 1)',
      },

      transitionDuration: {
        ab1: '150ms',
        ab2: '250ms',
        ab3: '400ms',
      },

      backdropBlur: {
        // 侧边栏与工具栏的半透明材质
        ab: '24px',
        'ab-heavy': '48px',
      },

      keyframes: {
        'ab-fade-up': {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        'ab-shimmer': {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        'ab-pulse-soft': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.55' },
        },
      },

      animation: {
        'ab-fade-up': 'ab-fade-up 250ms cubic-bezier(0.32, 0.72, 0, 1) both',
        'ab-shimmer': 'ab-shimmer 1.6s linear infinite',
        'ab-pulse-soft': 'ab-pulse-soft 2.4s ease-in-out infinite',
      },
    },
  },

  plugins: [],
}
