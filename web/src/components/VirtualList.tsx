/**
 * 虚拟滚动（P1 尾巴 T2）。
 *
 * 为什么自己写而不是引库
 * ----------------------
 * `@tanstack/react-virtual` 会再加一个运行时依赖，而这里需要的功能很小：
 * 在滚动容器里只渲染可见区（加少量 overscan）的条目。
 * 本项目从一开始就按"离线、少依赖"在做，因此不引。
 *
 * 为什么必须**测量**行高而不是写死
 * --------------------------------
 * 流水行的高度取决于内容：有标签/项目/备注的行更高，转账行更矮。
 * 写死一个估计值会让滚动位置逐渐偏移（滚到第 200 行时内容已经错位一大截），
 * 而这类错位很容易被当成"滚动卡了"而不是"算错了"。
 *
 * 因此这里用 ResizeObserver 量出每一行的真实高度，并维护前缀和数组：
 * 偏移量 = 前面所有行的实际高度之和。这比"假设等高的乘法"多一次遍历，
 * 但结果是对的。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

export interface VirtualWindow {
  /** 可见区（含 overscan）内的起止下标，左闭右开 */
  start: number
  end: number
  /** 上下各需要留出多少像素的占位 */
  paddingTop: number
  paddingBottom: number
  /** 每一行的测量回调，挂到行元素上 */
  measure: (index: number) => (element: HTMLElement | null) => void
  /** 挂到**滚动容器**内的任意元素上，用来定位滚动容器 */
  attach: (element: HTMLElement | null) => void
}

/**
 * 计算可见窗口。
 *
 * ``count`` 变化（加载更多、筛选）时会重建前缀和；未测量过的行先用
 * ``estimate`` 顶上 —— 这只会影响"还没滚到那里"的行，而它们本来就不渲染。
 */
export function useVirtualWindow(
  count: number,
  options: { estimate?: number; overscan?: number } = {},
): VirtualWindow {
  const { estimate = 56, overscan = 6 } = options
  // 用 state 存滚动容器而不是 ref：容器是在首次渲染之后才拿到的，
  // 而监听器要等它出现才挂得上。用 ref 就得自己安排"挂载后重跑"的时机。
  const [scroller, setScroller] = useState<HTMLElement | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [viewport, setViewport] = useState(600)
  // 行高用 state 存：测量到新值时要触发重算
  const [heights, setHeights] = useState<Map<number, number>>(new Map())
  const elements = useRef<Map<number, HTMLElement>>(new Map())
  const observer = useRef<ResizeObserver | null>(null)

  /** 找到最近的滚动容器。约定用 `data-virtual-scroll` 标记它 */
  const attach = useCallback((element: HTMLElement | null) => {
    if (!element) {
      setScroller(null)
      return
    }
    setScroller((element.closest('[data-virtual-scroll]') as HTMLElement | null) ?? element)
  }, [])

  useEffect(() => {
    if (!scroller) return
    const onScroll = () => setScrollTop(scroller.scrollTop)
    const onResize = () => setViewport(scroller.clientHeight)
    onScroll()
    onResize()
    scroller.addEventListener('scroll', onScroll, { passive: true })
    const resizeObserver = new ResizeObserver(onResize)
    resizeObserver.observe(scroller)
    return () => {
      scroller.removeEventListener('scroll', onScroll)
      resizeObserver.disconnect()
    }
  }, [scroller])

  // ``count`` 变化时丢掉超出范围的行高缓存，避免无限增长
  useEffect(() => {
    setHeights((previous) => {
      if (previous.size === 0) return previous
      const next = new Map<number, number>()
      for (const [index, value] of previous) if (index < count) next.set(index, value)
      return next
    })
  }, [count])

  useEffect(() => {
    observer.current = new ResizeObserver((entries) => {
      let changed = false
      const updates: [number, number][] = []
      for (const entry of entries) {
        const index = Number((entry.target as HTMLElement).dataset.virtualIndex)
        if (!Number.isFinite(index)) continue
        const height = entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height
        if (height > 0) {
          updates.push([index, height])
          changed = true
        }
      }
      if (!changed) return
      setHeights((previous) => {
        const next = new Map(previous)
        for (const [index, height] of updates) {
          // 只有真的变了才写回，否则 ResizeObserver 的每次回调都会引发一轮渲染
          if (Math.abs((next.get(index) ?? 0) - height) > 0.5) next.set(index, height)
        }
        return next
      })
    })
    return () => observer.current?.disconnect()
  }, [])

  const measure = useCallback(
    (index: number) => (element: HTMLElement | null) => {
      const previous = elements.current.get(index)
      if (previous === element) return
      if (previous) {
        observer.current?.unobserve(previous)
        elements.current.delete(index)
      }
      if (element) {
        element.dataset.virtualIndex = String(index)
        elements.current.set(index, element)
        observer.current?.observe(element)
      }
    },
    [],
  )

  const { start, end, paddingTop, paddingBottom } = useMemo(() => {
    // 前缀和：offset[i] = 前 i 行的总高度
    const offsets = new Array<number>(count + 1)
    offsets[0] = 0
    for (let index = 0; index < count; index += 1) {
      offsets[index + 1] = (offsets[index] ?? 0) + (heights.get(index) ?? estimate)
    }
    const total = offsets[count] ?? 0

    // 二分找到第一个"底边超过 scrollTop"的行
    const findIndex = (target: number): number => {
      let low = 0
      let high = count
      while (low < high) {
        const middle = (low + high) >> 1
        if ((offsets[middle + 1] ?? 0) < target) low = middle + 1
        else high = middle
      }
      return low
    }

    const first = Math.max(0, findIndex(scrollTop) - overscan)
    const last = Math.min(count, findIndex(scrollTop + viewport) + 1 + overscan)
    return {
      start: first,
      end: last,
      paddingTop: offsets[first] ?? 0,
      paddingBottom: Math.max(0, total - (offsets[last] ?? 0)),
    }
  }, [count, heights, estimate, overscan, scrollTop, viewport])

  return { start, end, paddingTop, paddingBottom, measure, attach }
}
