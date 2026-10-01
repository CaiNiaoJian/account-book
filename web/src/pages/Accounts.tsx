/**
 * 账户页（P1）—— 资产卡片墙。
 *
 * 设计要点
 * --------
 * * **余额按正负归类为资产/负债**，与后端口径一致（多还款的信用卡会变成正余额，
 *   按账户类型硬分类会算错）；
 * * 卡片显示账户图标、机构、卡号后四位；信用卡额外显示额度使用进度；
 * * 归档账户默认隐藏（它们是"不再使用但保留历史"的账户）。
 */

import { motion } from 'framer-motion'
import { useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { staggerContainer, staggerItem } from '@/design/motion'
import { useI18n } from '@/i18n'
import { api, ApiError, type Account } from '@/lib/api'
import { displayMinor } from '@/lib/format'
import { usePreferences } from '@/app/preferences'

import { IconPicker, Modal } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

const ACCOUNT_TYPES = [
  'cash',
  'debit_card',
  'credit_card',
  'e_wallet',
  'investment',
  'receivable',
  'payable',
  'prepaid',
  'virtual',
] as const

/** 新增/编辑账户的表单 */
interface FormState {
  name: string
  type: string
  initial_balance_minor: number
  icon: string
  color: string
  institution: string
  card_no_tail: string
  credit_limit_minor: number
  bill_day: number | null
  due_day: number | null
  include_in_net_worth: boolean
  note: string
}

const EMPTY_FORM: FormState = {
  name: '',
  type: 'debit_card',
  initial_balance_minor: 0,
  icon: 'accounts',
  color: 'accent',
  institution: '',
  card_no_tail: '',
  credit_limit_minor: 0,
  bill_day: null,
  due_day: null,
  include_in_net_worth: true,
  note: '',
}

const COLOR_CHOICES = [
  'accent',
  'green',
  'indigo',
  'purple',
  'pink',
  'teal',
  'orange',
  'yellow',
  'red',
  'gray',
]

export function AccountsPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { status, accounts, allAccounts, refresh } = useLedger()
  const [editing, setEditing] = useState<Account | null>(null)
  const [creating, setCreating] = useState(false)
  const [showArchived, setShowArchived] = useState(false)
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [iconPickerOpen, setIconPickerOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 待删除账户：只保留确认框需要的字段，避免把整个列表项塞进状态 */
  const [deleting, setDeleting] = useState<{ id: number; name: string } | null>(null)

  const visible = showArchived ? allAccounts : allAccounts.filter((item) => !item.is_archived)
  const totals = accounts.reduce(
    (accumulator, item) => {
      if (!item.include_in_net_worth) return accumulator
      if (item.balance_minor >= 0) accumulator.assets += item.balance_minor
      else accumulator.liabilities += -item.balance_minor
      return accumulator
    },
    { assets: 0, liabilities: 0 },
  )

  const openCreate = () => {
    setForm(EMPTY_FORM)
    setEditing(null)
    setCreating(true)
    setError(null)
  }

  const openEdit = (account: Account) => {
    setForm({
      name: account.name,
      type: account.type,
      initial_balance_minor: account.initial_balance_minor,
      icon: account.icon,
      color: account.color,
      institution: account.institution,
      card_no_tail: account.card_no_tail,
      credit_limit_minor: account.credit_limit_minor,
      bill_day: account.bill_day,
      due_day: account.due_day,
      include_in_net_worth: account.include_in_net_worth,
      note: account.note,
    })
    setEditing(account)
    setCreating(false)
    setError(null)
  }

  const closeForm = () => {
    setCreating(false)
    setEditing(null)
    setIconPickerOpen(false)
  }

  const submit = async () => {
    if (!form.name.trim()) {
      setError(t('ledger.accountNameRequired'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (editing) {
        await api.updateAccount(editing.id, form)
      } else {
        await api.createAccount(form)
      }
      await refresh()
      closeForm()
    } catch (cause) {
      // 后端返回的是稳定的 code + 可读 message；直接显示 message 即可，
      // 但冲突类错误给出更贴心的提示（用户往往想找的是"改名"而不是"报错"）
      const message =
        cause instanceof ApiError && cause.code === 'conflict'
          ? t('ledger.accountNameTaken')
          : cause instanceof Error
            ? cause.message
            : String(cause)
      setError(message)
    } finally {
      setBusy(false)
    }
  }

  const confirmDelete = async () => {
    if (!deleting) return
    setBusy(true)
    try {
      await api.deleteAccount(deleting.id)
      await refresh()
      setDeleting(null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
      setDeleting(null)
    } finally {
      setBusy(false)
    }
  }

  const toggleArchive = async (account: Account) => {
    await api.updateAccount(account.id, { is_archived: !account.is_archived })
    await refresh()
  }

  if (status === 'loading') {
    return (
      <div className="space-y-4">
        <Skeleton className="h-24 w-full" />
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((key) => (
            <Skeleton key={key} className="h-32 w-full" />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* 汇总条：资产 / 负债 / 净值 */}
      <div className="grid gap-3 sm:grid-cols-3">
        <Card dense>
          <div className="ab-section-label !px-0 !pt-0">{t('ledger.totalAssets')}</div>
          <div className="ab-metric text-positive">
            {displayMinor(totals.assets, 'CNY', preferences.privacy_mode)}
          </div>
        </Card>
        <Card dense>
          <div className="ab-section-label !px-0 !pt-0">{t('ledger.totalLiabilities')}</div>
          <div className="ab-metric text-negative">
            {displayMinor(totals.liabilities, 'CNY', preferences.privacy_mode)}
          </div>
        </Card>
        <Card dense>
          <div className="ab-section-label !px-0 !pt-0">{t('ledger.netWorth')}</div>
          <div className="ab-metric">
            {displayMinor(totals.assets - totals.liabilities, 'CNY', preferences.privacy_mode)}
          </div>
        </Card>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h1 className="text-ab-title3 font-semibold text-label">{t('nav.accounts')}</h1>
          <span className="text-ab-footnote text-label-3">
            {t('ledger.accountCount', { count: visible.length })}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="ab-chip"
            data-active={showArchived}
            onClick={() => setShowArchived((value) => !value)}
          >
            <Icon name="eye" size={12} />
            {t('ledger.showArchived')}
          </button>
          <button type="button" className="ab-btn-primary" onClick={openCreate}>
            <Icon name="plus" size={14} />
            {t('ledger.newAccount')}
          </button>
        </div>
      </div>

      {visible.length === 0 ? (
        <Card flush>
          <EmptyState
            icon="accounts"
            title={t('ledger.noAccountsTitle')}
            body={t('ledger.noAccountsBody')}
            action={
              <button type="button" className="ab-btn-primary" onClick={openCreate}>
                <Icon name="plus" size={14} />
                {t('ledger.newAccount')}
              </button>
            }
          />
        </Card>
      ) : (
        <motion.div
          variants={staggerContainer}
          initial="hidden"
          animate="show"
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"
        >
          {visible.map((account) => {
            const overview = accounts.find((item) => item.id === account.id)
            const balance = overview?.balance_minor ?? account.initial_balance_minor
            const limit = account.credit_limit_minor
            const used = limit > 0 ? Math.min(1, Math.max(0, -balance / limit)) : 0
            return (
              <motion.div key={account.id} variants={staggerItem}>
                <Card className="group h-full">
                  <div className="flex items-start gap-3">
                    <span
                      className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px]"
                      style={{
                        backgroundColor: `rgb(var(--ab-${account.color}) / 0.14)`,
                        color: `rgb(var(--ab-${account.color}))`,
                      }}
                    >
                      <Icon name={account.icon} size={20} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate text-ab-callout font-semibold text-label">
                          {account.name}
                        </span>
                        {account.is_archived ? (
                          <span className="ab-phase-badge">{t('ledger.archived')}</span>
                        ) : null}
                      </div>
                      <div className="mt-0.5 truncate text-ab-caption text-label-3">
                        {[t(`accountType.${account.type}`), account.institution, account.card_no_tail && `•••• ${account.card_no_tail}`]
                          .filter(Boolean)
                          .join(' · ')}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center opacity-0 transition-opacity group-hover:opacity-100">
                      <button
                        type="button"
                        className="ab-icon-btn"
                        aria-label={t('common.edit')}
                        onClick={() => openEdit(account)}
                      >
                        <Icon name="edit" size={14} />
                      </button>
                      <button
                        type="button"
                        className="ab-icon-btn"
                        aria-label={t('common.archive')}
                        onClick={() => void toggleArchive(account)}
                      >
                        <Icon name={account.is_archived ? 'refresh' : 'folder'} size={14} />
                      </button>
                      <button
                        type="button"
                        className="ab-icon-btn hover:text-negative"
                        aria-label={t('common.delete')}
                        onClick={() => setDeleting({ id: account.id, name: account.name })}
                      >
                        <Icon name="trash" size={14} />
                      </button>
                    </div>
                  </div>

                  <div className="mt-3.5">
                    <div className="text-ab-caption text-label-3">{t('ledger.balance')}</div>
                    <div
                      className={`ab-tnum mt-0.5 text-ab-title2 font-semibold tracking-tight ${
                        balance < 0 ? 'text-negative' : 'text-label'
                      }`}
                    >
                      {displayMinor(balance, account.currency, preferences.privacy_mode)}
                    </div>
                  </div>

                  {limit > 0 ? (
                    <div className="mt-3">
                      <div className="flex items-center justify-between text-ab-caption text-label-3">
                        <span>{t('ledger.creditUsed')}</span>
                        <span className="ab-tnum">
                          {displayMinor(-balance > 0 ? -balance : 0, account.currency, preferences.privacy_mode)}
                          {' / '}
                          {displayMinor(limit, account.currency, preferences.privacy_mode)}
                        </span>
                      </div>
                      {/* 额度使用条：超过 80% 变警示色 —— 这是用户真正关心的信号 */}
                      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-hairline/10">
                        <div
                          className={`h-full rounded-full transition-all duration-500 ${
                            used > 0.8 ? 'bg-negative' : used > 0.5 ? 'bg-warning' : 'bg-accent'
                          }`}
                          style={{ width: `${used * 100}%` }}
                        />
                      </div>
                    </div>
                  ) : null}
                </Card>
              </motion.div>
            )
          })}
        </motion.div>
      )}

      {/* 新增 / 编辑 */}
      <Modal
        open={creating || editing !== null}
        title={editing ? t('ledger.editAccount') : t('ledger.newAccount')}
        onClose={closeForm}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={closeForm}>
              {t('common.cancel')}
            </button>
            <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void submit()}>
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="space-y-3.5">
          {error ? (
            <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
              {error}
            </div>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="account-name">
                {t('ledger.accountName')}
              </label>
              <input
                id="account-name"
                className="ab-input"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                placeholder={t('ledger.accountNamePlaceholder')}
              />
            </div>
            <div>
              <label className="ab-field-label" htmlFor="account-type">
                {t('ledger.accountType')}
              </label>
              <select
                id="account-type"
                className="ab-select"
                value={form.type}
                onChange={(event) => setForm({ ...form, type: event.target.value })}
              >
                {ACCOUNT_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {t(`accountType.${type}`)}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="account-balance">
                {t('ledger.initialBalance')}
              </label>
              <input
                id="account-balance"
                className="ab-input ab-tnum"
                inputMode="decimal"
                value={(form.initial_balance_minor / 100).toString()}
                onChange={(event) => {
                  const parsed = Number(event.target.value)
                  setForm({
                    ...form,
                    initial_balance_minor: Number.isFinite(parsed) ? Math.round(parsed * 100) : 0,
                  })
                }}
              />
              <p className="mt-1 text-ab-caption1 text-label-3">{t('ledger.initialBalanceHint')}</p>
            </div>
            <div>
              <label className="ab-field-label" htmlFor="account-institution">
                {t('ledger.institution')}
              </label>
              <input
                id="account-institution"
                className="ab-input"
                value={form.institution}
                onChange={(event) => setForm({ ...form, institution: event.target.value })}
                placeholder={t('ledger.institutionPlaceholder')}
              />
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="account-tail">
                {t('ledger.cardNoTail')}
              </label>
              <input
                id="account-tail"
                className="ab-input ab-tnum"
                maxLength={4}
                value={form.card_no_tail}
                onChange={(event) =>
                  setForm({ ...form, card_no_tail: event.target.value.replace(/\D/g, '') })
                }
                placeholder="1234"
              />
            </div>
            <div>
              <label className="ab-field-label" htmlFor="account-limit">
                {t('ledger.creditLimit')}
              </label>
              <input
                id="account-limit"
                className="ab-input ab-tnum"
                inputMode="decimal"
                value={(form.credit_limit_minor / 100).toString()}
                onChange={(event) => {
                  const parsed = Number(event.target.value)
                  setForm({
                    ...form,
                    credit_limit_minor: Number.isFinite(parsed) ? Math.round(parsed * 100) : 0,
                  })
                }}
              />
            </div>
          </div>

          <div>
            <span className="ab-field-label">{t('ledger.icon')}</span>
            <div className="flex items-center gap-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-ab-sm border border-separator/60 text-label-2">
                <Icon name={form.icon} size={17} />
              </span>
              <button
                type="button"
                className="ab-btn-secondary"
                onClick={() => setIconPickerOpen(true)}
              >
                {t('ledger.chooseIcon')}
              </button>
            </div>
          </div>

          <div>
            <span className="ab-field-label">{t('ledger.color')}</span>
            <div className="flex flex-wrap gap-1.5">
              {COLOR_CHOICES.map((color) => (
                <button
                  key={color}
                  type="button"
                  aria-label={color}
                  aria-pressed={form.color === color}
                  onClick={() => setForm({ ...form, color })}
                  className={`h-7 w-7 rounded-full border-2 transition-transform ${
                    form.color === color ? 'border-label scale-110' : 'border-transparent'
                  }`}
                  style={{ backgroundColor: `rgb(var(--ab-${color}))` }}
                />
              ))}
            </div>
          </div>

          <label className="flex items-center gap-2 text-ab-subhead text-label-2">
            <input
              type="checkbox"
              className="accent-accent"
              checked={form.include_in_net_worth}
              onChange={(event) => setForm({ ...form, include_in_net_worth: event.target.checked })}
            />
            {t('ledger.includeInNetWorth')}
          </label>
        </div>
      </Modal>

      {/* 图标选择器（独立弹层，避免表单过长） */}
      <Modal
        open={iconPickerOpen}
        title={t('ledger.chooseIcon')}
        size="lg"
        onClose={() => setIconPickerOpen(false)}
      >
        <IconPicker
          kind="account"
          value={form.icon}
          onChange={(icon) => {
            setForm({ ...form, icon })
            setIconPickerOpen(false)
          }}
        />
      </Modal>

      {/* 删除确认 */}
      <Modal
        open={deleting !== null}
        title={t('ledger.deleteAccountTitle')}
        size="sm"
        onClose={() => setDeleting(null)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setDeleting(null)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="ab-btn-primary !bg-negative"
              disabled={busy}
              onClick={() => void confirmDelete()}
            >
              {t('common.delete')}
            </button>
          </>
        }
      >
        <p className="text-ab-subhead text-label-2">
          {t('ledger.deleteAccountBody', { name: deleting?.name ?? '' })}
        </p>
        {error ? <p className="mt-2 text-ab-footnote text-negative">{error}</p> : null}
      </Modal>
    </div>
  )
}
