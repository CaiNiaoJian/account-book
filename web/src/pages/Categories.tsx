/**
 * 分类管理页（P1）—— 分类树维护。
 *
 * 它要回答用户三个问题：
 *   1. 我有哪些分类？（树形展示，含内置与自建）
 *   2. 怎么调整？（新建子分类、改名、换图标、隐藏、合并）
 *   3. 为什么不能删？（内置分类受保护 —— 界面必须**说明原因并给出替代动作**，
 *      而不是只弹一句"不可删除"）
 *
 * 交互取舍：内置分类用「不可删除」而不是「完全只读」，
 * 因为内置项也要允许改名与换图标（每个家庭的"餐饮"含义并不相同）。
 */

import { motion } from 'framer-motion'
import { useMemo, useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Segmented, Skeleton } from '@/components/ui'
import { staggerContainer, staggerItem } from '@/design/motion'
import { useI18n } from '@/i18n'
import { api, ApiError, type Category, type CategoryNode } from '@/lib/api'

import { IconPicker, Modal } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

const COLOR_CHOICES = ['accent', 'orange', 'yellow', 'green', 'teal', 'indigo', 'purple', 'pink', 'red', 'gray']

export function CategoriesPage() {
  const { t } = useI18n()
  const { resourceStates, tree, categories, refresh } = useLedger()
  const [kind, setKind] = useState<'expense' | 'income'>('expense')
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const [dialog, setDialog] = useState<
    | { mode: 'create'; parent: Category | null }
    | { mode: 'edit'; target: Category }
    | { mode: 'merge'; target: Category }
    | { mode: 'delete'; target: Category }
    | null
  >(null)
  const [iconPickerOpen, setIconPickerOpen] = useState(false)
  const [form, setForm] = useState({ name: '', icon: 'tag', color: 'accent' })
  const [mergeTarget, setMergeTarget] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const nodes = kind === 'expense' ? tree.expense : tree.income
  const count = useMemo(
    () => categories.filter((item) => item.kind === kind).length,
    [categories, kind],
  )

  const openCreate = (parent: Category | null) => {
    setForm({ name: '', icon: parent?.icon ?? 'tag', color: parent?.color ?? 'accent' })
    setError(null)
    setDialog({ mode: 'create', parent })
  }

  const openEdit = (target: Category) => {
    setForm({ name: target.name, icon: target.icon, color: target.color })
    setError(null)
    setDialog({ mode: 'edit', target })
  }

  const submit = async () => {
    if (!dialog) return
    if (!form.name.trim()) {
      setError(t('ledger.categoryNameRequired'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (dialog.mode === 'create') {
        await api.createCategory({
          name: form.name.trim(),
          kind,
          parent_id: dialog.parent?.id ?? null,
          icon: form.icon,
          color: form.color,
        })
      } else if (dialog.mode === 'edit') {
        await api.updateCategory(dialog.target.id, {
          name: form.name.trim(),
          icon: form.icon,
          color: form.color,
        })
      }
      await refresh()
      setDialog(null)
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.code === 'conflict'
          ? t('ledger.categoryNameTaken')
          : cause instanceof Error
            ? cause.message
            : String(cause),
      )
    } finally {
      setBusy(false)
    }
  }

  const toggleHidden = async (target: Category) => {
    await api.hideCategory(target.id, !target.is_hidden)
    await refresh()
  }

  const doMerge = async () => {
    if (!dialog || dialog.mode !== 'merge' || mergeTarget === null) return
    setBusy(true)
    try {
      const result = await api.mergeCategory(dialog.target.id, mergeTarget)
      await refresh()
      setDialog(null)
      setError(null)
      // 合并结果值得明确告知：用户需要知道有多少流水被迁移了
      window.dispatchEvent(
        new CustomEvent('ab:toast', {
          detail: t('ledger.mergedResult', {
            transactions: result.transactions,
            splits: result.splits,
          }),
        }),
      )
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const doDelete = async () => {
    if (!dialog || dialog.mode !== 'delete') return
    setBusy(true)
    try {
      await api.deleteCategory(dialog.target.id)
      await refresh()
      setDialog(null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const renderNode = (node: CategoryNode, depth: number) => {
    const isCollapsed = collapsed.has(node.id)
    const hasChildren = node.children.length > 0
    return (
      <div key={node.id}>
        <div
          className="ab-row"
          style={{ paddingLeft: 12 + depth * 18 }}
          data-hidden={node.is_hidden}
        >
          {hasChildren ? (
            <button
              type="button"
              className="ab-icon-btn !h-5 !w-5"
              aria-label={isCollapsed ? t('common.expand') : t('common.collapse')}
              onClick={() =>
                setCollapsed((previous) => {
                  const next = new Set(previous)
                  if (next.has(node.id)) next.delete(node.id)
                  else next.add(node.id)
                  return next
                })
              }
            >
              <Icon name={isCollapsed ? 'chevronRight' : 'chevronDown'} size={12} />
            </button>
          ) : (
            <span className="w-5" />
          )}

          <span
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px]"
            style={{
              backgroundColor: `rgb(var(--ab-${node.color}) / 0.14)`,
              color: `rgb(var(--ab-${node.color}))`,
            }}
          >
            <Icon name={node.icon} size={15} />
          </span>

          <span
            className={`flex-1 truncate text-ab-subhead ${
              node.is_hidden ? 'text-label-3 line-through' : 'font-medium text-label'
            }`}
          >
            {node.name}
          </span>

          {node.is_system ? (
            <span className="ab-phase-badge" title={t('ledger.systemCategoryHint')}>
              {t('ledger.systemCategory')}
            </span>
          ) : null}
          {hasChildren ? (
            <span className="ab-tnum text-ab-caption text-label-3">{node.children.length}</span>
          ) : null}

          <div className="flex shrink-0 items-center gap-0.5">
            <button
              type="button"
              className="ab-icon-btn"
              aria-label={t('ledger.addChild')}
              title={t('ledger.addChild')}
              onClick={() => openCreate(node)}
            >
              <Icon name="plus" size={13} />
            </button>
            <button
              type="button"
              className="ab-icon-btn"
              aria-label={t('common.edit')}
              title={t('common.edit')}
              onClick={() => openEdit(node)}
            >
              <Icon name="edit" size={13} />
            </button>
            <button
              type="button"
              className="ab-icon-btn"
              aria-label={node.is_hidden ? t('ledger.show') : t('ledger.hide')}
              title={node.is_hidden ? t('ledger.show') : t('ledger.hide')}
              onClick={() => void toggleHidden(node)}
            >
              <Icon name={node.is_hidden ? 'eye' : 'hide'} size={13} />
            </button>
            <button
              type="button"
              className="ab-icon-btn"
              aria-label={t('ledger.merge')}
              title={t('ledger.merge')}
              onClick={() => {
                setMergeTarget(null)
                setError(null)
                setDialog({ mode: 'merge', target: node })
              }}
            >
              <Icon name="merge" size={13} />
            </button>
            {node.is_system ? null : (
              <button
                type="button"
                className="ab-icon-btn hover:text-negative"
                aria-label={t('common.delete')}
                title={t('common.delete')}
                onClick={() => {
                  setError(null)
                  setDialog({ mode: 'delete', target: node })
                }}
              >
                <Icon name="trash" size={13} />
              </button>
            )}
          </div>
        </div>

        {hasChildren && !isCollapsed ? (
          <div>{node.children.map((child) => renderNode(child, depth + 1))}</div>
        ) : null}
      </div>
    )
  }

  if (resourceStates.expenseTree === 'loading' && resourceStates.incomeTree === 'loading') {
    return (
      <div className="space-y-3">
        <Skeleton className="h-9 w-64" />
        {[0, 1, 2, 3, 4].map((key) => (
          <Skeleton key={key} className="h-11 w-full" />
        ))}
      </div>
    )
  }

  const unavailable = nodes.length === 0 && resourceStates[kind === 'expense' ? 'expenseTree' : 'incomeTree'] !== 'ready'

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <Segmented
            value={kind}
            onChange={(next) => setKind(next as 'expense' | 'income')}
            options={[
              { value: 'expense', label: t('ledger.expenseCategories') },
              { value: 'income', label: t('ledger.incomeCategories') },
            ]}
          />
          <span className="text-ab-footnote text-label-3">
            {unavailable ? '—' : t('ledger.categoryCount', { count })}
          </span>
        </div>
        <button type="button" className="ab-btn-primary" onClick={() => openCreate(null)}>
          <Icon name="plus" size={14} />
          {t('ledger.newCategory')}
        </button>
      </div>

      {nodes.length === 0 ? (
        <Card flush>
          <EmptyState icon="categories" title={t(unavailable ? 'nav.categories' : 'ledger.noCategoriesTitle')} body={t(unavailable ? 'ledgerLoading.notLoaded' : 'ledger.noCategoriesBody')} />
        </Card>
      ) : (
        <Card flush className="overflow-hidden">
          <motion.div variants={staggerContainer} initial="hidden" animate="show">
            {nodes.map((node) => (
              <motion.div key={node.id} variants={staggerItem}>
                {renderNode(node, 0)}
              </motion.div>
            ))}
          </motion.div>
        </Card>
      )}

      {/* 新建 / 编辑 */}
      <Modal
        open={dialog?.mode === 'create' || dialog?.mode === 'edit'}
        title={
          dialog?.mode === 'edit'
            ? t('ledger.editCategory')
            : dialog?.mode === 'create' && dialog.parent
              ? t('ledger.newChildCategory', { parent: dialog.parent.name })
              : t('ledger.newCategory')
        }
        onClose={() => setDialog(null)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setDialog(null)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void submit()}>
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="space-y-3.5">
          {error ? (
            <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
              {error}
            </div>
          ) : null}
          <div>
            <label className="ab-field-label" htmlFor="category-name">
              {t('ledger.categoryName')}
            </label>
            <input
              id="category-name"
              className="ab-input"
              autoFocus
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder={t('ledger.categoryNamePlaceholder')}
            />
          </div>
          <div>
            <span className="ab-field-label">{t('ledger.icon')}</span>
            <div className="flex items-center gap-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-ab-sm border border-separator/60 text-label-2">
                <Icon name={form.icon} size={17} />
              </span>
              <button type="button" className="ab-btn-secondary" onClick={() => setIconPickerOpen(true)}>
                {t('ledger.chooseIcon')}
              </button>
            </div>
          </div>
          <div>
            <span className="ab-field-label">{t('ledger.color')}</span>
            <div className="flex flex-wrap gap-1.5">
              {COLOR_CHOICES.map((color) => (
                <button
                  key={color}
                  type="button"
                  aria-label={color}
                  aria-pressed={form.color === color}
                  onClick={() => setForm({ ...form, color })}
                  className={`h-7 w-7 rounded-full border-2 transition-transform ${
                    form.color === color ? 'border-label scale-110' : 'border-transparent'
                  }`}
                  style={{ backgroundColor: `rgb(var(--ab-${color}))` }}
                />
              ))}
            </div>
          </div>
        </div>
      </Modal>

      <Modal
        open={iconPickerOpen}
        title={t('ledger.chooseIcon')}
        size="lg"
        onClose={() => setIconPickerOpen(false)}
      >
        <IconPicker
          kind="category"
          value={form.icon}
          onChange={(icon) => {
            setForm({ ...form, icon })
            setIconPickerOpen(false)
          }}
        />
      </Modal>

      {/* 合并 */}
      <Modal
        open={dialog?.mode === 'merge'}
        title={t('ledger.mergeTitle')}
        onClose={() => setDialog(null)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setDialog(null)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="ab-btn-primary"
              disabled={busy || mergeTarget === null}
              onClick={() => void doMerge()}
            >
              {t('ledger.merge')}
            </button>
          </>
        }
      >
        <p className="text-ab-subhead text-label-2">
          {t('ledger.mergeBody', { name: dialog?.mode === 'merge' ? dialog.target.name : '' })}
        </p>
        <div className="mt-3 max-h-64 space-y-1 overflow-y-auto">
          {categories
            .filter((item) => item.kind === kind && item.id !== (dialog?.mode === 'merge' ? dialog.target.id : -1))
            .map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setMergeTarget(item.id)}
                className={`flex w-full items-center gap-2 rounded-ab-sm px-2.5 py-1.5 text-left text-ab-subhead transition-colors ${
                  mergeTarget === item.id ? 'bg-accent/12 text-accent' : 'text-label hover:bg-hairline/5'
                }`}
              >
                <Icon name={item.icon} size={14} />
                <span className="flex-1 truncate">{item.name}</span>
                {item.is_system ? <span className="ab-phase-badge">{t('ledger.systemCategory')}</span> : null}
              </button>
            ))}
        </div>
        {error ? <p className="mt-2 text-ab-footnote text-negative">{error}</p> : null}
      </Modal>

      {/* 删除 */}
      <Modal
        open={dialog?.mode === 'delete'}
        title={t('ledger.deleteCategoryTitle')}
        size="sm"
        onClose={() => setDialog(null)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setDialog(null)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="ab-btn-primary !bg-negative"
              disabled={busy}
              onClick={() => void doDelete()}
            >
              {t('common.delete')}
            </button>
          </>
        }
      >
        <p className="text-ab-subhead text-label-2">
          {t('ledger.deleteCategoryBody', { name: dialog?.mode === 'delete' ? dialog.target.name : '' })}
        </p>
        {error ? (
          // 被引用时后端会给出"请先迁移或合并"，这里补充一个直达动作的说明
          <div className="mt-2 rounded-ab-sm bg-warning/10 px-3 py-2 text-ab-footnote text-warning">
            {error}
          </div>
        ) : null}
      </Modal>
    </div>
  )
}
