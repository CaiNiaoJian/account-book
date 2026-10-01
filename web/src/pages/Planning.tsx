/**
 * 规划三页：预算 / 周期记账 / 债务与应收应付（P1 收尾）。
 *
 * 放在同一个文件里是因为三者共享同一套"表单弹窗 + 金额输入 + 空状态"的结构，
 * 拆成三个文件只会让这三套东西各写一遍、然后慢慢长歪。
 *
 * 贯穿三页的两条规则
 * ------------------
 * 1. **进度不夹到 100%**。预算用掉 130%、债务多还了 200 元，
 *    都要如实显示 —— 夹取会把"出问题了"这个信息抹掉。
 * 2. **数据为零时给可行动的空状态**，而不是空白页：
 *    告诉用户"设一个预算之后这里会显示什么"。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { Icon } from '@/components/Icon'
import { Chart } from '@/components/Chart'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { useI18n } from '@/i18n'
import {
  api,
  type BudgetOverview,
  type BudgetStatus,
  type DebtOverview,
  type DebtStatus,
  type PostDueReport,
  type RecurringRule,
  type UpcomingRule,
} from '@/lib/api'
import { displayMinor, formatDayLabel, localDayKey } from '@/lib/format'
import { resolveToken } from '@/design/tokens'
import { usePreferences } from '@/app/preferences'

import { AmountField, CategoryPicker, Modal, MoneyText } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

// =============================================================================
// 预算
// =============================================================================
export function BudgetsPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()

  const [data, setData] = useState<BudgetOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<BudgetStatus | null>(null)
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)

  const [form, setForm] = useState({
    name: '',
    scope: 'total' as 'total' | 'category',
    category_id: null as number | null,
    period: 'monthly' as string,
    amount_minor: 0,
    rollover: false,
    alert_threshold: 0.8,
  })

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await api.budgets())
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

  const openCreate = () => {
    setForm({
      name: '',
      scope: 'total',
      category_id: null,
      period: 'monthly',
      amount_minor: 0,
      rollover: false,
      alert_threshold: 0.8,
    })
    setCreating(true)
    setEditing(null)
  }

  const openEdit = (item: BudgetStatus) => {
    setForm({
      name: item.name,
      scope: item.scope,
      category_id: item.category_id,
      period: item.period,
      amount_minor: item.amount_minor,
      rollover: false,
      alert_threshold: item.alert_threshold,
    })
    setEditing(item)
    setCreating(false)
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {
        name: form.name.trim() || t('budgets.untitled'),
        scope: form.scope,
        period: form.period,
        amount_minor: form.amount_minor,
        rollover: form.rollover,
        alert_threshold: form.alert_threshold,
      }
      if (form.scope === 'category') payload.category_id = form.category_id
      if (editing) await api.updateBudget(editing.id, payload)
      else await api.createBudget(payload)
      await load()
      setCreating(false)
      setEditing(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-48 w-full" />
      </div>
    )
  }

  const total = data?.total ?? null

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('nav.budgets')}</h1>
        {data?.alerts.length ? (
          <span className="ab-phase-badge !text-warning">
            <Icon name="warning" size={11} />
            {t('budgets.alertCount', { count: data.alerts.length })}
          </span>
        ) : null}
        <button type="button" className="ab-btn-primary ml-auto" onClick={openCreate}>
          <Icon name="plus" size={14} />
          {t('budgets.new')}
        </button>
      </div>

      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
      ) : null}

      {!data?.has_budget ? (
        <Card flush>
          <EmptyState
            icon="budgets"
            title={t('budgets.emptyTitle')}
            body={t('budgets.emptyBody')}
            action={
              <button type="button" className="ab-btn-primary" onClick={openCreate}>
                <Icon name="plus" size={14} />
                {t('budgets.new')}
              </button>
            }
          />
        </Card>
      ) : (
        <>
          {total ? (
            <Card title={t('budgets.totalTitle')}>
              <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
                <BudgetRing item={total} />
                <BudgetGauge item={total} />
              </div>
              <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Stat label={t('budgets.available')} value={displayMinor(total.available_minor, 'CNY', preferences.privacy_mode)} />
                <Stat
                  label={t('budgets.spent')}
                  value={displayMinor(total.spent_minor, 'CNY', preferences.privacy_mode)}
                  tone="negative"
                />
                <Stat
                  label={t('budgets.remaining')}
                  value={displayMinor(total.remaining_minor, 'CNY', preferences.privacy_mode)}
                  tone={total.over ? 'negative' : 'positive'}
                />
                <Stat
                  label={t('budgets.dailyAllowance')}
                  value={displayMinor(total.daily_allowance_minor, 'CNY', preferences.privacy_mode)}
                  hint={t('budgets.daysLeft', { days: total.days_left })}
                />
              </div>
              {total.carryover_minor > 0 ? (
                <p className="mt-2 text-ab-caption1 text-label-3">
                  {t('budgets.carryoverNote', {
                    amount: displayMinor(total.carryover_minor, 'CNY', preferences.privacy_mode),
                  })}
                </p>
              ) : null}
            </Card>
          ) : null}

          {data.category_budgets.length > 0 ? (
            <Card title={t('budgets.categoryTitle')} flush>
              {data.category_budgets.map((item) => (
                <CategoryBudgetRow key={item.id} item={item} onEdit={() => openEdit(item)} />
              ))}
            </Card>
          ) : null}

          {data.items.length > 1 ? (
            <p className="text-ab-caption1 text-label-3">{t('budgets.independentNote')}</p>
          ) : null}
        </>
      )}

      <Modal
        open={creating || editing !== null}
        title={editing ? t('budgets.editTitle') : t('budgets.newTitle')}
        onClose={() => {
          setCreating(false)
          setEditing(null)
        }}
        footer={
          <>
            {editing ? (
              <button
                type="button"
                className="ab-btn-secondary !text-negative"
                onClick={() => {
                  void api.deleteBudget(editing.id).then(() => {
                    setEditing(null)
                    return load()
                  })
                }}
              >
                <Icon name="trash" size={13} />
                {t('common.delete')}
              </button>
            ) : null}
            <span className="flex-1" />
            <button
              type="button"
              className="ab-btn-secondary"
              onClick={() => {
                setCreating(false)
                setEditing(null)
              }}
            >
              {t('common.cancel')}
            </button>
            <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void submit()}>
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="space-y-3.5">
          <div>
            <label className="ab-field-label" htmlFor="budget-name">
              {t('budgets.name')}
            </label>
            <input
              id="budget-name"
              className="ab-input"
              autoFocus
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder={t('budgets.namePlaceholder')}
            />
          </div>

          <div>
            <span className="ab-field-label">{t('budgets.scope')}</span>
            <div className="ab-segment">
              <button
                type="button"
                data-active={form.scope === 'total'}
                onClick={() => setForm({ ...form, scope: 'total', category_id: null })}
              >
                {t('budgets.scopeTotal')}
              </button>
              <button type="button" data-active={form.scope === 'category'} onClick={() => setForm({ ...form, scope: 'category' })}>
                {t('budgets.scopeCategory')}
              </button>
            </div>
          </div>

          {form.scope === 'category' ? (
            <div>
              <span className="ab-field-label">{t('ledger.category')}</span>
              <CategoryPicker
                kind="expense"
                value={form.category_id}
                onChange={(value) => setForm({ ...form, category_id: value })}
              />
            </div>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="budget-period">
                {t('budgets.period')}
              </label>
              <select
                id="budget-period"
                className="ab-select"
                value={form.period}
                onChange={(event) => setForm({ ...form, period: event.target.value })}
              >
                {(['weekly', 'monthly', 'quarterly', 'yearly'] as const).map((item) => (
                  <option key={item} value={item}>
                    {t(`budgets.periodName.${item}`)}
                  </option>
                ))}
              </select>
            </div>
            <label className="block">
              <span className="ab-field-label">{t('budgets.amount')}</span>
              <AmountField
                value={form.amount_minor}
                onChange={(value) => setForm({ ...form, amount_minor: value ?? 0 })}
              />
            </label>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="budget-alert">
                {t('budgets.alertThreshold')}
              </label>
              <input
                id="budget-alert"
                type="range"
                min={0.3}
                max={1.2}
                step={0.05}
                className="w-full"
                value={form.alert_threshold}
                onChange={(event) => setForm({ ...form, alert_threshold: Number(event.target.value) })}
              />
              <span className="ab-tnum text-ab-caption text-label-3">
                {t('budgets.alertAt', { percent: Math.round(form.alert_threshold * 100) })}
              </span>
            </div>
            <label className="flex items-end gap-2 pb-1 text-ab-footnote text-label-2">
              <input
                type="checkbox"
                checked={form.rollover}
                onChange={(event) => setForm({ ...form, rollover: event.target.checked })}
              />
              <span>
                {t('budgets.rollover')}
                <span className="block text-ab-caption1 text-label-3">{t('budgets.rolloverHint')}</span>
              </span>
            </label>
          </div>
        </div>
      </Modal>
    </div>
  )
}

/**
 * 预算进度环。
 *
 * 用环而不是条：环的中心可以放"还剩多少"这个最该被看到的数字，
 * 而条的中间放数字会被条形本身穿过。
 */
function BudgetRing({ item }: { item: BudgetStatus }) {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const ratio = Math.min(1, Math.max(0, item.ratio))
  const radius = 52
  const circumference = 2 * Math.PI * radius
  const color = item.over ? 'negative' : item.alert ? 'warning' : 'accent'

  return (
    <div className="flex items-center gap-5">
      <div className="relative h-32 w-32 shrink-0">
        <svg viewBox="0 0 120 120" className="h-full w-full -rotate-90">
          <circle cx="60" cy="60" r={radius} fill="none" strokeWidth="10" stroke="rgb(var(--ab-hairline) / 0.4)" />
          <circle
            cx="60"
            cy="60"
            r={radius}
            fill="none"
            strokeWidth="10"
            strokeLinecap="round"
            stroke={`rgb(var(--ab-${color}))`}
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - ratio)}
            style={{ transition: 'stroke-dashoffset 0.6s cubic-bezier(0.32,0.72,0,1)' }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="ab-tnum text-ab-title3 font-semibold text-label">
            {(item.ratio * 100).toFixed(0)}%
          </span>
          <span className="text-ab-caption2 text-label-3">{t('budgets.used')}</span>
        </div>
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-ab-callout font-semibold text-label">{item.name}</div>
        <div className="mt-0.5 text-ab-footnote text-label-3">
          {formatDayLabel(item.start)} – {formatDayLabel(item.end)}
        </div>
        <div className="ab-tnum mt-1 text-ab-subhead text-label-2">
          {t('budgets.remainingValue', {
            amount: displayMinor(item.remaining_minor, item.currency, preferences.privacy_mode),
          })}
        </div>
        {item.over ? (
          <div className="mt-1 text-ab-footnote text-negative">
            {t('budgets.overBy', {
              amount: displayMinor(-item.remaining_minor, item.currency, preferences.privacy_mode),
            })}
          </div>
        ) : null}
      </div>
    </div>
  )
}

/**
 * 预算仪表盘。
 *
 * 与旁边的进度环**不是重复**：环回答"用了多少"，仪表盘画出刻度与
 * 提醒阈值，回答"离那条线还有多远" —— 后者才是预算真正要盯的东西。
 *
 * 指针夹在 0–130%：超支再多，指针也停在最右侧并变红，
 * 因为再往后增长已经不再提供新信息（数字本身在旁边写着）。
 */
function BudgetGauge({ item }: { item: BudgetStatus }) {
  const { t } = useI18n()
  const ratio = Math.max(0, Math.min(130, item.ratio * 100))
  const threshold = Math.min(130, item.alert_threshold * 100)

  const option = useMemo(() => {
    const over = item.over
    const alert = item.alert
    return {
      series: [
        {
          type: 'gauge',
          min: 0,
          max: 130,
          startAngle: 200,
          endAngle: -20,
          radius: '96%',
          center: ['50%', '62%'],
          splitNumber: 0,
          axisLine: {
            lineStyle: {
              width: 12,
              // 三段色带直接表达"安全 / 接近上限 / 超支"
              color: [
                [threshold / 130, resolveToken('accent')],
                [1, resolveToken(over ? 'negative' : 'warning')],
              ],
            },
          },
          pointer: {
            width: 4,
            length: '62%',
            itemStyle: { color: resolveToken(over ? 'negative' : alert ? 'warning' : 'accent') },
          },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          detail: {
            fontSize: 15,
            fontWeight: 600,
            color: resolveToken('text'),
            offsetCenter: [0, '38%'],
            formatter: () => `${(item.ratio * 100).toFixed(0)}%`,
          },
          title: {
            fontSize: 11,
            color: resolveToken('text-3'),
            offsetCenter: [0, '68%'],
          },
          data: [{ value: ratio, name: t('stats.budgetGauge') }],
        },
      ],
    }
  }, [item, ratio, threshold, t])

  return (
    <div className="min-w-0 flex-1">
      <Chart option={option} height={150} />
      <p className="text-ab-caption1 text-label-3">
        {t('budgets.gaugeHint', { percent: Math.round(item.alert_threshold * 100) })}
      </p>
    </div>
  )
}

function CategoryBudgetRow({ item, onEdit }: { item: BudgetStatus; onEdit: () => void }) {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { categoryById } = useLedger()
  const category = item.category_id ? categoryById(item.category_id) : undefined
  const ratio = Math.min(1, Math.max(0, item.ratio))

  return (
    <button type="button" onClick={onEdit} className="ab-row w-full text-left">
      <Icon
        name={category?.icon ?? 'other'}
        size={14}
        className="shrink-0"
        {...(category ? { style: { color: `rgb(var(--ab-${category.color}))` } } : {})}
      />
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-2">
          <span className="truncate text-ab-subhead text-label">{item.category_name || item.name}</span>
          {item.alert ? (
            <span className="shrink-0 text-ab-caption1 text-warning">
              {t('budgets.nearLimit', { percent: Math.round(item.ratio * 100) })}
            </span>
          ) : null}
        </span>
        <span className="mt-1 block h-1.5 overflow-hidden rounded-full bg-hairline/15">
          <span
            className="block h-full rounded-full transition-all duration-500"
            style={{
              width: `${ratio * 100}%`,
              backgroundColor: `rgb(var(--ab-${item.over ? 'negative' : item.alert ? 'warning' : (category?.color ?? 'accent')}))`,
            }}
          />
        </span>
      </span>
      <span className="ab-tnum shrink-0 text-right">
        <span className="block text-ab-subhead font-medium text-label">
          {displayMinor(item.spent_minor, item.currency, preferences.privacy_mode)}
        </span>
        <span className="block text-ab-caption1 text-label-3">
          / {displayMinor(item.available_minor, item.currency, preferences.privacy_mode)}
        </span>
      </span>
    </button>
  )
}

// =============================================================================
// 周期记账
// =============================================================================
export function RecurringPage() {
  const { t } = useI18n()
  const { accounts } = useLedger()

  const [rules, setRules] = useState<RecurringRule[]>([])
  const [upcoming, setUpcoming] = useState<UpcomingRule[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<PostDueReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<RecurringRule | null>(null)

  const [form, setForm] = useState({
    name: '',
    type: 'expense' as 'expense' | 'income' | 'transfer',
    account_id: null as number | null,
    to_account_id: null as number | null,
    category_id: null as number | null,
    amount_minor: 0,
    frequency: 'monthly' as string,
    interval: 1,
    by_month_day: null as number | null,
    start_date: localDayKey(),
    end_date: null as string | null,
    auto_post: true,
    payee: '',
    note: '',
  })

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [list, soon] = await Promise.all([api.recurringRules(), api.recurringUpcoming(30)])
      setRules(list)
      setUpcoming(soon.items)
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

  const openCreate = () => {
    setForm({
      name: '',
      type: 'expense',
      account_id: accounts[0]?.id ?? null,
      to_account_id: null,
      category_id: null,
      amount_minor: 0,
      frequency: 'monthly',
      interval: 1,
      by_month_day: null,
      start_date: localDayKey(),
      end_date: null,
      auto_post: true,
      payee: '',
      note: '',
    })
    setCreating(true)
    setEditing(null)
  }

  const openEdit = (rule: RecurringRule) => {
    setForm({
      name: rule.name,
      type: rule.type,
      account_id: rule.account_id,
      to_account_id: rule.to_account_id,
      category_id: rule.category_id,
      amount_minor: rule.amount_minor,
      frequency: rule.frequency,
      interval: rule.interval,
      by_month_day: rule.by_month_day,
      start_date: rule.start_date,
      end_date: rule.end_date,
      auto_post: rule.auto_post,
      payee: rule.payee,
      note: rule.note,
    })
    setEditing(rule)
    setCreating(false)
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {
        name: form.name.trim() || t('recurring.untitled'),
        type: form.type,
        account_id: form.account_id,
        category_id: form.type === 'transfer' ? null : form.category_id,
        to_account_id: form.type === 'transfer' ? form.to_account_id : null,
        amount_minor: form.amount_minor,
        frequency: form.frequency,
        interval: form.interval,
        by_month_day: form.by_month_day,
        start_date: form.start_date,
        end_date: form.end_date,
        auto_post: form.auto_post,
        payee: form.payee,
        note: form.note,
      }
      if (editing) await api.updateRecurringRule(editing.id, payload)
      else await api.createRecurringRule(payload)
      await load()
      setCreating(false)
      setEditing(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  /** 先生成预览（dry-run），让用户看到后果再确认 —— 尤其是补记多期时 */
  const preview = async (ruleId?: number) => {
    setBusy(true)
    try {
      setReport(await api.postRecurringDue({ ruleId, dryRun: true }))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const confirmPost = async () => {
    setBusy(true)
    try {
      await api.postRecurringDue({ ruleId: report?.created[0]?.rule_id, dryRun: false })
      setReport(null)
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  const overdue = upcoming.filter((item) => item.overdue)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('nav.recurring')}</h1>
        {overdue.length > 0 ? (
          <span className="ab-phase-badge !text-warning">
            <Icon name="warning" size={11} />
            {t('recurring.overdueCount', { count: overdue.length })}
          </span>
        ) : null}
        <button
          type="button"
          className="ab-btn-secondary ml-auto"
          disabled={busy}
          onClick={() => void preview()}
        >
          <Icon name="refresh" size={14} />
          {t('recurring.postDue')}
        </button>
        <button type="button" className="ab-btn-primary" onClick={openCreate}>
          <Icon name="plus" size={14} />
          {t('recurring.new')}
        </button>
      </div>

      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
      ) : null}

      {upcoming.length > 0 ? (
        <Card title={t('recurring.upcomingTitle')} flush>
          {upcoming.map((item) => (
            <div key={`${item.rule_id}-${item.due_date}`} className="ab-row">
              <span
                className={`ab-tnum w-20 shrink-0 text-ab-footnote ${
                  item.overdue ? 'font-semibold text-warning' : 'text-label-3'
                }`}
              >
                {formatDayLabel(item.due_date)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-ab-subhead text-label">{item.name}</span>
                <span className="block text-ab-caption1 text-label-3">
                  {item.overdue
                    ? t('recurring.overdueBy', { days: Math.abs(item.days_until) })
                    : t('recurring.dueIn', { days: item.days_until })}
                  {item.auto_post ? ` · ${t('recurring.modeAuto')}` : ` · ${t('recurring.modeManual')}`}
                </span>
              </span>
              <MoneyText
                minor={item.type === 'expense' ? -item.amount_minor : item.amount_minor}
                currency={item.currency}
                tone={item.type === 'income' ? 'income' : item.type === 'expense' ? 'expense' : 'neutral'}
              />
              {!item.auto_post ? (
                <button
                  type="button"
                  className="ab-chip shrink-0"
                  disabled={busy}
                  onClick={() => void preview(item.rule_id)}
                >
                  {t('recurring.confirmPost')}
                </button>
              ) : null}
            </div>
          ))}
        </Card>
      ) : null}

      {rules.length === 0 ? (
        <Card flush>
          <EmptyState
            icon="recurring"
            title={t('recurring.emptyTitle')}
            body={t('recurring.emptyBody')}
            action={
              <button type="button" className="ab-btn-primary" onClick={openCreate}>
                <Icon name="plus" size={14} />
                {t('recurring.new')}
              </button>
            }
          />
        </Card>
      ) : (
        <Card title={t('recurring.rulesTitle')} flush>
          {rules.map((rule) => (
            <div key={rule.id} className="ab-row group">
              <button
                type="button"
                className="ab-icon-btn shrink-0"
                aria-label={rule.enabled ? t('recurring.disable') : t('recurring.enable')}
                onClick={() => {
                  void api.updateRecurringRule(rule.id, { enabled: !rule.enabled }).then(() => load())
                }}
              >
                <Icon name={rule.enabled ? 'check' : 'close'} size={13} className={rule.enabled ? 'text-accent' : 'text-label-3'} />
              </button>
              <button type="button" onClick={() => openEdit(rule)} className="min-w-0 flex-1 text-left">
                <span className={`block truncate text-ab-subhead ${rule.enabled ? 'text-label' : 'text-label-3 line-through'}`}>
                  {rule.name}
                </span>
                <span className="block text-ab-caption1 text-label-3">
                  {t(`recurring.freq.${rule.frequency}`, { interval: rule.interval })}
                  {rule.next_due_date ? ` · ${t('recurring.next', { date: formatDayLabel(rule.next_due_date) })}` : ` · ${t('recurring.finished')}`}
                  {rule.auto_post ? ` · ${t('recurring.modeAuto')}` : ` · ${t('recurring.modeManual')}`}
                </span>
              </button>
              <MoneyText
                minor={rule.type === 'expense' ? -rule.amount_minor : rule.amount_minor}
                currency={rule.currency}
                tone={rule.type === 'income' ? 'income' : rule.type === 'expense' ? 'expense' : 'neutral'}
              />
            </div>
          ))}
        </Card>
      )}

      {/* 生成预览：先看后果再落库 */}
      <Modal
        open={report !== null}
        title={t('recurring.previewTitle')}
        onClose={() => setReport(null)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setReport(null)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="ab-btn-primary"
              disabled={busy || !report?.created.length}
              onClick={() => void confirmPost()}
            >
              {t('recurring.confirmGenerate', { count: report?.created.length ?? 0 })}
            </button>
          </>
        }
      >
        {report?.created.length ? (
          <div className="max-h-72 space-y-1 overflow-y-auto">
            {report.created.map((item) => (
              <div key={`${item.rule_id}-${item.date}`} className="flex items-center gap-2 text-ab-footnote">
                <span className="ab-tnum w-20 text-label-3">{formatDayLabel(item.date)}</span>
                <span className="flex-1 truncate text-label-2">{item.name}</span>
                <MoneyText
                  minor={item.type === 'expense' ? -item.amount_minor : item.amount_minor}
                  currency="CNY"
                  tone={item.type === 'income' ? 'income' : 'expense'}
                />
              </div>
            ))}
          </div>
        ) : (
          <p className="py-4 text-center text-ab-footnote text-label-3">{t('recurring.nothingToPost')}</p>
        )}
        {report?.skipped.length ? (
          <div className="mt-3 space-y-1 border-t border-separator/50 pt-2">
            {report.skipped.map((item) => (
              <p key={`${item.rule_id}-${item.reason}`} className="text-ab-caption1 text-label-3">
                {item.name}：{t(`recurring.skip.${item.reason}`, { limit: item.limit ?? 0 })}
              </p>
            ))}
          </div>
        ) : null}
      </Modal>

      {/* 新建 / 编辑 */}
      <Modal
        open={creating || editing !== null}
        title={editing ? t('recurring.editTitle') : t('recurring.newTitle')}
        onClose={() => {
          setCreating(false)
          setEditing(null)
        }}
        footer={
          <>
            {editing ? (
              <button
                type="button"
                className="ab-btn-secondary !text-negative"
                onClick={() => {
                  void api.deleteRecurringRule(editing.id).then(() => {
                    setEditing(null)
                    return load()
                  })
                }}
              >
                <Icon name="trash" size={13} />
                {t('common.delete')}
              </button>
            ) : null}
            <span className="flex-1" />
            <button
              type="button"
              className="ab-btn-secondary"
              onClick={() => {
                setCreating(false)
                setEditing(null)
              }}
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="ab-btn-primary"
              disabled={busy || form.account_id === null}
              onClick={() => void submit()}
            >
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="space-y-3.5">
          <div>
            <label className="ab-field-label" htmlFor="rule-name">
              {t('recurring.name')}
            </label>
            <input
              id="rule-name"
              className="ab-input"
              autoFocus
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder={t('recurring.namePlaceholder')}
            />
          </div>

          <div>
            <span className="ab-field-label">{t('ledger.type')}</span>
            <div className="ab-segment">
              {(['expense', 'income', 'transfer'] as const).map((item) => (
                <button key={item} type="button" data-active={form.type === item} onClick={() => setForm({ ...form, type: item })}>
                  {t(`transactionType.${item}`)}
                </button>
              ))}
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="rule-account">
                {form.type === 'transfer' ? t('ledger.fromAccount') : t('ledger.account')}
              </label>
              <select
                id="rule-account"
                className="ab-select"
                value={form.account_id ?? ''}
                onChange={(event) => setForm({ ...form, account_id: Number(event.target.value) })}
              >
                {accounts.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </div>
            {form.type === 'transfer' ? (
              <div>
                <label className="ab-field-label" htmlFor="rule-to">
                  {t('ledger.toAccount')}
                </label>
                <select
                  id="rule-to"
                  className="ab-select"
                  value={form.to_account_id ?? ''}
                  onChange={(event) => setForm({ ...form, to_account_id: Number(event.target.value) })}
                >
                  <option value="">{t('ledger.pleaseSelect')}</option>
                  {accounts.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : (
              <label className="block">
                <span className="ab-field-label">{t('ledger.amount')}</span>
                <AmountField
                  value={form.amount_minor}
                  onChange={(value) => setForm({ ...form, amount_minor: value ?? 0 })}
                />
              </label>
            )}
          </div>

          {form.type === 'transfer' ? (
            <label className="block">
              <span className="ab-field-label">{t('ledger.amount')}</span>
              <AmountField
                value={form.amount_minor}
                onChange={(value) => setForm({ ...form, amount_minor: value ?? 0 })}
              />
            </label>
          ) : (
            <div>
              <span className="ab-field-label">{t('ledger.category')}</span>
              <CategoryPicker
                kind={form.type === 'income' ? 'income' : 'expense'}
                value={form.category_id}
                onChange={(value) => setForm({ ...form, category_id: value })}
              />
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <label className="ab-field-label" htmlFor="rule-freq">
                {t('recurring.frequency')}
              </label>
              <select
                id="rule-freq"
                className="ab-select"
                value={form.frequency}
                onChange={(event) => setForm({ ...form, frequency: event.target.value })}
              >
                {(['daily', 'weekly', 'monthly', 'yearly'] as const).map((item) => (
                  <option key={item} value={item}>
                    {t(`recurring.freqName.${item}`)}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="ab-field-label" htmlFor="rule-interval">
                {t('recurring.interval')}
              </label>
              <input
                id="rule-interval"
                type="number"
                min={1}
                className="ab-input ab-tnum"
                value={form.interval}
                onChange={(event) => setForm({ ...form, interval: Math.max(1, Number(event.target.value)) })}
              />
            </div>
            {form.frequency === 'monthly' ? (
              <div>
                <label className="ab-field-label" htmlFor="rule-day">
                  {t('recurring.byMonthDay')}
                </label>
                <select
                  id="rule-day"
                  className="ab-select"
                  value={form.by_month_day ?? ''}
                  onChange={(event) =>
                    setForm({ ...form, by_month_day: event.target.value === '' ? null : Number(event.target.value) })
                  }
                >
                  <option value="">{t('recurring.useStartDay')}</option>
                  <option value={-1}>{t('recurring.monthEnd')}</option>
                  {Array.from({ length: 31 }, (_, index) => index + 1).map((day) => (
                    <option key={day} value={day}>
                      {day}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="rule-start">
                {t('recurring.startDate')}
              </label>
              <input
                id="rule-start"
                type="date"
                className="ab-input ab-tnum"
                value={form.start_date}
                onChange={(event) => setForm({ ...form, start_date: localDayKey(event.target.value) })}
              />
            </div>
            <label className="flex items-end gap-2 pb-1 text-ab-footnote text-label-2">
              <input
                type="checkbox"
                checked={form.auto_post}
                onChange={(event) => setForm({ ...form, auto_post: event.target.checked })}
              />
              <span>
                {t('recurring.autoPost')}
                <span className="block text-ab-caption1 text-label-3">{t('recurring.autoPostHint')}</span>
              </span>
            </label>
          </div>

          <div>
            <label className="ab-field-label" htmlFor="rule-note">
              {t('ledger.note')}
            </label>
            <input
              id="rule-note"
              className="ab-input"
              value={form.note}
              onChange={(event) => setForm({ ...form, note: event.target.value })}
            />
          </div>
        </div>
      </Modal>
    </div>
  )
}

// =============================================================================
// 债务与应收应付
// =============================================================================
export function DebtsPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()

  const [data, setData] = useState<DebtOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [paying, setPaying] = useState<DebtStatus | null>(null)
  const [busy, setBusy] = useState(false)

  const [form, setForm] = useState({
    name: '',
    kind: 'lend' as 'lend' | 'borrow',
    counterparty: '',
    principal_minor: 0,
    start_date: localDayKey(),
    due_date: null as string | null,
    annual_rate_bps: 0,
    create_mirror_account: true,
    note: '',
  })
  const [payment, setPayment] = useState({ amount_minor: 0, interest_minor: 0, occurred_at: localDayKey() })

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await api.debts())
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

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.createDebt({
        name: form.name.trim() || t('debts.untitled'),
        kind: form.kind,
        counterparty: form.counterparty,
        principal_minor: form.principal_minor,
        start_date: form.start_date,
        due_date: form.due_date,
        annual_rate_bps: form.annual_rate_bps,
        create_mirror_account: form.create_mirror_account,
        note: form.note,
      })
      await load()
      setCreating(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const submitPayment = async () => {
    if (!paying) return
    setBusy(true)
    setError(null)
    try {
      // 利息单列，其余算本金。界面上必须让用户能填利息 ——
      // 混在一起就再也算不出"还剩多少本金"
      const interest = payment.interest_minor
      await api.addDebtPayment(paying.id, {
        amount_minor: payment.amount_minor,
        principal_minor: Math.max(0, payment.amount_minor - interest),
        interest_minor: interest,
        occurred_at: `${payment.occurred_at}T12:00:00`,
      })
      await load()
      setPaying(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3">
          {[0, 1, 2].map((key) => (
            <Skeleton key={key} className="h-20 w-full" />
          ))}
        </div>
        <Skeleton className="h-56 w-full" />
      </div>
    )
  }

  const summary = data?.summary

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('nav.debts')}</h1>
        {summary && summary.overdue_count > 0 ? (
          <span className="ab-phase-badge !text-warning">
            <Icon name="warning" size={11} />
            {t('debts.overdueCount', { count: summary.overdue_count })}
          </span>
        ) : null}
        <button type="button" className="ab-btn-primary ml-auto" onClick={() => setCreating(true)}>
          <Icon name="plus" size={14} />
          {t('debts.new')}
        </button>
      </div>

      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-3">
        <Card dense>
          <div className="ab-section-label !px-0 !pt-0">{t('debts.receivable')}</div>
          <div className="ab-tnum text-ab-title2 font-semibold tracking-tight text-positive">
            {displayMinor(summary?.receivable_minor ?? 0, 'CNY', preferences.privacy_mode)}
          </div>
          <div className="mt-0.5 text-ab-caption1 text-label-3">{t('debts.receivableHint')}</div>
        </Card>
        <Card dense>
          <div className="ab-section-label !px-0 !pt-0">{t('debts.payable')}</div>
          <div className="ab-tnum text-ab-title2 font-semibold tracking-tight text-negative">
            {displayMinor(summary?.payable_minor ?? 0, 'CNY', preferences.privacy_mode)}
          </div>
          <div className="mt-0.5 text-ab-caption1 text-label-3">{t('debts.payableHint')}</div>
        </Card>
        <Card dense>
          <div className="ab-section-label !px-0 !pt-0">{t('debts.net')}</div>
          <div
            className={`ab-tnum text-ab-title2 font-semibold tracking-tight ${
              (summary?.net_minor ?? 0) >= 0 ? 'text-positive' : 'text-negative'
            }`}
          >
            {displayMinor(summary?.net_minor ?? 0, 'CNY', preferences.privacy_mode, { showSign: true })}
          </div>
          <div className="mt-0.5 text-ab-caption1 text-label-3">
            {t('debts.counts', { active: summary?.active_count ?? 0, settled: summary?.settled_count ?? 0 })}
          </div>
        </Card>
      </div>

      {!data?.has_debt ? (
        <Card flush>
          <EmptyState
            icon="debts"
            title={t('debts.emptyTitle')}
            body={t('debts.emptyBody')}
            action={
              <button type="button" className="ab-btn-primary" onClick={() => setCreating(true)}>
                <Icon name="plus" size={14} />
                {t('debts.new')}
              </button>
            }
          />
        </Card>
      ) : (
        <Card title={t('debts.listTitle')} flush>
          {data.items.map((item) => (
            <div key={item.id} className="border-b border-separator/40 px-3 py-2.5 last:border-0">
              <div className="flex items-center gap-2">
                <span
                  className={`ab-phase-badge shrink-0 ${
                    item.kind === 'lend' ? '!text-positive' : '!text-negative'
                  }`}
                >
                  {t(`debts.kind.${item.kind}`)}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-ab-subhead text-label">
                    {item.name}
                    {item.counterparty ? <span className="text-label-3"> · {item.counterparty}</span> : null}
                  </span>
                  <span className="block text-ab-caption1 text-label-3">
                    {item.due_date
                      ? item.overdue
                        ? t('debts.overdueBy', { days: Math.abs(item.days_until_due ?? 0) })
                        : t('debts.dueIn', { date: formatDayLabel(item.due_date), days: item.days_until_due ?? 0 })
                      : t('debts.noDueDate')}
                    {item.payment_count > 0 ? ` · ${t('debts.paidTimes', { count: item.payment_count })}` : ''}
                  </span>
                </span>
                <span className="ab-tnum shrink-0 text-right">
                  <span className="block text-ab-subhead font-semibold text-label">
                    {displayMinor(item.remaining_minor, item.currency, preferences.privacy_mode)}
                  </span>
                  <span className="block text-ab-caption1 text-label-3">
                    / {displayMinor(item.principal_minor, item.currency, preferences.privacy_mode)}
                  </span>
                </span>
              </div>
              <div className="mt-1.5 flex items-center gap-2">
                <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-hairline/15">
                  <span
                    className="block h-full rounded-full bg-accent transition-all duration-500"
                    style={{ width: `${Math.min(1, item.progress) * 100}%` }}
                  />
                </span>
                {item.status === 'active' ? (
                  <>
                    <button
                      type="button"
                      className="ab-chip shrink-0"
                      onClick={() => {
                        setPayment({ amount_minor: Math.max(0, item.remaining_minor), interest_minor: 0, occurred_at: localDayKey() })
                        setPaying(item)
                      }}
                    >
                      {t('debts.addPayment')}
                    </button>
                    <button
                      type="button"
                      className="ab-icon-btn shrink-0"
                      aria-label={t('debts.settle')}
                      onClick={() => {
                        void api.settleDebt(item.id, 'settled').then(() => load())
                      }}
                    >
                      <Icon name="check" size={13} />
                    </button>
                  </>
                ) : (
                  <span className="shrink-0 text-ab-caption1 text-label-3">
                    {t(`debts.status.${item.status}`)}
                  </span>
                )}
              </div>
            </div>
          ))}
        </Card>
      )}

      {/* 新建债务 */}
      <Modal
        open={creating}
        title={t('debts.newTitle')}
        onClose={() => setCreating(false)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setCreating(false)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void submit()}>
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="space-y-3.5">
          <div>
            <span className="ab-field-label">{t('debts.kindLabel')}</span>
            <div className="ab-segment">
              {(['lend', 'borrow'] as const).map((item) => (
                <button key={item} type="button" data-active={form.kind === item} onClick={() => setForm({ ...form, kind: item })}>
                  {t(`debts.kind.${item}`)}
                </button>
              ))}
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="debt-name">
                {t('debts.name')}
              </label>
              <input
                id="debt-name"
                className="ab-input"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                placeholder={t('debts.namePlaceholder')}
              />
            </div>
            <div>
              <label className="ab-field-label" htmlFor="debt-party">
                {t('debts.counterparty')}
              </label>
              <input
                id="debt-party"
                className="ab-input"
                value={form.counterparty}
                onChange={(event) => setForm({ ...form, counterparty: event.target.value })}
              />
            </div>
          </div>
          <label className="block">
            <span className="ab-field-label">{t('debts.principal')}</span>
            <AmountField
              value={form.principal_minor}
              onChange={(value) => setForm({ ...form, principal_minor: value ?? 0 })}
            />
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="debt-start">
                {t('debts.startDate')}
              </label>
              <input
                id="debt-start"
                type="date"
                className="ab-input ab-tnum"
                value={form.start_date}
                onChange={(event) => setForm({ ...form, start_date: localDayKey(event.target.value) })}
              />
            </div>
            <div>
              <label className="ab-field-label" htmlFor="debt-due">
                {t('debts.dueDate')}
              </label>
              <input
                id="debt-due"
                type="date"
                className="ab-input ab-tnum"
                value={form.due_date ?? ''}
                onChange={(event) =>
                  setForm({ ...form, due_date: event.target.value ? localDayKey(event.target.value) : null })
                }
              />
            </div>
          </div>
          <div>
            <label className="ab-field-label" htmlFor="debt-rate">
              {t('debts.annualRate')}
            </label>
            <div className="flex items-center gap-2">
              <input
                id="debt-rate"
                type="number"
                step="0.01"
                min={0}
                className="ab-input ab-tnum !w-28"
                value={(form.annual_rate_bps / 100).toString()}
                onChange={(event) =>
                  setForm({ ...form, annual_rate_bps: Math.round(Number(event.target.value) * 100) })
                }
              />
              <span className="text-ab-footnote text-label-3">% {t('debts.perYear')}</span>
            </div>
            <p className="mt-1 text-ab-caption1 text-label-3">{t('debts.rateHint')}</p>
          </div>
          <label className="flex items-start gap-2 text-ab-footnote text-label-2">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={form.create_mirror_account}
              onChange={(event) => setForm({ ...form, create_mirror_account: event.target.checked })}
            />
            <span>
              {t('debts.mirrorAccount')}
              <span className="block text-ab-caption1 text-label-3">{t('debts.mirrorAccountHint')}</span>
            </span>
          </label>
        </div>
      </Modal>

      {/* 登记还款 */}
      <Modal
        open={paying !== null}
        title={t('debts.paymentTitle', { name: paying?.name ?? '' })}
        onClose={() => setPaying(null)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setPaying(null)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="ab-btn-primary"
              disabled={busy || payment.amount_minor <= 0}
              onClick={() => void submitPayment()}
            >
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="space-y-3.5">
          {paying ? (
            <div className="rounded-ab-sm bg-surface-2 px-3 py-2 text-ab-footnote text-label-2">
              {t('debts.remainingBefore', {
                amount: displayMinor(paying.remaining_minor, paying.currency, preferences.privacy_mode),
              })}
            </div>
          ) : null}
          <label className="block">
            <span className="ab-field-label">{t('debts.paymentAmount')}</span>
            <AmountField
              value={payment.amount_minor}
              onChange={(value) => setPayment({ ...payment, amount_minor: value ?? 0 })}
            />
          </label>
          <label className="block">
            <span className="ab-field-label">{t('debts.interestPart')}</span>
            <AmountField
              value={payment.interest_minor}
              onChange={(value) => setPayment({ ...payment, interest_minor: value ?? 0 })}
            />
          </label>
          <p className="text-ab-caption1 text-label-3">
            {t('debts.principalResolved', {
              amount: displayMinor(Math.max(0, payment.amount_minor - payment.interest_minor), 'CNY', preferences.privacy_mode),
            })}
          </p>
          <div>
            <label className="ab-field-label" htmlFor="payment-date">
              {t('debts.paymentDate')}
            </label>
            <input
              id="payment-date"
              type="date"
              className="ab-input ab-tnum"
              value={payment.occurred_at}
              onChange={(event) => setPayment({ ...payment, occurred_at: localDayKey(event.target.value) })}
            />
          </div>
        </div>
      </Modal>
    </div>
  )
}

// -----------------------------------------------------------------------------
function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string
  value: string
  tone?: 'positive' | 'negative' | 'label'
  hint?: string
}) {
  const color = tone === 'positive' ? 'text-positive' : tone === 'negative' ? 'text-negative' : 'text-label'
  return (
    <div>
      <div className="text-ab-caption text-label-3">{label}</div>
      <div className={`ab-tnum text-ab-callout font-semibold ${color}`}>{value}</div>
      {hint ? <div className="text-ab-caption1 text-label-3">{hint}</div> : null}
    </div>
  )
}
