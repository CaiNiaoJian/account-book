/**
 * 日历热力图与当日详情（P2 / REQ-20）。
 *
 * 为什么是"GitHub 式"而不是月历
 * ----------------------------
 * 月历回答"这个月哪天花得多"；年历热力图回答**"我这一年记账坚持得怎么样、
 * 哪些时段是支出高峰"**。后者需要一次看到 371 天，月历做不到。
 * 两者不是替代关系，所以流水页保留了月视图，这一页专做年视图。
 *
 * 颜色的分级由**服务端**给出（分位数），前端只负责映射到色板。
 * 这样日历、图例与导出的 PNG 一定是同一套深浅标准 ——
 * 如果前端自己定阈值，改一次图例就会和格子对不上，而且没人会发现。
 */

import { AnimatePresence, motion } from 'framer-motion'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { Chart } from '@/components/Chart'
import { Icon } from '@/components/Icon'
import { Card, EmptyState, Kbd, Skeleton } from '@/components/ui'
import { useI18n } from '@/i18n'
import {
  api,
  type CalendarDay,
  type CalendarMetric,
  type DayDetail,
  type DayEvent,
} from '@/lib/api'
import { displayMinor, formatDayLabel, weekdayShort } from '@/lib/format'
import { resolveToken } from '@/design/tokens'
import { usePreferences } from '@/app/preferences'

import { Modal, MoneyText } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

/** 指标 → 色板（0..4 档）。用设计令牌而不是写死色值，日夜主题各自校准 */
const METRIC_PALETTE: Record<CalendarMetric, string[]> = {
  entry: ['hairline', 'accent', 'accent'],
  expense: ['hairline', 'negative', 'negative', 'negative', 'negative'],
  income: ['hairline', 'positive', 'positive', 'positive', 'positive'],
  net: ['hairline', 'positive', 'positive', 'negative', 'negative'],
  net_worth_change: ['hairline', 'teal', 'teal', 'teal', 'teal'],
  anomaly: ['hairline', 'warning', 'warning', 'warning', 'warning'],
}

/** 每档的不透明度。0 档最淡（几乎只是"有格子"），4 档最实 */
const LEVEL_ALPHA = [0.06, 0.26, 0.46, 0.7, 1]

function cellColor(metric: CalendarMetric, level: number): string {
  const token = METRIC_PALETTE[metric][Math.min(level, 4)] ?? 'hairline'
  const alpha = LEVEL_ALPHA[Math.min(level, 4)] ?? 0.1
  return level === 0 ? `rgb(var(--ab-hairline) / ${alpha})` : `rgb(var(--ab-${token}) / ${alpha})`
}

export function CalendarPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { categoryById } = useLedger()

  const [year, setYear] = useState(() => new Date().getFullYear())
  const [metric, setMetric] = useState<CalendarMetric>('entry')
  const [days, setDays] = useState<CalendarDay[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [hovered, setHovered] = useState<CalendarDay | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<DayDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [eventDialogOpen, setEventDialogOpen] = useState(false)
  const [eventForm, setEventForm] = useState({ kind: 'event', title: '', body: '' })
  const [busy, setBusy] = useState(false)
  const gridRef = useRef<HTMLDivElement>(null)
  // `?day=YYYY-MM-DD` 直接打开某天的详情。
  // 这不只是为了截图：它让"某一天"成为可分享、可加书签的链接，
  // 也让将来从流水页/搜索结果跳到具体某天变得简单。
  const [searchParams, setSearchParams] = useSearchParams()
  const dayParam = searchParams.get('day')

  const range = useMemo(() => ({ start: `${year}-01-01`, end: `${year}-12-31` }), [year])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const response = await api.calendar(range.start, range.end, metric)
      setDays(response.days)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [range, metric])

  useEffect(() => {
    void load()
  }, [load])

  const loadDetail = useCallback(
    async (day: string) => {
      setSelected(day)
      setDetailLoading(true)
      try {
        setDetail(await api.dayDetail(day))
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause))
        setDetail(null)
      } finally {
        setDetailLoading(false)
      }
    },
    [],
  )

  // 首次进入时若带了 ?day= 就自动展开那一天；year 也跟着跳过去，
  // 否则用户会看到"面板打开了但格子在别的年份"
  useEffect(() => {
    if (!dayParam || selected === dayParam) return
    const target = Number(dayParam.slice(0, 4))
    if (Number.isFinite(target) && target !== year) {
      setYear(target)
      return
    }
    void loadDetail(dayParam)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dayParam, year])

  // 键盘左右切换年份 —— 看长期趋势时会连续翻好几年
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA') return
      if (event.key === 'ArrowLeft') setYear((value) => value - 1)
      if (event.key === 'ArrowRight') setYear((value) => value + 1)
      if (event.key === 'Escape') setSelected(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  /** 把 371 天排成 53 列 × 7 行（列 = 周，行 = 星期） */
  const weeks = useMemo(() => {
    const firstDay = days[0]
    if (!firstDay) return [] as (CalendarDay | null)[][]
    const first = new Date(`${firstDay.date}T00:00:00`)
    // 让第一列从所在周的周日开始，网格才不会错位
    const leading = first.getDay()
    const cells: (CalendarDay | null)[] = [...Array.from({ length: leading }, () => null), ...days]
    while (cells.length % 7 !== 0) cells.push(null)
    const columns: (CalendarDay | null)[][] = []
    for (let index = 0; index < cells.length; index += 7) columns.push(cells.slice(index, index + 7))
    return columns
  }, [days])

  /** 月度标签：每列对应的月份变化时打一个标签 */
  const monthLabels = useMemo(() => {
    const labels: { index: number; month: number }[] = []
    let last = -1
    weeks.forEach((column, index) => {
      const sample = column.find((cell) => cell !== null)
      if (!sample) return
      const month = Number(sample.date.slice(5, 7))
      if (month !== last) {
        labels.push({ index, month })
        last = month
      }
    })
    return labels
  }, [weeks])

  const yearly = useMemo(() => {
    const total = days.reduce(
      (accumulator, day) => {
        accumulator.income += day.income_minor
        accumulator.expense += day.expense_minor
        if (day.tx_count > 0) accumulator.logged += 1
        if (day.entry_state === 'confirmed') accumulator.confirmed += 1
        accumulator.anomalies += day.anomaly_score >= 0.5 ? 1 : 0
        return accumulator
      },
      { income: 0, expense: 0, logged: 0, confirmed: 0, anomalies: 0 },
    )
    return total
  }, [days])

  /** 导出 PNG：自己画到 canvas 上，不引第三方截图库 */
  const exportPng = () => {
    const cell = 11
    const gap = 3
    const left = 34
    const top = 26
    const width = left + weeks.length * (cell + gap) + 16
    const height = top + 7 * (cell + gap) + 34
    const canvas = document.createElement('canvas')
    const scale = window.devicePixelRatio || 1
    canvas.width = width * scale
    canvas.height = height * scale
    const context = canvas.getContext('2d')
    if (!context) return
    context.scale(scale, scale)

    context.fillStyle = resolveToken('surface')
    context.fillRect(0, 0, width, height)
    context.fillStyle = resolveToken('text')
    context.font = '600 13px -apple-system, "PingFang SC", "Microsoft YaHei UI", sans-serif'
    context.fillText(`${year} · ${t(`calendar.metric.${metric}`)}`, 12, 17)

    context.font = '10px -apple-system, "PingFang SC", "Microsoft YaHei UI", sans-serif'
    context.fillStyle = resolveToken('text-3')
    for (const label of monthLabels) {
      context.fillText(`${label.month}月`, left + label.index * (cell + gap), top - 6)
    }
    for (let row = 0; row < 7; row += 2) {
      context.fillText(weekdayShort(new Date(2024, 0, 7 + row)), 8, top + row * (cell + gap) + cell - 1)
    }

    weeks.forEach((column, columnIndex) => {
      column.forEach((dayData, rowIndex) => {
        const x = left + columnIndex * (cell + gap)
        const y = top + rowIndex * (cell + gap)
        context.fillStyle = dayData
          ? cssColorToCanvas(cellColor(metric, dayData.level))
          : 'rgb(0 0 0 / 0)'
        if (!dayData) return
        // 圆角矩形：与界面一致的 3px 视觉语言
        const radius = 2.5
        context.beginPath()
        context.moveTo(x + radius, y)
        context.arcTo(x + cell, y, x + cell, y + cell, radius)
        context.arcTo(x + cell, y + cell, x, y + cell, radius)
        context.arcTo(x, y + cell, x, y, radius)
        context.arcTo(x, y, x + cell, y, radius)
        context.closePath()
        context.fill()
      })
    })

    context.fillStyle = resolveToken('text-3')
    context.font = '10px -apple-system, "PingFang SC", sans-serif'
    // 导出件也遵守隐私模式：PNG 是会被发出去的文件，遮罩与否应按用户设置来
    context.fillText(
      t('calendar.exportFooter', {
        logged: yearly.logged,
        expense: displayMinor(yearly.expense, 'CNY', preferences.privacy_mode),
      }),
      12,
      height - 12,
    )

    const link = document.createElement('a')
    link.download = `accountbook-calendar-${year}.png`
    link.href = canvas.toDataURL('image/png')
    link.click()
  }

  const selectedDay = days.find((day) => day.date === selected) ?? null

  return (
    <div className="space-y-4">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1">
          <button
            type="button"
            className="ab-icon-btn"
            aria-label={t('calendar.previousYear')}
            onClick={() => setYear((value) => value - 1)}
          >
            <Icon name="chevronRight" size={15} className="rotate-180" />
          </button>
          <span className="ab-tnum min-w-[62px] text-center text-ab-callout font-semibold text-label">
            {year}
          </span>
          <button
            type="button"
            className="ab-icon-btn"
            aria-label={t('calendar.nextYear')}
            onClick={() => setYear((value) => value + 1)}
          >
            <Icon name="chevronRight" size={15} />
          </button>
          <Kbd>←</Kbd>
          <Kbd>→</Kbd>
        </div>

        <select
          className="ab-select w-auto"
          value={metric}
          onChange={(event) => setMetric(event.target.value as CalendarMetric)}
          aria-label={t('calendar.metricLabel')}
        >
          {(
            ['entry', 'expense', 'income', 'net', 'net_worth_change', 'anomaly'] as CalendarMetric[]
          ).map((item) => (
            <option key={item} value={item}>
              {t(`calendar.metric.${item}`)}
            </option>
          ))}
        </select>

        <span className="ab-tnum ml-auto text-ab-caption text-label-3">
          {t('calendar.yearSummary', {
            logged: yearly.logged,
            expense: displayMinor(yearly.expense, 'CNY', preferences.privacy_mode),
          })}
        </span>
        <button type="button" className="ab-btn-secondary" onClick={exportPng}>
          <Icon name="download" size={14} />
          {t('calendar.exportPng')}
        </button>
      </div>

      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
      ) : null}

      {/* 热力图 */}
      <Card>
        {loading ? (
          <Skeleton className="h-32 w-full" />
        ) : (
          <div className="flex gap-3">
            {/* 星期标签 */}
            {/* 星期标签必须与网格**逐行对齐**。之前用 flex justify-between，
                标签是按容器高度（含下方图例）分布的，于是"一/三/五"落到格子行之间 ——
                这类错位不报错、不影响功能，但会让整张图显得不专业。
                现在用同样的 7 行 × 11px + 3px 间距，并补上与月份标签等高的上边距。 */}
            <div
              className="mt-[16px] text-ab-caption2 text-label-3"
              style={{ display: 'grid', gridTemplateRows: 'repeat(7, 11px)', gap: 3 }}
            >
              {[0, 1, 2, 3, 4, 5, 6].map((row) => (
                <span key={row} className="leading-[11px]">
                  {row % 2 === 1 ? weekdayShort(new Date(2024, 0, 7 + row)) : ''}
                </span>
              ))}
            </div>

            <div className="min-w-0 flex-1 overflow-x-auto">
              <div ref={gridRef} className="relative inline-block">
                {/* 月份标签 */}
                <div className="relative mb-1 h-3">
                  {monthLabels.map((label) => (
                    <span
                      key={`${label.index}-${label.month}`}
                      className="absolute text-ab-caption2 text-label-3"
                      style={{ left: label.index * 14 }}
                    >
                      {label.month}月
                    </span>
                  ))}
                </div>
                <div className="flex gap-[3px]">
                  {weeks.map((column, columnIndex) => (
                    <div key={columnIndex} className="flex flex-col gap-[3px]">
                      {column.map((day, rowIndex) =>
                        day ? (
                          <button
                            key={day.date}
                            type="button"
                            onClick={() => {
                              setSearchParams({ day: day.date }, { replace: true })
                              void loadDetail(day.date)
                            }}
                            onMouseEnter={() => setHovered(day)}
                            onMouseLeave={() => setHovered(null)}
                            onFocus={() => setHovered(day)}
                            aria-label={`${day.date} ${t(`calendar.entry.${day.entry_state}`)}`}
                            className={`relative h-[11px] w-[11px] rounded-[3px] transition-transform hover:scale-[1.35] ${
                              selected === day.date ? 'ring-2 ring-accent' : ''
                            }`}
                            style={{ backgroundColor: cellColor(metric, day.level) }}
                          >
                            {/* 角标：一个 2px 的小点。比在格子上叠图标更能保持矩阵的整洁 */}
                            {day.badges.length > 0 ? (
                              <span
                                className="absolute -right-[1px] -top-[1px] h-[4px] w-[4px] rounded-full"
                                style={{
                                  backgroundColor:
                                    day.badges.includes('due') || day.badges.includes('bill')
                                      ? `rgb(var(--ab-warning))`
                                      : `rgb(var(--ab-info))`,
                                }}
                              />
                            ) : null}
                            {day.entry_state === 'confirmed' ? (
                              <span className="absolute inset-0 rounded-[3px] ring-1 ring-inset ring-white/45" />
                            ) : null}
                          </button>
                        ) : (
                          <span key={`pad-${columnIndex}-${rowIndex}`} className="h-[11px] w-[11px]" />
                        ),
                      )}
                    </div>
                  ))}
                </div>
              </div>

              {/* 图例 + 悬浮详情 */}
              <div className="mt-3 flex flex-wrap items-center gap-3 text-ab-caption2 text-label-3">
                <span>{t('calendar.legendLow')}</span>
                <span className="flex items-center gap-[3px]">
                  {[0, 1, 2, 3, 4].map((level) => (
                    <span
                      key={level}
                      className="h-[11px] w-[11px] rounded-[3px]"
                      style={{ backgroundColor: cellColor(metric, level) }}
                    />
                  ))}
                </span>
                <span>{t('calendar.legendHigh')}</span>
                {metric === 'entry' ? (
                  <span className="ml-2">
                    {t('calendar.entryLegend')}
                  </span>
                ) : null}
                {metric === 'anomaly' ? (
                  <span className="ml-2">{t('calendar.anomalyHint')}</span>
                ) : null}
              </div>

              <div className="mt-2 h-9 text-ab-footnote text-label-2">
                {hovered ? (
                  <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <span className="font-medium">{formatDayLabel(hovered.date)}</span>
                    <span className="ab-tnum">
                      {t('transactionType.income')}{' '}
                      <span className="text-positive">
                        {displayMinor(hovered.income_minor, 'CNY', preferences.privacy_mode)}
                      </span>
                    </span>
                    <span className="ab-tnum">
                      {t('transactionType.expense')}{' '}
                      <span className="text-negative">
                        {displayMinor(hovered.expense_minor, 'CNY', preferences.privacy_mode)}
                      </span>
                    </span>
                    <span className="ab-tnum text-label-3">
                      {t('calendar.txCount', { count: hovered.tx_count })} ·{' '}
                      {t(`calendar.entry.${hovered.entry_state}`)}
                    </span>
                    {hovered.top_category_name ? (
                      <span className="text-label-3">
                        {t('calendar.topCategory')} {hovered.top_category_name}
                      </span>
                    ) : null}
                    {hovered.badges.map((badge) => (
                      <span key={badge} className="ab-phase-badge">
                        {t(`calendar.badge.${badge}`)}
                      </span>
                    ))}
                  </span>
                ) : (
                  <span className="text-label-3">{t('calendar.hoverHint')}</span>
                )}
              </div>
            </div>
          </div>
        )}
      </Card>

      {/* 当日详情 */}
      <AnimatePresence>
        {selected ? (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 4 }}
            transition={{ duration: 0.2 }}
          >
            {detailLoading || !detail ? (
              <Skeleton className="h-64 w-full" />
            ) : (
              <DayPanel
                detail={detail}
                day={selectedDay}
                onClose={() => {
                  setSelected(null)
                  // 一并清掉 URL 参数，否则刷新后又会弹出来
                  searchParams.delete('day')
                  setSearchParams(searchParams, { replace: true })
                }}
                onChanged={() => void Promise.all([load(), loadDetail(detail.date)])}
                onAddEvent={() => {
                  setEventForm({ kind: 'event', title: '', body: '' })
                  setEventDialogOpen(true)
                }}
                categoryName={(id) => categoryById(id)?.name ?? ''}
              />
            )}
          </motion.div>
        ) : null}
      </AnimatePresence>

      {/* 新增事件 */}
      <Modal
        open={eventDialogOpen}
        title={t('calendar.newEvent')}
        onClose={() => setEventDialogOpen(false)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setEventDialogOpen(false)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="ab-btn-primary"
              disabled={busy || !eventForm.title.trim()}
              onClick={() => {
                if (!selected) return
                setBusy(true)
                void api
                  .createEvent({
                    date: selected,
                    kind: eventForm.kind,
                    title: eventForm.title.trim(),
                    body: eventForm.body,
                  })
                  .then(() => Promise.all([load(), loadDetail(selected)]))
                  .then(() => {
                    setEventDialogOpen(false)
                    setEventForm({ kind: 'event', title: '', body: '' })
                  })
                  .catch((cause: unknown) =>
                    setError(cause instanceof Error ? cause.message : String(cause)),
                  )
                  .finally(() => setBusy(false))
              }}
            >
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="ab-field-label" htmlFor="event-kind">
              {t('calendar.eventKind')}
            </label>
            <select
              id="event-kind"
              className="ab-select"
              value={eventForm.kind}
              onChange={(event) => setEventForm({ ...eventForm, kind: event.target.value })}
            >
              {['event', 'mood', 'anniversary', 'note', 'todo'].map((kind) => (
                <option key={kind} value={kind}>
                  {t(`calendar.kind.${kind}`)}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="ab-field-label" htmlFor="event-title">
              {t('calendar.eventTitle')}
            </label>
            <input
              id="event-title"
              className="ab-input"
              autoFocus
              value={eventForm.title}
              onChange={(event) => setEventForm({ ...eventForm, title: event.target.value })}
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="event-body">
              {t('ledger.note')}
            </label>
            <textarea
              id="event-body"
              className="ab-textarea"
              rows={3}
              value={eventForm.body}
              onChange={(event) => setEventForm({ ...eventForm, body: event.target.value })}
            />
          </div>
        </div>
      </Modal>
    </div>
  )
}

// -----------------------------------------------------------------------------
// 当日详情
// -----------------------------------------------------------------------------
function DayPanel({
  detail,
  day,
  onClose,
  onChanged,
  onAddEvent,
  categoryName: _categoryName,
}: {
  detail: DayDetail
  day: CalendarDay | null
  onClose: () => void
  onChanged: () => void
  onAddEvent: () => void
  categoryName: (id: number | null) => string
}) {
  const { t } = useI18n()
  const { preferences } = usePreferences()

  const stepOption = useMemo(() => {
    const points = detail.net_worth_series.map((point) => [
      point.at.slice(11, 16),
      point.net_worth_minor / 100,
    ])
    return {
      // 阶梯曲线：把"钱在某一刻变了"画成直角转折，而不是斜线过渡 ——
      // 斜线会暗示余额是连续变化的，那是错的
      series: [{ type: 'line', step: 'end', data: points, areaStyle: { opacity: 0.12 }, showSymbol: true, symbolSize: 4 }],
      xAxis: { type: 'category', boundaryGap: false },
      yAxis: { type: 'value', scale: true },
      grid: { left: 4, right: 12, top: 18, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis' },
    }
  }, [detail])

  const compositionOption = useMemo(
    () => ({
      series: [
        {
          type: 'pie',
          radius: ['58%', '82%'],
          center: ['50%', '52%'],
          // 环形图而不是饼图：中间的数字（当日支出）比扇形的角度更容易读
          label: { show: false },
          data: detail.composition.map((item) => ({
            name: item.category_name,
            value: item.amount_minor / 100,
          })),
        },
      ],
      tooltip: { trigger: 'item', valueFormatter: (value: number) => `¥${value.toFixed(2)}` },
    }),
    [detail],
  )

  const overAverage =
    detail.weekday_average_expense_minor > 0
      ? detail.stat.expense_minor / detail.weekday_average_expense_minor
      : null

  return (
    <div className="grid gap-4 xl:grid-cols-3">
      {/* 概览 + 构成 */}
      <Card
        title={formatDayLabel(detail.date)}
        action={
          <button type="button" className="ab-btn-ghost" onClick={onClose} aria-label={t('common.close')}>
            <Icon name="close" size={14} />
          </button>
        }
      >
        <div className="grid grid-cols-3 gap-2 text-center">
          <div>
            <div className="text-ab-caption text-label-3">{t('transactionType.income')}</div>
            <div className="ab-tnum text-ab-callout font-semibold text-positive">
              {displayMinor(detail.stat.income_minor, 'CNY', preferences.privacy_mode)}
            </div>
          </div>
          <div>
            <div className="text-ab-caption text-label-3">{t('transactionType.expense')}</div>
            <div className="ab-tnum text-ab-callout font-semibold text-negative">
              {displayMinor(detail.stat.expense_minor, 'CNY', preferences.privacy_mode)}
            </div>
          </div>
          <div>
            <div className="text-ab-caption text-label-3">{t('cards.dayChange')}</div>
            <div
              className={`ab-tnum text-ab-callout font-semibold ${
                detail.stat.net_worth_minor - detail.stat.opening_net_worth_minor < 0
                  ? 'text-negative'
                  : 'text-positive'
              }`}
            >
              {displayMinor(
                detail.stat.net_worth_minor - detail.stat.opening_net_worth_minor,
                'CNY',
                preferences.privacy_mode,
                { showSign: true },
              )}
            </div>
          </div>
        </div>

        <div className="mt-3 border-t border-separator/50 pt-3">
          <div className="text-ab-caption text-label-3">{t('cards.netWorthClose')}</div>
          <div className="ab-tnum text-ab-title3 font-semibold text-label">
            {displayMinor(detail.stat.net_worth_minor, 'CNY', preferences.privacy_mode)}
          </div>
          {overAverage !== null ? (
            <div className="mt-1 text-ab-caption1 text-label-3">
              {t('calendar.vsWeekdayAverage', {
                ratio: `${overAverage >= 1 ? '+' : ''}${((overAverage - 1) * 100).toFixed(0)}%`,
                average: displayMinor(detail.weekday_average_expense_minor, 'CNY', preferences.privacy_mode),
              })}
            </div>
          ) : null}
        </div>

        {detail.composition.length > 0 ? (
          <div className="mt-2">
            <Chart option={compositionOption} height={150} />
          </div>
        ) : null}

        <div className="mt-2 flex items-center justify-between">
          <span className="text-ab-caption text-label-3">{t(`calendar.entry.${detail.stat.entry_state}`)}</span>
          <button
            type="button"
            className="ab-chip"
            data-active={detail.stat.entry_state === 'confirmed'}
            onClick={() => {
              void api
                .confirmDay(detail.date, detail.stat.entry_state !== 'confirmed')
                .then(() => onChanged())
            }}
          >
            <Icon name="check" size={12} />
            {detail.stat.entry_state === 'confirmed' ? t('calendar.confirmed') : t('calendar.markConfirmed')}
          </button>
        </div>
      </Card>

      {/* 余额阶梯曲线 */}
      <Card title={t('calendar.intradayCurve')} className="xl:col-span-2">
        {detail.net_worth_series.length <= 1 ? (
          <EmptyState icon="transactions" title={t('calendar.noIntraday')} body={t('calendar.noIntradayBody')} />
        ) : (
          <>
            <Chart option={stepOption} height={190} />
            <p className="mt-1 text-ab-caption1 text-label-3">{t('calendar.intradayHint')}</p>
          </>
        )}
      </Card>

      {/* 变动归因 */}
      <Card title={t('calendar.attribution')} flush>
        {detail.contributions.length === 0 ? (
          <p className="px-3 py-5 text-center text-ab-footnote text-label-3">{t('calendar.noAttribution')}</p>
        ) : (
          detail.contributions.map((item) => (
            <div key={item.transaction_id} className="ab-row">
              <span className="flex-1 truncate text-ab-subhead text-label">{item.label}</span>
              {item.category_name ? (
                <span className="shrink-0 text-ab-caption text-label-3">{item.category_name}</span>
              ) : null}
              <MoneyText
                minor={item.delta_minor}
                currency="CNY"
                tone={item.delta_minor > 0 ? 'income' : 'expense'}
                showSign={item.delta_minor > 0}
              />
            </div>
          ))
        )}
        <p className="px-3 py-2 text-ab-caption1 text-label-3">{t('calendar.attributionHint')}</p>
      </Card>

      {/* 流水 */}
      <Card title={t('calendar.dayTransactions')} flush>
        {detail.transactions.length === 0 ? (
          <p className="px-3 py-5 text-center text-ab-footnote text-label-3">{t('calendar.noTransactions')}</p>
        ) : (
          detail.transactions.map((item) => (
            <div key={item.id} className="ab-row">
              <span className="ab-tnum w-11 shrink-0 text-ab-caption text-label-3">
                {item.occurred_at.slice(11, 16)}
              </span>
              <span className="flex-1 truncate text-ab-subhead text-label">
                {item.category_name || item.payee || t('ledger.uncategorized')}
              </span>
              <MoneyText
                minor={item.type === 'expense' ? -item.amount_minor : item.amount_minor}
                currency={item.currency}
                tone={item.type === 'income' ? 'income' : item.type === 'expense' ? 'expense' : 'neutral'}
              />
            </div>
          ))
        )}
      </Card>

      {/* 事件日志 */}
      <Card
        title={t('calendar.events')}
        action={
          <button type="button" className="ab-btn-ghost text-ab-footnote" onClick={onAddEvent}>
            <Icon name="plus" size={13} />
            {t('calendar.newEvent')}
          </button>
        }
        flush
      >
        {detail.events.length === 0 ? (
          <p className="px-3 py-5 text-center text-ab-footnote text-label-3">{t('calendar.noEvents')}</p>
        ) : (
          detail.events.map((event: DayEvent) => (
            <div key={event.id} className="ab-row group">
              <span className="shrink-0 text-ab-caption text-label-3">{t(`calendar.kind.${event.kind}`)}</span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-ab-subhead text-label">{event.title}</span>
                {event.body ? (
                  <span className="block truncate text-ab-caption text-label-3">{event.body}</span>
                ) : null}
              </span>
              <button
                type="button"
                className="ab-icon-btn opacity-0 transition-opacity group-hover:opacity-100 hover:text-negative"
                aria-label={t('common.delete')}
                onClick={() => {
                  void api.deleteEvent(event.id).then(() => onChanged())
                }}
              >
                <Icon name="trash" size={13} />
              </button>
            </div>
          ))
        )}
      </Card>

      {/* 未登记时的补录入入口 */}
      {day && day.tx_count === 0 ? (
        <Card className="xl:col-span-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-ab-subhead font-medium text-label">{t('calendar.backfillTitle')}</div>
              <p className="text-ab-footnote text-label-3">{t('calendar.backfillBody')}</p>
            </div>
            <a href="/transactions" className="ab-btn-primary">
              <Icon name="plus" size={14} />
              {t('ledger.quickAdd')}
            </a>
          </div>
        </Card>
      ) : null}
    </div>
  )
}

/** 把 `rgb(var(--x) / a)` 这类字符串转成 canvas 能用的颜色 */
function cssColorToCanvas(value: string): string {
  const match = value.match(/rgb\(var\(--ab-([a-z0-9-]+)\)\s*\/\s*([\d.]+)\)/)
  if (!match) return value
  const [, token, alpha] = match
  const resolved = resolveToken(token as never)
  if (resolved.startsWith('#')) {
    const hex = resolved.slice(1)
    const r = parseInt(hex.slice(0, 2), 16)
    const g = parseInt(hex.slice(2, 4), 16)
    const b = parseInt(hex.slice(4, 6), 16)
    return `rgba(${r}, ${g}, ${b}, ${alpha})`
  }
  return resolved
}
