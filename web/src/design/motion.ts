/**
 * 动效预设（Framer Motion）。
 *
 * 设计原则（REQ-5 流畅的动态交互 / REQ-12 Apple 美学）
 * ---------------------------------------------------
 * * **克制**：动效只用于解释"什么从哪里来、到哪里去"，
 *   不做纯粹的装饰性弹跳。记账软件一天要开几十次，花哨的动画很快会变成负担。
 * * **时长三档**：150ms（微反馈）/ 250ms（常规）/ 400ms（页面级）。
 * * **曲线统一**：`cubic-bezier(0.32, 0.72, 0, 1)` —— 先快后慢、收尾柔和，
 *   这是 Apple 界面动效的典型手感。
 * * **可关闭**：全部动效都以 `reduceMotion` 为前提（见 theme.tsx）。
 */

import type { Transition, Variants } from 'framer-motion'

/** 时长（秒，Framer Motion 使用秒为单位） */
export const DURATION = {
  micro: 0.15,
  base: 0.25,
  page: 0.4,
} as const

/** 缓动曲线，与 tailwind.config.js 的 transitionTimingFunction 保持一致 */
export const EASE = {
  standard: [0.32, 0.72, 0, 1] as const,
  emphasized: [0.2, 0, 0, 1] as const,
  spring: [0.34, 1.56, 0.64, 1] as const,
}

/** 常规过渡 */
export const transitionBase: Transition = { duration: DURATION.base, ease: EASE.standard }

/** 页面切换：轻微上浮 + 淡入，避免大幅位移带来的"晃动感" */
export const pageVariants: Variants = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0, transition: { duration: DURATION.page, ease: EASE.standard } },
  exit: { opacity: 0, y: -6, transition: { duration: DURATION.micro, ease: EASE.standard } },
}

/** 容器：让子项依次入场的编排器 */
export const staggerContainer: Variants = {
  initial: {},
  animate: {
    transition: { staggerChildren: 0.035, delayChildren: 0.02 },
  },
}

/** 子项：卡片、列表行的入场 */
export const staggerItem: Variants = {
  initial: { opacity: 0, y: 10 },
  animate: { opacity: 1, y: 0, transition: { duration: DURATION.base, ease: EASE.standard } },
}

/** 卡片悬停：极轻微的抬升，暗示"可点按" */
export const hoverLift = {
  whileHover: { y: -2, transition: { duration: DURATION.micro, ease: EASE.standard } },
  whileTap: { scale: 0.995 },
}

/** 数字滚动：KPI 卡的金额从旧值过渡到新值 */
export const numberSpring: Transition = { duration: 0.6, ease: EASE.emphasized }

/**
 * 把动效配置降级为"瞬时"。
 * 用于用户开启「减少动态效果」时（REQ-7 无障碍）。
 */
export function noMotion<T extends Transition>(transition: T): T {
  return { ...transition, duration: 0 } as T
}
