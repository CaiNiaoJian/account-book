/**
 * 格式化工具 —— 金额 / 数字 / 日期。
 *
 * 金额铁律（与后端一致）
 * ----------------------
 * 后端一律用**整数最小单位（分）**存储与传输，前端也只在"渲染的最后一步"
 * 才转成字符串。绝不允许把浮点数用于计算 —— 0.1 + 0.2 的教训不必再交一次学费。
 * 因此这里的 `formatMinor` 是**格式化**而非运算；所有运算发生在后端。
 */

/** 各币种的最小单位位数（默认 2；日元、韩元等无小数） */
const MINOR_UNITS: Record<string, number> = {
  CNY: 2,
  USD: 2,
  EUR: 2,
  GBP: 2,
  HKD: 2,
  JPY: 0,
  KRW: 0,
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: '¥',
  USD: '$',
  EUR: '€',
  GBP: '£',
  HKD: 'HK$',
  JPY: '¥',
  KRW: '₩',
}

export function currencySymbol(currency = 'CNY'): string {
  return CURRENCY_SYMBOLS[currency] ?? currency
}

/**
 * 把整数最小单位格式化为带千分位的金额字符串。
 *
 * @param minor    金额（最小单位，整数）
 * @param currency ISO 币种代码
 * @param options.showSymbol 是否显示币种符号（默认 true）
 * @param options.showSign   是否强制显示正负号（默认 false，仅负数带号）
 */
export function formatMinor(
  minor: number,
  currency = 'CNY',
  options: { showSymbol?: boolean; showSign?: boolean } = {},
): string {
  const { showSymbol = true, showSign = false } = options
  const digits = MINOR_UNITS[currency] ?? 2
  const negative = minor < 0
  const absolute = Math.abs(minor)
  const divisor = 10 ** digits
  const whole = Math.trunc(absolute / divisor)
  const fraction = absolute % divisor

  const wholeText = whole.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
  const fractionText = digits > 0 ? `.${String(fraction).padStart(digits, '0')}` : ''
  const sign = negative ? '-' : showSign ? '+' : ''
  const symbol = showSymbol ? currencySymbol(currency) : ''

  return `${sign}${symbol}${wholeText}${fractionText}`
}

/**
 * 隐私模式下的金额遮罩（REQ-14）。
 *
 * 刻意保留位数与分隔符的形状：用户仍能看出"这是个大额还是小额"，
 * 但旁人无法读到具体数值。这与"直接把金额全变成 ****"的体验差别很大。
 */
export function maskMinor(minor: number, currency = 'CNY'): string {
  const text = formatMinor(minor, currency)
  // 保留符号、千分位与小数点，其余数字替换为 •
  return text.replace(/\d/g, '•')
}

/** 按隐私模式选择真实金额或遮罩 */
export function displayMinor(
  minor: number,
  currency = 'CNY',
  privacyMode = false,
  options?: { showSymbol?: boolean; showSign?: boolean },
): string {
  return privacyMode ? maskMinor(minor, currency) : formatMinor(minor, currency, options)
}

/** 紧凑金额：用于 KPI 卡片与图表轴标签（12.3万 / 1.2M） */
export function compactMinor(minor: number, currency = 'CNY'): string {
  const digits = MINOR_UNITS[currency] ?? 2
  const value = minor / 10 ** digits
  const absolute = Math.abs(value)
  const symbol = currencySymbol(currency)
  const sign = value < 0 ? '-' : ''

  if (absolute >= 1_0000_0000) return `${sign}${symbol}${(absolute / 1_0000_0000).toFixed(2)}亿`
  if (absolute >= 1_0000) return `${sign}${symbol}${(absolute / 1_0000).toFixed(2)}万`
  return `${sign}${symbol}${absolute.toLocaleString('zh-CN', { maximumFractionDigits: digits })}`
}

/** 百分比（传入 0–1 的小数） */
export function formatPercent(ratio: number, digits = 1): string {
  if (!Number.isFinite(ratio)) return '—'
  return `${(ratio * 100).toFixed(digits)}%`
}

/** 带符号的百分比，用于环比同比 */
export function formatDelta(ratio: number, digits = 1): string {
  if (!Number.isFinite(ratio)) return '—'
  const sign = ratio > 0 ? '+' : ''
  return `${sign}${(ratio * 100).toFixed(digits)}%`
}

/** ISO 时间 → 本地日期时间（中文习惯，不依赖 Intl 默认区域） */
export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (value: number) => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

/** 秒 → 人类可读的运行时长（`3分12秒`） */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  if (hours > 0) return `${hours}小时${minutes}分`
  if (minutes > 0) return `${minutes}分${secs}秒`
  return `${secs}秒`
}
