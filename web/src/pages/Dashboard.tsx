/**
 * 概览仪表盘（P1：真实账目数据）。
 *
 * 与 P0 的区别
 * ------------
 * P0 刻意只显示占位与运行状态，因为当时没有任何账目数据 ——
 * 记账工具最忌讳"看起来有数字、其实是编的"。
 * P1 接入了真实数据，于是这一页回答用户最关心的四个问题：
 *   1. 我现在有多少钱？（净值 / 资产 / 负债）
 *   2. 这个月花了多少、比上月多还是少？（本月收支 + 环比）
 *   3. 钱主要花在哪？（分类占比）
 *   4. 最近记了什么？（最近流水）
 *
 * 口径说明：所有数字都来自 `/api/stats/dashboard`，**前端不做任何聚合**。
 * 转账与余额校准已被后端排除在收支之外 —— 否则"工资卡转支付宝"
 * 会让月支出凭空翻倍，这是记账软件最常见的口径错误。
 */

import { Reorder, motion } from 'framer-motion'
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { CountUp } from '@/components/CountUp'
import { Icon, type IconName } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { staggerContainer, staggerItem } from '@/design/motion'
import { useI18n } from '@/i18n'
import { api, type DashboardData } from '@/lib/api'
import { compactMinor, displayMinor, formatDelta, formatTime, relativeDayLabel } from '@/lib/format'
import { usePreferences } from '@/app/preferences'

import { MoneyText } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

// -----------------------------------------------------------------------------
// KPI 卡片
// -----------------------------------------------------------------------------
interface MetricProps {
  label: string
  icon: IconName
  tone: 'accent' | 'positive' | 'negative' | 'purple'
  /** 已格式化好的文本；当 `minor` 存在时改用 CountUp 呈现原始金额 */
  value: string
  /** 原始金额（最小单位）。提供后数字会滚动过渡，让「变了多少」可见 */
  minor?: number
  delta?: number | null
  hint?: string
}

/**
 * KPI 卡片。
 *
 * 环比为 `null` 时显示 `—` 而不是 0% 或 +100%：
 * "上期是 0，这期是 500"在数学上没有变化率，编一个出来是骗人。
 */
function MetricCard({ label, icon, tone, value, minor, delta, hint }: MetricProps) {
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
          {delta !== undefined ? (
            <span
              className={`ab-tnum ml-auto text-ab-caption font-semibold ${
                delta === null
                  ? 'text-label-3'
                  : delta > 0
                    ? 'text-negative'
                    : delta < 0
                      ? 'text-positive'
                      : 'text-label-3'
              }`}
            >
              {delta === null || delta === undefined ? '—' : formatDelta(delta)}
            </span>
          ) : null}
        </div>
        <div className="ab-metric mt-2.5">
          {minor === undefined ? value : <CountUp value={minor} />}
        </div>
        {hint ? <div className="mt-0.5 text-ab-caption1 text-label-3">{hint}</div> : null}
      </Card>
    </motion.div>
  )
}

export function DashboardPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { accounts, categoryById, refresh: refreshLedger } = useLedger()
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  /**
   * 仪表盘分区顺序。
   *
   * 存在**用户偏好**里而不是 localStorage：偏好已经有一条可靠的读写链路
   * （后端校验 + 原子写入），而 localStorage 会随浏览器缓存被清掉，
   * 且原生窗口与浏览器两种外壳下并不共享。布局这种事丢了会让人恼火。
   *
   * 只允许"重新排序"，不允许删除或新增分区 —— 一个能被拖空的仪表盘
   * 会让用户以为自己把功能弄丢了。
   */
  const DEFAULT_LAYOUT = ['charts', 'lists', 'status']
  const [layout, setLayout] = useState<string[]>(DEFAULT_LAYOUT)

  useEffect(() => {
    const saved = (preferences as unknown as { dashboard_layout?: unknown }).dashboard_layout
    if (Array.isArray(saved) && saved.length === DEFAULT_LAYOUT.length) {
      setLayout(saved.filter((item): item is string => typeof item === 'string'))
    }
    // 只在偏好首次就绪时套用，之后以本地状态为准（否则拖拽会被回写覆盖）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preferences.dashboard_layout])

  const persistLayout = useCallback((next: string[]) => {
    setLayout(next)
    void api.patchPreferences({ dashboard_layout: next } as never)
  }, [])

  const load = useCallback(async () => {
    try {
      setData(await api.dashboard({ trend_days: 30, recent_limit: 8 }))
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((key) => (
            <Skeleton key={key} className="h-24 w-full" />
          ))}
        </div>
        <Skeleton className="h-48 w-full" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <Card flush>
        <EmptyState
          icon="alert"
          title={t('dashboard.loadFailedTitle')}
          body={error ?? t('common.unknown')}
          action={
            <button type="button" className="ab-btn-primary" onClick={() => void load()}>
              <Icon name="refresh" size={14} />
              {t('common.retry')}
            </button>
          }
        />
      </Card>
    )
  }

  const hasData = data.month.transaction_count > 0 || data.net_worth.account_count > 0
  const maxExpense = Math.max(1, ...data.month.top_categories.map((item) => item.amount_minor))
  const trendMax = Math.max(1, ...data.trend.map((item) => Math.max(item.income_minor, item.expense_minor)))

  return (
    <div className="space-y-4">
      {/* KPI 行 */}
      <motion.div
        variants={staggerContainer}
        initial="hidden"
        animate="show"
        className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
      >
        <MetricCard
          label={t('ledger.netWorth')}
          icon="accounts"
          tone="accent"
          value={displayMinor(data.net_worth.net_worth_minor, 'CNY', preferences.privacy_mode)}
          minor={data.net_worth.net_worth_minor}
          hint={t('dashboard.assetsHint', {
            assets: compactMinor(data.net_worth.assets_minor),
            liabilities: compactMinor(data.net_worth.liabilities_minor),
          })}
        />
        <MetricCard
          label={t('dashboard.monthExpense')}
          icon="transactions"
          tone="negative"
          value={displayMinor(data.month.expense_minor, 'CNY', preferences.privacy_mode)}
          minor={data.month.expense_minor}
          delta={data.month.expense_change}
          hint={t('dashboard.monthRecords', { count: data.month.transaction_count })}
        />
        <MetricCard
          label={t('dashboard.monthIncome')}
          icon="salary"
          tone="positive"
          value={displayMinor(data.month.income_minor, 'CNY', preferences.privacy_mode)}
          minor={data.month.income_minor}
          delta={data.month.income_change}
        />
        <MetricCard
          label={t('dashboard.monthBalance')}
          icon="budgets"
          tone="purple"
          value={displayMinor(data.month.net_minor, 'CNY', preferences.privacy_mode)}
          minor={data.month.net_minor}
        />
      </motion.div>

      {!hasData ? (
        <Card flush>
          <EmptyState
            icon="quickAdd"
            title={t('dashboard.emptyTitle')}
            body={t('dashboard.emptyBody')}
            action={
              <Link to="/transactions" className="ab-btn-primary">
                <Icon name="plus" size={14} />
                {t('ledger.quickAdd')}
              </Link>
            }
          />
        </Card>
      ) : null}

      <Reorder.Group axis="y" values={layout} onReorder={persistLayout} className="space-y-4">
      <Reorder.Item value="charts" className="grid gap-4 xl:grid-cols-3">
        {/* 近 30 天趋势 */}
        <Card className="xl:col-span-2" title={t('dashboard.trendTitle')}>
          <div className="flex items-center gap-3 text-ab-caption text-label-3">
            <span className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-negative" />
              {t('transactionType.expense')}
            </span>
            <span className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-positive" />
              {t('transactionType.income')}
            </span>
            <span className="ml-auto">{t('dashboard.trendHint')}</span>
          </div>
          {/* 用 CSS 柱状图而不是图表库：P2 才引入 ECharts，
              在这之前用一个不引依赖、且能真实反映数据的画法更诚实 */}
          <div className="mt-3 flex h-40 items-end gap-[3px]">
            {data.trend.map((day) => {
              const expenseHeight = (day.expense_minor / trendMax) * 100
              const incomeHeight = (day.income_minor / trendMax) * 100
              return (
                <div
                  key={day.date}
                  className="group relative flex h-full flex-1 flex-col justify-end gap-[1px]"
                  title={`${day.date}：${t('transactionType.expense')} ${displayMinor(day.expense_minor, 'CNY')}，${t('transactionType.income')} ${displayMinor(day.income_minor, 'CNY')}`}
                >
                  {day.income_minor > 0 ? (
                    <div className="w-full rounded-t-[2px] bg-positive/70" style={{ height: `${incomeHeight}%` }} />
                  ) : null}
                  {day.expense_minor > 0 ? (
                    <div className="w-full rounded-t-[2px] bg-negative/70" style={{ height: `${expenseHeight}%` }} />
                  ) : null}
                  {/* 未记账的日子留一条极淡的底线，表示"这天没有数据"而不是"这天是 0" */}
                  {day.transaction_count === 0 ? (
                    <div className="h-[2px] w-full rounded-full bg-hairline/15" />
                  ) : null}
                </div>
              )
            })}
          </div>
          <div className="mt-1.5 flex justify-between text-ab-caption2 text-label-3">
            <span>{data.trend[0]?.date.slice(5)}</span>
            <span>{data.trend[data.trend.length - 1]?.date.slice(5)}</span>
          </div>
        </Card>

        {/* 分类占比 */}
        <Card title={t('dashboard.topCategories')}>
          {data.month.top_categories.length === 0 ? (
            <p className="py-6 text-center text-ab-footnote text-label-3">{t('dashboard.noCategoryData')}</p>
          ) : (
            <div className="space-y-2.5">
              {data.month.top_categories.map((item) => {
                const category = categoryById(item.category_id)
                return (
                  <div key={`${item.category_id ?? 'none'}`}>
                    <div className="flex items-center gap-2">
                      <Icon
                        name={category?.icon ?? 'other'}
                        size={13}
                        className="shrink-0"
                        // 图标颜色跟随分类色，与流水列表保持一致
                        {...(category ? { style: { color: `rgb(var(--ab-${category.color}))` } } : {})}
                      />
                      <span className="flex-1 truncate text-ab-footnote text-label-2">
                        {item.category_name}
                      </span>
                      <span className="ab-tnum text-ab-footnote font-semibold text-label">
                        {displayMinor(item.amount_minor, 'CNY', preferences.privacy_mode)}
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-hairline/10">
                      <div
                        className="h-full rounded-full transition-all duration-500"
                        style={{
                          width: `${(item.amount_minor / maxExpense) * 100}%`,
                          backgroundColor: `rgb(var(--ab-${category?.color ?? 'accent'}))`,
                        }}
                      />
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </Card>
      </Reorder.Item>

      <Reorder.Item value="lists" className="grid gap-4 xl:grid-cols-3">
        {/* 最近流水 */}
        <Card
          className="xl:col-span-2"
          title={t('dashboard.recentTitle')}
          action={
            <Link to="/transactions" className="ab-btn-ghost text-ab-footnote">
              {t('dashboard.viewAll')}
              <Icon name="chevronRight" size={13} />
            </Link>
          }
          flush
        >
          {data.recent_transactions.length === 0 ? (
            <p className="px-3 py-6 text-center text-ab-footnote text-label-3">
              {t('dashboard.noRecent')}
            </p>
          ) : (
            data.recent_transactions.map((item) => (
              <div key={item.id} className="ab-row">
                <span
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px]"
                  style={{
                    backgroundColor: `rgb(var(--ab-${item.category_color || 'separator'}) / 0.14)`,
                    color: `rgb(var(--ab-${item.category_color || 'separator'}))`,
                  }}
                >
                  <Icon name={item.category_icon || 'other'} size={15} />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-ab-subhead font-medium text-label">
                    {item.category_name || item.payee || t('ledger.uncategorized')}
                  </div>
                  <div className="truncate text-ab-caption text-label-3">
                    {[relativeDayLabel(item.occurred_at), formatTime(item.occurred_at), item.account_name]
                      .filter(Boolean)
                      .join(' · ')}
                  </div>
                </div>
                <MoneyText
                  minor={
                    item.type === 'expense'
                      ? -item.amount_minor
                      : item.type === 'income'
                        ? item.amount_minor
                        : item.amount_minor
                  }
                  currency={item.currency}
                  tone={item.type === 'income' ? 'income' : item.type === 'expense' ? 'expense' : 'neutral'}
                />
              </div>
            ))
          )}
        </Card>

        {/* 账户快照 */}
        <Card
          title={t('dashboard.accountsTitle')}
          action={
            <Link to="/accounts" className="ab-btn-ghost text-ab-footnote">
              {t('dashboard.manage')}
              <Icon name="chevronRight" size={13} />
            </Link>
          }
          flush
        >
          {accounts.length === 0 ? (
            <p className="px-3 py-6 text-center text-ab-footnote text-label-3">
              {t('ledger.noAccountsTitle')}
            </p>
          ) : (
            accounts.slice(0, 6).map((account) => (
              <div key={account.id} className="ab-row">
                <span
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px]"
                  style={{
                    backgroundColor: `rgb(var(--ab-${account.color}) / 0.14)`,
                    color: `rgb(var(--ab-${account.color}))`,
                  }}
                >
                  <Icon name={account.icon} size={15} />
                </span>
                <span className="flex-1 truncate text-ab-subhead text-label">{account.name}</span>
                <span
                  className={`ab-tnum text-ab-footnote font-semibold ${
                    account.balance_minor < 0 ? 'text-negative' : 'text-label'
                  }`}
                >
                  {displayMinor(account.balance_minor, account.currency, preferences.privacy_mode)}
                </span>
              </div>
            ))
          )}
        </Card>
      </Reorder.Item>

      <Reorder.Item value="status">
        <RunStatusCard onReload={() => void Promise.all([load(), refreshLedger()])} />
      </Reorder.Item>
      </Reorder.Group>
    </div>
  )
}

/**
 * 运行状态卡片。
 *
 * 它同时是**本地服务链路的实时自证**：能显示出来就说明
 * 令牌 → 环回请求 → 后端 → SQLite 整条链路当时是通的。
 * 出问题时这是最省事的排查入口，因此保留在首页。
 */
function RunStatusCard({ onReload }: { onReload: () => void }) {
  const { t } = useI18n()
  const [info, setInfo] = useState<Awaited<ReturnType<typeof api.runtimeInfo>> | null>(null)
  const [integrity, setIntegrity] = useState<Awaited<ReturnType<typeof api.integrity>> | null>(null)

  useEffect(() => {
    void api.runtimeInfo().then(setInfo).catch(() => setInfo(null))
    void api.integrity().then(setIntegrity).catch(() => setIntegrity(null))
  }, [])

  const rows: { label: string; value: string }[] = info
    ? [
        { label: t('dashboard.fieldShell'), value: info.shell },
        { label: t('dashboard.fieldDataDir'), value: info.paths.data_dir },
        { label: t('dashboard.fieldDatabase'), value: info.paths.database },
      ]
    : []

  return (
    <Card
      title={t('dashboard.systemTitle')}
      action={
        <button type="button" className="ab-btn-ghost" onClick={onReload} aria-label={t('common.retry')}>
          <Icon name="refresh" size={14} />
        </button>
      }
    >
      {rows.length === 0 ? (
        <p className="text-ab-footnote text-label-3">{t('common.unknown')}</p>
      ) : (
        <div className="space-y-1.5">
          {rows.map((row) => (
            <div key={row.label} className="flex items-baseline gap-3">
              <span className="w-24 shrink-0 text-ab-caption text-label-3">{row.label}</span>
              <span className="truncate font-mono text-ab-caption text-label-2" title={row.value}>
                {row.value}
              </span>
            </div>
          ))}
          {integrity ? (
            <div className="flex items-center gap-2 pt-1">
              <span className={`h-1.5 w-1.5 rounded-full ${integrity.ok ? 'bg-positive' : 'bg-warning'}`} />
              <span className="text-ab-caption text-label-2">
                {integrity.ok
                  ? t('dashboard.integrityOk', {
                      transactions: integrity.stats.transactions,
                      categories: integrity.stats.categories,
                    })
                  : t('dashboard.integrityIssues', { count: integrity.issues.length })}
              </span>
            </div>
          ) : null}
        </div>
      )}
    </Card>
  )
}
