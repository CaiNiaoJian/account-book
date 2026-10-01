/**
 * 概览仪表盘（P0：基座骨架 + 真实运行状态）。
 *
 * P0 阶段这里**刻意不放假数据**。
 * 记账工具最忌讳的就是"看起来有数字，其实是编的" —— 一旦用户误信，
 * 后果比空白页严重得多。因此：
 *   * KPI 卡片显示 `—` 与「待录入」，说明数据接入在 P1；
 *   * 趋势图明确标注「示意图形」，并带可见水印；
 *   * 唯一显示真实内容的是「运行状态」卡片，它的数据来自后端接口，
 *     同时也是**本地服务链路的实时自证**（能显示出来就说明链路通了）。
 */

import { motion } from 'framer-motion'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'

import { Icon, type IconName } from '@/components/Icon'
import { Sparkline } from '@/components/Sparkline'
import { Card, EmptyState, PhaseBadge, Skeleton } from '@/components/ui'
import { staggerContainer, staggerItem } from '@/design/motion'
import { useI18n } from '@/i18n'
import { api } from '@/lib/api'
import { boot } from '@/lib/boot'
import { formatDuration } from '@/lib/format'
import { useRuntimeInfo } from './useRuntimeInfo'

// -----------------------------------------------------------------------------
// 子组件
// -----------------------------------------------------------------------------
interface MetricCardProps {
  label: string
  icon: IconName
  tone: 'accent' | 'positive' | 'negative' | 'purple'
  pendingLabel: string
}

/**
 * KPI 卡片。P0 值为占位：显示 `—` 与「待录入」，
 * 保证用户不会把示意数据误当成真实账目。
 */
function MetricCard({ label, icon, tone, pendingLabel }: MetricCardProps) {
  return (
    <motion.div variants={staggerItem}>
      <Card dense className="h-full">
        <div className="flex items-center gap-2">
          <span
            className="flex h-6 w-6 items-center justify-center rounded-[8px]"
            style={{ backgroundColor: `rgb(var(--ab-${tone}) / 0.14)`, color: `rgb(var(--ab-${tone}))` }}
          >
            <Icon name={icon} size={14} strokeWidth={1.9} />
          </span>
          <span className="truncate text-ab-footnote text-label-2">{label}</span>
        </div>
        <div className="mt-2.5 flex items-baseline gap-2">
          <span className="ab-tnum text-ab-title1 font-semibold tracking-tight text-label-3">—</span>
          <span className="ab-phase-badge">{pendingLabel}</span>
        </div>
      </Card>
    </motion.div>
  )
}

interface StatusRowProps {
  label: string
  value: string
  mono?: boolean
  onReveal?: () => void
}

function StatusRow({ label, value, mono, onReveal }: StatusRowProps) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-separator/40 py-[7px] last:border-b-0">
      <span className="shrink-0 text-ab-footnote text-label-2">{label}</span>
      <span className="flex min-w-0 items-center gap-1.5">
        <span
          className={[
            'ab-selectable truncate text-right text-ab-footnote text-label',
            mono ? 'ab-tnum font-mono text-ab-caption' : '',
          ]
            .filter(Boolean)
            .join(' ')}
          title={value}
        >
          {value}
        </span>
        {onReveal ? (
          <button
            type="button"
            onClick={onReveal}
            className="shrink-0 text-label-3 transition-colors hover:text-accent"
            aria-label="reveal"
          >
            <Icon name="externalLink" size={13} />
          </button>
        ) : null}
      </span>
    </div>
  )
}

// -----------------------------------------------------------------------------
// 页面
// -----------------------------------------------------------------------------
export function DashboardPage() {
  const { t } = useI18n()
  const { state, reload } = useRuntimeInfo()
  const [revealError, setRevealError] = useState<string | null>(null)

  const hour = new Date().getHours()
  const greeting =
    hour < 12 ? t('dashboard.greetingMorning') : hour < 18 ? t('dashboard.greetingAfternoon') : t('dashboard.greetingEvening')

  const handleReveal = useCallback(() => {
    setRevealError(null)
    api
      .revealDataDir()
      .then((result) => {
        if (result.status !== 'ok') setRevealError(result.detail ?? t('common.unknown'))
      })
      .catch((error: unknown) => setRevealError(error instanceof Error ? error.message : String(error)))
  }, [t])

  return (
    <motion.div variants={staggerContainer} initial="initial" animate="animate" className="space-y-5">
      {/* ---- 欢迎语 ------------------------------------------------------- */}
      <motion.header variants={staggerItem}>
        <div className="flex flex-wrap items-baseline gap-2.5">
          <h2 className="text-ab-title1 text-label">{greeting}</h2>
          <PhaseBadge phase={boot.phase} />
        </div>
        <p className="mt-1 max-w-2xl text-ab-callout text-label-2">{t('dashboard.subtitle')}</p>
      </motion.header>

      {/* ---- KPI 卡片 ----------------------------------------------------- */}
      <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard label={t('dashboard.netWorth')} icon="accounts" tone="accent" pendingLabel={t('dashboard.awaitingData')} />
        <MetricCard label={t('dashboard.monthIncome')} icon="download" tone="positive" pendingLabel={t('dashboard.awaitingData')} />
        <MetricCard label={t('dashboard.monthExpense')} icon="upload" tone="negative" pendingLabel={t('dashboard.awaitingData')} />
        <MetricCard label={t('dashboard.monthBalance')} icon="statistics" tone="purple" pendingLabel={t('dashboard.awaitingData')} />
      </div>

      {/* ---- 趋势（示意）+ 运行状态 --------------------------------------- */}
      <div className="grid grid-cols-1 gap-3.5 lg:grid-cols-3">
        <motion.div variants={staggerItem} className="lg:col-span-2">
          <Card
            title={t('dashboard.trendTitle')}
            subtitle={t('dashboard.placeholderNote', { phase: 'P2' })}
            className="h-full"
          >
            {/* 明确的"示意"水印：避免任何误读为真实账目的可能 */}
            <div className="relative">
              <div className="relative overflow-hidden rounded-ab-md bg-surface-2/70 p-3">
                <span className="pointer-events-none absolute right-3 top-2 select-none text-ab-caption font-semibold uppercase tracking-widest text-label-3/70">
                  DEMO
                </span>
                <Sparkline
                  points={[18, 22, 19, 27, 24, 31, 29, 36, 33, 41, 38, 46, 44, 52, 49, 58]}
                  tone="accent"
                  width={880}
                  height={132}
                  className="h-[132px] w-full"
                  title={t('dashboard.trendTitle')}
                />
                <div className="mt-1 flex justify-between text-ab-caption2 text-label-3">
                  {['—', '—', '—', '—', '—', '—'].map((label, index) => (
                    <span key={index}>{label}</span>
                  ))}
                </div>
              </div>
            </div>
          </Card>
        </motion.div>

        <motion.div variants={staggerItem}>
          <Card
            title={t('dashboard.systemTitle')}
            subtitle={
              state.status === 'ready'
                ? t('dashboard.systemConnected')
                : state.status === 'error'
                  ? t('dashboard.systemDisconnected')
                  : t('common.loading')
            }
            action={
              <button
                type="button"
                onClick={reload}
                className="ab-btn-ghost"
                aria-label={t('common.retry')}
                title={t('common.retry')}
              >
                <Icon name="refresh" size={14} />
              </button>
            }
            className="h-full"
          >
            {state.status === 'loading' ? (
              <div className="space-y-2">
                {[0, 1, 2, 3, 4].map((index) => (
                  <Skeleton key={index} className="h-6 w-full" />
                ))}
              </div>
            ) : state.status === 'error' ? (
              <EmptyState
                icon="alert"
                title={t('dashboard.systemDisconnected')}
                body={state.message}
                action={
                  <button type="button" className="ab-btn-secondary" onClick={reload}>
                    {t('common.retry')}
                  </button>
                }
              />
            ) : (
              <div>
                <StatusRow label={t('dashboard.fieldVersion')} value={`${state.info.version} · ${state.info.phase}`} />
                <StatusRow label={t('dashboard.fieldPython')} value={state.info.python_version} />
                <StatusRow label={t('dashboard.fieldPlatform')} value={state.info.platform} />
                <StatusRow label={t('dashboard.fieldUptime')} value={formatDuration(state.info.uptime_seconds)} />
                <StatusRow label={t('dashboard.fieldPort')} value={String(state.info.port)} mono />
                <StatusRow
                  label={t('dashboard.fieldShell')}
                  value={
                    state.info.shell === 'pywebview'
                      ? t('dashboard.shellNative')
                      : t('dashboard.shellBrowser')
                  }
                />
                <StatusRow
                  label={t('dashboard.fieldMode')}
                  value={state.info.paths.portable ? t('dashboard.modePortable') : t('dashboard.modeInstalled')}
                />
                <StatusRow
                  label={t('dashboard.fieldDataDir')}
                  value={state.info.paths.data_dir}
                  mono
                  onReveal={handleReveal}
                />
                <StatusRow label={t('dashboard.fieldLogFile')} value={state.info.paths.log_file} mono />

                {/* 数据目录未能使用首选位置时，必须让用户看见"放在了哪里、为什么"。
                    默默换个地方存账本会让人以为数据丢了。 */}
                {state.info.paths.degraded ? (
                  <div className="mt-2.5 rounded-ab-sm bg-warning/12 p-2 text-ab-caption text-label">
                    <p className="flex items-start gap-1.5">
                      <Icon name="alert" size={13} className="mt-[1px] shrink-0 text-warning" />
                      {t('dashboard.degradedWarning')}
                    </p>
                    <p className="mt-1.5 pl-5 text-ab-caption2 text-label-2">
                      {t('dashboard.fieldDataDirSource')}：{state.info.paths.data_dir_source || '—'}
                    </p>
                    {state.info.paths.data_dir_attempts.length > 0 ? (
                      <details className="mt-1 pl-5">
                        <summary className="cursor-pointer text-ab-caption2 text-label-2">
                          {t('dashboard.degradedAttempts')}（{state.info.paths.data_dir_attempts.length}）
                        </summary>
                        <ul className="ab-selectable mt-1 space-y-0.5 font-mono text-ab-caption2 text-label-3">
                          {state.info.paths.data_dir_attempts.map((attempt) => (
                            <li key={attempt} className="break-all">
                              · {attempt}
                            </li>
                          ))}
                        </ul>
                      </details>
                    ) : null}
                  </div>
                ) : null}

                {state.info.shell === 'browser' ? (
                  <p className="mt-2.5 flex items-start gap-1.5 rounded-ab-sm bg-warning/12 p-2 text-ab-caption text-label">
                    <Icon name="alert" size={13} className="mt-[1px] shrink-0 text-warning" />
                    {t('dashboard.browserFallbackWarning')}
                  </p>
                ) : null}
                {state.info.is_admin ? (
                  <p className="mt-2.5 flex items-start gap-1.5 rounded-ab-sm bg-warning/12 p-2 text-ab-caption text-label">
                    <Icon name="alert" size={13} className="mt-[1px] shrink-0 text-warning" />
                    {t('dashboard.adminWarning')}
                  </p>
                ) : null}
                {revealError ? (
                  <p className="mt-2.5 text-ab-caption text-negative">
                    {t('dashboard.revealFailed', { message: revealError })}
                  </p>
                ) : null}
              </div>
            )}
          </Card>
        </motion.div>
      </div>

      {/* ---- 空状态 ------------------------------------------------------- */}
      <motion.div variants={staggerItem}>
        <Card flush>
          <EmptyState
            icon="ledger"
            title={t('dashboard.emptyTitle')}
            body={t('dashboard.emptyBody')}
            action={
              <Link to="/about" className="ab-btn-primary">
                <Icon name="about" size={14} />
                {t('dashboard.emptyAction')}
              </Link>
            }
          />
        </Card>
      </motion.div>
    </motion.div>
  )
}
