/**
 * 台账（P5 / 需求 21）。
 *
 * 这个页面的存在理由只有一条：**让用户能自己核对"余额是怎么来的"**。
 * 因此它把三样东西放在一起，而不是分散在不同页面：
 *
 * 1. 期初 + 逐笔（带滚动余额）+ 期末；
 * 2. **连续性校验**（这本账内部自洽吗？与聚合口径一致吗？）；
 * 3. 对账（拿银行 App 上的真实余额来比，差多少就补一笔调整分录）。
 *
 * 校验结果**始终显示**，不是只在出错时才冒出来。一个平时不出现的检查
 * 在出问题时也不会被注意到 —— 用户会以为"这页本来就没有这块"。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { usePreferences } from '@/app/preferences'
import { useI18n } from '@/i18n'
import { readSections, section } from '@/features/ledger/sections'
import {
  api,
  ApiError,
  type LedgerDocument,
  type LedgerEntry,
  type TrialBalance,
} from '@/lib/api'
import { displayMinor, formatTime, localDayKey, relativeDayLabel, weekdayShort } from '@/lib/format'

import { Modal } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

/** 默认看最近 30 天。台账很少需要看很远，而"最近一个月"是回归率最高的区间 */
function defaultRange(): { start: string; end: string } {
  const today = new Date()
  const start = new Date(today.getTime() - 29 * 86_400_000)
  return { start: localDayKey(start), end: localDayKey(today) }
}

export function LedgerPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { accounts } = useLedger()

  // `/ledger?account=&start=&end=` —— 与日历的 `?day=`、债务的 `?plan=` 同一个理由：
  // 让"某个账户某段时间的台账"成为可分享、可加书签的链接
  const [searchParams, setSearchParams] = useSearchParams()
  const [range, setRange] = useState(() => {
    const start = searchParams.get('start')
    const end = searchParams.get('end')
    return start && end ? { start, end } : defaultRange()
  })
  const [accountId, setAccountId] = useState<number | null>(() => {
    const raw = searchParams.get('account')
    return raw && /^\d+$/.test(raw) ? Number(raw) : null
  })
  const [document, setDocument] = useState<LedgerDocument | null>(null)
  const [trial, setTrial] = useState<TrialBalance | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [reconcileOpen, setReconcileOpen] = useState(false)

  // 默认选第一个账户。用状态而不是派生值，是为了让用户切走之后不被打回去
  useEffect(() => {
    if (accountId === null && accounts.length > 0) setAccountId(accounts[0]!.id)
  }, [accounts, accountId])

  // 把当前选择同步进 URL：刷新与分享都能回到同一个视图
  useEffect(() => {
    if (accountId === null) return
    const next = new URLSearchParams()
    next.set('account', String(accountId))
    next.set('start', range.start)
    next.set('end', range.end)
    setSearchParams(next, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, range.start, range.end])

  const load = useCallback(async () => {
    if (accountId === null) return
    setLoading(true)
    try {
      await readSections([
        section(() => api.ledger(accountId, range), setDocument),
        section(api.trialBalance, setTrial),
      ], t('ledgerLoading.notLoaded'))
      setError(null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setLoading(false)
    }
  }, [accountId, range])

  useEffect(() => {
    void load()
  }, [load])

  const grouped = useMemo(() => {
    const buckets = new Map<string, LedgerEntry[]>()
    for (const entry of document?.entries ?? []) {
      const day = entry.occurred_at.slice(0, 10)
      const list = buckets.get(day)
      if (list) list.push(entry)
      else buckets.set(day, [entry])
    }
    return [...buckets.entries()]
  }, [document])

  const money = (minor: number) => displayMinor(minor, 'CNY', preferences.privacy_mode)
  const check = document?.check

  return (
    <div className="space-y-4" data-print-root>
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('ledgerPage.title')}</h1>
        <div className="ml-auto flex flex-wrap items-center gap-2" data-print-hide>
          <button type="button" className="ab-btn-secondary" onClick={() => window.print()}>
            <Icon name="note" size={13} />
            {t('ledgerPage.print')}
          </button>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={!document}
            onClick={() => setReconcileOpen(true)}
          >
            <Icon name="check" size={13} />
            {t('ledgerPage.reconcile')}
          </button>
        </div>
      </div>

      {/* 账户与区间 */}
      <div className="flex flex-wrap items-center gap-2" data-print-hide>
        <div className="flex flex-wrap items-center gap-1.5">
          {accounts.map((account) => (
            <button
              key={account.id}
              type="button"
              className="ab-chip"
              data-active={accountId === account.id}
              onClick={() => setAccountId(account.id)}
            >
              {account.name}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <input
            type="date"
            className="ab-input !w-auto"
            value={range.start}
            onChange={(event) => setRange((previous) => ({ ...previous, start: event.target.value }))}
          />
          <span className="text-label-3">—</span>
          <input
            type="date"
            className="ab-input !w-auto"
            value={range.end}
            onChange={(event) => setRange((previous) => ({ ...previous, end: event.target.value }))}
          />
        </div>
      </div>

      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
          {error}
        </div>
      ) : null}

      {/* 连续性校验：**始终显示**（平时不出现的检查，出问题时也不会被注意到） */}
      {check ? (
        <Card>
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`ab-chip ${check.balanced ? '!text-positive' : '!text-negative'}`}
              data-active
            >
              <Icon name={check.balanced ? 'check' : 'warning'} size={12} />
              {check.balanced ? t('ledgerPage.balanced') : t('ledgerPage.unbalanced')}
            </span>
            <span className="text-ab-caption1 text-label-3">{t('ledgerPage.checkHint')}</span>
          </div>

          <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {[
              { label: t('ledgerPage.opening'), value: check.opening_balance_minor },
              { label: t('ledgerPage.closing'), value: check.closing_balance_minor },
              { label: t('ledgerPage.recomputed'), value: check.recomputed_closing_minor },
              { label: t('ledgerPage.aggregate'), value: check.aggregate_balance_minor },
            ].map((item) => (
              <div key={item.label}>
                <div className="text-ab-caption1 text-label-3">{item.label}</div>
                <div className="ab-tnum text-ab-subhead text-label">{money(item.value)}</div>
              </div>
            ))}
          </div>

          <div className="mt-3 space-y-1 text-ab-caption1 text-label-3">
            <p>
              {check.internal_ok ? '✓' : '✗'} {t('ledgerPage.internalOk')}
              {' · '}
              {check.aggregate_ok ? '✓' : '✗'} {t('ledgerPage.aggregateOk')}
              {check.covers_today ? '' : ` · ${t('ledgerPage.historicalRange')}`}
            </p>
            {/* 被排除/未纳入的行必须说清楚，否则用户会以为"我记的那笔丢了" */}
            {check.excluded_void_count > 0 ? (
              <p>{t('ledgerPage.voidExcluded', { count: check.excluded_void_count })}</p>
            ) : null}
            {check.future_dated_count > 0 ? (
              <p className="text-warning">
                {t('ledgerPage.futureDated', {
                  count: check.future_dated_count,
                  amount: money(check.future_dated_net_minor),
                })}
              </p>
            ) : null}
          </div>

          {trial ? (
            <p className="mt-2 border-t border-separator/60 pt-2 text-ab-caption1 text-label-3">
              {trial.balanced ? '✓' : '✗'} {t('ledgerPage.trialBalance')}
              {' · '}
              {t('ledgerPage.trialDetail', {
                accounts: trial.account_count,
                change: money(trial.balance_change_minor),
                expected: money(trial.expected_change_minor),
              })}
            </p>
          ) : null}
        </Card>
      ) : null}

      {/* 台账正文 */}
      {loading && !document ? (
        <Skeleton className="h-72 w-full" />
      ) : !document || document.count === 0 ? (
        <Card flush>
          <EmptyState
            icon="ledger"
            title={t('ledgerPage.empty')}
            body={t('ledgerPage.emptyBody', { account: document?.account_name ?? '' })}
          />
        </Card>
      ) : (
        <Card flush>
          <div className="ab-tnum overflow-x-auto">
            <table className="w-full border-collapse text-ab-footnote">
              <thead>
                <tr className="border-b border-separator">
                  <th className="px-3 py-2 text-left text-ab-caption1 font-medium text-label-3">
                    {t('ledgerPage.colTime')}
                  </th>
                  <th className="px-3 py-2 text-left text-ab-caption1 font-medium text-label-3">
                    {t('ledgerPage.colDetail')}
                  </th>
                  <th className="px-3 py-2 text-left text-ab-caption1 font-medium text-label-3">
                    {t('ledgerPage.colCategory')}
                  </th>
                  <th className="px-3 py-2 text-right text-ab-caption1 font-medium text-label-3">
                    {t('ledgerPage.colIn')}
                  </th>
                  <th className="px-3 py-2 text-right text-ab-caption1 font-medium text-label-3">
                    {t('ledgerPage.colOut')}
                  </th>
                  <th className="px-3 py-2 text-right text-ab-caption1 font-medium text-label-3">
                    {t('ledgerPage.colBalance')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {/* 期初行：没有它，第一笔之后的余额就没有参照 */}
                <tr className="bg-surface-2/50">
                  <td className="px-3 py-2 text-label-2" colSpan={5}>
                    {t('ledgerPage.openingRow', { date: document.start })}
                  </td>
                  <td className="px-3 py-2 text-right font-semibold text-label">
                    {money(document.opening_balance_minor)}
                  </td>
                </tr>
                {grouped.map(([day, entries]) => [
                  <tr key={`h-${day}`} className="bg-surface-2/30">
                    <td className="px-3 py-1.5 text-ab-caption1 text-label-3" colSpan={6}>
                      {relativeDayLabel(day)}
                      <span className="ml-1.5">{weekdayShort(day)}</span>
                    </td>
                  </tr>,
                  ...entries.map((entry) => (
                    <tr key={entry.transaction_id} className="border-b border-separator/40">
                      <td className="whitespace-nowrap px-3 py-1.5 text-label-3">
                        {formatTime(entry.occurred_at)}
                      </td>
                      <td className="px-3 py-1.5 text-label-2">
                        <span className="inline-flex items-center gap-1.5">
                          {/* 转账在本账户视角下可能是"收到的转入" */}
                          {entry.leg === 'incoming' ? (
                            <span className="ab-chip !py-0 !text-caption2">
                              {t('ledgerPage.incoming')}
                            </span>
                          ) : null}
                          {/* 转账通常没有商户：此时摘要只显示"→ 对方账户"。
    不加这个分支会渲染成 `— →支付宝`，那个破折号看起来像渲染坏了 */}
                          <span className="truncate">
                            {entry.payee || entry.note || (entry.counterparty ? '' : '—')}
                          </span>
                          {entry.counterparty ? (
                            <span className="text-ab-caption1 text-label-3">
                              → {entry.counterparty}
                            </span>
                          ) : null}
                          {entry.status === 'void' ? (
                            <span className="text-ab-caption1 text-negative">
                              {t('ledgerPage.void')}
                            </span>
                          ) : null}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 text-label-3">{entry.category_name || '—'}</td>
                      <td className="px-3 py-1.5 text-right text-positive">
                        {entry.signed_minor > 0 ? money(entry.signed_minor) : ''}
                      </td>
                      <td className="px-3 py-1.5 text-right text-negative">
                        {entry.signed_minor < 0 ? money(-entry.signed_minor) : ''}
                      </td>
                      <td className="px-3 py-1.5 text-right text-label">
                        {money(entry.running_balance_minor)}
                      </td>
                    </tr>
                  )),
                ])}
              </tbody>
              <tfoot>
                <tr className="border-t border-separator">
                  <td className="px-3 py-2 font-semibold text-label" colSpan={3}>
                    {t('ledgerPage.totals', { count: document.count })}
                  </td>
                  <td className="px-3 py-2 text-right text-positive">{money(document.inflow_minor)}</td>
                  <td className="px-3 py-2 text-right text-negative">
                    {money(document.outflow_minor)}
                  </td>
                  <td className="px-3 py-2 text-right font-semibold text-label">
                    {money(document.closing_balance_minor)}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        </Card>
      )}

      <ReconcileDialog
        open={reconcileOpen}
        accountId={accountId}
        accountName={document?.account_name ?? ''}
        computed={check?.aggregate_balance_minor ?? 0}
        onClose={() => setReconcileOpen(false)}
        onDone={async () => {
          setReconcileOpen(false)
          await load()
        }}
      />
    </div>
  )
}

function ReconcileDialog({
  open,
  accountId,
  accountName,
  computed,
  onClose,
  onDone,
}: {
  open: boolean
  accountId: number | null
  accountName: string
  computed: number
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const [actual, setActual] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<number | null>(null)

  useEffect(() => {
    setActual('')
    setError(null)
    setPreview(null)
  }, [open])

  const minor = () => Math.round(Number(actual || '0') * 100)

  const run = async (createAdjustment: boolean) => {
    if (accountId === null) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.reconcile(accountId, {
        actual_balance_minor: minor(),
        create_adjustment: createAdjustment,
      })
      setPreview(result.difference_minor)
      if (createAdjustment) onDone()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title={t('ledgerPage.reconcileTitle', { account: accountName })}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          {/* 先给差异、再决定是否落库：对账是会改余额的操作 */}
          <button
            type="button"
            className="ab-btn-secondary"
            disabled={busy || !actual}
            onClick={() => void run(false)}
          >
            {t('ledgerPage.previewDiff')}
          </button>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={busy || !actual}
            onClick={() => void run(true)}
          >
            {t('ledgerPage.createAdjustment')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
            {error}
          </div>
        ) : null}
        <div className="ab-tnum rounded-ab-sm bg-surface-2 px-3 py-2 text-ab-footnote text-label-2">
          {t('ledgerPage.computedBalance', {
            amount: displayMinor(computed, 'CNY', preferences.privacy_mode),
          })}
        </div>
        <div>
          <label className="ab-field-label" htmlFor="reconcile-actual">
            {t('ledgerPage.actualBalance')}
          </label>
          <input
            id="reconcile-actual"
            className="ab-input ab-tnum"
            inputMode="decimal"
            autoFocus
            value={actual}
            onChange={(event) => setActual(event.target.value)}
            placeholder="0.00"
          />
        </div>
        {preview !== null ? (
          <p
            className={`ab-tnum text-ab-footnote ${
              preview === 0 ? 'text-positive' : 'text-warning'
            }`}
          >
            {preview === 0
              ? t('ledgerPage.noDifference')
              : t('ledgerPage.difference', {
                  amount: displayMinor(preview, 'CNY', preferences.privacy_mode),
                })}
          </p>
        ) : null}
        <p className="text-ab-caption1 text-label-3">{t('ledgerPage.reconcileHint')}</p>
      </div>
    </Modal>
  )
}
