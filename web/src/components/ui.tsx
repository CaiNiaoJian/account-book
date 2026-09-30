/**
 * 基础 UI 组件库 —— Apple 风格的原子组件。
 *
 * 设计约束
 * --------
 * * 所有颜色走语义类（`bg-surface` / `text-label-2`），**禁止硬编码色值**，
 *   以保证主题切换与未来的自定义主题编辑器能整体生效（REQ-7、REQ-8）。
 * * 交互反馈统一为：悬停微变、按下微缩（`active:scale`）、焦点可见环。
 * * 每个组件都考虑键盘可达与读屏标签（REQ-7「全局兼容」）。
 *
 * 组件刻意保持"薄"：它们只负责视觉与交互，不含任何业务判断。
 */

import { motion } from 'framer-motion'
import {
  useId,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type ReactNode,
} from 'react'

import { Icon, type IconName } from './Icon'

// -----------------------------------------------------------------------------
// 卡片
// -----------------------------------------------------------------------------
export interface CardProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  title?: ReactNode
  subtitle?: ReactNode
  /** 右上角操作区（按钮、分段控件等） */
  action?: ReactNode
  /** 是否使用更紧凑的内边距 */
  dense?: boolean
  /** 是否去掉内边距（例如内部要放整宽表格） */
  flush?: boolean
}

export function Card({ title, subtitle, action, dense, flush, children, className, ...rest }: CardProps) {
  const padding = flush ? '' : dense ? 'p-3.5' : 'p-5'
  return (
    <div className={['ab-card', padding, className].filter(Boolean).join(' ')} {...rest}>
      {(title || action) && (
        <div className="mb-3.5 flex items-start justify-between gap-3">
          <div className="min-w-0">
            {title ? (
              <h3 className="truncate text-ab-headline text-label">{title}</h3>
            ) : null}
            {subtitle ? <p className="mt-0.5 text-ab-footnote text-label-2">{subtitle}</p> : null}
          </div>
          {action ? <div className="shrink-0">{action}</div> : null}
        </div>
      )}
      {children}
    </div>
  )
}

// -----------------------------------------------------------------------------
// 分段控件（Apple Segmented Control）
// -----------------------------------------------------------------------------
export interface SegmentedOption<T extends string> {
  value: T
  label: ReactNode
  icon?: IconName
  disabled?: boolean
  title?: string
}

export interface SegmentedProps<T extends string> {
  options: SegmentedOption<T>[]
  value: T
  onChange: (value: T) => void
  size?: 'sm' | 'md'
  ariaLabel?: string
  className?: string
}

/**
 * 分段控件：选中项由一条滑动的"药丸"指示（`layoutId` 实现）。
 * 这是 Apple 控件最具辨识度的动效之一 —— 指示器**移动**而非跳变。
 */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  size = 'md',
  ariaLabel,
  className,
}: SegmentedProps<T>) {
  // 同一页面可能有多个分段控件，layoutId 必须唯一，否则指示器会"串门"
  const layoutId = useId()
  const height = size === 'sm' ? 'h-6 text-ab-caption' : 'h-7 text-ab-footnote'

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={[
        'inline-flex items-center gap-0.5 rounded-ab-sm bg-surface-3/70 p-0.5',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={option.disabled}
            title={option.title}
            onClick={() => !option.disabled && onChange(option.value)}
            className={[
              'relative inline-flex items-center gap-1 rounded-[7px] px-2.5 font-medium transition-colors duration-ab1 ease-ab-standard',
              height,
              active ? 'text-label' : 'text-label-2 hover:text-label',
              option.disabled ? 'cursor-not-allowed opacity-40' : '',
            ]
              .filter(Boolean)
              .join(' ')}
          >
            {active && (
              <motion.span
                layoutId={layoutId}
                transition={{ duration: 0.22, ease: [0.32, 0.72, 0, 1] }}
                className="absolute inset-0 rounded-[7px] bg-surface shadow-ab-1"
              />
            )}
            <span className="relative z-10 inline-flex items-center gap-1">
              {option.icon ? <Icon name={option.icon} size={13} /> : null}
              {option.label}
            </span>
          </button>
        )
      })}
    </div>
  )
}

// -----------------------------------------------------------------------------
// 开关（iOS 风格）
// -----------------------------------------------------------------------------
export interface SwitchProps {
  checked: boolean
  onChange: (checked: boolean) => void
  label: string
  /** 不显示可见文字，仅用 aria-label（用于工具栏） */
  hideLabel?: boolean
  disabled?: boolean
}

export function Switch({ checked, onChange, label, hideLabel, disabled }: SwitchProps) {
  return (
    <label
      className={[
        'inline-flex items-center gap-2',
        disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
      ].join(' ')}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={[
          'relative h-[22px] w-[38px] shrink-0 rounded-full transition-colors duration-ab2 ease-ab-standard',
          checked ? 'bg-positive' : 'bg-surface-3',
        ].join(' ')}
      >
        <motion.span
          layout
          transition={{ type: 'spring', stiffness: 620, damping: 34 }}
          className="absolute top-[2px] h-[18px] w-[18px] rounded-full bg-white shadow-ab-1"
          style={{ left: checked ? 18 : 2 }}
        />
      </button>
      {!hideLabel ? <span className="text-ab-subhead text-label">{label}</span> : null}
    </label>
  )
}

// -----------------------------------------------------------------------------
// 阶段徽标（REQ-10：诚实标注"已预留 / 未实现"）
// -----------------------------------------------------------------------------
export function PhaseBadge({ phase, className }: { phase: string; className?: string }) {
  return <span className={['ab-phase-badge', className].filter(Boolean).join(' ')}>{phase}</span>
}

// -----------------------------------------------------------------------------
// 图标按钮
// -----------------------------------------------------------------------------
export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: IconName
  /** 无障碍标签，同时作为原生 tooltip */
  label: string
  active?: boolean
  size?: number
}

export function IconButton({ icon, label, active, size = 17, className, ...rest }: IconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={[
        'ab-btn-ghost',
        active ? 'bg-accent/12 text-accent' : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    >
      <Icon name={icon} size={size} />
    </button>
  )
}

// -----------------------------------------------------------------------------
// 骨架屏
// -----------------------------------------------------------------------------
export function Skeleton({ className }: { className?: string }) {
  return <div className={['ab-skeleton rounded-ab-sm', className].filter(Boolean).join(' ')} />
}

// -----------------------------------------------------------------------------
// 空状态
// -----------------------------------------------------------------------------
export interface EmptyStateProps {
  icon?: IconName
  title: string
  body?: ReactNode
  action?: ReactNode
}

export function EmptyState({ icon = 'folder', title, body, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center">
      <div className="mb-1 flex h-12 w-12 items-center justify-center rounded-ab-md bg-surface-3/80 text-label-3">
        <Icon name={icon} size={22} />
      </div>
      <p className="text-ab-headline text-label">{title}</p>
      {body ? <p className="max-w-sm text-ab-footnote text-label-2">{body}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  )
}

// -----------------------------------------------------------------------------
// 键位提示（为 P1 的快捷键体系预留视觉语言）
// -----------------------------------------------------------------------------
export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded-[5px] border border-separator/70 bg-surface-2 px-1.5 py-[1px] font-mono text-ab-caption2 text-label-2">
      {children}
    </kbd>
  )
}

// -----------------------------------------------------------------------------
// 数值徽标（环比同比的正负色）
// -----------------------------------------------------------------------------
export function DeltaBadge({ text, tone }: { text: string; tone: 'positive' | 'negative' | 'neutral' }) {
  const toneClass =
    tone === 'positive' ? 'text-positive' : tone === 'negative' ? 'text-negative' : 'text-label-2'
  return (
    <span className={['ab-tnum inline-flex items-center text-ab-footnote font-medium', toneClass].join(' ')}>
      {text}
    </span>
  )
}
