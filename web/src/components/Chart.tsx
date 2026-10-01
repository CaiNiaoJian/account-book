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
  DataZoomComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
  RadarComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { useEffect, useRef } from 'react'

import { useTheme } from '@/app/preferences'
import { chartPalette, resolveToken } from '@/design/tokens'

// 只注册用到的图表与组件：ECharts 全量引入会让分包从 ~580KB 涨到 1MB+，
// 而这里每种图表都对应界面上一个具体位置，多注册一种就该多一个用途。
echarts.use([
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
  DataZoomComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
  RadarComponent,
  TitleComponent,
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

export interface ChartProps {
  /** ECharts option（每次变化都会 setOption，默认不合并以支持切换图表类型） */
  option: echarts.EChartsCoreOption
  height?: number
  className?: string
  /** 数据为空时的占位文案 */
  emptyLabel?: string
  onEvents?: Record<string, (params: unknown) => void>
}

/**
 * 图表容器。
 *
 * 三个必须处理的细节，否则图表在真实使用中一定会出问题：
 *   1. **容器尺寸为 0 时初始化**会得到一张空白图（用 ResizeObserver 兜住）；
 *   2. **主题切换**必须重建实例 —— ECharts 的主题在初始化时固化；
 *   3. **组件卸载**必须 dispose，否则切页几次就泄漏一批 canvas。
 */
export function Chart({ option, height = 240, className, emptyLabel, onEvents }: ChartProps) {
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

    return () => {
      observer.disconnect()
      instance.dispose()
      chartRef.current = null
    }
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

  return (
    <div className={className} style={{ position: 'relative' }}>
      <div ref={containerRef} style={{ width: '100%', height }} />
      {emptyLabel ? (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-ab-footnote text-label-3">
          {emptyLabel}
        </div>
      ) : null}
    </div>
  )
}

export { echarts }
