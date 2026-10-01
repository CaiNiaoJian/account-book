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
  TWD: 2,
  SGD: 2,
  AUD: 2,
  CAD: 2,
  JPY: 0,
  KRW: 0,
  VND: 0,
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: '¥',
  USD: '$',
  EUR: '€',
  GBP: '£',
  HKD: 'HK$',
  TWD: 'NT$',
  SGD: 'S$',
  AUD: 'A$',
  CAD: 'C$',
  JPY: '¥',
  KRW: '₩',
  VND: '₫',
}

/**
 * 用后端下发的币种字典覆盖内置表。
 *
 * 为什么以**后端为准**：最小单位位数是金额正确性的前提（日元 0 位、
 * 第纳尔 3 位）。若前端维护第二份，新币种上线时两边一旦不一致，
 * 所有金额都会差整数量级，而且很难看出是前端的错。
 * 内置表只作为"接口尚未返回时的兜底"。
 */
export function registerCurrencies(
  items: ReadonlyArray<{ code: string; symbol?: string; minor_units?: number }>,
): void {
  for (const item of items) {
    if (typeof item.minor_units === 'number') MINOR_UNITS[item.code] = item.minor_units
    if (item.symbol) CURRENCY_SYMBOLS[item.code] = item.symbol
  }
}

export function currencySymbol(currency = 'CNY'): string {
  return CURRENCY_SYMBOLS[currency] ?? currency
}

/** 币种的最小单位位数（供输入解析使用） */
export function minorUnits(currency = 'CNY'): number {
  return MINOR_UNITS[currency] ?? 2
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

// -----------------------------------------------------------------------------
// 日期（记账场景）
// -----------------------------------------------------------------------------

/**
 * 本地日期键（YYYY-MM-DD）。
 *
 * **不要用 `toISOString()`** —— 它会转成 UTC，在东八区会把当天 08:00
 * 之前的记录算到前一天，日历与时间轴会因此整体错位一天。
 */
export function localDayKey(value: Date | string = new Date()): string {
  const date = typeof value === 'string' ? new Date(value) : value
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 时间（HH:mm） */
export function formatTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 日期（M月D日，省略年份；跨年时补上年份） */
export function formatDayLabel(iso: string, today = new Date()): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  const sameYear = date.getFullYear() === today.getFullYear()
  const base = `${date.getMonth() + 1}月${date.getDate()}日`
  return sameYear ? base : `${date.getFullYear()}年${base}`
}

/** 星期几（中文单字，用于时间轴与日历表头） */
export function weekdayShort(value: Date | string): string {
  const date = typeof value === 'string' ? new Date(value) : value
  return ['日', '一', '二', '三', '四', '五', '六'][date.getDay()] ?? ''
}

/** 相对日：今天 / 昨天 / 明天 / 具体日期 */
export function relativeDayLabel(iso: string, today = new Date()): string {
  const key = localDayKey(iso)
  if (!key) return '—'
  const todayKey = localDayKey(today)
  const yesterday = new Date(today)
  yesterday.setDate(yesterday.getDate() - 1)
  const tomorrow = new Date(today)
  tomorrow.setDate(tomorrow.getDate() + 1)

  if (key === todayKey) return '今天'
  if (key === localDayKey(yesterday)) return '昨天'
  if (key === localDayKey(tomorrow)) return '明天'
  return formatDayLabel(iso, today)
}

/**
 * 把「元」文本解析为整数最小单位（分）。非法输入返回 `null`。
 *
 * 容错范围与后端 `core/money.py` 的 `parse_amount` 保持一致：
 * 允许币种符号、千分位、全角数字、中文"元"、前后空格。
 * 快捷记账场景下用户会直接粘贴 `¥1,234.56` 或 `1234.5元`，
 * 为此报错会毁掉"快速"二字。
 */
export function parseAmountToMinor(text: string, currency = 'CNY'): number | null {
  if (typeof text !== 'string') return null
  let cleaned = text.trim()
  for (const noise of ['¥', '￥', '$', '€', '£', '元', '人民币', 'RMB', 'rmb', ' ', '\u3000']) {
    cleaned = cleaned.split(noise).join('')
  }
  cleaned = cleaned.split(',').join('').split('，').join('')
  cleaned = cleaned.replace(/[０-９．－]/g, (ch) => {
    const map: Record<string, string> = {
      '０': '0', '１': '1', '２': '2', '３': '3', '４': '4',
      '５': '5', '６': '6', '７': '7', '８': '8', '９': '9',
      '．': '.', '－': '-',
    }
    return map[ch] ?? ch
  })
  if (!/^-?\d*(\.\d*)?$/.test(cleaned) || cleaned === '' || cleaned === '-' || cleaned === '.') return null

  const digits = minorUnits(currency)
  const negative = cleaned.startsWith('-')
  const body = negative ? cleaned.slice(1) : cleaned
  const [wholePart = '0', fractionPart = ''] = body.split('.')
  // 超出币种精度时四舍五入（与后端 ROUND_HALF_UP 一致）
  const padded = (fractionPart + '0'.repeat(digits + 1)).slice(0, digits + 1)
  const kept = padded.slice(0, digits)
  const nextDigit = Number(padded.charAt(digits) || '0')
  let minor = Number(wholePart || '0') * 10 ** digits + Number(kept || '0')
  if (nextDigit >= 5) minor += 1
  return negative ? -minor : minor
}
