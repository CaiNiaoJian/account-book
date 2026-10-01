/**
 * 设置页（P0 已真实可用）。
 *
 * 这里实现的三组设置**不是占位**：改主题、关动效、开隐私模式、切语言、
 * 打开数据目录，全部即时生效并持久化到 `<data>/config.json`。
 * 其余设置项以「后续设置项」的形式列出（带阶段徽标），
 * 让用户看到完整的规划而不是一个空页面（需求 10）。
 *
 * 每一个开关都遵循同一条链路：
 *   用户操作 → PreferencesProvider.update（乐观更新）
 *           → PATCH /api/system/preferences
 *           → 后端写 config.json 并通知桌面外壳同步原生标题栏
 * 因此"设置页改了但标题栏没变"这类不一致不可能发生。
 */

import { motion } from 'framer-motion'
import { useState } from 'react'

import { Icon, type IconName } from '@/components/Icon'
import { Card, PhaseBadge, Segmented, Switch } from '@/components/ui'
import { staggerContainer, staggerItem } from '@/design/motion'
import { LANGUAGE_LABELS, LANGUAGES, useI18n } from '@/i18n'
import { api } from '@/lib/api'
import { boot, type LanguageCode, type ThemePreference } from '@/lib/boot'
import { usePreferences } from '@/app/preferences'

interface UpcomingItem {
  icon: IconName
  labelKey: string
  phase: string
}

const UPCOMING: UpcomingItem[] = [
  { icon: 'quickAdd', labelKey: 'settings.upcomingHotkeys', phase: 'P1' },
  { icon: 'scheduler', labelKey: 'settings.upcomingScheduler', phase: 'P6' },
  { icon: 'intelligence', labelKey: 'settings.upcomingAi', phase: 'P7' },
  { icon: 'lock', labelKey: 'settings.upcomingSecurity', phase: 'P8' },
  { icon: 'plugins', labelKey: 'settings.upcomingPlugins', phase: 'P9' },
]

import { AiConfigCard } from '@/features/reports/AiPanel'

export function SettingsPage() {
  const { t, language } = useI18n()
  const { preferences, update, syncError } = usePreferences()
  const [openError, setOpenError] = useState<string | null>(null)

  const themeOptions: { value: ThemePreference; label: string; icon: IconName }[] = [
    { value: 'light', label: t('topbar.themeLight'), icon: 'sun' },
    { value: 'dark', label: t('topbar.themeDark'), icon: 'moon' },
    { value: 'system', label: t('topbar.themeSystem'), icon: 'monitor' },
  ]

  const handleOpenFolder = () => {
    setOpenError(null)
    api
      .revealDataDir()
      .then((result) => {
        if (result.status !== 'ok') setOpenError(result.detail ?? 'unknown')
      })
      .catch((error: unknown) => setOpenError(error instanceof Error ? error.message : String(error)))
  }

  return (
    <motion.div variants={staggerContainer} initial="initial" animate="animate" className="space-y-4">
      {syncError ? (
        <motion.div variants={staggerItem}>
          <p className="flex items-center gap-2 rounded-ab-md bg-negative/12 px-3.5 py-2.5 text-ab-footnote text-label">
            <Icon name="alert" size={14} className="shrink-0 text-negative" />
            {t('common.saveFailed', { message: syncError })}
          </p>
        </motion.div>
      ) : null}

      {/* ---- 外观 --------------------------------------------------------- */}
      <motion.div variants={staggerItem}>
        <Card title={t('settings.appearance')} subtitle={t('settings.appearanceNote')}>
          <div className="space-y-3.5">
            <SettingRow label={t('settings.theme')}>
              <Segmented<ThemePreference>
                ariaLabel={t('settings.theme')}
                options={themeOptions}
                value={preferences.theme}
                onChange={(value) => void update({ theme: value })}
              />
            </SettingRow>

            <SettingRow label={t('settings.reduceMotion')} hint={t('settings.reduceMotionNote')}>
              <Switch
                label={t('settings.reduceMotion')}
                hideLabel
                checked={preferences.reduce_motion}
                onChange={(checked) => void update({ reduce_motion: checked })}
              />
            </SettingRow>

            <SettingRow label={t('settings.privacy')} hint={t('settings.privacyNote')}>
              <Switch
                label={t('settings.privacy')}
                hideLabel
                checked={preferences.privacy_mode}
                onChange={(checked) => void update({ privacy_mode: checked })}
              />
            </SettingRow>
          </div>
        </Card>
      </motion.div>

      {/* ---- 语言 --------------------------------------------------------- */}
      <motion.div variants={staggerItem}>
        <Card title={t('settings.languageSection')} subtitle={t('settings.languageNote')}>
          <SettingRow label={t('topbar.language')}>
            <Segmented<LanguageCode>
              ariaLabel={t('topbar.language')}
              options={LANGUAGES.map((code) => ({ value: code, label: LANGUAGE_LABELS[code] }))}
              value={language}
              onChange={(value) => void update({ language: value })}
            />
          </SettingRow>
        </Card>
      </motion.div>

      {/* ---- 数据与隐私 --------------------------------------------------- */}
      <motion.div variants={staggerItem}>
        <Card title={t('settings.dataSection')} subtitle={t('settings.dataNote')}>
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-4">
              <span className="shrink-0 text-ab-subhead text-label">{t('settings.dataDir')}</span>
              <button type="button" className="ab-btn-secondary" onClick={handleOpenFolder}>
                <Icon name="folder" size={14} />
                {t('settings.openFolder')}
              </button>
            </div>
            <p className="ab-selectable break-all rounded-ab-sm bg-surface-2/80 px-2.5 py-2 font-mono text-ab-caption text-label-2">
              {boot.paths.dataDir || '—'}
            </p>
            <div className="flex items-center gap-2 text-ab-footnote text-label-2">
              <Icon name="shield" size={14} className="shrink-0 text-positive" />
              {t('about.privacyNote')}
            </div>
            {openError ? (
              <p className="text-ab-caption text-negative">{t('dashboard.revealFailed', { message: openError })}</p>
            ) : null}
          </div>
        </Card>
      </motion.div>

      {/* ---- 后续设置项 --------------------------------------------------- */}
      <motion.div variants={staggerItem}>
        <AiConfigCard />

        <Card title={t('settings.upcoming')} subtitle={t('settings.upcomingNote')}>
          <ul className="divide-y divide-separator/40">
            {UPCOMING.map((item) => (
              <li key={item.labelKey} className="flex items-center gap-2.5 py-2">
                <Icon name={item.icon} size={15} className="shrink-0 text-label-3" />
                <span className="flex-1 text-ab-subhead text-label-2">{t(item.labelKey)}</span>
                <PhaseBadge phase={item.phase} />
              </li>
            ))}
          </ul>
        </Card>
      </motion.div>
    </motion.div>
  )
}

/** 设置行：左标签（可带说明），右侧控件 */
function SettingRow({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="text-ab-subhead font-medium text-label">{label}</p>
        {hint ? <p className="mt-0.5 max-w-lg text-ab-footnote text-label-2">{hint}</p> : null}
      </div>
      <div className="shrink-0 pt-0.5">{children}</div>
    </div>
  )
}
