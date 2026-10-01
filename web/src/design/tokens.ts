/**
 * 设计令牌的 JS 侧访问层。
 *
 * 为什么需要它（而不仅仅是 CSS 类）
 * ---------------------------------
 * ECharts 把图形画在 `<canvas>` 上，**不认 CSS 变量**，必须拿到具体的
 * `rgb(...)` 字符串。同理，Framer Motion 的部分动画也需要真实色值。
 * 因此这里提供两种读取方式：
 *
 *   tokenVar(name)     → `rgb(var(--ab-name))`，用于 style 属性与 CSS-in-JS
 *   resolveToken(name) → 从计算样式解析出的 `rgb(r, g, b)`，用于 canvas / 图表
 *
 * 注意：`resolveToken` 依赖当前主题的计算结果，
 * 主题切换后必须重新读取（P2 的图表主题会订阅主题变化后重绘）。
 */

/** 令牌名 → CSS 变量表达式，可直接用于 inline style */
export function tokenVar(name: TokenName): string {
  return `rgb(var(--ab-${name}))`
}

/** 令牌名 → 带透明度的 CSS 变量表达式 */
export function tokenVarAlpha(name: TokenName, alpha: number): string {
  return `rgb(var(--ab-${name}) / ${alpha})`
}

/** 从文档根节点的计算样式中解析出真实色值（供 canvas 使用） */
export function resolveToken(name: TokenName): string {
  if (typeof window === 'undefined') return '#000000'
  const raw = getComputedStyle(document.documentElement).getPropertyValue(`--ab-${name}`).trim()
  if (!raw) return '#000000'
  const parts = raw.split(/[\s,]+/).map((value) => Number.parseFloat(value))
  if (parts.length < 3 || parts.some((value) => Number.isNaN(value))) return '#000000'
  const [r, g, b] = parts as [number, number, number]
  return `rgb(${r}, ${g}, ${b})`
}

/** 从令牌解析出 rgba（图表需要半透明填充时使用） */
export function resolveTokenAlpha(name: TokenName, alpha: number): string {
  if (typeof window === 'undefined') return `rgba(0,0,0,${alpha})`
  const raw = getComputedStyle(document.documentElement).getPropertyValue(`--ab-${name}`).trim()
  const parts = raw.split(/[\s,]+/).map((value) => Number.parseFloat(value))
  if (parts.length < 3 || parts.some((value) => Number.isNaN(value))) return `rgba(0,0,0,${alpha})`
  const [r, g, b] = parts as [number, number, number]
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** 允许被 JS 访问的令牌名（与 tokens.css 保持同步） */
export type TokenName =
  | 'bg'
  | 'bg-elevated'
  | 'surface'
  | 'surface-2'
  | 'surface-3'
  | 'text'
  | 'text-2'
  | 'text-3'
  | 'separator'
  | 'accent'
  | 'accent-hover'
  | 'positive'
  | 'negative'
  | 'warning'
  | 'info'
  | 'purple'
  | 'pink'
  | 'indigo'
  | 'teal'
  | 'mint'
  | 'orange'
  | 'yellow'
  // 分类与机构主色引用的补充色（种子数据里已在用）
  | 'red'
  | 'green'
  | 'blue'
  | 'brown'
  | 'gray'

/** 图表分类色序（P2 起被 ECharts 主题直接消费） */
export const CHART_SERIES_TOKENS: TokenName[] = [
  'accent',
  'positive',
  'orange',
  'purple',
  'negative',
  'teal',
  'yellow',
  'indigo',
]

/** 解析出当前主题下的图表色板 */
export function chartPalette(): string[] {
  return CHART_SERIES_TOKENS.map((name) => resolveToken(name))
}

/**
 * 涨跌配色（REQ-17 资产 K 线）。
 *
 * 这不是"偏好设置里的一个小开关"，而是**文化差异**：
 * A 股用户看到绿色上涨会本能地认为"出了问题"。因此默认 `cn`，
 * 并在设置中显式提供 `intl` 选项。
 */
export function moneyColors(scheme: 'cn' | 'intl'): { up: string; down: string } {
  const positive = resolveToken('positive')
  const negative = resolveToken('negative')
  return scheme === 'cn' ? { up: negative, down: positive } : { up: positive, down: negative }
}
