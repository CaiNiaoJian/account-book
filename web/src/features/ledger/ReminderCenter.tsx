import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useI18n } from '@/i18n'
import { api, type NotificationItem, type PendingPrompt } from '@/lib/api'
import { Modal } from './parts'

export function ReminderCenter() {
  const { t } = useI18n()
  const navigate = useNavigate()
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<PendingPrompt[]>([])
  const [notes, setNotes] = useState<NotificationItem[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [skipId, setSkipId] = useState<number | null>(null)
  const [reason, setReason] = useState('')
  const generation = useRef(0)
  const mounted = useRef(true)
  const load = useCallback(async () => {
    const current = ++generation.current
    const results = await Promise.allSettled([api.reminders(), api.notifications({ unread_only: true, limit: 20 })])
    if (!mounted.current || current !== generation.current) return
    if (results[0].status === 'fulfilled') setItems(results[0].value.items)
    if (results[1].status === 'fulfilled') setNotes(results[1].value.items)
    setError(results.some(result => result.status === 'rejected') ? t('reminders.failed') : '')
  }, [t])
  useEffect(() => {
    mounted.current = true; void load()
    const timer = setInterval(() => void load(), 30000)
    return () => { clearInterval(timer); mounted.current = false; generation.current++ }
  }, [load, location.pathname])
  const act = async (work: () => Promise<unknown>) => {
    if (busy) return
    setBusy(true)
    try { await work(); setSkipId(null); setReason(''); await load() }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
    finally { setBusy(false) }
  }
  const go = (path: string) => { setOpen(false); navigate(path) }
  return <>
    <button type="button" className="ab-btn-secondary" onClick={() => { setOpen(true); void load() }}>
      {t('reminders.button', { count: items.length })}{notes.length > 0 ? ' •' : ''}{error ? ' !' : ''}
    </button>
    <Modal open={open} title={t('reminders.title')} onClose={() => setOpen(false)}>
      <div className="space-y-3">
        <button type="button" className="ab-btn-secondary" disabled={busy} onClick={() => void load()}>{t('common.retry')}</button>
        {error ? <p role="alert" className="text-negative text-ab-footnote">{error}</p> : null}
        {items.length === 0 && !error ? <p>{t('reminders.empty')}</p> : null}
        {items.map(item => <div key={item.id} className="space-y-2 rounded-ab-sm bg-warning/10 p-3 text-ab-footnote">
          <p className="font-semibold">{item.title}</p><p>{item.body}</p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className="ab-btn-primary" onClick={() => go(item.target_kind === 'payroll_record' ? `/payroll?record=${item.target_id ?? ''}` : item.target_kind === 'account' ? '/accounts' : item.target_kind === 'debt' ? '/debts' : '/scheduler')}>{t('reminders.handle')}</button>
            <button type="button" className="ab-btn-secondary" disabled={busy} onClick={() => void act(() => api.resolvePrompt(item.id))}>{t('reminders.done')}</button>
            <button type="button" className="ab-btn-secondary" disabled={busy || item.snooze_left === 0} onClick={() => void act(() => api.snoozePrompt(item.id))}>{t('reminders.later')}</button>
            <button type="button" className="ab-btn-secondary" disabled={busy} onClick={() => { setSkipId(item.id); setReason('') }}>{t('reminders.skip')}</button>
          </div>
          {skipId === item.id ? <div className="flex gap-2">
            <input className="ab-input" aria-label={t('reminders.reason')} placeholder={t('reminders.reason')} value={reason} onChange={e => setReason(e.target.value)} />
            <button type="button" className="ab-btn-secondary" disabled={busy || !reason.trim()} onClick={() => void act(() => api.skipPrompt(item.id, reason))}>{t('reminders.skip')}</button>
          </div> : null}
        </div>)}
        {notes.map(item => <div key={item.id} className="space-y-2 rounded-ab-sm bg-surface-2 p-3 text-ab-footnote">
          <p className="font-semibold">{item.title}</p><p>{item.body}</p>
          {item.action_path ? <button type="button" className="ab-btn-secondary" onClick={() => go(item.action_path)}>{item.action_label || t('reminders.handle')}</button> : null}
          <button type="button" className="ab-btn-secondary" disabled={busy} onClick={() => void act(() => api.readNotification(item.id))}>{t('reminders.read')}</button>
        </div>)}
      </div>
    </Modal>
  </>
}
