/**
 * 指标口径说明（P3）。
 *
 * 为什么这一页必须有
 * ------------------
 * K 线图上的每个数字都要能被解释。一个说不清来路的指标比没有指标更糟 ——
 * 用户会照着它做判断，然后发现对不上。这里把四件事写清楚：
 * 每个价的定义、指标的参数、预热区间的作用、以及已知局限。
 *
 * **参数从接口读，不写死在文案里**：参数一改说明页自动跟着变，
 * 不会出现"文档说 MA20、代码里是 MA25"这种最难发现的不一致。
 */

import { useCallback, useEffect, useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { useI18n } from '@/i18n'
import { api, type KlineParams } from '@/lib/api'

export function MetricsPage() {
  const { t } = useI18n()
  const [params, setParams] = useState<KlineParams | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setParams(await api.klineParams())
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (error) {
    return (
      <Card flush>
        <EmptyState icon="alert" title={t('metrics.loadFailed')} body={error} />
      </Card>
    )
  }

  if (!params) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-56 w-full" />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <Card title={t('metrics.priceTitle')}>
        <p className="text-ab-footnote text-label-2">{t('metrics.priceIntro')}</p>
        <dl className="mt-3 space-y-2">
          {(['open', 'high', 'low', 'close', 'volume'] as const).map((field) => (
            <div key={field} className="flex gap-3">
              <dt className="w-16 shrink-0 text-ab-footnote font-semibold text-label">
                {t(`kline.${field}`)}
              </dt>
              <dd className="text-ab-footnote text-label-2">{t(`metrics.price.${field}`)}</dd>
            </div>
          ))}
        </dl>
      </Card>

      <Card title={t('metrics.paramsTitle')}>
        <p className="text-ab-footnote text-label-2">{t('metrics.paramsIntro')}</p>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <ParamTile label={t('metrics.maWindows')} value={params.ma_windows.map((w) => `MA${w}`).join(' · ')} />
          <ParamTile
            label="MACD"
            value={`(${params.macd.fast}, ${params.macd.slow}, ${params.macd.signal})`}
          />
          <ParamTile label="RSI" value={`RSI${params.rsi_period}`} />
        </div>
        <div className="mt-3 rounded-ab-sm bg-accent/8 px-3 py-2 text-ab-footnote text-label-2">
          <span className="font-semibold">{t('metrics.warmupLabel')}</span>
          {t('metrics.warmupBody', { bars: params.warmup_bars })}
        </div>
      </Card>

      <Card title={t('metrics.rulesTitle')}>
        <ul className="space-y-2.5">
          {(['clamp', 'ma', 'macd', 'rsi', 'drawdown'] as const).map((rule) => (
            <li key={rule} className="flex gap-2.5">
              <Icon name="check" size={14} className="mt-0.5 shrink-0 text-accent" />
              <span className="text-ab-footnote text-label-2">{t(`metrics.rule.${rule}`)}</span>
            </li>
          ))}
        </ul>
      </Card>

      <Card title={t('metrics.limitTitle')}>
        <ul className="space-y-2.5">
          {(['estimate', 'noIntraday', 'noZeroBars', 'notAdvice'] as const).map((item) => (
            <li key={item} className="flex gap-2.5">
              <Icon name="warning" size={14} className="mt-0.5 shrink-0 text-warning" />
              <span className="text-ab-footnote text-label-2">{t(`metrics.limit.${item}`)}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  )
}

function ParamTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-ab-sm bg-surface-2 px-3 py-2">
      <div className="text-ab-caption text-label-3">{label}</div>
      <div className="ab-tnum text-ab-subhead font-semibold text-label">{value}</div>
    </div>
  )
}
