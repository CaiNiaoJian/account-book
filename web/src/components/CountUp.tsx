/**
 * 数字滚动 —— KPI 卡片的金额从旧值平滑过渡到新值。
 *
 * 为什么值得单独做一个组件
 * ------------------------
 * 记账应用的数字**经常变化**（记一笔、换区间、切换账户）。
 * 数字直接跳变时，用户很难察觉"变了多少"；而滚动过去的过程本身
 * 就在表达"从 X 变成 Y"，这正是动效该有的作用 ——
 * 解释变化，而不是装饰。
 *
 * 三个必须处理的点：
 *   1. **减少动态效果**时直接跳到终值（REQ-7 无障碍），不做任何插值；
 *   2. 用 `requestAnimationFrame` 而不是 setInterval —— 后者在标签页
 *      不可见时仍会跑，白耗电，且帧率不稳导致动画一顿一顿；
 *   3. **金额是整数最小单位**，插值后必须取整，否则会出现 1234.56 分
 *      这种不存在的中间值，格式化时又会被四舍五入成看起来"跳了一下"的数。
 */

import { useEffect, useRef, useState } from 'react'

import { usePreferences } from '@/app/preferences'
import { displayMinor } from '@/lib/format'

interface CountUpProps {
  /** 目标金额（最小单位） */
  value: number
  currency?: string
  /** 动画时长（毫秒） */
  duration?: number
  className?: string
  showSign?: boolean
}

/** 缓出曲线：起步快、收尾慢，与界面其它动效一致 */
function easeOut(t: number): number {
  return 1 - (1 - t) ** 3
}

export function CountUp({ value, currency = 'CNY', duration = 600, className, showSign }: CountUpProps) {
  const { preferences } = usePreferences()
  const reduced = preferences.reduce_motion
  const [display, setDisplay] = useState(value)
  const frameRef = useRef<number | null>(null)
  const fromRef = useRef(value)

  useEffect(() => {
    if (reduced || duration <= 0) {
      setDisplay(value)
      fromRef.current = value
      return
    }

    const from = fromRef.current
    if (from === value) return
    const started = performance.now()

    const step = (now: number) => {
      const progress = Math.min(1, (now - started) / duration)
      const current = from + (value - from) * easeOut(progress)
      // 金额是整数最小单位：插值结果必须取整
      setDisplay(Math.round(current))
      if (progress < 1) {
        frameRef.current = requestAnimationFrame(step)
      } else {
        fromRef.current = value
      }
    }

    frameRef.current = requestAnimationFrame(step)
    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
      // 中断时把起点记成当前显示值，下一次动画从"看得见的地方"继续，
      // 而不是从最初的目标回跳
      fromRef.current = display
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, reduced, duration])

  return (
    <span className={`ab-tnum ${className ?? ''}`}>
      {displayMinor(display, currency, preferences.privacy_mode, { showSign })}
    </span>
  )
}
