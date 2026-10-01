/**
 * 流水页（P1）—— 三种视图 + 筛选 + 搜索。
 *
 * 为什么是"三视图"而不是一个列表加排序
 * ------------------------------------
 * 记账的人会用三种不同的眼光看同一批数据：
 *   * **列表**：找某一笔、核对金额（最常用）；
 *   * **时间轴**：看"这一天怎么花的"（时间顺序与间隔本身就是信息）；
 *   * **日历**：看"这个月哪些天有记账、哪天花超了"（REQ-20 的登记状态）。
 * 把它们做成切换而不是三个页面，是因为切换成本必须接近零 ——
 * 一旦需要跳页，用户就不会去用另外两种视角了。
 *
 * 数据获取策略
 * ------------
 * 列表走分页（limit/offset），日历走 `/api/stats/calendar`（按天聚合）。
 * 前端**不做**聚合计算：统计口径必须与首页、报表完全一致，
 * 而唯一能保证一致的做法是让它们都来自后端的同一段代码。
 */

import { motion } from 'framer-motion'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Kbd, Skeleton } from '@/components/ui'
import { useVirtualWindow } from '@/components/VirtualList'
import { useI18n } from '@/i18n'
import {
  api,
  type CalendarDay as HeatCell,
  type Transaction,
  type TransactionFilter,
} from '@/lib/api'
import { displayMinor, formatTime, localDayKey, relativeDayLabel, weekdayShort } from '@/lib/format'
import { usePreferences } from '@/app/preferences'

import { CategoryBadge, MoneyText, Modal } from '@/features/ledger/parts'
import { BatchEditDialog } from '@/features/ledger/BatchEditDialog'
import { QuickAddDialog, TransactionEditor } from '@/features/ledger/TransactionForms'
import { useLedger } from '@/features/ledger/store'

type ViewMode = 'list' | 'timeline' | 'calendar'
type RangePreset = 'last7' | 'last30' | 'thisMonth' | 'lastMonth' | 'thisYear' | 'all'

const PAGE_SIZE = 40

/** 区间预设 → [起, 止]（本地日期字符串） */
function presetRange(preset: RangePreset): { start?: string; end?: string } {
  const today = new Date()
  const day = localDayKey(today)
  if (preset === 'all') return {}
  if (preset === 'last7' || preset === 'last30') {
    const from = new Date(today)
    from.setDate(from.getDate() - (preset === 'last7' ? 6 : 29))
    return { start: localDayKey(from), end: day }
  }
  if (preset === 'thisMonth') {
    const first = new Date(today.getFullYear(), today.getMonth(), 1)
    const last = new Date(today.getFullYear(), today.getMonth() + 1, 0)
    return { start: localDayKey(first), end: localDayKey(last) }
  }
  if (preset === 'lastMonth') {
    const first = new Date(today.getFullYear(), today.getMonth() - 1, 1)
    const last = new Date(today.getFullYear(), today.getMonth(), 0)
    return { start: localDayKey(first), end: localDayKey(last) }
  }
  return { start: `${today.getFullYear()}-01-01`, end: `${today.getFullYear()}-12-31` }
}

export function TransactionsPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { status, accounts, categoryById, tags, refresh } = useLedger()
  // 用 react-router 的 hook 而不是全局 location：后者在 SPA 里"恰好能用"，
  // 但一旦将来引入 basename 或内存路由就会悄悄失效
  const location = useLocation()
  const navigate = useNavigate()

  const [view, setView] = useState<ViewMode>('list')
  /**
   * 默认「近 30 天」而不是「本月」。
   *
   * 这是真实的可用性问题：每个月 1 日打开时，"本月"区间只覆盖一天，
   * 列表看起来空空如也，用户第一反应是"我的数据丢了"。
   * 而流水列表的核心用途是**找某一笔**，30 天的视野比日历月更合适；
   * "本月花了多少"由首页的 KPI 回答，那里用日历月才是对的。
   */
  const [preset, setPreset] = useState<RangePreset>('last30')
  const [keyword, setKeyword] = useState('')
  const [accountIds, setAccountIds] = useState<number[]>([])
  const [typeFilter, setTypeFilter] = useState<string[]>([])
  const [tagIds, setTagIds] = useState<number[]>([])
  const [includeTransfers, setIncludeTransfers] = useState(true)
  const [items, setItems] = useState<Transaction[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [calendar, setCalendar] = useState<HeatCell[]>([])
  const [monthCursor, setMonthCursor] = useState(() => {
    const today = new Date()
    return { year: today.getFullYear(), month: today.getMonth() }
  })
  const [editing, setEditing] = useState<Transaction | null>(null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [quickAddOpen, setQuickAddOpen] = useState(false)
  /**
   * 批量选择。用 Set 而不是数组：勾选/取消是热路径，
   * `includes` 在几百行时会变成明显的卡顿。
   */
  const [selection, setSelection] = useState<Set<number>>(new Set())
  const [batchOpen, setBatchOpen] = useState(false)
  const [selectMode, setSelectMode] = useState(false)
  const [batchNotice, setBatchNotice] = useState<string | null>(null)

  const toggleSelect = useCallback((id: number) => {
    setSelection((previous) => {
      const next = new Set(previous)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  /** 退出选择模式时清空选择：留着上一批的勾选是最容易造成误操作的残留 */
  const exitSelectMode = useCallback(() => {
    setSelectMode(false)
    setSelection(new Set())
  }, [])

  // `/quick-add` 这个入口此前是坏的：导航点进去只是到了流水页，
  // 对话框并不会打开。现在由页面读路径决定 —— 顺带让全局快捷键
  // 只需要一次 navigate 就能唤起快捷记账。
  const onQuickAddRoute = location.pathname === '/quick-add'
  useEffect(() => {
    if (onQuickAddRoute) setQuickAddOpen(true)
  }, [onQuickAddRoute])
  const [deleting, setDeleting] = useState<Transaction | null>(null)
  const [error, setError] = useState<string | null>(null)

  const range = useMemo(() => presetRange(preset), [preset])

  const filter = useMemo<TransactionFilter>(() => {
    const base: TransactionFilter = {
      start: range.start ? `${range.start}T00:00:00` : undefined,
      end: range.end ? `${range.end}T23:59:59` : undefined,
      keyword: keyword.trim() || undefined,
      account_ids: accountIds.length > 0 ? accountIds : undefined,
      tag_ids: tagIds.length > 0 ? tagIds : undefined,
      types: typeFilter.length > 0 ? typeFilter : undefined,
      limit: PAGE_SIZE,
      offset: 0,
    }
    // 未显式筛类型时，用"是否包含转账"控制；显式筛类型时以类型为准
    if (typeFilter.length === 0) base.include_transfers = includeTransfers
    return base
  }, [range, keyword, accountIds, tagIds, typeFilter, includeTransfers])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api.transactions(filter)
      setItems(result.items)
      setTotal(result.total)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [filter])

  useEffect(() => {
    void load()
  }, [load])

  // 日历视图只在需要时请求，且随月份变化重新拉取
  useEffect(() => {
    if (view !== 'calendar') return
    const first = new Date(monthCursor.year, monthCursor.month, 1)
    const last = new Date(monthCursor.year, monthCursor.month + 1, 0)
    void api
      .calendar(localDayKey(first), localDayKey(last), 'expense')
      .then((response) => setCalendar(response.days))
      .catch((cause: unknown) => setError(cause instanceof Error ? cause.message : String(cause)))
  }, [view, monthCursor, includeTransfers])

  const loadMore = async () => {
    setLoadingMore(true)
    try {
      const result = await api.transactions({ ...filter, offset: items.length })
      setItems((previous) => [...previous, ...result.items])
      setTotal(result.total)
    } finally {
      setLoadingMore(false)
    }
  }

  const remove = async () => {
    if (!deleting) return
    try {
      await api.deleteTransaction(deleting.id)
      setDeleting(null)
      await Promise.all([load(), refresh()])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setDeleting(null)
    }
  }

  /** 按天分组（列表视图用"日期分组"，而不是每行都重复日期） */
  const grouped = useMemo(() => {
    const buckets = new Map<string, Transaction[]>()
    for (const item of items) {
      const key = localDayKey(item.occurred_at)
      const bucket = buckets.get(key)
      if (bucket) bucket.push(item)
      else buckets.set(key, [item])
    }
    return [...buckets.entries()]
  }, [items])

  const dayTotals = useMemo(() => {
    const totals = new Map<string, { income: number; expense: number }>()
    for (const [key, bucket] of grouped) {
      let income = 0
      let expense = 0
      for (const item of bucket) {
        // 转账不计入当天收支 —— 与后端口径一致，否则日小计会与月统计对不上
        if (item.type === 'income') income += item.amount_minor
        else if (item.type === 'expense') expense += item.amount_minor
      }
      totals.set(key, { income, expense })
    }
    return totals
  }, [grouped])

  /**
   * 把「分组标题 + 若干行」**拍平**成一个条目数组，再交给虚拟滚动。
   *
   * 只对行做窗口而让标题照旧渲染是不行的：标题的高度就没进前缀和，
   * 滚动位置的偏移会随着标题数量累积。拍平之后每个条目都参与测量，
   * 偏移量才是准的。
   */
  const listEntries = useMemo(() => {
    const entries: (
      | { kind: 'header'; key: string; day: string; income: number; expense: number }
      | { kind: 'row'; key: string; item: Transaction }
    )[] = []
    for (const [day, bucket] of grouped) {
      const totals = dayTotals.get(day) ?? { income: 0, expense: 0 }
      entries.push({ kind: 'header', key: `h-${day}`, day, ...totals })
      for (const item of bucket) entries.push({ kind: 'row', key: `r-${item.id}`, item })
    }
    return entries
  }, [grouped, dayTotals])

  // 估计值取得比实际行高略小：宁可先渲染多几行，也不要出现空白间隙
  const listVw = useVirtualWindow(listEntries.length, { estimate: 52 })

  const rangeLabel = useMemo(() => {
    if (range.start && range.end) return `${range.start} — ${range.end}`
    return t('ledger.rangeAll')
  }, [range, t])

  // 键盘：n 新建、/ 聚焦搜索（桌面端高频操作，值得做成快捷键）
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const typing = target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA' || target?.tagName === 'SELECT'
      if (typing || event.metaKey || event.ctrlKey || event.altKey) return
      if (event.key === 'n') {
        event.preventDefault()
        setQuickAddOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const transferLike = (item: Transaction) => item.type === 'transfer' || item.type === 'adjust'

  return (
    <div className="space-y-3">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="ab-segment">
          {(['list', 'timeline', 'calendar'] as ViewMode[]).map((mode) => (
            <button key={mode} type="button" data-active={view === mode} onClick={() => setView(mode)}>
              {t(`ledger.view.${mode}`)}
            </button>
          ))}
        </div>

        <select
          className="ab-select w-auto"
          value={preset}
          onChange={(event) => setPreset(event.target.value as RangePreset)}
        >
          {(['last7', 'last30', 'thisMonth', 'lastMonth', 'thisYear', 'all'] as RangePreset[]).map((item) => (
            <option key={item} value={item}>
              {t(`ledger.range.${item}`)}
            </option>
          ))}
        </select>

        <div className="relative flex-1 min-w-[180px]">
          <Icon
            name="search"
            size={14}
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-label-3"
          />
          <input
            className="ab-input pl-8"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder={t('ledger.searchPlaceholder')}
            aria-label={t('common.search')}
          />
        </div>

        <button
          type="button"
          className="ab-chip"
          data-active={includeTransfers}
          onClick={() => setIncludeTransfers((value) => !value)}
          title={t('ledger.includeTransfersHint')}
        >
          <Icon name="recurring" size={12} />
          {t('ledger.includeTransfers')}
        </button>

        <button type="button" className="ab-btn-primary" onClick={() => setQuickAddOpen(true)}>
          <Icon name="plus" size={14} />
          {t('ledger.quickAdd')}
          <Kbd>N</Kbd>
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="ab-chip"
          data-active={selectMode}
          onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
        >
          <Icon name="check" size={12} />
          {selectMode ? t('batch.exit') : t('batch.enter')}
        </button>
        {selectMode ? (
          <button
            type="button"
            className="ab-chip"
            onClick={() => setSelection(new Set(items.map((item) => item.id)))}
          >
            {t('batch.selectAll')}
          </button>
        ) : null}
        {batchNotice ? <span className="text-ab-caption text-positive">{batchNotice}</span> : null}
      </div>

      {/* 账户筛选 */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-ab-caption text-label-3">{t('ledger.account')}</span>
        {accounts.map((account) => (
          <button
            key={account.id}
            type="button"
            className="ab-chip"
            data-active={accountIds.includes(account.id)}
            onClick={() =>
              setAccountIds((previous) =>
                previous.includes(account.id)
                  ? previous.filter((id) => id !== account.id)
                  : [...previous, account.id],
              )
            }
          >
            <Icon name={account.icon} size={11} />
            {account.name}
          </button>
        ))}
        {(['expense', 'income', 'transfer'] as const).map((type) => (
          <button
            key={type}
            type="button"
            className="ab-chip"
            data-active={typeFilter.includes(type)}
            onClick={() =>
              setTypeFilter((previous) =>
                previous.includes(type) ? previous.filter((item) => item !== type) : [...previous, type],
              )
            }
          >
            {t(`transactionType.${type}`)}
          </button>
        ))}
      </div>

      {/* 标签筛选：只在确实有标签时出现 —— 一个永远空的筛选栏只是噪声 */}
      {tags.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-ab-caption text-label-3">{t('ledger.tags')}</span>
          {tags.map((tag) => (
            <button
              key={tag.id}
              type="button"
              className="ab-chip"
              data-active={tagIds.includes(tag.id)}
              onClick={() =>
                setTagIds((previous) =>
                  previous.includes(tag.id)
                    ? previous.filter((id) => id !== tag.id)
                    : [...previous, tag.id],
                )
              }
            >
              <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: `rgb(var(--ab-${tag.color}))` }} />
              {tag.name}
            </button>
          ))}
        </div>
      ) : null}

      <div className="flex items-center justify-between text-ab-caption text-label-3">
        <span>{rangeLabel}</span>
        <span className="ab-tnum">{t('ledger.resultCount', { count: total })}</span>
      </div>

      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
      ) : null}

      {status === 'loading' || loading ? (
        <div className="space-y-2">
          {[0, 1, 2, 3, 4].map((key) => (
            <Skeleton key={key} className="h-14 w-full" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <Card flush>
          <EmptyState
            icon="transactions"
            title={t('ledger.noTransactionsTitle')}
            body={t('ledger.noTransactionsBody')}
            action={
              <button type="button" className="ab-btn-primary" onClick={() => setQuickAddOpen(true)}>
                <Icon name="plus" size={14} />
                {t('ledger.quickAdd')}
              </button>
            }
          />
        </Card>
      ) : view === 'calendar' ? (
        <CalendarView
          cursor={monthCursor}
          onCursorChange={setMonthCursor}
          days={calendar}
          preview={items}
          onPickDay={(day) => {
            setPreset('all')
            setKeyword('')
            setView('list')
            setMonthCursor({ year: Number(day.slice(0, 4)), month: Number(day.slice(5, 7)) - 1 })
          }}
        />
      ) : view === 'timeline' ? (
        <TimelineView
          items={items}
          categoryOf={(id) => categoryById(id)}
          onEdit={(item) => {
            setEditing(item)
            setEditorOpen(true)
          }}
          transferLike={transferLike}
        />
      ) : (
        <Card flush className="overflow-hidden">
          <div ref={listVw.attach} data-virtual-scroll className="max-h-[70vh] overflow-y-auto">
            <div style={{ paddingTop: listVw.paddingTop, paddingBottom: listVw.paddingBottom }}>
              {listEntries.slice(listVw.start, listVw.end).map((entry, offset) => (
                <div key={entry.key} ref={listVw.measure(listVw.start + offset)}>
                  {entry.kind === 'header' ? (
                    <div className="flex items-center justify-between border-b border-separator/50 bg-surface-2/40 px-3 py-1.5">
                      <span className="text-ab-footnote font-semibold text-label-2">
                        {relativeDayLabel(entry.day)}
                        <span className="ml-1.5 font-normal text-label-3">{weekdayShort(entry.day)}</span>
                      </span>
                      <span className="flex items-center gap-3 text-ab-caption">
                        {entry.income > 0 ? (
                          <span className="ab-tnum text-positive">
                            +{displayMinor(entry.income, 'CNY', preferences.privacy_mode, { showSymbol: false })}
                          </span>
                        ) : null}
                        {entry.expense > 0 ? (
                          <span className="ab-tnum text-negative">
                            -{displayMinor(entry.expense, 'CNY', preferences.privacy_mode, { showSymbol: false })}
                          </span>
                        ) : null}
                      </span>
                    </div>
                  ) : (
                    <TransactionRow
                      item={entry.item}
                      categoryName={categoryById(entry.item.category_id)?.name}
                      category={categoryById(entry.item.category_id)}
                      transferLike={transferLike(entry.item)}
                      onEdit={() => {
                        setEditing(entry.item)
                        setEditorOpen(true)
                      }}
                      onDelete={() => setDeleting(entry.item)}
                      selectable={selectMode}
                      selected={selection.has(entry.item.id)}
                      onToggleSelect={() => toggleSelect(entry.item.id)}
                    />
                  )}
                </div>
              ))}
            </div>
          </div>
        </Card>
      )}

      {items.length < total ? (
        <div className="flex justify-center">
          <button type="button" className="ab-btn-secondary" disabled={loadingMore} onClick={() => void loadMore()}>
            {loadingMore ? t('common.loading') : t('ledger.loadMore', { count: total - items.length })}
          </button>
        </div>
      ) : null}

      {selectMode ? (
        <div className="fixed inset-x-0 bottom-4 z-30 mx-auto flex w-fit max-w-[92vw] items-center gap-2 rounded-full bg-surface px-3 py-2 shadow-ab-4 ring-1 ring-separator">
          <span className="ab-tnum px-1 text-ab-footnote text-label-2">
            {t('batch.selected', { count: selection.size })}
          </span>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={selection.size === 0}
            onClick={() => setBatchOpen(true)}
          >
            <Icon name="edit" size={13} />
            {t('batch.edit')}
          </button>
          <button
            type="button"
            className="ab-btn-secondary !text-negative"
            disabled={selection.size === 0}
            onClick={() => {
              void api.batchDeleteTransactions([...selection]).then(async (report) => {
                setBatchNotice(t('batch.deleted', { count: report.count }))
                exitSelectMode()
                await refresh()
              })
            }}
          >
            <Icon name="trash" size={13} />
            {t('common.delete')}
          </button>
          <button type="button" className="ab-btn-ghost" onClick={exitSelectMode}>
            {t('batch.exit')}
          </button>
        </div>
      ) : null}

      <BatchEditDialog
        ids={[...selection]}
        open={batchOpen}
        onClose={() => setBatchOpen(false)}
        onDone={(report) => {
          setBatchOpen(false)
          exitSelectMode()
          setBatchNotice(
            report.skipped.length > 0
              ? t('batch.appliedWithSkipped', { count: report.count, skipped: report.skipped.length })
              : t('batch.applied', { count: report.count }),
          )
          void refresh()
        }}
      />

      <QuickAddDialog
        open={quickAddOpen}
        onClose={() => {
          setQuickAddOpen(false)
          // 关掉之后回到流水页：否则 URL 停在 /quick-add 上，
          // 刷新或前进后退会把对话框又弹出来
          if (onQuickAddRoute) navigate('/transactions', { replace: true })
        }}
        onSaved={() => void Promise.all([load(), refresh()])}
      />
      <TransactionEditor
        open={editorOpen}
        transaction={editing}
        onClose={() => {
          setEditorOpen(false)
          setEditing(null)
        }}
        onSaved={() => void Promise.all([load(), refresh()])}
      />

      <Modal
        open={deleting !== null}
        title={t('ledger.deleteTransactionTitle')}
        size="sm"
        onClose={() => setDeleting(null)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setDeleting(null)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="ab-btn-primary !bg-negative" onClick={() => void remove()}>
              {t('common.delete')}
            </button>
          </>
        }
      >
        <p className="text-ab-subhead text-label-2">{t('ledger.deleteTransactionBody')}</p>
        {deleting ? (
          <div className="mt-2 flex items-center justify-between rounded-ab-sm bg-surface-2/60 px-3 py-2">
            <span className="text-ab-footnote text-label-2">
              {categoryById(deleting.category_id)?.name || deleting.payee || t('ledger.uncategorized')}
            </span>
            <MoneyText minor={deleting.amount_minor} currency={deleting.currency} />
          </div>
        ) : null}
      </Modal>
    </div>
  )
}

// -----------------------------------------------------------------------------
// 列表行
// -----------------------------------------------------------------------------
interface RowProps {
  item: Transaction
  category: ReturnType<ReturnType<typeof useLedger>['categoryById']>
  categoryName?: string
  transferLike: boolean
  onEdit: () => void
  onDelete: () => void
  /** 批量选择模式：整行点击变为切换勾选，而不是打开编辑器 */
  selectable?: boolean
  selected?: boolean
  onToggleSelect?: () => void
}

function TransactionRow({
  item,
  category,
  transferLike,
  onEdit,
  onDelete,
  selectable = false,
  selected = false,
  onToggleSelect,
}: RowProps) {
  const { t } = useI18n()
  const { tagByName } = useLedger()
  const tone = item.type === 'income' ? 'income' : item.type === 'expense' ? 'expense' : 'neutral'
  const signed = item.type === 'expense' ? -item.amount_minor : item.amount_minor

  return (
    <motion.div
      // FLIP：批量删除、切换筛选导致列表增删时，行会平滑地移动而不是瞬移。
      // 只加 layout 不加 variants —— 后者需要父级驱动 initial/animate，
      // 而这个列表的父级是分组容器，没有那一层（卡片墙上踩过这个坑）。
      layout
      transition={{ duration: 0.2, ease: [0.32, 0.72, 0, 1] }}
      className={`ab-row group ${selectable ? 'cursor-pointer' : ''} ${selected ? 'bg-accent/8' : ''}`}
      {...(selectable
        ? {
            role: 'button',
            tabIndex: 0,
            onClick: () => onToggleSelect?.(),
            onKeyDown: (event: React.KeyboardEvent) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                onToggleSelect?.()
              }
            },
          }
        : {})}
    >
      {selectable ? (
        <span
          className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-[5px] border ${
            selected ? 'border-accent bg-accent text-white' : 'border-separator'
          }`}
          aria-hidden
        >
          {selected ? <Icon name="check" size={11} /> : null}
        </span>
      ) : null}
      <CategoryBadge category={category} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-ab-subhead font-medium text-label">
            {category?.name || item.payee || t('ledger.uncategorized')}
          </span>
          {item.status === 'pending' ? (
            <span className="ab-phase-badge">{t('transactionStatus.pending')}</span>
          ) : null}
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-ab-caption text-label-3">
          <span className="truncate">
            {[
              formatTime(item.occurred_at),
              item.payee && category?.name ? item.payee : '',
              transferLike
                ? `${item.account_name} → ${item.to_account_name || t('ledger.adjustTarget')}`
                : item.account_name,
              item.note,
            ]
              .filter(Boolean)
              .join(' · ')}
          </span>
          {/* 标签用自身颜色显示，与表单里的选中态一致 —— 用户才能一眼认出"这条是那次出差" */}
          {item.tags.map((name) => {
            const tag = tagByName(name)
            return (
              <span
                key={name}
                className="shrink-0 rounded-full px-1.5 py-[1px] text-ab-caption2 font-medium"
                style={
                  tag
                    ? {
                        backgroundColor: `rgb(var(--ab-${tag.color}) / 0.14)`,
                        color: `rgb(var(--ab-${tag.color}))`,
                      }
                    : undefined
                }
              >
                {name}
              </span>
            )
          })}
          {item.project_name ? (
            <span className="shrink-0 rounded-full bg-hairline/10 px-1.5 py-[1px] text-ab-caption2 text-label-2">
              {item.project_name}
            </span>
          ) : null}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <span className="ab-tnum text-ab-callout font-semibold">
          <MoneyText minor={signed} currency={item.currency} tone={tone} showSign={item.type === 'income'} />
        </span>
        <div className="flex items-center opacity-0 transition-opacity group-hover:opacity-100">
          <button type="button" className="ab-icon-btn" aria-label={t('common.edit')} onClick={onEdit}>
            <Icon name="edit" size={14} />
          </button>
          <button
            type="button"
            className="ab-icon-btn hover:text-negative"
            aria-label={t('common.delete')}
            onClick={onDelete}
          >
            <Icon name="trash" size={14} />
          </button>
        </div>
      </div>
    </motion.div>
  )
}

// -----------------------------------------------------------------------------
// 时间轴
// -----------------------------------------------------------------------------
function TimelineView({
  items,
  categoryOf,
  onEdit,
  transferLike,
}: {
  items: Transaction[]
  categoryOf: (id: number | null) => ReturnType<ReturnType<typeof useLedger>['categoryById']>
  onEdit: (item: Transaction) => void
  transferLike: (item: Transaction) => boolean
}) {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  /**
   * 虚拟滚动。
   *
   * 时间轴视图原本把**已加载的全部流水**一次渲染出来 —— 用户点几次
   * 「加载更多」之后 DOM 就没有上界了。这里只渲染可见区（加 overscan），
   * 滚动位置用一个撑高的占位块维持。
   *
   * 行高是**测量**出来的而不是写死的：带标签或备注的行更高，
   * 写死估计值会让滚动位置逐渐错位，而那种错位很容易被误认为"滚动卡了"。
   */
  const vw = useVirtualWindow(items.length, { estimate: 64 })

  return (
    <Card flush>
      <div ref={vw.attach} data-virtual-scroll className="relative max-h-[68vh] overflow-y-auto pl-6">
        {/* 竖线：时间轴的视觉锚点。它必须随内容滚动，因此在滚动容器**内部** */}
        <span className="absolute bottom-2 left-[9px] top-2 w-px bg-separator" aria-hidden />
        <div style={{ paddingTop: vw.paddingTop, paddingBottom: vw.paddingBottom }}>
          {items.slice(vw.start, vw.end).map((item, offset) => {
            const category = categoryOf(item.category_id)
            return (
              <div
                key={item.id}
                ref={vw.measure(vw.start + offset)}
                className="relative pb-3"
              >
                <span
                  className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full ring-2 ring-surface"
                  style={{
                    backgroundColor: `rgb(var(--ab-${category?.color ?? 'separator'}))`,
                  }}
                  aria-hidden
                />
                <button
                  type="button"
                  onClick={() => onEdit(item)}
                  className="w-full rounded-ab-sm px-2 py-1.5 text-left transition-colors hover:bg-hairline/5"
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="flex items-center gap-1.5 text-ab-footnote text-label-3">
                      <span className="ab-tnum">{formatTime(item.occurred_at)}</span>
                      <span>·</span>
                      <span>{relativeDayLabel(item.occurred_at)}</span>
                    </span>
                    <MoneyText
                      minor={item.type === 'expense' ? -item.amount_minor : item.amount_minor}
                      currency={item.currency}
                      tone={item.type === 'income' ? 'income' : item.type === 'expense' ? 'expense' : 'neutral'}
                    />
                  </div>
                  <div className="mt-0.5 flex items-center gap-2">
                    <CategoryBadge category={category} size={22} />
                    <span className="truncate text-ab-subhead font-medium text-label">
                      {category?.name || item.payee || t('ledger.uncategorized')}
                    </span>
                    <span className="truncate text-ab-caption text-label-3">
                      {transferLike(item)
                        ? `${item.account_name} → ${item.to_account_name || t('ledger.adjustTarget')}`
                        : item.note || item.payee}
                    </span>
                  </div>
                </button>
              </div>
            )
          })}
        </div>
      </div>
      {preferences.privacy_mode ? (
        <p className="mt-3 text-ab-caption1 text-label-3">{t('common.privacyModeOn')}</p>
      ) : null}
    </Card>
  )
}

// -----------------------------------------------------------------------------
// 日历
// -----------------------------------------------------------------------------
/** 空日历格：字段必须与后端 CalendarDay 完全一致，否则类型检查会（也应该）失败 */
function EMPTY_CELL(date: string): HeatCell {
  return {
    date,
    income_minor: 0,
    expense_minor: 0,
    net_minor: 0,
    net_worth_minor: 0,
    tx_count: 0,
    pending_count: 0,
    entry_state: 'none',
    anomaly_score: 0,
    top_category_id: null,
    top_category_name: '',
    event_count: 0,
    has_attachment: false,
    metric_value: 0,
    level: 0,
    badges: [],
  }
}
function CalendarView({
  cursor,
  onCursorChange,
  days,
  preview,
  onPickDay,
}: {
  cursor: { year: number; month: number }
  onCursorChange: (next: { year: number; month: number }) => void
  days: HeatCell[]
  preview: Transaction[]
  onPickDay: (day: string) => void
}) {
  const { t } = useI18n()
  const { preferences } = usePreferences()

  const byDay = useMemo(() => {
    const map = new Map<string, HeatCell>()
    for (const day of days) map.set(day.date, day)
    return map
  }, [days])

  // 日历需要前导空格：把 1 号对齐到正确的星期列
  const first = new Date(cursor.year, cursor.month, 1)
  const leading = first.getDay()
  const monthDays = new Date(cursor.year, cursor.month + 1, 0).getDate()
  const maxExpense = Math.max(1, ...days.map((day) => day.expense_minor))
  const todayKey = localDayKey()

  const cells: (HeatCell | null)[] = [
    ...Array.from({ length: leading }, () => null),
    ...Array.from({ length: monthDays }, (_, index) => {
      const key = localDayKey(new Date(cursor.year, cursor.month, index + 1))
      return byDay.get(key) ?? EMPTY_CELL(key)
    }),
  ]

  const monthSummary = days.reduce(
    (accumulator, day) => {
      accumulator.income += day.income_minor
      accumulator.expense += day.expense_minor
      if (day.tx_count > 0) accumulator.days += 1
      return accumulator
    },
    { income: 0, expense: 0, days: 0 },
  )

  return (
    <div className="space-y-3">
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-1">
            <button
              type="button"
              className="ab-icon-btn"
              aria-label={t('common.previousMonth')}
              onClick={() =>
                onCursorChange(
                  cursor.month === 0
                    ? { year: cursor.year - 1, month: 11 }
                    : { year: cursor.year, month: cursor.month - 1 },
                )
              }
            >
              <Icon name="chevronRight" size={15} className="rotate-180" />
            </button>
            <span className="ab-tnum min-w-[86px] text-center text-ab-callout font-semibold text-label">
              {cursor.year} 年 {cursor.month + 1} 月
            </span>
            <button
              type="button"
              className="ab-icon-btn"
              aria-label={t('common.nextMonth')}
              onClick={() =>
                onCursorChange(
                  cursor.month === 11
                    ? { year: cursor.year + 1, month: 0 }
                    : { year: cursor.year, month: cursor.month + 1 },
                )
              }
            >
              <Icon name="chevronRight" size={15} />
            </button>
          </div>
          <div className="flex items-center gap-4 text-ab-footnote">
            <span className="text-label-3">
              {t('ledger.entryDays', { count: monthSummary.days })}
            </span>
            <span className="ab-tnum text-positive">
              +{displayMinor(monthSummary.income, 'CNY', preferences.privacy_mode, { showSymbol: false })}
            </span>
            <span className="ab-tnum text-negative">
              -{displayMinor(monthSummary.expense, 'CNY', preferences.privacy_mode, { showSymbol: false })}
            </span>
          </div>
        </div>

        <div className="mt-3 grid grid-cols-7 gap-1">
          {['日', '一', '二', '三', '四', '五', '六'].map((label) => (
            <div key={label} className="pb-1 text-center text-ab-caption2 font-medium text-label-3">
              {label}
            </div>
          ))}
          {cells.map((cell, index) =>
            cell === null ? (
              <div key={`blank-${index}`} />
            ) : (
              <button
                key={cell.date}
                type="button"
                onClick={() => onPickDay(cell.date)}
                title={
                  cell.tx_count > 0
                    ? `${cell.date}：${cell.tx_count} 笔`
                    : `${cell.date}：${t('ledger.noEntry')}`
                }
                className={`relative flex aspect-square flex-col items-center justify-start rounded-ab-sm border p-1 text-left transition-all ${
                  cell.date === todayKey ? 'border-accent' : 'border-transparent'
                } hover:border-separator`}
                style={
                  cell.tx_count > 0
                    ? {
                        // 颜色深浅表示当天支出占比 —— 一眼能看出"哪天花超了"，
                        // 这正是日历视图相对于列表的全部价值
                        backgroundColor: `rgb(var(--ab-negative) / ${0.08 + 0.3 * (cell.expense_minor / maxExpense)})`,
                      }
                    : undefined
                }
              >
                <span
                  className={`ab-tnum text-ab-caption ${
                    cell.date === todayKey ? 'font-bold text-accent' : 'text-label-2'
                  }`}
                >
                  {Number(cell.date.slice(8, 10))}
                </span>
                {cell.tx_count > 0 ? (
                  <span className="ab-tnum mt-auto text-[9px] leading-tight text-label-2">
                    {displayMinor(cell.expense_minor, 'CNY', preferences.privacy_mode, { showSymbol: false })}
                  </span>
                ) : null}
              </button>
            ),
          )}
        </div>
        <p className="mt-2 text-ab-caption1 text-label-3">{t('ledger.calendarHint')}</p>
      </Card>

      {/* 当月最近几笔，作为日历的"脚注"，避免用户必须切回列表才能看到明细 */}
      {preview.length > 0 ? (
        <Card flush className="overflow-hidden">
          {preview.slice(0, 5).map((item) => (
            <div key={item.id} className="ab-row">
              <span className="ab-tnum w-24 shrink-0 text-ab-caption text-label-3">
                {item.occurred_at.slice(5, 10)}
              </span>
              <span className="flex-1 truncate text-ab-subhead text-label">
                {item.payee || item.note || t('ledger.uncategorized')}
              </span>
              <MoneyText minor={item.amount_minor} currency={item.currency} tone="neutral" />
            </div>
          ))}
        </Card>
      ) : null}
    </div>
  )
}
