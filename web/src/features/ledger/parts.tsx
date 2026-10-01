/**
 * 记账界面的共享零件。
 *
 * 这些组件的共同点：**在多个页面里必须长得一模一样**。
 * 金额的着色规则、分类选择器的交互、金额输入框的解析容错，
 * 只要有一处不同，用户就会怀疑"是不是两个地方算得不一样"。
 */

import { AnimatePresence, motion } from 'framer-motion'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { Icon } from '@/components/Icon'
import { usePreferences } from '@/app/preferences'
import { ACCOUNT_ICON_GROUPS, CATEGORY_ICON_GROUPS } from '@/design/icons'
import { useI18n } from '@/i18n'
import { displayMinor, parseAmountToMinor } from '@/lib/format'
import { api, ApiError, type Category } from '@/lib/api'

import { useLedger } from './store'

// -----------------------------------------------------------------------------
// 金额
// -----------------------------------------------------------------------------
interface MoneyTextProps {
  minor: number
  currency?: string
  /** 方向着色：收入绿、支出红。传 undefined 表示按正负自动判断 */
  tone?: 'income' | 'expense' | 'neutral' | 'auto'
  className?: string
  showSign?: boolean
}

/**
 * 金额文本。
 *
 * 三条统一规则（改这里即全局生效）：
 *   * 隐私模式下自动遮罩 —— 不能有哪个角落漏出真实金额；
 *   * 等宽数字（`ab-tnum`）—— 否则金额列在纵向对比时轻微抖动，很显廉价；
 *   * 收入/支出按**方向**着色，而不是按正负 —— 一笔支出显示为红色，
 *     即便它内部用正数存储。
 */
export function MoneyText({
  minor,
  currency = 'CNY',
  tone = 'auto',
  className = '',
  showSign = false,
}: MoneyTextProps) {
  const { preferences } = usePreferences()
  const resolved =
    tone === 'auto' ? (minor > 0 ? 'income' : minor < 0 ? 'expense' : 'neutral') : tone
  const color =
    resolved === 'income' ? 'text-positive' : resolved === 'expense' ? 'text-negative' : 'text-label'
  const text = displayMinor(minor, currency, preferences.privacy_mode, { showSign })

  return <span className={`ab-tnum font-semibold ${color} ${className}`}>{text}</span>
}

// -----------------------------------------------------------------------------
// 弹层
// -----------------------------------------------------------------------------
interface ModalProps {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  /** 宽度档位：sm 用于确认框，md 用于表单，lg 用于图标选择器 */
  size?: 'sm' | 'md' | 'lg'
}

const MODAL_WIDTH = { sm: 'max-w-sm', md: 'max-w-lg', lg: 'max-w-2xl' } as const

/**
 * 模态弹层。
 *
 * 用 `role="dialog"` + `aria-modal` 而不是自己造可访问性语义：
 * 读屏软件、键盘 Tab 顺序、Esc 关闭都依赖这些标准属性。
 * Esc 与背景点击关闭是"弹层"这一交互的基本预期，缺一个都会让人恼火。
 */
export function Modal({ open, title, onClose, children, footer, size = 'md' }: ModalProps) {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }}
        >
          <div
            className="absolute inset-0 bg-black/25 backdrop-blur-[2px]"
            onClick={onClose}
            aria-hidden
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label={title}
            className={`relative w-full ${MODAL_WIDTH[size]} ab-card overflow-hidden`}
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 4 }}
            transition={{ duration: 0.18, ease: [0.32, 0.72, 0, 1] }}
          >
            <header className="flex items-center justify-between border-b border-separator/60 px-4 py-3">
              <h2 className="text-ab-headline font-semibold text-label">{title}</h2>
              <button
                type="button"
                onClick={onClose}
                className="ab-icon-btn"
                aria-label="close"
              >
                <Icon name="close" size={15} />
              </button>
            </header>
            <div className="max-h-[70vh] overflow-y-auto px-4 py-3.5">{children}</div>
            {footer ? (
              <footer className="flex items-center justify-end gap-2 border-t border-separator/60 px-4 py-3">
                {footer}
              </footer>
            ) : null}
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  )
}

// -----------------------------------------------------------------------------
// 金额输入
// -----------------------------------------------------------------------------
interface AmountFieldProps {
  value: number | null
  onChange: (minor: number | null) => void
  currency?: string
  autoFocus?: boolean
  onSubmit?: () => void
  placeholder?: string
}

/**
 * 金额输入框。
 *
 * 内部保存**文本**而不是数字：用户输入 `12.` 或 `12.30` 的过程中，
 * 若每敲一个字符就回写一个规范化的数字，光标会跳、小数位会被吃掉。
 * 只在失焦或提交时才解析为最小单位。
 */
export function AmountField({
  value,
  onChange,
  currency = 'CNY',
  autoFocus,
  onSubmit,
  placeholder,
}: AmountFieldProps) {
  const { t } = useI18n()
  const [text, setText] = useState(value === null ? '' : String(value / 100))
  const [invalid, setInvalid] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus()
  }, [autoFocus])

  const commit = (raw: string) => {
    if (raw.trim() === '') {
      setInvalid(false)
      onChange(null)
      return
    }
    const minor = parseAmountToMinor(raw, currency)
    if (minor === null) {
      setInvalid(true)
      return
    }
    setInvalid(false)
    onChange(minor)
  }

  return (
    <div>
      <div
        className={`flex items-center gap-2 rounded-ab-sm border bg-surface px-3 py-2 transition-colors ${
          invalid ? 'border-negative' : 'border-separator focus-within:border-accent'
        }`}
      >
        <span className="text-ab-title3 font-semibold text-label-3">{currency === 'CNY' ? '¥' : currency}</span>
        <input
          ref={inputRef}
          inputMode="decimal"
          className="ab-tnum w-full bg-transparent text-ab-title2 font-semibold tracking-tight text-label outline-none"
          value={text}
          placeholder={placeholder ?? '0.00'}
          onChange={(event) => {
            setText(event.target.value)
            commit(event.target.value)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && onSubmit) {
              event.preventDefault()
              onSubmit()
            }
          }}
        />
      </div>
      {invalid ? <p className="mt-1 text-ab-caption1 text-negative">{t('ledger.amountInvalid')}</p> : null}
    </div>
  )
}

// -----------------------------------------------------------------------------
// 图标选择器
// -----------------------------------------------------------------------------
interface IconPickerProps {
  value: string
  onChange: (icon: string) => void
  /** 用哪一套图标目录（分类 / 账户） */
  kind?: 'category' | 'account'
}

/** 分组图标选择器：按语义分组，比字母序更容易找到目标 */
export function IconPicker({ value, onChange, kind = 'category' }: IconPickerProps) {
  const { t } = useI18n()
  const groups = kind === 'category' ? CATEGORY_ICON_GROUPS : ACCOUNT_ICON_GROUPS

  return (
    <div className="space-y-3">
      {groups.map((group) => (
        <div key={group.id}>
          <div className="mb-1.5 text-ab-caption1 font-medium uppercase tracking-wide text-label-3">
            {t(`design.iconGroup.${group.id}`)}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {group.icons.map((icon) => (
              <button
                key={icon}
                type="button"
                onClick={() => onChange(icon)}
                aria-label={icon}
                aria-pressed={value === icon}
                className={`flex h-9 w-9 items-center justify-center rounded-ab-sm border transition-all ${
                  value === icon
                    ? 'border-accent bg-accent/15 text-accent'
                    : 'border-separator/60 text-label-2 hover:border-separator hover:bg-hairline/5'
                }`}
              >
                <Icon name={icon} size={17} />
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

// -----------------------------------------------------------------------------
// 分类选择器
// -----------------------------------------------------------------------------
interface CategoryPickerProps {
  value: number | null
  onChange: (categoryId: number | null) => void
  kind: 'expense' | 'income'
  /** 紧凑模式用于表单内嵌 */
  compact?: boolean
}

/**
 * 分类选择器。
 *
 * 交互取舍：用**两级平铺**而不是弹出树 ——
 * 记账时最常见的动作是"选一个子分类"，展开式树需要两次点击，
 * 而两级平铺一次就能看到全部叶子。分类超过两级时只展示前两级，
 * 更深的层级通过搜索到达（P2 补搜索）。
 */
export function CategoryPicker({ value, onChange, kind, compact }: CategoryPickerProps) {
  const { t } = useI18n()
  const { tree } = useLedger()
  const [expanded, setExpanded] = useState<number | null>(null)

  const nodes = kind === 'expense' ? tree.expense : tree.income
  const visible = useMemo(() => nodes.filter((node) => !node.is_hidden), [nodes])

  useEffect(() => {
    // 已选分类属于哪个根节点，就默认展开它 —— 编辑已有流水时立刻能看到选中项
    if (value === null) return
    for (const node of visible) {
      if (node.id === value || node.children.some((child) => child.id === value)) {
        setExpanded(node.id)
        return
      }
    }
  }, [value, visible])

  return (
    <div className={compact ? 'space-y-1' : 'space-y-2'}>
      {visible.map((node) => {
        const isOpen = expanded === node.id
        const selectedInside = node.id === value || node.children.some((c) => c.id === value)
        return (
          <div key={node.id}>
            <button
              type="button"
              onClick={() => {
                if (node.children.length === 0) {
                  onChange(node.id)
                } else {
                  setExpanded(isOpen ? null : node.id)
                  // 一级分类本身也可以被选中（用户不想细分时）
                  if (!isOpen) onChange(node.id)
                }
              }}
              className={`flex w-full items-center gap-2 rounded-ab-sm px-2.5 py-1.5 text-left text-ab-subhead transition-colors ${
                selectedInside ? 'bg-accent/12 text-accent' : 'text-label hover:bg-hairline/5'
              }`}
            >
              <Icon name={node.icon} size={15} />
              <span className="flex-1 truncate font-medium">{node.name}</span>
              {node.children.length > 0 ? (
                <Icon name={isOpen ? 'chevronDown' : 'chevronRight'} size={13} />
              ) : null}
            </button>
            {isOpen && node.children.length > 0 ? (
              <div className="ml-6 mt-0.5 flex flex-wrap gap-1">
                {node.children
                  .filter((child) => !child.is_hidden)
                  .map((child) => (
                    <button
                      key={child.id}
                      type="button"
                      onClick={() => onChange(child.id)}
                      className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-ab-footnote transition-all ${
                        value === child.id
                          ? 'border-accent bg-accent text-white'
                          : 'border-separator/60 text-label-2 hover:border-accent/50 hover:text-label'
                      }`}
                    >
                      <Icon name={child.icon} size={12} />
                      {child.name}
                    </button>
                  ))}
              </div>
            ) : null}
          </div>
        )
      })}
      {visible.length === 0 ? (
        <p className="px-2 py-1 text-ab-footnote text-label-3">{t('ledger.noCategory')}</p>
      ) : null}
    </div>
  )
}

// -----------------------------------------------------------------------------
// 标签选择器
// -----------------------------------------------------------------------------
interface TagPickerProps {
  /** 已选标签的 **id** 列表 */
  value: number[]
  onChange: (tagIds: number[]) => void
  /** 允许在选标签时就地新建（记账时想到一个新维度是常态） */
  allowCreate?: boolean
  compact?: boolean
}

/**
 * 标签选择器。
 *
 * 为什么支持"就地新建"：标签是**记账过程中才想起来的**维度
 * （"这笔是出差"）。如果强制用户先跑去标签管理页建好再回来，
 * 他大概率会放弃打标签 —— 一个用不起来的维度等于不存在。
 *
 * 选中态用标签自身的颜色填充，与列表里的展示保持一致。
 */
export function TagPicker({ value, onChange, allowCreate = true, compact }: TagPickerProps) {
  const { t } = useI18n()
  const { tags, refresh } = useLedger()
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const toggle = (tagId: number) => {
    onChange(value.includes(tagId) ? value.filter((id) => id !== tagId) : [...value, tagId])
  }

  const create = async () => {
    const name = draft.trim()
    if (!name) return
    setBusy(true)
    setError(null)
    try {
      const created = await api.createTag({ name })
      await refresh()
      onChange([...value, created.id])
      setDraft('')
    } catch (cause) {
      // 重名是最常见的失败（409）。给出明确提示，而不是让输入框默默清空
      setError(
        cause instanceof ApiError && cause.code === 'conflict' ? t('ledger.tagNameTaken') : String(cause),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
      <div className="flex flex-wrap gap-1.5">
        {tags.length === 0 ? <span className="text-ab-footnote text-label-3">{t('ledger.noTags')}</span> : null}
        {tags.map((tag) => {
          const active = value.includes(tag.id)
          return (
            <button
              key={tag.id}
              type="button"
              onClick={() => toggle(tag.id)}
              aria-pressed={active}
              className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-[3px] text-ab-footnote font-medium transition-all duration-150 ${
                active ? '' : 'border-separator/70 text-label-2 hover:border-separator hover:text-label'
              }`}
              style={
                active
                  ? {
                      backgroundColor: `rgb(var(--ab-${tag.color}) / 0.16)`,
                      borderColor: `rgb(var(--ab-${tag.color}))`,
                      color: `rgb(var(--ab-${tag.color}))`,
                    }
                  : undefined
              }
            >
              {/* 未选中时用小圆点保留颜色线索：一排彩色标签会把界面搅乱，
                  但完全去掉颜色又让人认不出自己建的那个 */}
              {active ? null : (
                <span
                  className="h-1.5 w-1.5 rounded-full"
                  style={{ backgroundColor: `rgb(var(--ab-${tag.color}))` }}
                />
              )}
              {tag.name}
            </button>
          )
        })}
      </div>

      {allowCreate ? (
        <div className="flex items-center gap-1.5">
          <input
            className="ab-input !w-40 !py-1 !text-ab-footnote"
            value={draft}
            placeholder={t('ledger.newTagPlaceholder')}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void create()
              }
            }}
          />
          <button
            type="button"
            className="ab-btn-secondary !py-1"
            disabled={busy || !draft.trim()}
            onClick={() => void create()}
          >
            <Icon name="plus" size={12} />
            {t('ledger.addTag')}
          </button>
        </div>
      ) : null}
      {error ? <p className="text-ab-caption1 text-negative">{error}</p> : null}
    </div>
  )
}

// -----------------------------------------------------------------------------
// 分类图标徽标（列表里复用）
// -----------------------------------------------------------------------------
export function CategoryBadge({ category, size = 30 }: { category: Category | undefined; size?: number }) {
  if (!category) {
    return (
      <span
        className="flex shrink-0 items-center justify-center rounded-[10px] bg-hairline/10 text-label-3"
        style={{ width: size, height: size }}
      >
        <Icon name="other" size={size * 0.5} />
      </span>
    )
  }
  return (
    <span
      className="flex shrink-0 items-center justify-center rounded-[10px]"
      style={{
        width: size,
        height: size,
        backgroundColor: `rgb(var(--ab-${category.color}) / 0.14)`,
        color: `rgb(var(--ab-${category.color}))`,
      }}
    >
      <Icon name={category.icon} size={size * 0.52} />
    </span>
  )
}
