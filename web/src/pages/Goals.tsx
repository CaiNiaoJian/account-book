/**
 * 储蓄目标（P4）。
 *
 * 与存钱罐的分工
 * --------------
 * 存钱罐是"抽屉里的硬币一毛一毛攒"，目标是"我要在什么时候攒够多少钱"。
 * 因此这里的进度条**来自指定账户的实时余额**（或手工注入），
 * 而不是一笔笔硬币 —— 用户在这个页面上关心的是"还差多少、来不来得及"。
 *
 * 账户余额可能为负（信用卡）。界面**照实显示**而不是夹到 0：
 * 夹掉会让"我把首付账户刷爆了"看起来跟"一分没存"一样，
 * 而前者显然需要更紧急的处理。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { usePreferences } from '@/app/preferences'
import { useI18n } from '@/i18n'
import { api, ApiError, type Goal } from '@/lib/api'
import { displayMinor, formatDayLabel } from '@/lib/format'

import { Modal } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

/** 目标类型 → 图标 */
const KIND_ICON: Record<string, 'goals' | 'cards' | 'piggy' | 'note'> = {
  purchase: 'cards',
  emergency: 'goals',
  travel: 'piggy',
  education: 'note',
  other: 'goals',
}

/**
 * 进度环。
 *
 * 用 SVG 圆环而不是"横条"：这个页面上一屏会有好几个目标，
 * 横条之间很难一眼比出进度差异，而环形在同样面积下更容易扫读。
 * 比例**不封顶**：攒超了要把圆环画满并标出百分比，而不是看起来刚好 100%。
 */
function ProgressRing({ ratio, size = 84 }: { ratio: number; size?: number }) {
  const stroke = 7
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  const clamped = Math.max(0, Math.min(1, ratio))
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0">
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        className="stroke-separator"
        strokeWidth={stroke}
        fill="none"
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        className={ratio >= 1 ? 'stroke-positive' : 'stroke-accent'}
        strokeWidth={stroke}
        strokeLinecap="round"
        fill="none"
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - clamped)}
        // 从 12 点方向开始，而不是 3 点 —— 后者看起来像个进度不明的加载圈
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: 'stroke-dashoffset 500ms cubic-bezier(0.32, 0.72, 0, 1)' }}
      />
      <text
        x="50%"
        y="50%"
        textAnchor="middle"
        dominantBaseline="central"
        className="ab-tnum fill-label text-ab-footnote font-semibold"
      >
        {Math.round(ratio * 100)}%
      </text>
    </svg>
  )
}

export function GoalsPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { accounts } = useLedger()

  const [goals, setGoals] = useState<Goal[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [contributeFor, setContributeFor] = useState<Goal | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const list = await api.goals()
      setGoals(list.items)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setGoals([])
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const totals = useMemo(() => {
    const items = goals ?? []
    return {
      saved: items.reduce((sum, goal) => sum + goal.saved_minor, 0),
      target: items
        .filter((goal) => goal.status === 'active')
        .reduce((sum, goal) => sum + goal.target_amount_minor, 0),
    }
  }, [goals])

  const remove = async (goal: Goal) => {
    setBusy(true)
    try {
      await api.deleteGoal(goal.id)
      setNotice(t('goals.deleted', { name: goal.name }))
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('goals.title')}</h1>
        {goals && goals.length > 0 ? (
          <span className="ab-tnum text-ab-caption text-label-3">
            {t('goals.summary', {
              saved: displayMinor(totals.saved, 'CNY', preferences.privacy_mode),
              target: displayMinor(totals.target, 'CNY', preferences.privacy_mode),
            })}
          </span>
        ) : null}
        <button type="button" className="ab-btn-primary ml-auto" onClick={() => setCreateOpen(true)}>
          <Icon name="plus" size={14} />
          {t('goals.newGoal')}
        </button>
      </div>

      {notice ? (
        <div className="rounded-ab-sm bg-positive/10 px-3 py-2 text-ab-footnote text-positive">{notice}</div>
      ) : null}
      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
      ) : null}

      {goals === null ? (
        <div className="grid gap-4 sm:grid-cols-2">
          {[0, 1].map((key) => (
            <Skeleton key={key} className="h-40 w-full" />
          ))}
        </div>
      ) : goals.length === 0 ? (
        <Card flush>
          <EmptyState
            icon="goals"
            title={t('goals.empty')}
            body={t('goals.emptyBody')}
            action={
              <button type="button" className="ab-btn-primary" onClick={() => setCreateOpen(true)}>
                <Icon name="plus" size={14} />
                {t('goals.newGoal')}
              </button>
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {goals.map((goal) => {
            const remaining = Math.max(0, goal.target_amount_minor - goal.saved_minor)
            const days =
              goal.deadline
                ? Math.ceil(
                    (new Date(goal.deadline).getTime() - Date.now()) / 86_400_000,
                  )
                : null
            return (
              <Card key={goal.id}>
                <div className="flex items-start gap-4">
                  <ProgressRing ratio={goal.ratio} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start gap-1">
                      <span className="shrink-0 text-label-3">
                        <Icon name={KIND_ICON[goal.kind] ?? 'goals'} size={15} />
                      </span>
                      <h2 className="min-w-0 flex-1 truncate text-ab-headline font-semibold text-label">
                        {goal.name}
                      </h2>
                      <button
                        type="button"
                        className="ab-icon-btn shrink-0 hover:text-negative"
                        aria-label={t('common.delete')}
                        disabled={busy}
                        onClick={() => void remove(goal)}
                      >
                        <Icon name="trash" size={13} />
                      </button>
                    </div>

                    <div className="ab-tnum mt-1 text-ab-subhead text-label">
                      {goal.hide_amount
                        ? t('goals.percentOnly', { percent: Math.round(goal.ratio * 100) })
                        : t('goals.savedOf', {
                            saved: displayMinor(goal.saved_minor, 'CNY', preferences.privacy_mode),
                            target: displayMinor(
                              goal.target_amount_minor,
                              'CNY',
                              preferences.privacy_mode,
                            ),
                          })}
                    </div>

                    {!goal.hide_amount && remaining > 0 ? (
                      <div className="ab-tnum text-ab-caption1 text-label-3">
                        {t('goals.remaining', {
                          amount: displayMinor(remaining, 'CNY', preferences.privacy_mode),
                        })}
                      </div>
                    ) : null}

                    <div className="mt-1 flex flex-wrap items-center gap-1">
                      {goal.status === 'achieved' ? (
                        <span className="ab-chip" data-active>
                          <Icon name="check" size={11} />
                          {t('goals.achieved')}
                        </span>
                      ) : null}
                      {goal.account_id ? (
                        <span className="ab-chip">
                          <Icon name="accounts" size={11} />
                          {accounts.find((item) => item.id === goal.account_id)?.name ??
                            t('goals.linkedAccount')}
                        </span>
                      ) : null}
                      {goal.deadline ? (
                        <span className="ab-chip">
                          {t('goals.deadlineChip', { date: formatDayLabel(goal.deadline) })}
                        </span>
                      ) : null}
                    </div>

                    {/* 期限提示：只有给出期限时才可能有"来不来得及"这个问题 */}
                    {days !== null && goal.status === 'active' ? (
                      <p
                        className={`mt-1 text-ab-caption1 ${
                          days < 0 ? 'text-negative' : 'text-label-3'
                        }`}
                      >
                        {days < 0
                          ? t('goals.overdue', { days: -days })
                          : t('goals.daysLeft', { days })}
                        {days > 0 && remaining > 0
                          ? ` · ${t('goals.perDay', {
                              amount: displayMinor(
                                Math.ceil(remaining / days),
                                'CNY',
                                preferences.privacy_mode,
                              ),
                            })}`
                          : ''}
                      </p>
                    ) : null}
                  </div>
                </div>

                <div className="mt-3 flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    className="ab-btn-primary"
                    onClick={() => setContributeFor(goal)}
                  >
                    <Icon name="plus" size={13} />
                    {t('goals.contribute')}
                  </button>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      <CreateGoalDialog
        open={createOpen}
        accounts={accounts}
        onClose={() => setCreateOpen(false)}
        onCreated={async () => {
          setCreateOpen(false)
          setNotice(t('goals.created'))
          await load()
        }}
      />

      <ContributeDialog
        goal={contributeFor}
        onClose={() => setContributeFor(null)}
        onDone={async () => {
          setContributeFor(null)
          await load()
        }}
      />
    </div>
  )
}

function CreateGoalDialog({
  open,
  accounts,
  onClose,
  onCreated,
}: {
  open: boolean
  accounts: { id: number; name: string }[]
  onClose: () => void
  onCreated: () => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [target, setTarget] = useState('')
  const [deadline, setDeadline] = useState('')
  const [kind, setKind] = useState('purchase')
  const [accountId, setAccountId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.createGoal({
        name,
        target_amount_minor: Math.round(Number(target || '0') * 100),
        deadline: deadline || null,
        kind: kind as never,
        account_id: accountId ? Number(accountId) : null,
      })
      setName('')
      setTarget('')
      setDeadline('')
      onCreated()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title={t('goals.newGoal')}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void submit()}>
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
        ) : null}
        <div>
          <label className="ab-field-label" htmlFor="goal-name">
            {t('goals.name')}
          </label>
          <input
            id="goal-name"
            className="ab-input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={t('goals.namePlaceholder')}
          />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="ab-field-label" htmlFor="goal-target">
              {t('goals.targetAmount')}
            </label>
            <input
              id="goal-target"
              className="ab-input ab-tnum"
              inputMode="decimal"
              value={target}
              onChange={(event) => setTarget(event.target.value)}
              placeholder="0.00"
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="goal-deadline">
              {t('goals.deadline')}
            </label>
            <input
              id="goal-deadline"
              type="date"
              className="ab-input"
              value={deadline}
              onChange={(event) => setDeadline(event.target.value)}
            />
          </div>
        </div>
        <div>
          <span className="ab-field-label">{t('goals.kind')}</span>
          <div className="flex flex-wrap gap-1.5">
            {(['purchase', 'emergency', 'travel', 'education', 'other'] as const).map((item) => (
              <button
                key={item}
                type="button"
                className="ab-chip"
                data-active={kind === item}
                onClick={() => setKind(item)}
              >
                {t(`goals.kindName.${item}`)}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="ab-field-label" htmlFor="goal-account">
            {t('goals.linkedAccount')}
          </label>
          <select
            id="goal-account"
            className="ab-select"
            value={accountId}
            onChange={(event) => setAccountId(event.target.value)}
          >
            <option value="">{t('goals.noAccount')}</option>
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.name}
              </option>
            ))}
          </select>
          {/* 两种进度来源要在界面上说清楚，否则用户会以为"没选账户就没法用" */}
          <p className="mt-1 text-ab-caption1 text-label-3">{t('goals.accountHint')}</p>
        </div>
      </div>
    </Modal>
  )
}

function ContributeDialog({
  goal,
  onClose,
  onDone,
}: {
  goal: Goal | null
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setAmount('')
    setNote('')
    setError(null)
  }, [goal])

  const submit = async (sign: 1 | -1) => {
    if (!goal) return
    setBusy(true)
    setError(null)
    try {
      await api.contributeGoal(goal.id, {
        amount_minor: sign * Math.round(Number(amount || '0') * 100),
        note,
      })
      onDone()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={goal !== null}
      title={goal ? t('goals.contributeTitle', { name: goal.name }) : ''}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="ab-btn-secondary !text-negative"
            disabled={busy || !amount}
            onClick={() => void submit(-1)}
          >
            {t('goals.withdraw')}
          </button>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={busy || !amount}
            onClick={() => void submit(1)}
          >
            <Icon name="plus" size={13} />
            {t('goals.contribute')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
        ) : null}
        <div>
          <label className="ab-field-label" htmlFor="contribute-amount">
            {t('goals.amount')}
          </label>
          <input
            id="contribute-amount"
            className="ab-input ab-tnum"
            inputMode="decimal"
            autoFocus
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            placeholder="0.00"
          />
        </div>
        <div>
          <label className="ab-field-label" htmlFor="contribute-note">
            {t('ledger.note')}
          </label>
          <input
            id="contribute-note"
            className="ab-input"
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
        </div>
        {/* 目标如果挂着账户，注入金额会叠加在账户余额之上 —— 这一点必须说清楚，
            否则用户会奇怪"我为什么记了两次" */}
        {goal?.account_id ? (
          <p className="rounded-ab-sm bg-warning/10 px-3 py-2 text-ab-caption1 text-label-2">
            {t('goals.doubleCountWarning')}
          </p>
        ) : null}
      </div>
    </Modal>
  )
}
