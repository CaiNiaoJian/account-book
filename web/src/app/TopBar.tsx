/**
 * 顶部工具栏 —— 页面标题、全局操作与外观控制。
 *
 * 布局参照 macOS 的「统一工具栏」：标题靠左、操作靠右，
 * 中间留白，整条工具栏与内容区之间只用一条极淡的分隔线。
 *
 * 包含的外观开关（都在 P0 就可真实生效并持久化，REQ-7）：
 *   * 侧边栏折叠
 *   * 隐私模式（金额打码）—— 图标按钮，一眼可见当前状态
 *   * 主题：浅色 / 深色 / 跟随系统（分段控件，选中态即时生效并写入后端）
 *   * 语言：中 / 英（P0 即完整可用，非占位）
 */

import { motion } from 'framer-motion'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { Icon } from '@/components/Icon'
import { IconButton, Segmented } from '@/components/ui'
import { useLedger } from '@/features/ledger/store'
import { useI18n, LANGUAGES, LANGUAGE_LABELS } from '@/i18n'
import type { LanguageCode, ThemePreference } from '@/lib/boot'
import { ALL_NAV_ITEMS, isImplemented } from './navigation'
import { usePreferences } from './preferences'

interface TopBarProps {
  title: string
  phase: string
  collapsed: boolean
  onToggleCollapse: () => void
}

export function TopBar({ title, phase, collapsed, onToggleCollapse }: TopBarProps) {
  const { t, language } = useI18n()
  const { preferences, update, saving, syncError } = usePreferences()
  const navigate = useNavigate()
  const { allAccounts } = useLedger()
  const [refreshing, setRefreshing] = useState(false)

  const refreshPage = () => {
    if (refreshing) return
    setRefreshing(true)
    // 整页重新加载会重试页面请求和全局账本字典，保留当前 URL 与会话。
    // 仅刷新账本 Context 无法恢复各页面独立维护的加载失败状态。
    window.location.reload()
  }

  const themeOptions: { value: ThemePreference; label: string; icon: 'sun' | 'moon' | 'monitor'; title: string }[] = [
    { value: 'light', label: '', icon: 'sun', title: t('topbar.themeLight') },
    { value: 'dark', label: '', icon: 'moon', title: t('topbar.themeDark') },
    { value: 'system', label: '', icon: 'monitor', title: t('topbar.themeSystem') },
  ]

  // 快捷记账的可用性由导航清单驱动：模块一旦在本阶段实现，按钮自动变为可用，
  // 不需要在工具栏里硬编码阶段号（避免两处维护导致不一致）。
  //
  // **这里曾经写成 `quickAddPhase === IMPLEMENTED_PHASE`，那是个会自己坏掉的判断。**
  // 快捷记账属于 P1，而 `IMPLEMENTED_PHASE` 随项目推进变成了 'P6'，
  // 于是 `'P1' === 'P6'` 为假，按钮被渲染成「禁用 + P1 徽标」——
  // 一个早已完成的功能，在阶段推进之后自己变成了"未实现"。
  //
  // 正确的判断是"这个模块的阶段**已包含在**已完成阶段里"，
  // 也就是 `isImplemented()`（侧边栏一直用的是它）。
  // 相等判断只在"最新阶段恰好就是这个模块的阶段"时才对，而那只是巧合。
  const quickAddItem = ALL_NAV_ITEMS.find((item) => item.id === 'quickAdd')
  const quickAddPhase = quickAddItem?.phase ?? 'P1'
  const quickAddAvailable = quickAddItem ? isImplemented(quickAddItem) : false

  return (
    <header className="ab-material relative z-10 flex h-[52px] shrink-0 items-center gap-2 border-b border-separator/60 px-3.5">
      <IconButton
        icon="panelLeft"
        label={collapsed ? t('topbar.expandSidebar') : t('topbar.collapseSidebar')}
        onClick={onToggleCollapse}
      />

      {/* ---- 标题区 ------------------------------------------------------- */}
      <div className="ml-0.5 flex min-w-0 items-baseline gap-2">
        <h1 className="truncate text-ab-title3 text-label">{title}</h1>
        {phase !== 'P0' ? <span className="ab-phase-badge">{phase}</span> : null}
        {allAccounts.some((account) => account.currency !== 'CNY') ? (
          <span className="shrink-0 text-ab-caption text-label-3" title={t('topbar.currencyHint')}>
            {t('topbar.currencySummary')}
          </span>
        ) : null}
      </div>

      <div className="flex-1" />

      {/* ---- 全局搜索（完整命令面板属 P1，这里先提供可聚焦入口） ---------- */}
      <div className="hidden items-center gap-1.5 rounded-ab-sm bg-hairline/5 px-2 py-[5px] transition-colors focus-within:bg-hairline/10 lg:flex">
        <Icon name="search" size={13} className="shrink-0 text-label-3" />
        <input
          aria-label={t('topbar.searchPlaceholder')}
          placeholder={t('topbar.searchPlaceholder')}
          className="w-[200px] bg-transparent text-ab-footnote text-label placeholder:text-label-3 focus:outline-none"
        />
      </div>

      {/* ---- 隐私模式 ----------------------------------------------------- */}
      <IconButton
        icon="eye"
        label={t('topbar.privacyMode')}
        active={preferences.privacy_mode}
        onClick={() => void update({ privacy_mode: !preferences.privacy_mode })}
      />

      {/* ---- 主题切换 ----------------------------------------------------- */}
      <Segmented<ThemePreference>
        ariaLabel={t('topbar.cycleTheme', { theme: preferences.theme })}
        options={themeOptions}
        value={preferences.theme}
        onChange={(value) => void update({ theme: value })}
        size="sm"
      />

      {/* ---- 语言切换 ----------------------------------------------------- */}
      <Segmented<LanguageCode>
        ariaLabel={t('topbar.language')}
        options={LANGUAGES.map((code) => ({ value: code, label: LANGUAGE_LABELS[code].slice(0, 2) }))}
        value={language}
        onChange={(value) => void update({ language: value })}
        size="sm"
      />

      <button
        type="button"
        className="ab-btn-secondary ml-1 shrink-0 whitespace-nowrap"
        title={t('topbar.refreshHint')}
        disabled={refreshing || saving}
        aria-busy={refreshing}
        onClick={refreshPage}
      >
        <Icon
          name="refresh"
          size={15}
          className={refreshing && !preferences.reduce_motion ? 'animate-spin' : undefined}
        />
        {t(refreshing ? 'topbar.refreshing' : 'topbar.refresh')}
      </button>

      {/* ---- 主操作：记一笔 ------------------------------------------------
          尚未实现的阶段**不**把它画成一个醒目的可用按钮 —— 那会误导用户。
          这里降级为次级按钮 + 阶段徽标，并把原因写进 tooltip。

          可用时**必须挂 onClick**：这个按钮此前只有外观没有行为，
          即使可用性判断正确，点下去也什么都不会发生。
          与全局快捷键走同一条路由（`/quick-add`），
          由流水页据路由打开快捷记账对话框。 */}
      {quickAddAvailable ? (
        <button
          type="button"
          className="ab-btn-primary ml-1"
          title={t('topbar.quickAdd')}
          onClick={() => navigate('/quick-add')}
        >
          <Icon name="plus" size={15} strokeWidth={2.2} />
          {t('topbar.quickAdd')}
        </button>
      ) : (
        <button
          type="button"
          className="ab-btn-secondary ml-1"
          disabled
          title={t('phase.badge', { phase: quickAddPhase })}
        >
          <Icon name="plus" size={15} strokeWidth={2.2} />
          {t('topbar.quickAdd')}
          <span className="ab-phase-badge ml-0.5">{quickAddPhase}</span>
        </button>
      )}

      {/* ---- 同步状态指示：保存中 / 保存失败（不打扰，但绝不隐瞒） -------- */}
      {saving || syncError ? (
        <motion.span
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          className={[
            'ml-1 h-1.5 w-1.5 shrink-0 rounded-full',
            syncError ? 'bg-negative' : 'animate-ab-pulse-soft bg-accent',
          ].join(' ')}
          title={syncError ? t('common.saveFailed', { message: syncError }) : t('common.saving')}
        />
      ) : null}
    </header>
  )
}
