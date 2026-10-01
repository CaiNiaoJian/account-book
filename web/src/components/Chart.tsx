/**
 * ECharts 的 Apple 风格主题与自适应封装。
 *
 * 为什么必须做成主题而不是每处传样式
 * ----------------------------------
 * 计划里要画 12 类图表。如果每个图表各自指定颜色与字体，
 * 必然出现"这个图的网格线比那个深一点""两个图的字号不同"——
 * 拼在一起立刻显得廉价，而这正是 REQ-13「美术重要」要避免的。
 *
 * 因此这里做两件事：
 *   1. 从**设计令牌**生成一整套 ECharts 主题（颜色、字体、网格、提示框、坐标轴），
 *      日夜切换时重新生成 —— 图表与界面永远同一套变量；
 *   2. 提供一个 `<Chart>` 组件处理 resize、销毁与主题变化，
 *      避免每个页面各写一遍 useRef + useEffect + ResizeObserver。
 */

import * as echarts from 'echarts/core'
import {
  BarChart,
  BoxplotChart,
  CandlestickChart,
  GaugeChart,
  LineChart,
  PieChart,
  RadarChart,
  SankeyChart,
  ScatterChart,
  SunburstChart,
  TreemapChart,
} from 'echarts/charts'
import {
  BrushComponent,
  DataZoomComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
  RadarComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { useEffect, useRef } from 'react'

import { useTheme } from '@/app/preferences'
import { chartPalette, resolveToken } from '@/design/tokens'

// 只注册用到的图表与组件：ECharts 全量引入会让分包从 ~580KB 涨到 1MB+，
// 而这里每种图表都对应界面上一个具体位置，多注册一种就该多一个用途。
echarts.use([
  BrushComponent,
  BarChart,  // 注意：BrushComponent 加在下面的组件段里

  BoxplotChart,
  CandlestickChart,
  GaugeChart,
  LineChart,
  PieChart,
  RadarChart,
  SankeyChart,
  ScatterChart,
  SunburstChart,
  TreemapChart,
  DataZoomComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
  RadarComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  CanvasRenderer,
])

/** 设计令牌 → ECharts 主题 */
function buildTheme(): Record<string, unknown> {
  const text = resolveToken('text')
  const text2 = resolveToken('text-2')
  const text3 = resolveToken('text-3')
  const separator = resolveToken('separator')
  const surface = resolveToken('surface')
  const palette = chartPalette()

  const axisCommon = {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: text3, fontSize: 11 },
    splitLine: { lineStyle: { color: separator, opacity: 0.4, type: 'dashed' } },
  }

  return {
    // 与界面同一套字体栈：图表里的文字与旁边的正文不会"看起来不是一家人"
    textStyle: {
      fontFamily:
        '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable Text", "PingFang SC", "Microsoft YaHei UI", sans-serif',
      color: text2,
    },
    color: palette,
    backgroundColor: 'transparent',
    title: { textStyle: { color: text, fontSize: 13, fontWeight: 600 } },
    grid: { left: 8, right: 12, top: 28, bottom: 4, containLabel: true },
    tooltip: {
      backgroundColor: surface,
      borderColor: separator,
      borderWidth: 1,
      textStyle: { color: text, fontSize: 12 },
      // Apple 的提示框是"浮起来的一小块"，圆角与阴影都克制
      extraCssText: 'border-radius:10px;box-shadow:0 8px 28px rgb(0 0 0 / 0.14);padding:8px 10px;',
    },
    legend: { textStyle: { color: text2, fontSize: 11 }, icon: 'roundRect', itemWidth: 9, itemHeight: 9 },
    categoryAxis: axisCommon,
    valueAxis: axisCommon,
    timeAxis: axisCommon,
    logAxis: axisCommon,
    line: { smooth: true, symbol: 'none', lineStyle: { width: 2 } },
    bar: { itemStyle: { borderRadius: [4, 4, 0, 0] } },
    pie: { itemStyle: { borderColor: surface, borderWidth: 2 } },
  }
}

/** ECharts 实例。导出给需要 `dispatchAction` 的页面用（例如清空框选高亮） */
export type ChartInstance = echarts.ECharts

export interface ChartProps {
  /** ECharts option（每次变化都会 setOption，默认不合并以支持切换图表类型） */
  option: echarts.EChartsCoreOption
  height?: number
  className?: string
  /** 数据为空时的占位文案 */
  emptyLabel?: string
  onEvents?: Record<string, (params: unknown) => void>
  /**
   * 实例就绪回调。
   *
   * 有些交互（例如**清空框选**）只能靠 `dispatchAction` 完成 ——
   * 光把 state 置空，图上那块高亮还留着，用户会以为"点了没反应"。
   * ECharts 的 brush 状态在实例内部，React 的 state 管不到它。
   */
  onReady?: (instance: ChartInstance) => void
}

/**
 * 图表容器。
 *
 * 三个必须处理的细节，否则图表在真实使用中一定会出问题：
 *   1. **容器尺寸为 0 时初始化**会得到一张空白图（用 ResizeObserver 兜住）；
 *   2. **主题切换**必须重建实例 —— ECharts 的主题在初始化时固化；
 *   3. **组件卸载**必须 dispose，否则切页几次就泄漏一批 canvas。
 */
/**
 * 判断一份 ECharts option 里有没有**非零**数据。
 *
 * 只看 series：坐标轴、图例这些即使没有数据也一直存在。
 * 数值可能是数字，也可能是 `{ value }` 或 `{ value_minor }` 形式的对象。
 */
function optionHasData(option: Record<string, unknown>): boolean {
  const series = option?.series
  if (!Array.isArray(series)) return false
  for (const entry of series) {
    const data = (entry as { data?: unknown[] })?.data
    if (!Array.isArray(data)) continue
    for (const point of data) {
      const raw =
        typeof point === 'number'
          ? point
          : ((point as { value?: unknown })?.value ?? (point as { value_minor?: unknown })?.value_minor)
      const value = Number(raw)
      if (Number.isFinite(value) && value !== 0) return true
    }
  }
  return false
}

export function Chart({ option, height = 240, className, emptyLabel, onEvents, onReady }: ChartProps) {
  const theme = useTheme()
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  // 主题变化 → 重建实例（ECharts 的主题只能在 init 时指定）
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const instance = echarts.init(container, buildTheme() as never, { renderer: 'canvas' })
    chartRef.current = instance

    const observer = new ResizeObserver(() => instance.resize())
    observer.observe(container)

    onReady?.(instance)

    return () => {
      observer.disconnect()
      instance.dispose()
      chartRef.current = null
    }
    // onReady 只在实例就绪时用一次；把它放进依赖会让父组件每次渲染都重建实例
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme])

  useEffect(() => {
    const instance = chartRef.current
    if (!instance) return
    // notMerge=true：切换图表类型时旧系列必须被清掉，否则会两张图叠在一起
    instance.setOption(option, { notMerge: true })
  }, [option, theme])

  useEffect(() => {
    const instance = chartRef.current
    if (!instance || !onEvents) return
    for (const [event, handler] of Object.entries(onEvents)) {
      instance.on(event, handler as never)
    }
    return () => {
      for (const event of Object.keys(onEvents)) instance.off(event)
    }
  }, [onEvents, theme])

  // **`emptyLabel` 只有在真的没有数据时才显示。**
  //
  // 早先的实现是"提供了就渲染"，于是它变成一层永远盖在图上方的蒙版 ——
  // 瀑布图上就出现过"这个来源还没有组成项，因此应发合计是 0"
  // 叠在一张画得好好的图上。名字叫 emptyLabel，语义就该是 empty 才显示。
  //
  // 判断口径：把所有 series 的 data 摊平，
  // 没有数据点、或所有数值都是 0，才算空。
  // 不能用"data 数组长度为 0" —— 瀑布图在零金额时仍有
  // 「应发」「实发」两个值为 0 的点。
  const isEmpty = !optionHasData(option)

  return (
    <div className={className} style={{ position: 'relative' }}>
      <div ref={containerRef} style={{ width: '100%', height }} />
      {emptyLabel && isEmpty ? (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-ab-footnote text-label-3">
          {emptyLabel}
        </div>
      ) : null}
    </div>
  )
}

export { echarts }
