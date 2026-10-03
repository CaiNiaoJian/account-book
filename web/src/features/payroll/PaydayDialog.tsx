import { useEffect, useState } from 'react'

import { Modal } from '@/features/ledger/parts'
import { useI18n } from '@/i18n'
import { api, ApiError, type PaydayRule } from '@/lib/api'

type Rule = Omit<PaydayRule, 'id' | 'source_id'>
const defaults: Rule = {
  day_of_month: 15, day_kind: 'fixed', nth: 1,
  weekend_policy: 'advance', holiday_policy: 'advance', enabled: true,
  remind_at: '09:00', grace_days: 3, require_form: true, note: '',
}

export function PaydayDialog({ open, sourceId, onClose, onDone }: {
  open: boolean; sourceId: number | null; onClose: () => void; onDone: () => void
}) {
  const { t } = useI18n()
  const [rule, setRule] = useState<Rule>(defaults)
  const [day, setDay] = useState('15')
  const [nth, setNth] = useState('1')
  const [ready, setReady] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!open || sourceId === null) return
    let cancelled = false
    setReady(false)
    setError(null)
    void api.paydayRule(sourceId).then((saved) => {
      if (cancelled) return
      const { id: _id, source_id: _source, ...values } = saved ?? { id: 0, source_id: 0, ...defaults }
      setRule(values)
      setDay(String(values.day_of_month))
      setNth(String(values.nth))
      setReady(true)
    }).catch((cause) => {
      if (!cancelled) setError(cause instanceof ApiError ? cause.detail : String(cause))
    })
    return () => { cancelled = true }
  }, [open, sourceId])

  const save = async () => {
    if (sourceId === null) return
    const dayNumber = Number(day), nthNumber = Number(nth)
    if (!Number.isInteger(dayNumber) || dayNumber < 1 || dayNumber > 31 ||
      !Number.isInteger(nthNumber) || nthNumber < 1 || nthNumber > 23) {
      setError(t('payroll.invalidPayday'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.setPaydayRule(sourceId, { ...rule, day_of_month: dayNumber, nth: nthNumber })
      onDone()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally { setBusy(false) }
  }

  return <Modal open={open} title={t('payroll.editPayday')} onClose={() => { if (!busy) onClose() }}
    footer={<>
      <button type="button" className="ab-btn-secondary" disabled={busy} onClick={onClose}>{t('common.cancel')}</button>
      <button type="button" className="ab-btn-primary" disabled={!ready || busy} onClick={() => void save()}>{t('common.save')}</button>
    </>}>
    <div className="space-y-3">
      {error ? <p className="text-ab-footnote text-negative">{error}</p> : null}
      <label className="ab-field-label" htmlFor="payday-kind">{t('payroll.dayKindLabel')}</label>
      <select id="payday-kind" className="ab-select" value={rule.day_kind} disabled={!ready || busy}
        onChange={(event) => setRule({ ...rule, day_kind: event.target.value as Rule['day_kind'] })}>
        {(['fixed', 'month_end', 'last_workday', 'nth_workday'] as const).map((kind) =>
          <option key={kind} value={kind}>{t(`payroll.dayKind.${kind}`)}</option>)}
      </select>
      {rule.day_kind === 'fixed' ? <div>
        <label className="ab-field-label" htmlFor="payday-day">{t('payroll.dayOfMonth')}</label>
        <input id="payday-day" className="ab-input" type="number" min="1" max="31" step="1"
          value={day} disabled={!ready || busy} onChange={(event) => setDay(event.target.value)} />
      </div> : rule.day_kind === 'nth_workday' ? <div>
        <label className="ab-field-label" htmlFor="payday-nth">{t('payroll.nthWorkday')}</label>
        <input id="payday-nth" className="ab-input" type="number" min="1" max="23" step="1"
          value={nth} disabled={!ready || busy} onChange={(event) => setNth(event.target.value)} />
      </div> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        {(['weekend_policy', 'holiday_policy'] as const).map((field) => <div key={field}>
          <label className="ab-field-label" htmlFor={field}>{t(`payroll.${field}`)}</label>
          <select id={field} className="ab-select" value={rule[field]} disabled={!ready || busy}
            onChange={(event) => setRule({ ...rule, [field]: event.target.value })}>
            {(['advance', 'postpone', 'none'] as const).map((policy) =>
              <option key={policy} value={policy}>{t(`payroll.dayPolicy.${policy}`)}</option>)}
          </select>
        </div>)}
      </div>
      <label className="flex items-center gap-2 text-ab-footnote text-label-2">
        <input type="checkbox" checked={rule.enabled} disabled={!ready || busy}
          onChange={(event) => setRule({ ...rule, enabled: event.target.checked })} />{t('payroll.ruleEnabled')}
      </label>
      <p className="text-ab-caption1 text-label-3">{t('payroll.paydayHint')}</p>
    </div>
  </Modal>
}
