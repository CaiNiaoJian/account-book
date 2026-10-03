/**
 * 回收站（P1 尾巴 T4）。
 *
 * 三个刻意的设计
 * --------------
 * 1. **先按类别分组看数量，再看具体条目**。用户来回收站通常是想找
 *    "我刚才删的那笔账"，而不是巡视所有类别 —— 因此左侧是带计数的类别，
 *    默认选中"流水"。
 * 2. **恢复是主操作，彻底删除是次操作且需要确认**。两者在视觉上明确分开：
 *    恢复是普通按钮，彻底删除是危险色并弹确认。
 * 3. **彻底删除被拒绝时把后端的原话显示出来**。后端会写明"被 budgets（1 条）
 *    引用"，这比界面自己编一句"删除失败"有用得多。
 */

import { useCallback, useEffect, useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { useI18n } from '@/i18n'
import { readSections, section } from '@/features/ledger/sections'
import { api, ApiError, type TrashItem, type TrashList, type TrashSummary } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

import { Modal } from '@/features/ledger/parts'

/** 默认选中的类别。流水是最常被误删的，因此放第一位 */
const DEFAULT_ENTITY = 'transactions'

export function TrashPage() {
  const { t } = useI18n()
  const [summary, setSummary] = useState<TrashSummary | null>(null)
  const [entity, setEntity] = useState(DEFAULT_ENTITY)
  const [list, setList] = useState<TrashList | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [pending, setPending] = useState<TrashItem | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (target: string) => {
    setLoading(true)
    try {
      await readSections([
        section(api.trashSummary, setSummary),
        section(() => api.trashList(target), setList),
      ], t('ledgerLoading.notLoaded'))
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(entity)
  }, [load, entity])

  const restore = async (item: TrashItem) => {
    setBusy(true)
    setError(null)
    try {
      await api.trashRestore(item.entity, item.id)
      setNotice(t('trash.restored', { title: item.title || `#${item.id}` }))
      await load(entity)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const purge = async () => {
    if (!pending) return
    setBusy(true)
    setError(null)
    try {
      await api.trashPurge(pending.entity, pending.id)
      setNotice(t('trash.purged', { title: pending.title || `#${pending.id}` }))
      setPending(null)
      await load(entity)
    } catch (cause) {
      // 后端的拒绝理由（被谁引用）比界面自己编的"删除失败"有用得多，
      // 因此原样显示 ApiError.detail
      setError(cause instanceof ApiError ? cause.detail : cause instanceof Error ? cause.message : String(cause))
      setPending(null)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('trash.title')}</h1>
        {summary ? (
          <span className="ab-tnum text-ab-caption text-label-3">
            {t('trash.totalCount', { count: summary.total })}
          </span>
        ) : null}
      </div>

      {notice ? (
        <div className="rounded-ab-sm bg-positive/10 px-3 py-2 text-ab-footnote text-positive">{notice}</div>
      ) : null}
      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-4">
        {/* 类别（带计数）。只列出有内容的类别，但至少保留当前选中项 ——
            否则恢复最后一条时左侧会突然空掉、用户失去上下文 */}
        <Card dense className="xl:col-span-1">
          <div className="ab-section-label !px-0 !pt-0">{t('trash.selectEntity')}</div>
          <div className="space-y-0.5">
            {(summary?.entities ?? []).map((item) => {
              const count = item.count
              if (count === 0 && item.entity !== entity) return null
              return (
                <button
                  key={item.entity}
                  type="button"
                  onClick={() => setEntity(item.entity)}
                  className={`ab-row w-full rounded-ab-sm px-2 text-left ${
                    entity === item.entity ? 'bg-accent/10 text-accent' : 'text-label-2'
                  }`}
                >
                  <span className="flex-1 truncate text-ab-footnote">
                    {t(`trash.entity.${item.entity}`)}
                  </span>
                  <span className="ab-tnum shrink-0 text-ab-caption text-label-3">{count}</span>
                </button>
              )
            })}
          </div>
        </Card>

        <div className="xl:col-span-3">
          {loading ? (
            <Skeleton className="h-64 w-full" />
          ) : !list || list.items.length === 0 ? (
            <Card flush>
              <EmptyState icon="trash" title={t('trash.empty')} body={t('trash.emptyBody')} />
            </Card>
          ) : (
            <Card flush>
              {list.items.map((item) => (
                <div key={`${item.entity}-${item.id}`} className="ab-row group">
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-ab-subhead text-label">
                      {item.title || `#${item.id}`}
                    </span>
                    <span className="block truncate text-ab-caption1 text-label-3">
                      {item.subtitle ? `${item.subtitle} · ` : ''}
                      {item.deleted_at
                        ? t('trash.deletedAt', { time: formatDateTime(item.deleted_at) })
                        : ''}
                    </span>
                  </span>
                  <button
                    type="button"
                    className="ab-btn-secondary shrink-0"
                    disabled={busy}
                    onClick={() => void restore(item)}
                  >
                    <Icon name="refresh" size={13} />
                    {t('trash.restore')}
                  </button>
                  <button
                    type="button"
                    className="ab-icon-btn shrink-0 hover:text-negative"
                    aria-label={t('trash.purge')}
                    onClick={() => setPending(item)}
                  >
                    <Icon name="trash" size={13} />
                  </button>
                </div>
              ))}
            </Card>
          )}
        </div>
      </div>

      <Modal
        open={pending !== null}
        title={t('trash.purgeConfirmTitle')}
        onClose={() => setPending(null)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setPending(null)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="ab-btn-primary !bg-negative"
              disabled={busy}
              onClick={() => void purge()}
            >
              <Icon name="trash" size={14} />
              {t('trash.purge')}
            </button>
          </>
        }
      >
        <p className="text-ab-footnote text-label-2">{t('trash.purgeConfirmBody')}</p>
        <p className="mt-2 text-ab-subhead font-medium text-label">{pending?.title || ''}</p>
      </Modal>
    </div>
  )
}
