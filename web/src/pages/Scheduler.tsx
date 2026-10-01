/**
 * 定时任务与提醒中心（P6）。
 *
 * 这个页面有两块性质完全不同的东西，因此刻意分区：
 *
 * * **提醒中心**（上）：这是整个应用里唯一会**拦住用户**的界面。
 *   门禁要有出口 —— 稍后提醒有次数上限、本月跳过必须给原因。
 *   只有"必须填"会让真的没工资的月份变成死锁，而用户会开始随手填假数据。
 * * **定时任务**（下）：只读性质的调度视图。
 *   未实现的类型（备份、报表）**显示成灰色"尚未实现"**，
 *   而不是一个看起来在跑的成功状态。
 */

import { useCallback, useEffect, useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { ApiError, api, type NotificationItem, type PendingPrompt, type ScheduledTask, type TaskHealth, type TaskRun } from '@/lib/api'
import { useI18n } from '@/i18n'
import { formatDateTime } from '@/lib/format'

const CATCH_UP_LABELS: Record<string, string> = {
  startup: 'scheduler.catchUp.startup',
  immediate: 'scheduler.catchUp.immediate',
  record_only: 'scheduler.catchUp.record_only',
}

export function SchedulerPage() {
  const { t } = useI18n()
  const [prompts, setPrompts] = useState<PendingPrompt[]>([])
  const [tasks, setTasks] = useState<ScheduledTask[]>([])
  const [runs, setRuns] = useState<TaskRun[]>([])
  const [health, setHealth] = useState<TaskHealth | null>(null)
  const [notifications, setNotifications] = useState<NotificationItem[]>([])
  const [unread, setUnread] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [skipFor, setSkipFor] = useState<PendingPrompt | null>(null)
  const [skipReason, setSkipReason] = useState('')
  const [taskOpen, setTaskOpen] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [blocking, taskList, history, healthBody, notes] = await Promise.all([
        api.blockingPrompts(),
        api.scheduledTasks(),
        api.taskHistory(50),
        api.taskHealth(),
        api.notifications({ limit: 50 }),
      ])
      setPrompts(blocking.items)
      setTasks(taskList.items)
      setRuns(history.items)
      setHealth(healthBody)
      setNotifications(notes.items)
      setUnread(notes.unread)
      setError(null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const act = async (run: () => Promise<unknown>, message?: string) => {
    try {
      await run()
      if (message) setNotice(message)
      setError(null)
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    }
  }

  if (loading && tasks.length === 0 && prompts.length === 0) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-56 w-full" />
      </div>
    )
  }

  const hasAnything = tasks.length > 0 || prompts.length > 0 || notifications.length > 0

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('scheduler.title')}</h1>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            type="button"
            className="ab-btn-secondary"
            onClick={() => void act(() => api.runDueTasks(false), t('scheduler.ran'))}
          >
            <Icon name="repeat" size={13} />
            {t('scheduler.runDue')}
          </button>
          <button type="button" className="ab-btn-primary" onClick={() => setTaskOpen(true)}>
            <Icon name="plus" size={13} />
            {t('scheduler.newTask')}
          </button>
        </div>
      </div>

      {notice ? (
        <div className="rounded-ab-sm bg-positive/10 px-3 py-2 text-ab-footnote text-positive">
          {notice}
        </div>
      ) : null}
      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
          {error}
        </div>
      ) : null}

      {!hasAnything ? (
        <Card flush>
          <EmptyState
            icon="scheduler"
            title={t('scheduler.empty')}
            body={t('scheduler.emptyBody')}
            action={
              <button type="button" className="ab-btn-primary" onClick={() => setTaskOpen(true)}>
                <Icon name="plus" size={14} />
                {t('scheduler.newTask')}
              </button>
            }
          />
        </Card>
      ) : null}

      {/* 提醒中心：唯一会拦住用户的区域 */}
      {prompts.length > 0 ? (
        <Card className="!border-warning/40">
          <div className="ab-section-label !px-0 !pt-0">{t('scheduler.blocking')}</div>
          <div className="space-y-2">
            {prompts.map((prompt) => (
              <div
                key={prompt.id}
                className="rounded-ab-sm bg-warning/10 px-3 py-2.5"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Icon name="warning" size={13} className="text-warning" />
                  <span className="text-ab-footnote font-medium text-label">{prompt.title}</span>
                  <span className="text-ab-caption1 text-label-3">
                    {t('scheduler.snoozeLeft', { count: prompt.snooze_left })}
                  </span>
                </div>
                {prompt.body ? (
                  <p className="mt-1 text-ab-caption1 text-label-2">{prompt.body}</p>
                ) : null}
                {/* **出口必须显眼。** 只有"必须填"会让真的没工资的月份变成死锁 */}
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    className="ab-btn-primary !py-1 text-ab-caption1"
                    onClick={() => {
                      if (prompt.target_kind === 'payroll_record') {
                        window.location.hash = ''
                        window.location.assign(`/payroll?record=${prompt.target_id ?? ''}`)
                      }
                    }}
                  >
                    {t('scheduler.goFill')}
                  </button>
                  <button
                    type="button"
                    className="ab-btn-secondary !py-1 text-ab-caption1"
                    disabled={prompt.snooze_left <= 0}
                    title={
                      prompt.snooze_left <= 0 ? t('scheduler.snoozeExhausted') : undefined
                    }
                    onClick={() =>
                      void act(() => api.snoozePrompt(prompt.id, 30), t('scheduler.snoozed'))
                    }
                  >
                    {t('scheduler.snooze')}
                  </button>
                  <button
                    type="button"
                    className="ab-btn-secondary !py-1 text-ab-caption1 !text-negative"
                    onClick={() => {
                      setSkipFor(prompt)
                      setSkipReason('')
                    }}
                  >
                    {t('scheduler.skip')}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {/* 任务列表 */}
      <Card>
        <div className="flex flex-wrap items-center gap-2">
          <div className="ab-section-label !px-0 !pt-0">{t('scheduler.tasks')}</div>
          {health && health.total > 0 ? (
            <span className="ab-tnum ml-auto text-ab-caption1 text-label-3">
              {t('scheduler.health', {
                success: health.success,
                skipped: health.skipped,
                failed: health.failed,
              })}
              {health.caught_up > 0
                ? ` · ${t('scheduler.caughtUpCount', { count: health.caught_up })}`
                : ''}
            </span>
          ) : null}
        </div>

        {tasks.length === 0 ? (
          <p className="mt-2 text-ab-footnote text-label-3">{t('scheduler.noTasks')}</p>
        ) : (
          <div className="mt-1 space-y-1">
            {tasks.map((task) => (
              <div key={task.id} className="ab-row">
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-1.5">
                    <span className="truncate text-ab-footnote text-label-2">{task.name}</span>
                    {/* **未实现的类型要显示成灰色"尚未实现"**，
                        而不是一个看起来在跑的成功状态 */}
                    {!task.implemented ? (
                      <span className="ab-chip !text-warning" data-active>
                        <Icon name="warning" size={10} />
                        {t('scheduler.notImplemented')}
                      </span>
                    ) : null}
                  </span>
                  <span className="block text-ab-caption1 text-label-3">
                    {task.not_implemented_reason ??
                      t(CATCH_UP_LABELS[task.catch_up_policy] ?? 'scheduler.catchUp.startup')}
                    {task.next_run_at
                      ? ` · ${t('scheduler.next', { time: formatDateTime(task.next_run_at) })}`
                      : ` · ${t('scheduler.noNextRun')}`}
                  </span>
                </span>
                <button
                  type="button"
                  className="ab-chip shrink-0"
                  onClick={() =>
                    void act(() => api.runScheduledTask(task.id), t('scheduler.taskRan'))
                  }
                >
                  {t('scheduler.runNow')}
                </button>
                <button
                  type="button"
                  className="ab-icon-btn shrink-0 hover:text-negative"
                  aria-label={t('common.delete')}
                  onClick={() => void act(() => api.deleteScheduledTask(task.id))}
                >
                  <Icon name="trash" size={12} />
                </button>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 执行历史 */}
      {runs.length > 0 ? (
        <Card>
          <div className="ab-section-label !px-0 !pt-0">{t('scheduler.history')}</div>
          <div className="mt-1 space-y-1">
            {runs.slice(0, 20).map((run) => (
              <div key={run.id} className="flex items-start gap-2 text-ab-footnote">
                <span className="ab-tnum w-32 shrink-0 text-ab-caption1 text-label-3">
                  {formatDateTime(run.started_at)}
                </span>
                <span
                  className={`shrink-0 ${
                    run.status === 'failed'
                      ? 'text-negative'
                      : run.status === 'skipped'
                        ? 'text-label-3'
                        : 'text-positive'
                  }`}
                >
                  {t(`scheduler.status.${run.status}`)}
                </span>
                <span className="min-w-0 flex-1 text-label-2">{run.summary || run.error}</span>
                {/* **补办要标出来**：软件当时没运行，事后补的 */}
                {run.caught_up ? (
                  <span className="shrink-0 text-ab-caption2 text-warning">
                    {t('scheduler.caughtUp')}
                  </span>
                ) : null}
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {/* 通知中心 */}
      {notifications.length > 0 ? (
        <Card>
          <div className="flex flex-wrap items-center gap-2">
            <div className="ab-section-label !px-0 !pt-0">{t('scheduler.notifications')}</div>
            {unread > 0 ? (
              <button
                type="button"
                className="ab-chip ml-auto"
                onClick={() => void act(() => api.readAllNotifications())}
              >
                {t('scheduler.markAllRead', { count: unread })}
              </button>
            ) : null}
          </div>
          <div className="mt-1 space-y-1">
            {notifications.map((note) => (
              <div key={note.id} className="flex items-start gap-2">
                <span
                  className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${
                    note.read
                      ? 'bg-transparent'
                      : note.level === 'error' || note.level === 'warn'
                        ? 'bg-warning'
                        : 'bg-accent'
                  }`}
                />
                <span className="min-w-0 flex-1">
                  <span className="block text-ab-footnote text-label-2">{note.title}</span>
                  {note.body ? (
                    <span className="block text-ab-caption1 text-label-3">{note.body}</span>
                  ) : null}
                </span>
                <span className="ab-tnum shrink-0 text-ab-caption2 text-label-3">
                  {note.created_at ? formatDateTime(note.created_at) : ''}
                </span>
                {!note.read ? (
                  <button
                    type="button"
                    className="shrink-0 text-ab-caption1 text-accent"
                    onClick={() => void act(() => api.readNotification(note.id))}
                  >
                    {t('scheduler.markRead')}
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {/* 跳过必须给原因 —— 这是合规出口，不是可有可无的形式 */}
      {skipFor ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
          <div className="w-full max-w-md rounded-ab-lg bg-surface p-4 shadow-ab-lg">
            <div className="text-ab-headline font-semibold text-label">
              {t('scheduler.skipTitle')}
            </div>
            <p className="mt-1 text-ab-footnote text-label-3">{t('scheduler.skipBody')}</p>
            <input
              className="ab-input mt-3"
              value={skipReason}
              autoFocus
              onChange={(event) => setSkipReason(event.target.value)}
              placeholder={t('scheduler.skipPlaceholder')}
            />
            <div className="mt-3 flex justify-end gap-1.5">
              <button
                type="button"
                className="ab-btn-secondary"
                onClick={() => setSkipFor(null)}
              >
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="ab-btn-primary"
                disabled={!skipReason.trim()}
                onClick={() =>
                  void act(async () => {
                    await api.skipPrompt(skipFor.id, skipReason.trim())
                    setSkipFor(null)
                  }, t('scheduler.skipped'))
                }
              >
                {t('scheduler.confirmSkip')}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <TaskDialog
        open={taskOpen}
        onClose={() => setTaskOpen(false)}
        onDone={() => {
          setTaskOpen(false)
          void load()
        }}
      />
    </div>
  )
}

function TaskDialog({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [kind, setKind] = useState('custom')
  const [frequency, setFrequency] = useState<'daily' | 'weekly' | 'monthly'>('monthly')
  const [dayOfMonth, setDayOfMonth] = useState('15')
  const [at, setAt] = useState('09:00')
  const [catchUp, setCatchUp] = useState<'startup' | 'immediate' | 'record_only'>('startup')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setName('')
      setKind('custom')
      setFrequency('monthly')
      setDayOfMonth('15')
      setAt('09:00')
      setCatchUp('startup')
      setError(null)
    }
  }, [open])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const rule: Record<string, unknown> = { frequency, at }
      if (frequency === 'monthly') rule.day_of_month = Number(dayOfMonth)
      await api.upsertScheduledTask({
        code: `custom:${Date.now()}`,
        name,
        kind,
        rule,
        catch_up_policy: catchUp,
      })
      onDone()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4 ${open ? '' : 'hidden'}`}
    >
      <div className="w-full max-w-md rounded-ab-lg bg-surface p-4 shadow-ab-lg">
        <div className="text-ab-headline font-semibold text-label">{t('scheduler.newTask')}</div>
        <div className="mt-3 space-y-3">
          {error ? (
            <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
              {error}
            </div>
          ) : null}
          <div>
            <label className="ab-field-label" htmlFor="task-name">
              {t('scheduler.taskName')}
            </label>
            <input
              id="task-name"
              className="ab-input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={t('scheduler.taskNamePlaceholder')}
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="task-frequency">
                {t('scheduler.frequency')}
              </label>
              <select
                id="task-frequency"
                className="ab-select"
                value={frequency}
                onChange={(event) =>
                  setFrequency(event.target.value as 'daily' | 'weekly' | 'monthly')
                }
              >
                <option value="daily">{t('scheduler.daily')}</option>
                <option value="weekly">{t('scheduler.weekly')}</option>
                <option value="monthly">{t('scheduler.monthly')}</option>
              </select>
            </div>
            <div>
              <label className="ab-field-label" htmlFor="task-at">
                {t('scheduler.at')}
              </label>
              <input
                id="task-at"
                type="time"
                className="ab-input ab-tnum"
                value={at}
                onChange={(event) => setAt(event.target.value)}
              />
            </div>
          </div>
          {frequency === 'monthly' ? (
            <div>
              <label className="ab-field-label" htmlFor="task-day">
                {t('scheduler.dayOfMonth')}
              </label>
              <input
                id="task-day"
                className="ab-input ab-tnum"
                inputMode="numeric"
                value={dayOfMonth}
                onChange={(event) => setDayOfMonth(event.target.value)}
              />
            </div>
          ) : null}
          <div>
            <label className="ab-field-label" htmlFor="task-catchup">
              {t('scheduler.catchUpLabel')}
            </label>
            <select
              id="task-catchup"
              className="ab-select"
              value={catchUp}
              onChange={(event) =>
                setCatchUp(event.target.value as 'startup' | 'immediate' | 'record_only')
              }
            >
              <option value="startup">{t('scheduler.catchUp.startup')}</option>
              <option value="immediate">{t('scheduler.catchUp.immediate')}</option>
              <option value="record_only">{t('scheduler.catchUp.record_only')}</option>
            </select>
            <p className="mt-1 text-ab-caption1 text-label-3">{t('scheduler.catchUpHint')}</p>
          </div>
        </div>
        <div className="mt-3 flex justify-end gap-1.5">
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={busy || !name}
            onClick={() => void submit()}
          >
            {t('common.save')}
          </button>
        </div>
      </div>
    </div>
  )
}
