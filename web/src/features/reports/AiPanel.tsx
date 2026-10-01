/**
 * AI 分析（P5）。
 *
 * 两个组件，一份配置：
 * * `AiConfigCard` 放在设置页；
 * * `AiAnalysisPanel` 放在报表页 —— 配置也内联一份，
 *   因为**"配好了没生效"这件事只有在这里才能立刻看出来**：
 *   保存后马上分析一次，来源徽章会直接告诉你是 `online` 还是 `offline`。
 *
 * 界面上最重要的一件事：**如实标注来源**。
 * 离线回落时给出明确徽章与原因（没填 key / 断网 / 超时 / 服务端错误），
 * 绝不让用户以为眼前这段文字是模型说的。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, Skeleton } from '@/components/ui'
import { useI18n } from '@/i18n'
import {
  api,
  ApiError,
  type AiAnalysisRecord,
  type AiConfig,
  type AiStreamMeta,
  type ReportKind,
} from '@/lib/api'
import { streamSse } from '@/lib/sse'

/** 离线回落原因 → 用户能看懂的一句话 */
const REASON_KEYS: Record<string, string> = {
  disabled: 'ai.reason.disabled',
  no_key: 'ai.reason.no_key',
  network: 'ai.reason.network',
  timeout: 'ai.reason.timeout',
  server: 'ai.reason.server',
  bad_response: 'ai.reason.bad_response',
}

export function AiConfigCard() {
  const { t } = useI18n()
  const [config, setConfig] = useState<AiConfig | null>(null)
  const [keyDraft, setKeyDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setConfig(await api.aiConfig())
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const save = async (changes: Partial<AiConfig> & { api_key?: string }) => {
    setBusy(true)
    setError(null)
    try {
      const next = await api.updateAiConfig(changes)
      setConfig(next)
      setKeyDraft('')
      setNotice(t('ai.saved'))
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  if (!config) return <Skeleton className="h-32 w-full" />

  return (
    <Card title={t('ai.title')} subtitle={t('ai.subtitle')}>
      <div className="space-y-3">
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

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={config.enabled}
            disabled={busy}
            onChange={(event) => void save({ enabled: event.target.checked })}
          />
          <span className="text-ab-footnote text-label-2">{t('ai.enabled')}</span>
        </label>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="ab-field-label" htmlFor="ai-base">
              {t('ai.baseUrl')}
            </label>
            <input
              id="ai-base"
              className="ab-input"
              defaultValue={config.base_url}
              disabled={busy}
              onBlur={(event) => {
                if (event.target.value !== config.base_url) {
                  void save({ base_url: event.target.value })
                }
              }}
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="ai-model">
              {t('ai.model')}
            </label>
            <input
              id="ai-model"
              className="ab-input"
              defaultValue={config.model}
              disabled={busy}
              onBlur={(event) => {
                if (event.target.value !== config.model) void save({ model: event.target.value })
              }}
            />
          </div>
        </div>

        <div>
          <label className="ab-field-label" htmlFor="ai-key">
            {t('ai.apiKey')}
          </label>
          <div className="flex gap-2">
            <input
              id="ai-key"
              type="password"
              className="ab-input flex-1"
              autoComplete="off"
              value={keyDraft}
              placeholder={config.has_key ? t('ai.keyPresent') : t('ai.keyAbsent')}
              onChange={(event) => setKeyDraft(event.target.value)}
            />
            <button
              type="button"
              className="ab-btn-secondary shrink-0"
              disabled={busy || !keyDraft}
              onClick={() => void save({ api_key: keyDraft })}
            >
              {t('common.save')}
            </button>
            {/* 空串=清除。不给这个按钮，用户就没办法删掉一个填错的 key */}
            {config.has_key ? (
              <button
                type="button"
                className="ab-btn-secondary shrink-0 !text-negative"
                disabled={busy}
                onClick={() => void save({ api_key: '' })}
              >
                {t('ai.clearKey')}
              </button>
            ) : null}
          </div>
          <p className="mt-1 text-ab-caption1 text-label-3">{t('ai.keyHint')}</p>
        </div>

        <label className="flex items-start gap-2">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={config.redact}
            disabled={busy}
            onChange={(event) => void save({ redact: event.target.checked })}
          />
          <span className="text-ab-footnote text-label-2">
            {t('ai.redact')}
            <span className="mt-0.5 block text-ab-caption1 text-label-3">{t('ai.redactHint')}</span>
          </span>
        </label>
      </div>
    </Card>
  )
}

export function AiAnalysisPanel({
  kind,
  start,
  end,
  /** 报告变了就清掉上一次的分析，避免两份不同期间的分析同时挂在屏幕上 */
  resetKey,
}: {
  kind: ReportKind
  start?: string
  end?: string
  resetKey: string
}) {
  const { t } = useI18n()
  const [content, setContent] = useState('')
  const [meta, setMeta] = useState<AiStreamMeta | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [history, setHistory] = useState<AiAnalysisRecord[]>([])
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    setContent('')
    setMeta(null)
    setError(null)
    abortRef.current?.abort()
  }, [resetKey])

  const loadHistory = useCallback(async () => {
    try {
      const body = await api.aiAnalyses(5)
      setHistory(body.items)
    } catch {
      // 历史读不到不影响主流程
    }
  }, [])

  useEffect(() => {
    void loadHistory()
  }, [loadHistory])

  const run = async () => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setContent('')
    setMeta(null)
    setError(null)
    setRunning(true)
    await streamSse(
      api.aiAnalyzeUrl({ kind, start, end }),
      {
        onEvent: (event, data) => {
          if (event === 'meta') setMeta(data as AiStreamMeta)
          else if (event === 'delta') {
            setContent((previous) => previous + ((data as { text: string }).text ?? ''))
          } else if (event === 'done') {
            setRunning(false)
            void loadHistory()
          }
        },
        onError: (cause) => {
          setError(cause.message)
          setRunning(false)
        },
      },
      controller.signal,
    )
    setRunning(false)
  }

  return (
    <Card>
      <div className="flex flex-wrap items-center gap-2">
        <div className="ab-section-label !px-0 !pt-0">{t('ai.analysis')}</div>
        {meta ? (
          <span
            className={`ab-chip ${meta.source === 'online' ? '!text-accent' : '!text-warning'}`}
            data-active
          >
            <Icon name={meta.source === 'online' ? 'check' : 'warning'} size={11} />
            {meta.source === 'online' ? t('ai.sourceOnline') : t('ai.sourceOffline')}
          </span>
        ) : null}
        {meta?.model ? (
          <span className="ab-tnum text-ab-caption1 text-label-3">{meta.model}</span>
        ) : null}
        <button
          type="button"
          className="ab-btn-primary ml-auto"
          disabled={running}
          onClick={() => void run()}
        >
          <Icon name="statistics" size={13} />
          {running ? t('ai.running') : content ? t('ai.rerun') : t('ai.start')}
        </button>
      </div>

      {/* 离线回落必须**明说**，并给出原因 */}
      {meta && meta.source === 'offline' ? (
        <p className="mt-2 rounded-ab-sm bg-warning/10 px-3 py-2 text-ab-footnote text-label-2">
          {t(REASON_KEYS[meta.fallback_reason] ?? 'ai.reason.unknown')}
          {meta.error ? <span className="ml-1 text-ab-caption1 text-label-3">（{meta.error}）</span> : null}
        </p>
      ) : null}
      {error ? (
        <p className="mt-2 rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
          {error}
        </p>
      ) : null}

      {content ? (
        <div className="mt-3 whitespace-pre-wrap text-ab-footnote leading-relaxed text-label-2">
          {content}
          {running ? <span className="ml-0.5 animate-pulse">▌</span> : null}
        </div>
      ) : running ? (
        <div className="mt-3 space-y-2">
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      ) : (
        <p className="mt-3 text-ab-footnote text-label-3">{t('ai.idle')}</p>
      )}

      {history.length > 0 ? (
        <div className="mt-4 border-t border-separator/60 pt-3">
          <div className="ab-section-label !px-0">{t('ai.history')}</div>
          <div className="space-y-1">
            {history.map((row) => (
              <div key={row.id} className="flex items-center gap-2 text-ab-caption1 text-label-3">
                <span className={`ab-chip ${row.source === 'online' ? '' : '!text-warning'}`}>
                  {row.source === 'online' ? t('ai.sourceOnline') : t('ai.sourceOffline')}
                </span>
                <span className="ab-tnum">
                  {row.period_start} — {row.period_end}
                </span>
                {row.redacted ? <span>{t('ai.redactedBadge')}</span> : null}
                <button
                  type="button"
                  className="ml-auto text-accent"
                  onClick={() => {
                    setContent(row.content)
                    setMeta({
                      source: row.source,
                      model: row.model,
                      fallback_reason: row.fallback_reason,
                      error: row.error,
                      analysis_id: row.id,
                    })
                  }}
                >
                  {t('ai.view')}
                </button>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </Card>
  )
}
