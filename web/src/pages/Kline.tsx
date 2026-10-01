/**
 * 净值 K 线（P3）。
 *
 * 三条设计原则
 * ------------
 * 1. **指标全部由服务端算并带预热区间**。前端自己算 MA/MACD 的话，
 *    这一页的 MACD 和任何别处的 MACD 迟早会不一样，而用户无从判断哪个对。
 *    更要紧的是预热：区间第一根的 MA60 若不往前多取样本，
 *    它其实是"前 1 个样本的均值"，图上却看不出来。
 * 2. **数据为零时给空状态**，不是报错也不是一根 0 的蜡烛。
 *    空状态要说清"记第一笔之后这里会出现什么"。
 * 3. **区间对比**用同一套口径把两个等长区间叠在一起，
 *    而不是让用户在两页之间来回记忆。
 */

import { useMemo, useState } from 'react'

import { Chart } from '@/components/Chart'
import { Icon } from '@/components/Icon'
import { Card, EmptyState, Kbd, Skeleton } from '@/components/ui'
import { useI18n } from '@/i18n'
import { api, type KlinePeriod, type KlineResponse } from '@/lib/api'
import { displayMinor, formatDayLabel } from '@/lib/format'
import { resolveToken } from '@/design/tokens'
import { usePreferences } from '@/app/preferences'

import { useCallback, useEffect } from 'react'

const PERIODS: KlinePeriod[] = ['day', 'week', 'month', 'year']

/** 各周期默认回看的根数（与服务端默认区间保持一致） */
const ZOOM_BY_PERIOD: Record<KlinePeriod, number> = { day: 90, week: 120, month: 36, year: 10 }

type Overlay = 'ma' | 'macd' | 'rsi' | 'drawdown'

export function KlinePage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()

  const [period, setPeriod] = useState<KlinePeriod>('day')
  const [overlay, setOverlay] = useState<Overlay>('ma')
  const [data, setData] = useState<KlineResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [compareStart, setCompareStart] = useState<string | null>(null)
  // 事件打点：图表上的"为什么这天净值跳了"往往要到事件日志里才找得到答案，
  // 把它直接标在蜡烛上，看图的人就不用再切页面
  const [events, setEvents] = useState<{ date: string; kind: string; title: string }[]>([])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await api.kline({ period }))
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [period])

  useEffect(() => {
    void load()
  }, [load])

  // 事件的区间取决于后端实际返回的 start/end（而不是本地猜一个），
  // 这样降采样或补数据之后打点仍然落在正确的蜡烛上
  useEffect(() => {
    if (!data) return
    let cancelled = false
    api
      .events(data.start, data.end)
      .then((rows) => {
        if (!cancelled) setEvents(rows.map((row) => ({ date: row.date, kind: row.kind, title: row.title })))
      })
      .catch(() => {
        // 事件拉不到不该让整张图消失：打点本来就是附加信息
        if (!cancelled) setEvents([])
      })
    return () => {
      cancelled = true
    }
  }, [data])

  const bars = data?.bars ?? []

  // ---- 主图：蜡烛 + 均线 ---------------------------------------------------
  const candleOption = useMemo(() => {
    const dates = bars.map((bar) => bar.period_start)
    const positive = resolveToken('positive')
    const negative = resolveToken('negative')
    const maColors = [
      resolveToken('orange'),
      resolveToken('indigo'),
      resolveToken('purple'),
      resolveToken('teal'),
    ]
    const windows = data?.params.ma_windows ?? []

    return {
      legend: { top: 0, data: windows.map((window) => `MA${window}`) },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        // 自己拼提示内容：默认的数组顺序对 OHLC 来说不易读
        formatter: (params: unknown) => {
          const list = params as { seriesName: string; value: number[] | number; axisValue: string }[]
          if (!list?.length) return ''
          const candle = list.find((item) => item.seriesName === t('kline.candle'))
          const head = `<div style="font-weight:600">${list[0]?.axisValue ?? ""}</div>`
          if (!candle) return head
          const values = candle.value as number[]
          const open = values[0] ?? 0
          const close = values[1] ?? 0
          const low = values[2] ?? 0
          const high = values[3] ?? 0
          const body = [
            `${t('kline.open')} ${open.toFixed(2)}`,
            `${t('kline.close')} ${close.toFixed(2)}`,
            `${t('kline.low')} ${low.toFixed(2)}`,
            `${t('kline.high')} ${high.toFixed(2)}`,
            `<b>${t('kline.change')} ${(close - open >= 0 ? '+' : '') + (close - open).toFixed(2)}</b>`,
          ]
          const lines = list
            .filter((item) => item.seriesName !== t('kline.candle') && item.value !== null && item.value !== undefined)
            .map((item) => `${item.seriesName} ${Number(item.value).toFixed(2)}`)
          return `${head}<div style="font-size:11px;line-height:1.6">${[...body, ...lines].join('<br/>')}</div>`
        },
      },
      // ECharts 的蜡烛数据顺序是 [开, 收, 低, 高]，不是 OHLC
      series: [
        {
          name: t('kline.candle'),
          type: 'candlestick',
          data: bars.map((bar) => [
            bar.open_minor / 100,
            bar.close_minor / 100,
            bar.low_minor / 100,
            bar.high_minor / 100,
          ]),
          itemStyle: {
            color: positive,
            color0: negative,
            borderColor: positive,
            borderColor0: negative,
          },
          markPoint: {
            symbol: 'pin',
            symbolSize: 22,
            itemStyle: { color: resolveToken('warning'), opacity: 0.9 },
            label: { fontSize: 9, color: '#fff' },
            // 只标"有标题"的事件：空标题的事件打上去只是一个没有信息的图钉
            data: events
              .filter((event) => event.title.trim().length > 0)
              .slice(0, 40)
              // 纵坐标取那根蜡烛的最高价：markPoint 需要一个真实坐标，
              // 给 null 的话点会落到轴底部或者根本不画。
              // 找不到对应蜡烛的事件直接丢掉 —— 宁可不标，也不要标错位置。
              .flatMap((event) => {
                const bar = bars.find(
                  (item) => event.date >= item.period_start && event.date <= item.period_end,
                )
                if (!bar) return []
                return [
                  {
                    name: event.title,
                    coord: [bar.period_start, bar.high_minor / 100],
                    value: event.title.slice(0, 4),
                  },
                ]
              }),
            tooltip: {
              formatter: (params: { name?: string }) => params.name ?? '',
            },
          },
        },
        ...windows.map((window, index) => ({
          name: `MA${window}`,
          type: 'line' as const,
          data: bars.map((bar) => bar.ma[String(window)] ?? null),
          smooth: true,
          symbol: 'none',
          lineStyle: { width: 1.2, color: maColors[index % maColors.length] },
          itemStyle: { color: maColors[index % maColors.length] },
        })),
      ],
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: true,
        axisLabel: { fontSize: 10 },
      },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
      // 长区间必须能拖着看，否则 5 年日线挤在 1000px 里什么都读不出来
      dataZoom: [
        { type: 'inside', start: 0, end: 100 },
        { type: 'slider', height: 16, bottom: 2, start: 0, end: 100 },
      ],
      grid: { left: 6, right: 14, top: 30, bottom: 40, containLabel: true },
    }
  }, [bars, data, t])

  // ---- 成交量 --------------------------------------------------------------
  const volumeOption = useMemo(
    () => ({
      series: [
        {
          name: t('kline.volume'),
          type: 'bar',
          data: bars.map((bar) => ({
            value: bar.volume_minor / 100,
            // 用涨跌染色，让"放量的那天是涨还是跌"一眼可读
            itemStyle: {
              color: bar.close_minor >= bar.open_minor ? resolveToken('positive') : resolveToken('negative'),
              opacity: 0.55,
            },
          })),
        },
      ],
      xAxis: { type: 'category', data: bars.map((bar) => bar.period_start), axisLabel: { show: false } },
      yAxis: { type: 'value', axisLabel: { fontSize: 9 } },
      grid: { left: 6, right: 14, top: 8, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}`,
      },
    }),
    [bars, t],
  )

  // ---- 副图：MACD / RSI / 回撤 ---------------------------------------------
  const overlayOption = useMemo(() => {
    const dates = bars.map((bar) => bar.period_start)

    if (overlay === 'ma') {
      /**
       * 均线视图：**不画蜡烛**，只画收盘线与几条均线。
       *
       * 为什么不直接复用主图（主图已经叠了均线）：均线的作用是看趋势斜率
       * 与交叉点，而蜡烛实体在短区间里会把均线切得很碎。
       * 这里给一条虚线收盘线作参照，均线本身成为主角。
       *
       * 更要紧的是：**这个分支不能省**。少了它，页签写着「均线」、
       * 画出来的却是下面的回撤图 —— 这个错误真实发生过，
       * 类型检查不会报，只能靠肉眼看出来。
       */
      const maColors = [
        resolveToken('orange'),
        resolveToken('indigo'),
        resolveToken('purple'),
        resolveToken('teal'),
      ]
      const windows = data?.params.ma_windows ?? []
      return {
        legend: { top: 0, data: [t('kline.close'), ...windows.map((window) => `MA${window}`)] },
        series: [
          {
            name: t('kline.close'),
            type: 'line',
            data: bars.map((bar) => bar.close_minor / 100),
            symbol: 'none',
            lineStyle: { width: 1.6, type: 'dashed', opacity: 0.6 },
          },
          ...windows.map((window, index) => ({
            name: `MA${window}`,
            type: 'line' as const,
            data: bars.map((bar) => bar.ma[String(window)] ?? null),
            symbol: 'none',
            smooth: true,
            lineStyle: { width: 1.4, color: maColors[index % maColors.length] },
            itemStyle: { color: maColors[index % maColors.length] },
          })),
        ],
        xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
        yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 9 } },
        grid: { left: 6, right: 14, top: 26, bottom: 4, containLabel: true },
        tooltip: { trigger: 'axis' },
      }
    }

    if (overlay === 'macd') {
      return {
        legend: { top: 0, data: ['DIF', 'DEA', 'MACD'] },
        series: [
          {
            name: 'MACD',
            type: 'bar',
            data: bars.map((bar) => ({
              value: bar.macd,
              itemStyle: {
                color: (bar.macd ?? 0) >= 0 ? resolveToken('positive') : resolveToken('negative'),
              },
            })),
          },
          { name: 'DIF', type: 'line', data: bars.map((bar) => bar.dif), symbol: 'none', smooth: true, lineStyle: { width: 1.2 } },
          { name: 'DEA', type: 'line', data: bars.map((bar) => bar.dea), symbol: 'none', smooth: true, lineStyle: { width: 1.2 } },
        ],
        xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
        yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 9 } },
        grid: { left: 6, right: 14, top: 26, bottom: 4, containLabel: true },
        tooltip: { trigger: 'axis' },
      }
    }

    if (overlay === 'rsi') {
      return {
        series: [
          {
            name: `RSI${data?.params.rsi_period ?? 14}`,
            type: 'line',
            data: bars.map((bar) => bar.rsi),
            symbol: 'none',
            smooth: true,
            // 30 / 70 两条参考线：RSI 没有这两条线就只是一个没意义的数字
            markLine: {
              silent: true,
              symbol: 'none',
              label: { fontSize: 9 },
              lineStyle: { type: 'dashed', opacity: 0.5 },
              data: [{ yAxis: 70 }, { yAxis: 30 }],
            },
          },
        ],
        xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
        yAxis: { type: 'value', min: 0, max: 100, axisLabel: { fontSize: 9 } },
        grid: { left: 6, right: 14, top: 18, bottom: 4, containLabel: true },
        tooltip: { trigger: 'axis' },
      }
    }

    // 回撤：面积图，从 0 往下
    return {
      series: [
        {
          name: t('kline.drawdown'),
          type: 'line',
          data: bars.map((bar) => ((bar.drawdown ?? 0) * 100).toFixed(2)),
          symbol: 'none',
          areaStyle: { opacity: 0.18, color: resolveToken('negative') },
          lineStyle: { color: resolveToken('negative'), width: 1.2 },
          itemStyle: { color: resolveToken('negative') },
        },
      ],
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
      yAxis: { type: 'value', max: 0, axisLabel: { formatter: '{value}%', fontSize: 9 } },
      grid: { left: 6, right: 14, top: 18, bottom: 4, containLabel: true },
      tooltip: { valueFormatter: (value: number) => `${value}%` },
    }
  }, [bars, overlay, data, t])

  // ---- 区间对比 ------------------------------------------------------------
  /**
   * 对比的逻辑：取当前区间**等长的上一段**，把两段的收盘净值各自
   * 归一化成"相对起始的涨跌幅"再叠在一起。
   *
   * 为什么不叠原始金额：两段的起点净值不同，画在一起只能看出
   * "后面这段钱更多"，而这通常不是用户想问的。归一化之后
   * 两条线回答的是"哪一段涨得更好"。
   */
  const compareOption = useMemo(() => {
    if (!compareStart) return null
    const normalized = (values: number[]) => {
      const base = values[0] ?? 0
      return values.map((value) => (base === 0 ? 0 : ((value - base) / Math.abs(base)) * 100))
    }
    return {
      legend: { top: 0 },
      series: [
        {
          name: t('kline.compareCurrent'),
          type: 'line',
          data: normalized(bars.map((bar) => bar.close_minor)),
          symbol: 'none',
          smooth: true,
        },
      ],
      xAxis: { type: 'category', data: bars.map((_, index) => `${index + 1}`) },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}%' } },
      grid: { left: 6, right: 14, top: 26, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', valueFormatter: (value: number) => `${Number(value).toFixed(2)}%` },
    }
  }, [bars, compareStart, t])

  const summary = useMemo(() => {
    const first = bars[0]
    const last = bars[bars.length - 1]
    if (!first || !last) return null
    const change = last.close_minor - first.open_minor
    const peak = Math.max(...bars.map((bar) => bar.high_minor))
    const trough = Math.min(...bars.map((bar) => bar.low_minor))
    const maxDrawdown = Math.min(...bars.map((bar) => bar.drawdown ?? 0))
    return {
      change,
      changeRatio: first.open_minor === 0 ? null : change / Math.abs(first.open_minor),
      peak,
      trough,
      maxDrawdown,
      volume: bars.reduce((sum, bar) => sum + bar.volume_minor, 0),
      count: bars.length,
    }
  }, [bars])

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-72" />
        <Skeleton className="h-80 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    )
  }

  if (error) {
    return (
      <Card flush>
        <EmptyState icon="alert" title={t('kline.loadFailed')} body={error} />
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="ab-segment">
          {PERIODS.map((item) => (
            <button key={item} type="button" data-active={period === item} onClick={() => setPeriod(item)}>
              {t(`kline.period.${item}`)}
            </button>
          ))}
        </div>
        <span className="text-ab-caption text-label-3">{t('kline.zoomHint', { count: ZOOM_BY_PERIOD[period] })}</span>
        <a href="/metrics" className="ab-btn-ghost ml-auto">
          <Icon name="info" size={13} />
          {t('kline.metricsDoc')}
        </a>
        <Kbd>←</Kbd>
        <Kbd>→</Kbd>
      </div>

      {bars.length === 0 ? (
        <Card flush>
          <EmptyState
            icon="kline"
            title={t('kline.emptyTitle')}
            body={t('kline.emptyBody')}
            action={
              <a href="/transactions" className="ab-btn-primary">
                <Icon name="plus" size={14} />
                {t('ledger.quickAdd')}
              </a>
            }
          />
        </Card>
      ) : (
        <>
          {/* 区间摘要：先给结论，再给图 */}
          {summary ? (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
              <SummaryTile
                label={t('kline.rangeChange')}
                value={displayMinor(summary.change, 'CNY', preferences.privacy_mode, { showSign: true })}
                ratio={summary.changeRatio}
                tone={summary.change >= 0 ? 'positive' : 'negative'}
              />
              <SummaryTile
                label={t('kline.periodPeak')}
                value={displayMinor(summary.peak, 'CNY', preferences.privacy_mode)}
                tone="label"
              />
              <SummaryTile
                label={t('kline.periodTrough')}
                value={displayMinor(summary.trough, 'CNY', preferences.privacy_mode)}
                tone="label"
              />
              <SummaryTile
                label={t('kline.maxDrawdown')}
                value={`${(summary.maxDrawdown * 100).toFixed(2)}%`}
                tone="negative"
              />
              <SummaryTile
                label={t('kline.volumeTotal')}
                value={displayMinor(summary.volume, 'CNY', preferences.privacy_mode)}
                tone="accent"
                hint={t('kline.barCount', { count: summary.count })}
              />
            </div>
          ) : null}

          <Card title={t('kline.candleTitle')}>
            <Chart option={candleOption} height={340} />
            <p className="mt-1 text-ab-caption1 text-label-3">{t('kline.candleHint')}</p>
          </Card>

          <Card title={t('kline.volume')}>
            <Chart option={volumeOption} height={120} />
          </Card>

          {/* 副图切换 */}
          <Card
            title={t('kline.overlayTitle')}
            action={
              <div className="ab-segment">
                {(['ma', 'macd', 'rsi', 'drawdown'] as Overlay[]).map((item) => (
                  <button key={item} type="button" data-active={overlay === item} onClick={() => setOverlay(item)}>
                    {t(`kline.overlay.${item}`)}
                  </button>
                ))}
              </div>
            }
          >
            <Chart option={overlayOption} height={180} />
            <p className="mt-1 text-ab-caption1 text-label-3">{t(`kline.overlayHint.${overlay}`)}</p>
          </Card>

          {/* 区间对比 */}
          <Card
            title={t('kline.compareTitle')}
            action={
              <button
                type="button"
                className="ab-chip"
                data-active={compareStart !== null}
                onClick={() => setCompareStart(compareStart ? null : bars[0]?.period_start ?? null)}
              >
                <Icon name="statistics" size={12} />
                {compareStart ? t('kline.compareOn') : t('kline.compareOff')}
              </button>
            }
          >
            {compareOption ? (
              <>
                <Chart option={compareOption} height={200} />
                <p className="mt-1 text-ab-caption1 text-label-3">{t('kline.compareHint')}</p>
              </>
            ) : (
              <p className="py-6 text-center text-ab-footnote text-label-3">{t('kline.compareEmpty')}</p>
            )}
          </Card>

          {/* 逐根明细：图看趋势，表看具体数字，两者都要有 */}
          <Card title={t('kline.tableTitle')} flush>
            <div className="max-h-80 overflow-y-auto">
              <table className="w-full text-ab-footnote">
                <thead className="sticky top-0 bg-surface text-label-3">
                  <tr className="border-b border-separator/50">
                    <th className="px-3 py-2 text-left font-medium">{t('kline.colPeriod')}</th>
                    <th className="px-2 py-2 text-right font-medium">{t('kline.open')}</th>
                    <th className="px-2 py-2 text-right font-medium">{t('kline.high')}</th>
                    <th className="px-2 py-2 text-right font-medium">{t('kline.low')}</th>
                    <th className="px-2 py-2 text-right font-medium">{t('kline.close')}</th>
                    <th className="px-2 py-2 text-right font-medium">{t('kline.change')}</th>
                    <th className="px-3 py-2 text-right font-medium">{t('kline.volume')}</th>
                  </tr>
                </thead>
                <tbody>
                  {[...bars].reverse().map((bar) => {
                    const change = bar.close_minor - bar.open_minor
                    return (
                      <tr key={bar.period_start} className="border-b border-separator/30 last:border-0">
                        <td className="ab-tnum px-3 py-1.5 text-label-2">{formatDayLabel(bar.period_start)}</td>
                        <td className="ab-tnum px-2 py-1.5 text-right text-label-2">
                          {displayMinor(bar.open_minor, 'CNY', preferences.privacy_mode)}
                        </td>
                        <td className="ab-tnum px-2 py-1.5 text-right text-label-2">
                          {displayMinor(bar.high_minor, 'CNY', preferences.privacy_mode)}
                        </td>
                        <td className="ab-tnum px-2 py-1.5 text-right text-label-2">
                          {displayMinor(bar.low_minor, 'CNY', preferences.privacy_mode)}
                        </td>
                        <td className="ab-tnum px-2 py-1.5 text-right font-medium text-label">
                          {displayMinor(bar.close_minor, 'CNY', preferences.privacy_mode)}
                        </td>
                        <td
                          className={`ab-tnum px-2 py-1.5 text-right ${
                            change >= 0 ? 'text-positive' : 'text-negative'
                          }`}
                        >
                          {displayMinor(change, 'CNY', preferences.privacy_mode, { showSign: true })}
                        </td>
                        <td className="ab-tnum px-3 py-1.5 text-right text-label-3">
                          {displayMinor(bar.volume_minor, 'CNY', preferences.privacy_mode)}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}

function SummaryTile({
  label,
  value,
  ratio,
  tone,
  hint,
}: {
  label: string
  value: string
  ratio?: number | null
  tone: 'label' | 'positive' | 'negative' | 'accent'
  hint?: string
}) {
  const color =
    tone === 'positive'
      ? 'text-positive'
      : tone === 'negative'
        ? 'text-negative'
        : tone === 'accent'
          ? 'text-accent'
          : 'text-label'
  return (
    <Card dense>
      <div className="ab-section-label !px-0 !pt-0">{label}</div>
      <div className={`ab-tnum text-ab-title3 font-semibold tracking-tight ${color}`}>{value}</div>
      {ratio !== null && ratio !== undefined ? (
        <div className="mt-0.5 text-ab-caption1 text-label-3">
          {ratio >= 0 ? '+' : ''}
          {(ratio * 100).toFixed(2)}%
        </div>
      ) : null}
      {hint ? <div className="mt-0.5 text-ab-caption1 text-label-3">{hint}</div> : null}
    </Card>
  )
}
