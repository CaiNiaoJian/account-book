/**
 * 用户偏好上下文 —— 主题、语言、隐私模式、涨跌配色的**唯一真源**。
 *
 * 为什么用一个 Provider 承载全部偏好
 * ----------------------------------
 * 这些偏好彼此关联：切换主题要同时影响界面与图表；隐私模式要影响所有金额渲染；
 * 语言要影响 i18n。若各模块自己存一份，很快就会出现"设置页显示深色、
 * 图表还是浅色"这类不一致。集中一处，改动一处生效。
 *
 * 持久化策略
 * ----------
 * * **后端是唯一持久化载体**（`<data>/config.json`），不写 localStorage。
 *   理由：localStorage 与后端配置会出现两份真源，冲突时无法判定谁对，
 *   而 WebView2 的用户数据目录在便携模式下随 U 盘移动，语义更清晰。
 * * 采用**乐观更新**：先改内存让界面立即响应，再发请求；
 *   失败时**回滚**并在控制台告警，绝不让界面停留在"假的成功"状态。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { api } from '@/lib/api'
import {
  boot,
  DEFAULT_PREFERENCES,
  type MoneyColorScheme,
  type Preferences,
  type ResolvedTheme,
  type ThemePreference,
} from '@/lib/boot'

interface PreferencesContextValue {
  preferences: Preferences
  /** 更新一项或多项偏好（乐观更新 + 失败回滚） */
  update: (changes: Partial<Preferences>) => Promise<void>
  /** 当前**实际生效**的主题（`system` 已解析为 light/dark） */
  resolvedTheme: ResolvedTheme
  /** 切换主题（在 light → dark → system 之间循环，供工具栏按钮使用） */
  cycleTheme: () => void
  /** 是否正在与后端通信（用于显示轻微的状态指示） */
  saving: boolean
  /** 最近一次同步错误（有值时可提示用户"设置未能保存"） */
  syncError: string | null
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null)

/** 读取系统当前是否为深色（`prefers-color-scheme`） */
function systemPrefersDark(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

/** 把 `light | dark | system` 解析为实际生效主题 */
export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference === 'system') return systemPrefersDark() ? 'dark' : 'light'
  return preference
}

/**
 * 把主题应用到 `<html>` 上。
 *
 * 这个函数在**模块加载时**就会被调用一次（见文件末尾），
 * 目的是让首帧就是正确主题 —— 否则深色用户会看到一闪而过的白屏。
 */
export function applyThemeToDocument(theme: ResolvedTheme): void {
  const root = document.documentElement
  root.classList.toggle('dark', theme === 'dark')
  // 让原生控件（滚动条、表单控件）也跟随
  root.style.colorScheme = theme
}

/** 应用「减少动态效果」（REQ-7 无障碍） */
function applyReduceMotion(enabled: boolean): void {
  document.documentElement.classList.toggle('ab-reduce-motion', enabled)
}

/** 短暂开启颜色过渡，避免主题切换时出现生硬的跳变 */
function withThemeTransition(action: () => void): void {
  const root = document.documentElement
  root.classList.add('ab-theme-transition')
  action()
  window.setTimeout(() => root.classList.remove('ab-theme-transition'), 320)
}

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [preferences, setPreferences] = useState<Preferences>(boot.preferences ?? DEFAULT_PREFERENCES)
  const [saving, setSaving] = useState(false)
  const [syncError, setSyncError] = useState<string | null>(null)
  const [systemDark, setSystemDark] = useState<boolean>(systemPrefersDark)

  // 用 ref 保存最新偏好，供异步回调读取，避免闭包捕获旧值
  const preferencesRef = useRef(preferences)
  preferencesRef.current = preferences

  // ---- 跟随系统主题：仅在 preference === 'system' 时生效 -------------------
  useEffect(() => {
    if (!window.matchMedia) return
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = (event: MediaQueryListEvent) => setSystemDark(event.matches)
    query.addEventListener('change', handler)
    return () => query.removeEventListener('change', handler)
  }, [])

  const resolvedTheme: ResolvedTheme = useMemo(() => {
    if (preferences.theme === 'system') return systemDark ? 'dark' : 'light'
    return preferences.theme
  }, [preferences.theme, systemDark])

  // ---- 主题落到 DOM -------------------------------------------------------
  useEffect(() => {
    withThemeTransition(() => applyThemeToDocument(resolvedTheme))
  }, [resolvedTheme])

  // ---- 减少动效落到 DOM ---------------------------------------------------
  useEffect(() => {
    applyReduceMotion(preferences.reduce_motion)
  }, [preferences.reduce_motion])

  // ---- 首次挂载时与后端对齐 ----------------------------------------------
  // 场景：用户直接刷新页面，或后端配置被外部修改过。
  // 失败不阻塞使用（用 boot 注入值继续工作）。
  useEffect(() => {
    let cancelled = false
    api
      .getPreferences()
      .then((response) => {
        if (!cancelled) setPreferences(response.preferences)
      })
      .catch((error: unknown) => {
        // eslint-disable-next-line no-console
        console.warn('[AccountBook] 读取后端偏好失败，继续使用启动注入值：', error)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // ---- 更新（乐观 + 回滚） ------------------------------------------------
  const update = useCallback(async (changes: Partial<Preferences>) => {
    const previous = preferencesRef.current
    const optimistic = { ...previous, ...changes }
    setPreferences(optimistic)
    setSaving(true)
    setSyncError(null)

    try {
      const response = await api.patchPreferences(changes)
      // 以后端返回为准：它能规范化字段（例如裁剪过长的数组）
      setPreferences(response.preferences)
    } catch (error) {
      setPreferences(previous) // 回滚，避免"界面上改了其实没保存"
      const message = error instanceof Error ? error.message : String(error)
      setSyncError(message)
      // eslint-disable-next-line no-console
      console.error('[AccountBook] 偏好保存失败，已回滚：', message)
    } finally {
      setSaving(false)
    }
  }, [])

  const cycleTheme = useCallback(() => {
    const order: ThemePreference[] = ['light', 'dark', 'system']
    const currentIndex = order.indexOf(preferencesRef.current.theme)
    const next = order[(currentIndex + 1) % order.length] ?? 'system'
    void update({ theme: next })
  }, [update])

  const value = useMemo<PreferencesContextValue>(
    () => ({ preferences, update, resolvedTheme, cycleTheme, saving, syncError }),
    [preferences, update, resolvedTheme, cycleTheme, saving, syncError],
  )

  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>
}

/** 读取偏好上下文；必须在 `PreferencesProvider` 内使用 */
export function usePreferences(): PreferencesContextValue {
  const context = useContext(PreferencesContext)
  if (!context) {
    throw new Error('usePreferences 必须在 <PreferencesProvider> 内部使用')
  }
  return context
}

/**
 * 便捷钩子：只关心主题相关状态（工具栏、图表主题会用到）。
 */
export function useTheme() {
  const { preferences, resolvedTheme, cycleTheme } = usePreferences()
  return {
    preference: preferences.theme,
    resolved: resolvedTheme,
    cycleTheme,
    /** 涨跌配色：K 线、热力图等需要 */
    moneyColorScheme: preferences.money_color_scheme as MoneyColorScheme,
    /** 隐私模式：金额渲染统一走 lib/format.ts 的 displayMinor */
    privacyMode: preferences.privacy_mode,
  }
}

// -----------------------------------------------------------------------------
// 首帧前置应用：模块加载即执行，早于 React 渲染，消除主题闪烁
// -----------------------------------------------------------------------------
applyThemeToDocument(resolveTheme(boot.preferences?.theme ?? 'system'))
applyReduceMotion(Boolean(boot.preferences?.reduce_motion))
