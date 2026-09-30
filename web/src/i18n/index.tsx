/**
 * 极简 i18n 实现。
 *
 * 为什么自研而不引入 i18next
 * -------------------------
 * 本项目需要的只有三件事：**类型安全的键**、**点分路径查找**、**占位符插值**。
 * i18next 提供了命名空间、复数、后处理、语言检测等大量能力，代价是
 * ~40KB 运行时与一套需要学习的配置。考虑到 P0 只有中英两种语言、
 * 且语言包已在编译期被类型检查约束，自研 60 行更划算，也更容易审计。
 *
 * 若未来需要复数规则或按需加载语言包，再替换为成熟方案即可 ——
 * 调用方只依赖 `useI18n()` 这一个接口，替换成本被限制在本文件内。
 */

import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react'

import { boot, type LanguageCode } from '@/lib/boot'
import { enUS } from './en-US'
import { zhCN, type Messages } from './zh-CN'

export type { Messages }

const DICTIONARIES: Record<LanguageCode, Messages> = {
  'zh-CN': zhCN,
  'en-US': enUS,
}

export const LANGUAGE_LABELS: Record<LanguageCode, string> = {
  'zh-CN': '简体中文',
  'en-US': 'English',
}

/** 所有可用语言的列表（设置页与工具栏切换用） */
export const LANGUAGES: LanguageCode[] = ['zh-CN', 'en-US']

export type TranslateParams = Record<string, string | number>

/**
 * 按点分路径查找文案。
 *
 * 返回 `undefined` 而不是抛错：文案缺失不应导致整个界面白屏。
 * 调用方（`t`）会退化为显示键名本身，这样开发者一眼就能看出漏了什么。
 */
function lookup(dictionary: Messages, path: string): string | undefined {
  const segments = path.split('.')
  let cursor: unknown = dictionary
  for (const segment of segments) {
    if (typeof cursor !== 'object' || cursor === null) return undefined
    cursor = (cursor as Record<string, unknown>)[segment]
  }
  return typeof cursor === 'string' ? cursor : undefined
}

/** 占位符插值：`{name}` → 实际值。未提供的占位符原样保留，便于定位漏传参数。 */
function interpolate(template: string, params?: TranslateParams): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, key: string) => {
    const value = params[key]
    return value === undefined ? match : String(value)
  })
}

/** 翻译函数类型：`t('nav.dashboard')` 或 `t('common.saveFailed', { message })` */
export type TranslateFn = (key: string, params?: TranslateParams) => string

interface I18nContextValue {
  language: LanguageCode
  t: TranslateFn
}

const I18nContext = createContext<I18nContextValue | null>(null)

export function createTranslator(language: LanguageCode): TranslateFn {
  const dictionary = DICTIONARIES[language] ?? zhCN
  return (key, params) => {
    const text = lookup(dictionary, key) ?? lookup(zhCN, key)
    if (text === undefined) {
      // 开发期显式暴露漏译，生产期也不至于显示空白
      // eslint-disable-next-line no-console
      console.warn(`[AccountBook][i18n] 缺少文案：${key}`)
      return key
    }
    return interpolate(text, params)
  }
}

/** 语言提供者：语言值来自偏好上下文，本组件只负责翻译查找 */
export function I18nProvider({ language, children }: { language: LanguageCode; children: ReactNode }) {
  const value = useMemo<I18nContextValue>(
    () => ({ language, t: createTranslator(language) }),
    [language],
  )
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

/**
 * 读取翻译函数。
 *
 * 不在 Provider 内时**不抛错**，而是按启动注入的语言降级工作 ——
 * 这样即使某个组件被单独渲染（单测、Storybook、错误边界内），也不会二次崩溃。
 */
export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext)
  if (context) return context
  const fallbackLanguage = boot.preferences?.language ?? 'zh-CN'
  return { language: fallbackLanguage, t: createTranslator(fallbackLanguage) }
}

/** 仅取翻译函数的便捷钩子（更常用的形式） */
export function useT(): TranslateFn {
  return useTInternal()
}

function useTInternal(): TranslateFn {
  const { t } = useI18n()
  return useCallback<TranslateFn>((key, params) => t(key, params), [t])
}
