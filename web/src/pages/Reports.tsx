/**
 * 报表（P5 / 需求 21）。
 *
 * 页面上的一切都由 `<ReportRenderer>` 从 `ReportDocument` 渲染 ——
 * 与 Markdown / HTML / PDF 三个导出走的是**同一份 Schema**。
 * 这个页面因此没有"报表长什么样"的知识，只有"怎么取数和怎么导出"。
 *
 * 关于 PNG：这里导出的是**摘要图**（KPI + 首要图表 + 页脚），
 * 而不是整份报告的长图。理由：整份报告可能有十几张表和图表，
 * 拼成一张长图既看不清也不便分享；摘要图才是会被真正转发的东西。
 * 完整报告应该导出 HTML 或 PDF。这一点在按钮文案里写清楚了。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Icon } from '@/components/Icon'
import { ReportRenderer } from '@/components/ReportRenderer'
import { Card, Skeleton } from '@/components/ui'
import { usePreferences } from '@/app/preferences'
import { resolveToken } from '@/design/tokens'
import { useI18n } from '@/i18n'
import { api, ApiError, type ReportDocument, type ReportKind } from '@/lib/api'
import { displayMinor, localDayKey } from '@/lib/format'

const KINDS: ReportKind[] = ['daily', 'weekly', 'monthly', 'yearly', 'custom']

function defaultCustomRange(): { start: string; end: string } {
  const today = new Date()
  return { start: localDayKey(new Date(today.getTime() - 29 * 86_400_000)), end: localDayKey(today) }
}

/**
 * 把报告画成一张摘要 PNG。
 *
 * 刻意用 Canvas 2D **从 block 数据直接画**，而不是把 DOM 截成图：
 * 截图方案要么引入一个新依赖（html2canvas 之类），要么依赖
 * `foreignObject` 序列化（跨浏览器行为不一致，且无法在无头环境验证）。
 * 直接用数据画，结果是确定的、可测的、零依赖。
 */
function drawSummary(document: ReportDocument, privacyMode: boolean): HTMLCanvasElement {
  const scale = 2
  const width = 900
  const height = 560
  const canvas = window.document.createElement('canvas')
  canvas.width = width * scale
  canvas.height = height * scale
  const ctx = canvas.getContext('2d')
  if (!ctx) return canvas
  ctx.scale(scale, scale)

  const bg = resolveToken('surface')
  const surface2 = resolveToken('surface-2')
  const label = resolveToken('text')
  const label3 = resolveToken('text-3')
  const accent = resolveToken('accent')
  const positive = resolveToken('positive')
  const negative = resolveToken('negative')
  const separator = resolveToken('separator')
  const font = '-apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif'

  ctx.fillStyle = bg
  ctx.fillRect(0, 0, width, height)

  // 标题
  ctx.fillStyle = label
  ctx.font = `600 26px ${font}`
  ctx.fillText(document.title, 40, 62)
  ctx.fillStyle = label3
  ctx.font = `13px ${font}`
  ctx.fillText(document.subtitle, 40, 88)
  ctx.fillText(
    `${document.period.start} — ${document.period.end}（${document.period.days} 天）`,
    40,
    110,
  )

  // KPI 行
  const kpis = document.kpis.slice(0, 4)
  const cardWidth = (width - 80 - (kpis.length - 1) * 14) / Math.max(1, kpis.length)
  kpis.forEach((item, index) => {
    const x = 40 + index * (cardWidth + 14)
    const y = 138
    ctx.fillStyle = surface2
    ctx.beginPath()
    ctx.roundRect(x, y, cardWidth, 84, 12)
    ctx.fill()
    ctx.fillStyle = label3
    ctx.font = `12px ${font}`
    ctx.fillText(item.label, x + 14, y + 26)
    ctx.fillStyle =
      item.tone === 'positive' ? positive : item.tone === 'negative' ? negative : label
    ctx.font = `600 24px ${font}`
    const value =
      item.kind === 'money'
        ? displayMinor(item.value_minor ?? 0, 'CNY', privacyMode)
        : item.kind === 'percent'
          ? `${((item.value ?? 0) * 100).toFixed(1)}%`
          : String(item.value ?? 0)
    ctx.fillText(value, x + 14, y + 58)
    if (item.delta_ratio !== null && item.delta_ratio !== undefined) {
      ctx.fillStyle = item.delta_ratio >= 0 ? positive : negative
      ctx.font = `11px ${font}`
      ctx.fillText(
        `${item.delta_label} ${item.delta_ratio >= 0 ? '+' : ''}${(item.delta_ratio * 100).toFixed(1)}%`,
        x + 14,
        y + 76,
      )
    }
  })

  // 首要柱状图（取第一个 chart 块里能画的）
  const chart = document.sections
    .flatMap((section) => section.blocks)
    .find((block) => block.type === 'chart' && (block.dataset?.points?.length ?? 0) > 1)
  const chartTop = 250
  if (chart) {
    const points = (chart.dataset?.points ?? []).slice(0, 24)
    const values = points.map((point) => Number(point.value_minor ?? point.value ?? 0))
    const max = Math.max(...values, 1)
    const areaWidth = width - 80
    const areaHeight = 190
    ctx.fillStyle = label3
    ctx.font = `12px ${font}`
    ctx.fillText(chart.title ?? '', 40, chartTop - 8)
    const slot = areaWidth / points.length
    values.forEach((value, index) => {
      const barHeight = Math.max(1, (areaHeight * Math.max(0, value)) / max)
      ctx.fillStyle = accent
      ctx.beginPath()
      ctx.roundRect(
        40 + index * slot + slot * 0.15,
        chartTop + areaHeight - barHeight,
        Math.max(1, slot * 0.7),
        barHeight,
        2,
      )
      ctx.fill()
    })
    ctx.strokeStyle = separator
    ctx.beginPath()
    ctx.moveTo(40, chartTop + areaHeight + 0.5)
    ctx.lineTo(width - 40, chartTop + areaHeight + 0.5)
    ctx.stroke()
    ctx.fillStyle = label3
    ctx.font = `11px ${font}`
    if (points[0]) {
      ctx.fillText(points[0].date ?? points[0].name ?? '', 40, chartTop + areaHeight + 18)
    }
    const last = points[points.length - 1]
    if (last) {
      const text = last.date ?? last.name ?? ''
      ctx.fillText(text, width - 40 - ctx.measureText(text).width, chartTop + areaHeight + 18)
    }
  }

  // 页脚
  ctx.fillStyle = label3
  ctx.font = `11px ${font}`
  ctx.fillText(
    `离线生成 · ${document.generated_at.replace('T', ' ')} · ${document.schema}`,
    40,
    height - 26,
  )
  return canvas
}

export function ReportsPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()

  const [kind, setKind] = useState<ReportKind>('monthly')
  const [custom, setCustom] = useState(defaultCustomRange)
  const [document, setDocument] = useState<ReportDocument | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState<string | null>(null)
  const articleRef = useRef<HTMLDivElement | null>(null)

  const params = useMemo(() => {
    if (kind === 'custom') return { kind, start: custom.start, end: custom.end }
    return { kind }
  }, [kind, custom])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setDocument(await api.buildReport(params))
      setError(null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
      setDocument(null)
    } finally {
      setLoading(false)
    }
  }, [params])

  useEffect(() => {
    void load()
  }, [load])

  /** 导出走浏览器的下载机制：几 MB 的 PDF 不该先在 JS 里被完整读进内存 */
  const download = (format: 'markdown' | 'html' | 'pdf') => {
    const url = api.reportExportUrl({ ...params, format })
    const link = window.document.createElement('a')
    link.href = url
    link.download = ''
    window.document.body.appendChild(link)
    link.click()
    link.remove()
    setNotice(t('reports.exportStarted', { format: format.toUpperCase() }))
  }

  const exportPng = () => {
    if (!document) return
    const canvas = drawSummary(document, Boolean(preferences.privacy_mode))
    canvas.toBlob((blob) => {
      if (!blob) return
      const url = URL.createObjectURL(blob)
      const link = window.document.createElement('a')
      link.href = url
      link.download = `report-${document.kind}-${document.period.start}-summary.png`
      link.click()
      URL.revokeObjectURL(url)
      setNotice(t('reports.exportStarted', { format: 'PNG' }))
    }, 'image/png')
  }

  return (
    <div className="space-y-4" ref={articleRef} data-print-root>
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('reports.title')}</h1>
        <div className="ml-auto flex flex-wrap items-center gap-1.5" data-print-hide>
          <button type="button" className="ab-btn-secondary" onClick={() => window.print()}>
            <Icon name="note" size={13} />
            {t('ledgerPage.print')}
          </button>
          <button type="button" className="ab-btn-secondary" onClick={() => download('markdown')}>
            MD
          </button>
          <button type="button" className="ab-btn-secondary" onClick={() => download('html')}>
            HTML
          </button>
          <button type="button" className="ab-btn-primary" onClick={() => download('pdf')}>
            PDF
          </button>
          <button
            type="button"
            className="ab-btn-secondary"
            title={t('reports.pngHint')}
            onClick={exportPng}
          >
            {t('reports.pngSummary')}
          </button>
        </div>
      </div>

      {/* 报告类型 */}
      <div className="flex flex-wrap items-center gap-2" data-print-hide>
        <div className="ab-segment">
          {KINDS.map((item) => (
            <button
              key={item}
              type="button"
              data-active={kind === item}
              onClick={() => setKind(item)}
            >
              {t(`reports.kind.${item}`)}
            </button>
          ))}
        </div>
        {kind === 'custom' ? (
          <div className="flex items-center gap-2">
            <input
              type="date"
              className="ab-input !w-auto"
              value={custom.start}
              onChange={(event) =>
                setCustom((previous) => ({ ...previous, start: event.target.value }))
              }
            />
            <span className="text-label-3">—</span>
            <input
              type="date"
              className="ab-input !w-auto"
              value={custom.end}
              onChange={(event) =>
                setCustom((previous) => ({ ...previous, end: event.target.value }))
              }
            />
          </div>
        ) : null}
      </div>

      {notice ? (
        <div className="rounded-ab-sm bg-positive/10 px-3 py-2 text-ab-footnote text-positive">
          {notice}
        </div>
      ) : null}
      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
          {error}
        </div>
      ) : null}

      {loading ? (
        <div className="space-y-3">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-56 w-full" />
        </div>
      ) : document ? (
        <ReportRenderer document={document} />
      ) : (
        <Card>
          <p className="py-6 text-center text-ab-footnote text-label-3">{t('reports.failed')}</p>
        </Card>
      )}
    </div>
  )
}
