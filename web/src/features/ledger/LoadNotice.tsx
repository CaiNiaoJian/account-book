import { useI18n } from '@/i18n'
import { useLedger } from './store'

export function LedgerLoadNotice({ inline = false }: { inline?: boolean }) {
  const { t } = useI18n()
  const { loadErrors, refreshing, retryFailed } = useLedger()
  const failed = Object.keys(loadErrors)
  if (failed.length === 0) return null
  return <div role="status" className={`${inline ? '' : 'mx-7 mt-4 '}flex flex-wrap items-center gap-3 rounded-ab-sm bg-warning/10 px-3 py-2 text-ab-footnote text-label-2`}>
    <div className="min-w-0 flex-1">
      <p>{t('ledgerLoading.failed', { items: failed.map((key) => t(`ledgerLoading.resources.${key}`)).join('、') })}</p>
      <p className="mt-1 text-ab-caption1 text-label-3">{t('ledgerLoading.preserved')}</p>
    </div>
    <button type="button" className="ab-btn-secondary" disabled={refreshing} onClick={() => void retryFailed()}>
      {t(refreshing ? 'ledgerLoading.retrying' : 'ledgerLoading.retry')}
    </button>
  </div>
}
