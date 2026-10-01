/**
 * 批量编辑流水（P1 尾巴 T1）。
 *
 * 一条核心规则：**只提交被显式改动过的字段**。
 *
 * 后端用 `model_fields_set` 区分"没传这个字段"（不动）与"传了 null"
 * （清空）。界面据此只把用户真正碰过的控件放进请求体 ——
 * 如果一股脑把所有字段都发过去，那些用户没打算动的字段会被一起清掉，
 * 而批量操作的后果用户看不清，这种错误很难被发现。
 *
 * 因此每个控件都有三态：未触碰 / 已设为某值 / 已设为清空。
 * 这里用一个 `touched` 集合记录"哪些字段被碰过"。
 */

import { useState } from 'react'

import { Icon } from '@/components/Icon'
import { useI18n } from '@/i18n'
import { api, ApiError, type BatchReport } from '@/lib/api'

import { CategoryPicker, Modal, TagPicker } from './parts'
import { useLedger } from './store'

interface BatchBarProps {
  ids: number[]
  open: boolean
  onClose: () => void
  /** 成功后由页面刷新列表并清空选择 */
  onDone: (report: BatchReport) => void
}

/** 可批量设置的清算状态 */
const STATUSES = ['pending', 'cleared', 'reconciled', 'void'] as const

export function BatchEditDialog({ ids, open, onClose, onDone }: BatchBarProps) {
  const { t } = useI18n()
  const { projects, members } = useLedger()

  /** 被用户碰过的字段 —— 只有这些会进请求体 */
  const [touched, setTouched] = useState<Set<string>>(new Set())
  const [categoryId, setCategoryId] = useState<number | null>(null)
  const [projectId, setProjectId] = useState<number | null>(null)
  const [memberId, setMemberId] = useState<number | null>(null)
  const [status, setStatus] = useState<string>('')
  const [addTagIds, setAddTagIds] = useState<number[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const touch = (name: string) => setTouched((previous) => new Set(previous).add(name))

  const submit = async () => {
    if (touched.size === 0 && addTagIds.length === 0) {
      setError(t('batch.nothingChanged'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = { ids }
      // 逐字段判断是否被碰过：这是"不清掉用户没打算动的字段"的关键
      if (touched.has('category')) payload.category_id = categoryId
      if (touched.has('project')) payload.project_id = projectId
      if (touched.has('member')) payload.member_id = memberId
      if (touched.has('status')) payload.status = status || null
      if (addTagIds.length > 0) payload.add_tag_ids = addTagIds

      const report = await api.batchUpdateTransactions(payload as never)
      onDone(report)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title={t('batch.title', { count: ids.length })}
      size="lg"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void submit()}>
            <Icon name="check" size={14} />
            {t('batch.apply')}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <p className="rounded-ab-sm bg-accent/8 px-3 py-2 text-ab-caption1 text-label-2">
          {t('batch.hint')}
        </p>
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
            {error}
          </div>
        ) : null}

        <div>
          <span className="ab-field-label">{t('batch.category')}</span>
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1">
              <CategoryPicker
                kind="expense"
                value={categoryId}
                onChange={(value) => {
                  setCategoryId(value)
                  touch('category')
                }}
              />
            </div>
            <button
              type="button"
              className="ab-chip shrink-0"
              data-active={touched.has('category') && categoryId === null}
              onClick={() => {
                setCategoryId(null)
                touch('category')
              }}
            >
              {t('batch.clear')}
            </button>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="ab-field-label" htmlFor="batch-project">
              {t('ledger.project')}
            </label>
            <select
              id="batch-project"
              className="ab-select"
              value={projectId ?? ''}
              onChange={(event) => {
                setProjectId(event.target.value === '' ? null : Number(event.target.value))
                touch('project')
              }}
            >
              <option value="">{t('batch.noChange')}</option>
              {projects.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="ab-field-label" htmlFor="batch-member">
              {t('ledger.member')}
            </label>
            <select
              id="batch-member"
              className="ab-select"
              value={memberId ?? ''}
              onChange={(event) => {
                setMemberId(event.target.value === '' ? null : Number(event.target.value))
                touch('member')
              }}
            >
              <option value="">{t('batch.noChange')}</option>
              {members.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <label className="ab-field-label" htmlFor="batch-status">
            {t('ledger.status')}
          </label>
          <select
            id="batch-status"
            className="ab-select"
            value={status}
            onChange={(event) => {
              setStatus(event.target.value)
              touch('status')
            }}
          >
            <option value="">{t('batch.noChange')}</option>
            {STATUSES.map((item) => (
              <option key={item} value={item}>
                {t(`ledger.statusName.${item}`)}
              </option>
            ))}
          </select>
        </div>

        <div>
          <span className="ab-field-label">{t('batch.addTags')}</span>
          {/* 追加而不是替换：批量打标签是常见需求，
              而"把这一批的标签全部换成某几个"几乎总是误操作 */}
          <TagPicker value={addTagIds} onChange={setAddTagIds} />
          <p className="mt-1 text-ab-caption1 text-label-3">{t('batch.addTagsHint')}</p>
        </div>
      </div>
    </Modal>
  )
}
