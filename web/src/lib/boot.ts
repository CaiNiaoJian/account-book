/**
 * 启动引导数据（Boot Payload）。
 *
 * 数据来源：后端在渲染 index.html 时注入的 `window.__AB_BOOT__`
 * （实现见 `accountbook/api/server.py::_render_index`）。
 *
 * 为什么用服务端注入而不是再开一个接口：
 *   首帧就能拿到主题与语言，不会出现"先白屏 → 请求配置 → 再重绘"的闪烁；
 *   同时令牌随页面到达，第一个 API 调用可以立即发出，省掉一次往返。
 *
 * 开发模式兜底：直接访问 Vite 开发服务器（5173）时没有注入，
 * 此时从 URL 查询参数里取令牌并给出合理默认值 —— 保证 `npm run dev` 可用。
 */

/** 后端报告的路径信息（仅路径字符串，见 api/routes/system.py） */
export interface BootPaths {
  dataDir: string
  logFile: string
  database: string
}

export interface BootRuntime {
  portable: boolean
  frozen: boolean
}

/** 与后端 `UserPreferences` 字段一一对应（见 accountbook/config.py） */
export interface Preferences {
  schema_version: number
  theme: ThemePreference
  language: LanguageCode
  reduce_motion: boolean
  money_color_scheme: MoneyColorScheme
  privacy_mode: boolean
  sidebar_collapsed: boolean
  sidebar_order: string[]
  /** 仪表盘分区顺序（空数组 = 用内置默认顺序） */
  dashboard_layout: string[]
  window: {
    width: number
    height: number
    x: number | null
    y: number | null
    maximized: boolean
  }
  last_version: string
  first_run_completed: boolean
  backup_enabled: boolean
  backup_interval_hours: number
}

export type ThemePreference = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'
export type LanguageCode = 'zh-CN' | 'en-US'
/** cn = 红涨绿跌（A 股习惯）；intl = 绿涨红跌（国际习惯） */
export type MoneyColorScheme = 'cn' | 'intl'

export interface BootPayload {
  appId: string
  appName: string
  appNameEn: string
  version: string
  /** 当前实现阶段，用于给未完成功能打「计划于 Pn」标记（需求 10） */
  phase: string
  token: string
  port: number
  serverTime: string
  paths: BootPaths
  runtime: BootRuntime
  preferences: Preferences
}

/** 与后端保持一致的默认偏好 —— 后端不可用时前端仍应能正常渲染 */
export const DEFAULT_PREFERENCES: Preferences = {
  schema_version: 1,
  theme: 'system',
  language: 'zh-CN',
  reduce_motion: false,
  money_color_scheme: 'cn',
  privacy_mode: false,
  sidebar_collapsed: false,
  sidebar_order: [],
  dashboard_layout: [],
  window: { width: 1320, height: 880, x: null, y: null, maximized: false },
  last_version: '',
  first_run_completed: false,
  backup_enabled: true,
  backup_interval_hours: 24,
}

declare global {
  interface Window {
    __AB_BOOT__?: Partial<BootPayload>
  }
}

/** 从 URL 中读取开发模式所需的令牌（生产模式下由后端注入，无需此路径） */
function tokenFromLocation(): string {
  try {
    return new URLSearchParams(window.location.search).get('token') ?? ''
  } catch {
    return ''
  }
}

function detectLanguage(): LanguageCode {
  return navigator.language?.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en-US'
}

/**
 * 归一化后的引导数据。模块加载即完成，任何组件都可安全地同步读取。
 */
export const boot: BootPayload = (() => {
  const injected = window.__AB_BOOT__
  const fallback: BootPayload = {
    appId: 'AccountBook',
    appName: '记账本',
    appNameEn: 'AccountBook',
    version: '0.0.0-dev',
    phase: 'P6',
    token: tokenFromLocation(),
    port: 0,
    serverTime: new Date().toISOString(),
    paths: { dataDir: '', logFile: '', database: '' },
    runtime: { portable: false, frozen: false },
    preferences: { ...DEFAULT_PREFERENCES, language: detectLanguage() },
  }

  if (!injected) {
    // 开发模式：没有服务端注入，使用兜底值并给出可读提示
    // eslint-disable-next-line no-console
    console.info('[AccountBook] 未检测到启动注入数据，使用开发模式兜底配置')
    return fallback
  }

  return {
    ...fallback,
    ...injected,
    paths: { ...fallback.paths, ...(injected.paths ?? {}) },
    runtime: { ...fallback.runtime, ...(injected.runtime ?? {}) },
    preferences: { ...fallback.preferences, ...(injected.preferences ?? {}) },
  }
})()

/** 是否运行在打包后的产物中（影响"关于"页提示与调试入口显隐） */
export const isFrozen = boot.runtime.frozen

/** 版本号带阶段标记的展示形式，例如 `0.1.0 · P0` */
export const versionLabel = `${boot.version} · ${boot.phase}`
