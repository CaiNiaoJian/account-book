import { useEffect, useState } from 'react'
import { usePreferences } from '@/app/preferences'
import { Card, Switch } from '@/components/ui'
import { useI18n } from '@/i18n'
import { api, type BackupInfo } from '@/lib/api'

export function BackupCard() {
  const { t } = useI18n()
  const { preferences, update } = usePreferences()
  const [items, setItems] = useState<BackupInfo[]>([])
  const [selected, setSelected] = useState('')
  const [preview, setPreview] = useState<BackupInfo | null>(null)
  const [restored, setRestored] = useState<{ name: string; directory: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const load = async () => { const data = await api.backups(); setItems(data.items) }
  const run = async (work: () => Promise<void>) => {
    if (busy) return
    setBusy(true); setError(''); setNotice('')
    try { await work() } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
    finally { setBusy(false) }
  }
  useEffect(() => { void load().catch(cause => setError(String(cause))) }, [])
  return <Card title={t('backup.title')} subtitle={t('backup.scope')}>
    <div className="space-y-3">
      <p className="text-ab-footnote text-label-2">{t('backup.local')}</p>
      <div className="flex flex-wrap items-center gap-3">
        <Switch label={t('backup.automatic')} checked={preferences.backup_enabled} onChange={value => void update({ backup_enabled: value })} />
        <label className="text-ab-footnote">{t('backup.interval')}
          <select className="ab-select ml-2 !w-auto" value={preferences.backup_interval_hours} onChange={e => void update({ backup_interval_hours: Number(e.target.value) })}>
            {[1, 24, 168].map(hours => <option key={hours} value={hours}>{hours}</option>)}
          </select>
        </label>
        <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void run(async () => {
          const result = await api.createBackup(); await load(); setSelected(result.name); setPreview(null); setNotice(t('backup.created'))
        })}>{t(busy ? 'common.loading' : 'backup.create')}</button>
        <label className="ab-btn-secondary cursor-pointer">{t('backup.import')}
          <input type="file" accept=".abk" className="hidden" disabled={busy} onChange={event => {
            const file = event.target.files?.[0]; event.target.value = ''
            if (file) void run(async () => { setPreview(null); const result = await api.importBackup(file); await load(); setSelected(result.name); setPreview(result); setRestored(null) })
          }} />
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        <select aria-label={t('backup.select')} className="ab-select flex-1" value={selected} disabled={busy} onChange={e => { setSelected(e.target.value); setPreview(null); setRestored(null) }}>
          <option value="">{t('backup.select')}</option>
          {items.map(item => <option key={item.name} value={item.name}>{item.created_at.slice(0, 19).replace('T', ' ')} · {item.name}</option>)}
        </select>
        <button type="button" className="ab-btn-secondary" disabled={busy || !selected} onClick={() => void run(async () => { setPreview(null); setPreview(await api.previewBackup(selected)) })}>{t('backup.preview')}</button>
        {selected ? <a className="ab-btn-secondary" href={`/api/backups/${encodeURIComponent(selected)}/download`} download>{t('backup.download')}</a> : null}
        <button type="button" className="ab-btn-secondary" disabled={busy} onClick={() => void run(load)}>{t('common.retry')}</button>
      </div>
      {preview ? <div className="space-y-2 rounded-ab-sm bg-surface-2 px-3 py-2 text-ab-footnote">
        <p>{t('backup.contents', { records: preview.summary.transactions ?? 0, accounts: preview.summary.accounts ?? 0, files: preview.attachment_files ?? 0 })}</p>
        <p>{t('backup.settings', { theme: preview.settings?.theme ?? '', language: preview.settings?.language ?? '' })}</p>
        <p>{t('backup.restoreNote')}</p>
        <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void run(async () => { setRestored(await api.restoreBackup(preview.name)); setNotice(t('backup.restored')) })}>{t('backup.restore')}</button>
      </div> : null}
      {restored ? <div className="space-y-2 text-ab-footnote">
        <p className="ab-selectable break-all">{restored.directory}</p>
        <button type="button" className="ab-btn-secondary" disabled={busy} onClick={() => void run(async () => { await api.openRestored(restored.name) })}>{t('backup.open')}</button>
      </div> : null}
      {error ? <p role="alert" className="text-ab-footnote text-negative">{error}</p> : null}
      {notice ? <p role="status" className="text-ab-footnote text-positive">{notice}</p> : null}
    </div>
  </Card>
}
