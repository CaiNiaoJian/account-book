/**
 * 报告渲染器（P5）。
 *
 * 这是"一份 Schema，多处消费"的**第四个**消费者（前三个是
 * Markdown / HTML / PDF 导出）。因此它有一条硬性约束：
 *
 *   **只读 block 结构，不出现 `if (section.key === 'breakdown')` 这类分支。**
 *
 * 一旦这里开始按"是哪一节"来分支，新增一节就要改四个地方，
 * 而"四个地方都记得改"是不可能长期成立的。块（block）类型是唯一的扩展点。
 *
 * 图表由**前端**把中性数据集翻译成 ECharts 配置：
 * 后端只给 `{chart: 'bar', dataset: {points}, unit}`。
 * 这样 schema 不会绑上某个图表库的版本，导出侧也不需要"读懂 ECharts"。
 * 注意 ECharts 用的是 canvas，**不认 CSS 变量**，因此颜色必须经 `resolveToken()`
 * 取成实际值（这个坑在 P2 时踩过：`rgb(var(--ab-x))` 传进去只会得到黑色）。
 */

import { useMemo } from 'react'

import { Chart } from '@/components/Chart'
import { Icon } from '@/components/Icon'
import { Card } from '@/components/ui'
import { usePreferences } from '@/app/preferences'
import { resolveToken } from '@/design/tokens'
import { useI18n } from '@/i18n'
import type { ReportBlock, ReportDocument, ReportInsight, ReportMetric } from '@/lib/api'
import { displayMinor, formatDayLabel } from '@/lib/format'

/** 洞察等级 → 图标与色调 */
const INSIGHT_STYLE: Record<string, { icon: 'check' | 'note' | 'warning' | 'close'; tone: string }> = {
  good: { icon: 'check', tone: 'text-positive' },
  info: { icon: 'note', tone: 'text-accent' },
  warn: { icon: 'warning', tone: 'text-warning' },
  critical: { icon: 'warning', tone: 'text-negative' },
}

function useValueFormatter(unit: string | undefined) {
  const { preferences } = usePreferences()
  return useMemo(() => {
    return (value: unknown): string => {
      if (value === null || value === undefined || value === '') return '—'
      if (unit === 'money') return displayMinor(Number(value), 'CNY', preferences.privacy_mode)
      if (unit === 'percent') return `${(Number(value) * 100).toFixed(1)}%`
      return String(value)
    }
  }, [unit, preferences.privacy_mode])
}

/** 中性数据集 → ECharts 配置。**这是图表唯一的翻译点。** */
function chartOption(block: ReportBlock, formatValue: (value: unknown) => string) {
  const points = block.dataset?.points ?? []
  const labels = points.map((point, index) => point.date ?? point.name ?? String(index + 1))
  const values = points.map((point) => Number(point.value_minor ?? point.value ?? 0))
  const axisColor = resolveToken('text-3')
  const splitColor = resolveToken('separator')
  const accent = resolveToken('accent')
  const palette = [
    resolveToken('accent'),
    resolveToken('positive'),
    resolveToken('warning'),
    resolveToken('negative'),
    resolveToken('blue'),
    resolveToken('brown'),
  ]

  if (block.chart === 'pie') {
    return {
      color: palette,
      tooltip: { trigger: 'item', valueFormatter: (value: number) => formatValue(value) },
      legend: { type: 'scroll', bottom: 0, textStyle: { color: axisColor, fontSize: 11 } },
      series: [
        {
          type: 'pie',
          radius: ['40%', '68%'],
          center: ['50%', '44%'],
          itemStyle: { borderWidth: 0 },
          label: { show: false },
          data: points.map((point, index) => ({
            name: point.name ?? labels[index],
            value: Number(point.value_minor ?? point.value ?? 0),
          })),
        },
      ],
    }
  }

  const isLine = block.chart === 'line'
  return {
    grid: { left: 8, right: 16, top: 16, bottom: 6, containLabel: true },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value: number) => formatValue(value),
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: { color: axisColor, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: splitColor } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: axisColor, fontSize: 10, formatter: (value: number) => formatValue(value) },
      splitLine: { lineStyle: { color: splitColor, type: 'dashed' } },
    },
    series: [
      isLine
        ? {
            type: 'line',
            data: values,
            smooth: true,
            symbol: 'none',
            lineStyle: { color: accent, width: 2 },
            areaStyle: { color: accent, opacity: 0.08 },
          }
        : {
            type: 'bar',
            data: values,
            itemStyle: { color: accent, borderRadius: [3, 3, 0, 0] },
            barMaxWidth: 22,
          },
    ],
  }
}

function MetricCard({ item }: { item: ReportMetric }) {
  const { preferences } = usePreferences()
  const value =
    item.kind === 'money'
      ? displayMinor(item.value_minor ?? 0, 'CNY', preferences.privacy_mode)
      : item.kind === 'percent'
        ? `${((item.value ?? 0) * 100).toFixed(1)}%`
        : String(item.value ?? 0)
  const tone =
    item.tone === 'positive' ? 'text-positive' : item.tone === 'negative' ? 'text-negative' : 'text-label'
  const delta =
    item.delta_ratio === null || item.delta_ratio === undefined
      ? null
      : `${item.delta_ratio >= 0 ? '+' : ''}${(item.delta_ratio * 100).toFixed(1)}%`
  return (
    <div className="rounded-ab-sm border border-separator/70 bg-surface px-3 py-2">
      <div className="text-ab-caption1 text-label-3">{item.label}</div>
      <div className={`ab-tnum mt-0.5 text-ab-headline font-semibold ${tone}`}>{value}</div>
      <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-ab-caption2 text-label-3">
        {delta ? (
          <span
            className={`ab-tnum ${(item.delta_ratio ?? 0) >= 0 ? 'text-positive' : 'text-negative'}`}
          >
            {item.delta_label} {delta}
          </span>
        ) : null}
        {item.hint ? <span className="truncate">{item.hint}</span> : null}
      </div>
    </div>
  )
}

function DataTable({ block }: { block: ReportBlock }) {
  const { preferences } = usePreferences()
  const columns = block.columns ?? []
  const rows = block.rows ?? []

  const cell = (column: { key: string; format?: string }, row: Record<string, unknown>) => {
    const value = row[column.key]
    if (value === null || value === undefined || value === '') return '—'
    if (column.format === 'money') {
      return displayMinor(Number(value), 'CNY', preferences.privacy_mode)
    }
    if (column.format === 'percent') return `${(Number(value) * 100).toFixed(1)}%`
    return String(value)
  }

  return (
    <div className="overflow-x-auto">
      {block.title ? <div className="ab-section-label !px-0">{block.title}</div> : null}
      {rows.length === 0 ? (
        <p className="py-3 text-ab-footnote text-label-3">{block.empty || '无数据'}</p>
      ) : (
        <table className="w-full border-collapse text-ab-footnote">
          <thead>
            <tr>
              {columns.map((column) => (
                <th
                  key={column.key}
                  className={`border-b border-separator py-1.5 text-ab-caption1 font-medium text-label-3 ${
                    column.align === 'right' ? 'text-right' : 'text-left'
                  }`}
                >
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={`ab-tnum border-b border-separator/50 py-1.5 text-label-2 ${
                      column.align === 'right' ? 'text-right' : 'text-left'
                    }`}
                  >
                    {cell(column, row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
          {block.footer ? (
            <tfoot>
              <tr>
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={`ab-tnum border-t border-separator py-1.5 font-semibold text-label ${
                      column.align === 'right' ? 'text-right' : 'text-left'
                    }`}
                  >
                    {cell(column, block.footer as Record<string, unknown>)}
                  </td>
                ))}
              </tr>
            </tfoot>
          ) : null}
        </table>
      )}
    </div>
  )
}

function Block({ block }: { block: ReportBlock }) {
  const formatValue = useValueFormatter(block.unit)
  if (block.type === 'text') {
    return <p className="text-ab-footnote text-label-2">{block.text}</p>
  }
  if (block.type === 'note') {
    return (
      <p className="border-l-2 border-separator pl-2.5 text-ab-caption1 text-label-3">{block.text}</p>
    )
  }
  if (block.type === 'unavailable') {
    return (
      <p className="rounded-ab-sm bg-warning/10 px-3 py-2 text-ab-footnote text-label-2">
        <Icon name="warning" size={13} className="mr-1 inline align-[-2px]" />
        {block.text}
      </p>
    )
  }
  if (block.type === 'metrics') {
    return (
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {(block.items ?? []).map((item) => (
          <MetricCard key={item.key} item={item} />
        ))}
      </div>
    )
  }
  if (block.type === 'table') {
    return <DataTable block={block} />
  }
  if (block.type === 'chart') {
    const points = block.dataset?.points ?? []
    return (
      <div>
        {block.title ? <div className="ab-section-label !px-0">{block.title}</div> : null}
        {points.length === 0 ? (
          <p className="py-3 text-ab-footnote text-label-3">无数据</p>
        ) : (
          <Chart
            option={chartOption(block, formatValue)}
            height={block.chart === 'pie' ? 220 : 180}
          />
        )}
      </div>
    )
  }
  return null
}

function InsightCard({ item }: { item: ReportInsight }) {
  const style = INSIGHT_STYLE[item.level] ?? INSIGHT_STYLE.info!
  return (
    <div className="rounded-ab-sm border border-separator/70 bg-surface px-3 py-2">
      <div className={`flex items-start gap-1.5 text-ab-subhead font-medium ${style.tone}`}>
        <Icon name={style.icon} size={13} className="mt-0.5 shrink-0" />
        <span className="min-w-0">{item.title}</span>
      </div>
      <p className="mt-1 text-ab-footnote text-label-2">{item.detail}</p>
      {item.evidence.length > 0 ? (
        <ul className="mt-1 list-disc pl-4 text-ab-caption1 text-label-3">
          {item.evidence.map((text, index) => (
            <li key={index}>{text}</li>
          ))}
        </ul>
      ) : null}
      {item.suggestion ? (
        <p className="mt-1 text-ab-caption1 text-label-3">{t_suggestion(item.suggestion)}</p>
      ) : null}
    </div>
  )
}

/** 统一的建议前缀，避免每个渲染器各拼一遍文案 */
function t_suggestion(text: string): string {
  return `建议：${text}`
}

export function ReportRenderer({
  document,
  /** 只要其中几节（界面上"自定义报告"用得上） */
  only,
}: {
  document: ReportDocument
  only?: string[]
}) {
  const { t } = useI18n()
  const sections = only ? document.sections.filter((item) => only.includes(item.key)) : document.sections

  return (
    <article className="space-y-4">
      {/* 封面 */}
      <Card>
        <h1 className="text-ab-title2 font-semibold text-label">{document.title}</h1>
        <p className="mt-1 text-ab-subhead text-label-2">{document.cover.headline}</p>
        <p className="mt-1 text-ab-caption1 text-label-3">
          {t('reports.generatedAt', {
            time: document.generated_at.replace('T', ' '),
            schema: document.schema,
          })}
        </p>
        {document.cover.highlights.length > 0 ? (
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {document.cover.highlights.map((text, index) => (
              <li key={index} className="ab-chip">
                {text}
              </li>
            ))}
          </ul>
        ) : null}
      </Card>

      {/* 关键指标 */}
      {document.kpis.length > 0 ? (
        <Card>
          <div className="ab-section-label !px-0 !pt-0">{t('reports.kpis')}</div>
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            {document.kpis.map((item) => (
              <MetricCard key={item.key} item={item} />
            ))}
          </div>
        </Card>
      ) : null}

      {sections.map((section) => (
        <Card key={section.key}>
          <h2 className="text-ab-headline font-semibold text-label">{section.title}</h2>
          <div className="mt-3 space-y-3">
            {section.blocks.map((block, index) => (
              <Block key={index} block={block} />
            ))}
            {section.insights && section.insights.length > 0 ? (
              <div className="space-y-2">
                {section.insights.map((item) => (
                  <InsightCard key={item.key} item={item} />
                ))}
              </div>
            ) : null}
          </div>
        </Card>
      ))}

      {/* 口径说明 */}
      {document.notes.length > 0 ? (
        <Card>
          <div className="ab-section-label !px-0 !pt-0">{t('reports.notes')}</div>
          <ul className="space-y-1">
            {document.notes.map((note, index) => (
              <li key={index} className="text-ab-caption1 text-label-3">
                {note}
              </li>
            ))}
          </ul>
          {document.period ? (
            <p className="mt-2 text-ab-caption2 text-label-3">
              {formatDayLabel(document.period.start)} — {formatDayLabel(document.period.end)}
            </p>
          ) : null}
        </Card>
      ) : null}
    </article>
  )
}
