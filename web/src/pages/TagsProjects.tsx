/**
 * 标签与项目管理页（P1）。
 *
 * 三者放在一页的理由
 * ------------------
 * 标签、项目、成员在数据模型上是同一类东西：**没有层级、字段极少、
 * 被流水引用的横向维度**。用户在心里的分类也是"记账用的几个维度"，
 * 而不是三个互不相干的模块 —— 分成三个页面只会让他来回跳。
 *
 * 交互立场：这些维度都是**可选**的，所以界面必须回答"不填会怎样"。
 * 每一节都写清楚它的用途，而不是只给一个空列表和「新建」按钮。
 */

import { motion } from 'framer-motion'
import { useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Segmented, Skeleton } from '@/components/ui'
import { staggerContainer, staggerItem } from '@/design/motion'
import { useI18n } from '@/i18n'
import { api, ApiError, type Member, type Project, type Tag } from '@/lib/api'
import { displayMinor, parseAmountToMinor } from '@/lib/format'
import { usePreferences } from '@/app/preferences'

import { Modal } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

type Tab = 'tags' | 'projects' | 'members'

const COLOR_CHOICES = [
  'accent',
  'teal',
  'indigo',
  'purple',
  'pink',
  'orange',
  'yellow',
  'green',
  'red',
  'gray',
]

export function TagsProjectsPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { resourceStates, tags, projects, members, refresh } = useLedger()

  const [tab, setTab] = useState<Tab>('tags')
  const [dialog, setDialog] = useState<
    | { mode: 'create'; tab: Tab }
    | { mode: 'edit'; tab: Tab; id: number }
    | { mode: 'delete'; tab: Tab; id: number; name: string }
    | null
  >(null)
  const [form, setForm] = useState({ name: '', color: 'teal', note: '', budget: '0', isSelf: false })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const openCreate = () => {
    const defaultColor = tab === 'tags' ? 'teal' : tab === 'projects' ? 'indigo' : 'purple'
    setForm({ name: '', color: defaultColor, note: '', budget: '0', isSelf: false })
    setError(null)
    setDialog({ mode: 'create', tab })
  }

  const openEdit = (item: Tag | Project | Member) => {
    setForm({
      name: item.name,
      color: item.color,
      note: item.note,
      budget: 'budget_minor' in item ? String(item.budget_minor / 100) : '0',
      isSelf: 'is_self' in item ? item.is_self : false,
    })
    setError(null)
    setDialog({ mode: 'edit', tab, id: item.id })
  }

  const submit = async () => {
    if (!dialog || dialog.mode === 'delete') return
    const name = form.name.trim()
    if (!name) {
      setError(t('ledger.nameRequired'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (dialog.tab === 'tags') {
        const payload = { name, color: form.color, note: form.note }
        if (dialog.mode === 'create') await api.createTag(payload)
        else await api.updateTag(dialog.id, payload)
      } else if (dialog.tab === 'projects') {
        const payload = {
          name,
          color: form.color,
          note: form.note,
          budget_minor: parseAmountToMinor(form.budget) ?? 0,
        }
        if (dialog.mode === 'create') await api.createProject(payload)
        else await api.updateProject(dialog.id, payload)
      } else {
        const payload = { name, color: form.color, note: form.note, is_self: form.isSelf }
        if (dialog.mode === 'create') await api.createMember(payload)
        else await api.updateMember(dialog.id, payload)
      }
      await refresh()
      setDialog(null)
    } catch (cause) {
      // 重名（409）是最常见的失败，给出明确提示而不是原始错误串
      setError(
        cause instanceof ApiError && cause.code === 'conflict' ? t('ledger.nameTaken') : String(cause),
      )
    } finally {
      setBusy(false)
    }
  }

  const confirmDelete = async () => {
    if (!dialog || dialog.mode !== 'delete') return
    setBusy(true)
    try {
      if (dialog.tab === 'tags') await api.deleteTag(dialog.id)
      else if (dialog.tab === 'projects') await api.deleteProject(dialog.id)
      else await api.deleteMember(dialog.id)
      await refresh()
      setDialog(null)
    } catch (cause) {
      // 被流水引用时后端会拒绝并说明原因（含引用条数），原样展示即可 ——
      // 那句提示本身就是可操作的（"请先解除引用"）
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const toggleArchive = async (project: Project) => {
    await api.updateProject(project.id, {
      status: project.status === 'archived' ? 'active' : 'archived',
    })
    await refresh()
  }

  if (resourceStates.tags === 'loading' && resourceStates.projects === 'loading' && resourceStates.members === 'loading') {
    return (
      <div className="space-y-3">
        <Skeleton className="h-9 w-64" />
        {[0, 1, 2].map((key) => (
          <Skeleton key={key} className="h-16 w-full" />
        ))}
      </div>
    )
  }

  const counts = { tags: tags.length, projects: projects.length, members: members.length }
  const unavailable = resourceStates[tab] !== 'ready' && counts[tab] === 0
  const tabLabels: Record<Tab, string> = {
    tags: t('ledger.tabTags'),
    projects: t('ledger.tabProjects'),
    members: t('ledger.tabMembers'),
  }
  const hints: Record<Tab, string> = {
    tags: t('ledger.tagsHint'),
    projects: t('ledger.projectsHint'),
    members: t('ledger.membersHint'),
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <Segmented
            value={tab}
            onChange={(next) => setTab(next as Tab)}
            options={(['tags', 'projects', 'members'] as Tab[]).map((item) => ({
              value: item,
              label: `${tabLabels[item]} ${resourceStates[item] !== 'ready' && counts[item] === 0 ? '—' : counts[item]}`,
            }))}
          />
        </div>
        <button type="button" className="ab-btn-primary" onClick={openCreate}>
          <Icon name="plus" size={14} />
          {tab === 'tags' ? t('ledger.newTag') : tab === 'projects' ? t('ledger.newProjectTitle') : t('ledger.newMember')}
        </button>
      </div>

      {/* 说明这个维度"是干什么的、不填会怎样" —— 可选维度的界面必须自己解释价值 */}
      <p className="text-ab-footnote text-label-3">{hints[tab]}</p>

      {counts[tab] === 0 ? (
        <Card flush>
          <EmptyState
            icon={tab === 'tags' ? 'tags' : tab === 'projects' ? 'ledger' : 'goals'}
            title={unavailable ? tabLabels[tab] : t('ledger.emptyDimensionTitle')}
            body={unavailable ? t('ledgerLoading.notLoaded') : hints[tab]}
            action={
              <button type="button" className="ab-btn-primary" onClick={openCreate}>
                <Icon name="plus" size={14} />
                {t('common.new')}
              </button>
            }
          />
        </Card>
      ) : (
        <motion.div variants={staggerContainer} initial="hidden" animate="show" className="space-y-3">
          {tab === 'tags'
            ? tags.map((tag) => (
                <motion.div key={tag.id} variants={staggerItem}>
                  <Card dense className="group">
                    <div className="flex items-center gap-3">
                      <span
                        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px]"
                        style={{
                          backgroundColor: `rgb(var(--ab-${tag.color}) / 0.16)`,
                          color: `rgb(var(--ab-${tag.color}))`,
                        }}
                      >
                        <Icon name="tags" size={14} />
                      </span>
                      <span className="flex-1 truncate text-ab-subhead font-medium text-label">{tag.name}</span>
                      {tag.note ? (
                        <span className="hidden truncate text-ab-caption text-label-3 sm:block">{tag.note}</span>
                      ) : null}
                      <RowActions
                        onEdit={() => openEdit(tag)}
                        onDelete={() => {
                          setError(null)
                          setDialog({ mode: 'delete', tab: 'tags', id: tag.id, name: tag.name })
                        }}
                      />
                    </div>
                  </Card>
                </motion.div>
              ))
            : null}

          {tab === 'projects'
            ? projects.map((project) => (
                <motion.div key={project.id} variants={staggerItem}>
                  <Card dense className="group">
                    <div className="flex items-center gap-3">
                      <span
                        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px]"
                        style={{
                          backgroundColor: `rgb(var(--ab-${project.color}) / 0.16)`,
                          color: `rgb(var(--ab-${project.color}))`,
                        }}
                      >
                        <Icon name="ledger" size={14} />
                      </span>
                      <span className="flex-1 truncate text-ab-subhead font-medium text-label">
                        {project.name}
                      </span>
                      {project.status === 'archived' ? (
                        <span className="ab-phase-badge">{t('ledger.archived')}</span>
                      ) : null}
                      {project.budget_minor > 0 ? (
                        <span className="ab-tnum text-ab-caption text-label-3">
                          {t('ledger.budget')} {displayMinor(project.budget_minor, 'CNY', preferences.privacy_mode)}
                        </span>
                      ) : null}
                      <RowActions
                        extra={
                          <button
                            type="button"
                            className="ab-icon-btn"
                            aria-label={t('common.archive')}
                            title={t('common.archive')}
                            onClick={() => void toggleArchive(project)}
                          >
                            <Icon name={project.status === 'archived' ? 'refresh' : 'folder'} size={14} />
                          </button>
                        }
                        onEdit={() => openEdit(project)}
                        onDelete={() => {
                          setError(null)
                          setDialog({ mode: 'delete', tab: 'projects', id: project.id, name: project.name })
                        }}
                      />
                    </div>
                  </Card>
                </motion.div>
              ))
            : null}

          {tab === 'members'
            ? members.map((member) => (
                <motion.div key={member.id} variants={staggerItem}>
                  <Card dense className="group">
                    <div className="flex items-center gap-3">
                      <span
                        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px]"
                        style={{
                          backgroundColor: `rgb(var(--ab-${member.color}) / 0.16)`,
                          color: `rgb(var(--ab-${member.color}))`,
                        }}
                      >
                        <Icon name="goals" size={14} />
                      </span>
                      <span className="flex-1 truncate text-ab-subhead font-medium text-label">
                        {member.name}
                      </span>
                      {member.is_self ? (
                        <span className="ab-phase-badge">{t('ledger.self')}</span>
                      ) : null}
                      <RowActions
                        onEdit={() => openEdit(member)}
                        onDelete={() => {
                          setError(null)
                          setDialog({ mode: 'delete', tab: 'members', id: member.id, name: member.name })
                        }}
                      />
                    </div>
                  </Card>
                </motion.div>
              ))
            : null}
        </motion.div>
      )}

      {/* 新建 / 编辑 */}
      <Modal
        open={dialog?.mode === 'create' || dialog?.mode === 'edit'}
        title={
          dialog?.mode === 'edit'
            ? t('common.edit')
            : dialog?.tab === 'projects'
              ? t('ledger.newProjectTitle')
              : t('common.new')
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
            <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
          ) : null}
          <div>
            <label className="ab-field-label" htmlFor="dimension-name">
              {t('ledger.name')}
            </label>
            <input
              id="dimension-name"
              className="ab-input"
              autoFocus
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder={
                dialog?.tab === 'tags'
                  ? t('ledger.tagPlaceholder')
                  : dialog?.tab === 'projects'
                    ? t('ledger.projectPlaceholder')
                    : t('ledger.memberPlaceholder')
              }
            />
          </div>

          {dialog?.tab === 'projects' ? (
            <div>
              <label className="ab-field-label" htmlFor="dimension-budget">
                {t('ledger.budget')}
              </label>
              <input
                id="dimension-budget"
                className="ab-input ab-tnum"
                inputMode="decimal"
                value={form.budget}
                onChange={(event) => setForm({ ...form, budget: event.target.value })}
              />
              <p className="mt-1 text-ab-caption1 text-label-3">{t('ledger.budgetHint')}</p>
            </div>
          ) : null}

          {dialog?.tab === 'members' ? (
            <label className="flex items-center gap-2 text-ab-subhead text-label-2">
              <input
                type="checkbox"
                className="accent-accent"
                checked={form.isSelf}
                onChange={(event) => setForm({ ...form, isSelf: event.target.checked })}
              />
              {t('ledger.isSelf')}
            </label>
          ) : null}

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

          <div>
            <label className="ab-field-label" htmlFor="dimension-note">
              {t('ledger.note')}
            </label>
            <input
              id="dimension-note"
              className="ab-input"
              value={form.note}
              onChange={(event) => setForm({ ...form, note: event.target.value })}
            />
          </div>
        </div>
      </Modal>

      {/* 删除 */}
      <Modal
        open={dialog?.mode === 'delete'}
        title={t('ledger.deleteDimensionTitle')}
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
              onClick={() => void confirmDelete()}
            >
              {t('common.delete')}
            </button>
          </>
        }
      >
        <p className="text-ab-subhead text-label-2">
          {t('ledger.deleteDimensionBody', { name: dialog?.mode === 'delete' ? dialog.name : '' })}
        </p>
        {error ? (
          <div className="mt-2 rounded-ab-sm bg-warning/10 px-3 py-2 text-ab-footnote text-warning">{error}</div>
        ) : null}
      </Modal>
    </div>
  )
}

/** 行内操作按钮（编辑 / 删除 / 额外动作）。悬停才显示，避免列表视觉噪声 */
function RowActions({
  onEdit,
  onDelete,
  extra,
}: {
  onEdit: () => void
  onDelete: () => void
  extra?: React.ReactNode
}) {
  const { t } = useI18n()
  return (
    <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
      {extra}
      <button type="button" className="ab-icon-btn" aria-label={t('common.edit')} onClick={onEdit}>
        <Icon name="edit" size={14} />
      </button>
      <button
        type="button"
        className="ab-icon-btn hover:text-negative"
        aria-label={t('common.delete')}
        onClick={onDelete}
      >
        <Icon name="trash" size={14} />
      </button>
    </div>
  )
}
