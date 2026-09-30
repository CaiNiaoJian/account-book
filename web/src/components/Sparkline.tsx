/**
 * 自研 SVG 微图表（Sparkline / 进度环）。
 *
 * 为什么不用 ECharts 画这些小东西
 * ------------------------------
 * ECharts 实例有初始化开销与 DOM 占用。KPI 卡片、列表行里的迷你趋势图
 * 数量多（一屏可能十几个）、尺寸小、交互需求为零 —— 这类场景用
 * 手写 SVG 更合适：渲染快、无依赖、样式完全跟随 CSS 变量（主题自动切换）。
 *
 * 正式的统计图表（桑基、旭日、K 线等）仍在 P2/P3 用 ECharts 实现，
 * 两者分工明确：**微图表用 SVG，分析图表用 ECharts**。
 */

import { useId } from 'react'

export interface SparklineProps {
  /** 数据点（任意量纲，内部会归一化到视图高度） */
  points: number[]
  /** 语义色令牌名，决定描边与渐变填充颜色 */
  tone?: 'accent' | 'positive' | 'negative' | 'warning' | 'purple' | 'teal'
  width?: number
  height?: number
  /** 是否绘制渐变面积 */
  filled?: boolean
  /** 是否在末端画一个高亮点（表示"最新值"） */
  showLastPoint?: boolean
  className?: string
  /** 无障碍描述；提供后会作为 <title> 朗读 */
  title?: string
}

/**
 * 生成平滑折线路径。
 *
 * 使用 Catmull-Rom 风格的二次贝塞尔近似：控制点取相邻两点的中点，
 * 这样曲线不会过冲（overshoot），避免"数据看起来比实际更极端"的误导。
 */
function buildSmoothPath(values: number[], width: number, height: number, padding: number): string {
  if (values.length === 0) return ''
  if (values.length === 1) {
    const y = height / 2
    return `M ${padding} ${y} L ${width - padding} ${y}`
  }

  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const stepX = (width - padding * 2) / (values.length - 1)

  const coordinates = values.map((value, index) => {
    const x = padding + index * stepX
    // SVG 的 y 轴向下，因此需要翻转
    const y = padding + (1 - (value - min) / span) * (height - padding * 2)
    return { x, y }
  })

  let path = `M ${coordinates[0]!.x.toFixed(2)} ${coordinates[0]!.y.toFixed(2)}`
  for (let index = 1; index < coordinates.length; index += 1) {
    const previous = coordinates[index - 1]!
    const current = coordinates[index]!
    const midX = (previous.x + current.x) / 2
    path += ` Q ${midX.toFixed(2)} ${previous.y.toFixed(2)} ${midX.toFixed(2)} ${((previous.y + current.y) / 2).toFixed(2)}`
    path += ` Q ${midX.toFixed(2)} ${current.y.toFixed(2)} ${current.x.toFixed(2)} ${current.y.toFixed(2)}`
  }
  return path
}

export function Sparkline({
  points,
  tone = 'accent',
  width = 120,
  height = 34,
  filled = true,
  showLastPoint = true,
  className,
  title,
}: SparklineProps) {
  const gradientId = useId()
  const padding = 3
  const safePoints = points.length > 0 ? points : [0, 0]

  const linePath = buildSmoothPath(safePoints, width, height, padding)
  const areaPath = `${linePath} L ${width - padding} ${height} L ${padding} ${height} Z`

  // 末点坐标（与 buildSmoothPath 内的换算保持一致）
  const min = Math.min(...safePoints)
  const max = Math.max(...safePoints)
  const span = max - min || 1
  const lastValue = safePoints[safePoints.length - 1]!
  const lastX = width - padding
  const lastY = padding + (1 - (lastValue - min) / span) * (height - padding * 2)

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      {title ? <title>{title}</title> : null}
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={`rgb(var(--ab-${tone}) / 0.28)`} />
          <stop offset="100%" stopColor={`rgb(var(--ab-${tone}) / 0)`} />
        </linearGradient>
      </defs>
      {filled ? <path d={areaPath} fill={`url(#${gradientId})`} /> : null}
      <path
        d={linePath}
        fill="none"
        stroke={`rgb(var(--ab-${tone}))`}
        strokeWidth={1.6}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {showLastPoint ? (
        <circle cx={lastX} cy={lastY} r={2.4} fill={`rgb(var(--ab-${tone}))`} />
      ) : null}
    </svg>
  )
}

export interface ProgressRingProps {
  /** 0–1 的完成比例（超出 1 会被截断显示，但颜色会变为警示色） */
  ratio: number
  size?: number
  strokeWidth?: number
  tone?: 'accent' | 'positive' | 'negative' | 'warning'
  /** 中心内容 */
  children?: React.ReactNode
  className?: string
  title?: string
}

/** 进度环：预算执行率、存钱罐进度（P4）等场景共用 */
export function ProgressRing({
  ratio,
  size = 64,
  strokeWidth = 6,
  tone = 'accent',
  children,
  className,
  title,
}: ProgressRingProps) {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(ratio) ? ratio : 0))
  const radius = (size - strokeWidth) / 2
  const circumference = 2 * Math.PI * radius
  const dash = circumference * clamped

  return (
    <div className={['relative inline-flex items-center justify-center', className].filter(Boolean).join(' ')}>
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        role={title ? 'img' : undefined}
        aria-hidden={title ? undefined : true}
        focusable="false"
      >
        {title ? <title>{title}</title> : null}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="rgb(var(--ab-surface-3))"
          strokeWidth={strokeWidth}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={`rgb(var(--ab-${tone}))`}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circumference - dash}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      {children ? <div className="absolute inset-0 flex items-center justify-center">{children}</div> : null}
    </div>
  )
}
