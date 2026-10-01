/**
 * 资产卡片墙（P2 / REQ-15）。
 *
 * 三件事决定了这一页好不好用
 * --------------------------
 * 1. **卡面是数据驱动的自绘图形**，不是位图。渐变 + 纹理 + 光泽都由
 *    `card_artworks.spec` 描述，因此任意尺寸与 DPI 下都清晰，
 *    也随日夜主题自动校准（位图做不到这两点）。
 *    刻意不复制任何银行商标 —— 用户可换机构主色，也可自建卡面。
 * 2. **分组按用途而不是数据库枚举顺序**：银行卡 / 电子钱包 / 现金 /
 *    投资 / 负债。用户找卡时想的是"我那张信用卡"，不是 `credit_card`。
 * 3. **拖拽排序是整体提交**：拖完一次提交完整顺序，
 *    既避免 N 次写入，也不会让中途刷新看到半截顺序。
 */

import { Reorder } from 'framer-motion'
import { useCallback, useEffect, useMemo, useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { useI18n } from '@/i18n'
import { api, ApiError, type AssetWall, type CardArtwork, type CardItem } from '@/lib/api'
import { displayMinor } from '@/lib/format'
import { usePreferences } from '@/app/preferences'

import { CardImageUpload } from '@/features/ledger/Attachments'
import { Modal } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

const CARD_NETWORKS = [
  { value: '', label: '—' },
  { value: 'unionpay', label: '银联' },
  { value: 'visa', label: 'Visa' },
  { value: 'mastercard', label: 'Mastercard' },
  { value: 'amex', label: 'American Express' },
  { value: 'jcb', label: 'JCB' },
]

/**
 * 卡面配方 → CSS。渐变角度固定 135°，与信用卡的斜向光泽一致。
 *
 * **优先用用户上传的图片**：有图片时铺满卡面（cover），没有才回落到
 * spec 里的自绘渐变 —— 两条路都通，用户不必为了"想用自己那张卡的照片"
 * 而放弃自绘卡面（反之亦然）。
 */
function cardStyle(artwork: CardArtwork | undefined, fallbackColor: string): React.CSSProperties {
  if (artwork?.image_url) {
    return {
      backgroundImage: `url(${artwork.image_url})`,
      backgroundSize: 'cover',
      backgroundPosition: 'center',
    }
  }
  if (!artwork) {
    return { background: `linear-gradient(135deg, rgb(var(--ab-${fallbackColor})), rgb(var(--ab-${fallbackColor}) / 0.72))` }
  }
  const stops = [...artwork.spec.stops]
    .sort((a, b) => a[1] - b[1])
    .map(([color, position]) => `${color} ${position}%`)
    .join(', ')
  return { background: `linear-gradient(135deg, ${stops})` }
}

/** 卡面上的文字色：由卡面配方决定，避免"深色渐变配深色文字" */
function inkClass(artwork: AvailableInk): string {
  return artwork === 'dark' ? 'text-black/80' : 'text-white/95'
}

type AvailableInk = 'light' | 'dark'

export function CardsPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { refresh: refreshLedger } = useLedger()

  const [wall, setWall] = useState<AssetWall | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<CardItem | null>(null)
  const [form, setForm] = useState({ card_style: '', brand_key: '', card_network: '', card_no_tail: '' })
  const [busy, setBusy] = useState(false)
  const [showArchived, setShowArchived] = useState(false)

  const load = useCallback(async () => {
    try {
      setWall(await api.assetWall({ include_archived: showArchived }))
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [showArchived])

  useEffect(() => {
    void load()
  }, [load])

  const artworks = useMemo(() => {
    const index = new Map<string, CardArtwork>()
    for (const item of wall?.artworks ?? []) index.set(item.key, item)
    return index
  }, [wall])

  /** 拖拽结束时整体提交新顺序（跨分组也成立：顺序是全局的） */
  const commitOrder = async (cards: CardItem[]) => {
    if (!wall) return
    const all = wall.groups.flatMap((group) => group.cards)
    // 只对"被拖动的那一组"重排，其余保持原相对次序：
    // 分组是派生的展示结构，卡片顺序才是用户的意图
    const reordered = all.map((card) => (cards.some((item) => item.id === card.id) ? cards.shift() ?? card : card))
    try {
      await api.reorderAccounts(reordered.map((card) => card.id))
      await Promise.all([load(), refreshLedger()])
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    }
  }

  const openEdit = (card: CardItem) => {
    setForm({
      card_style: card.card_style,
      brand_key: card.brand_key,
      card_network: card.card_network,
      card_no_tail: card.card_no_tail,
    })
    setEditing(card)
    setError(null)
  }

  const submitCard = async () => {
    if (!editing) return
    setBusy(true)
    setError(null)
    try {
      await api.updateAccountCard(editing.id, {
        card_style: form.card_style,
        brand_key: form.brand_key,
        card_network: form.card_network,
        card_no_tail: form.card_no_tail,
      })
      await Promise.all([load(), refreshLedger()])
      setEditing(null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((key) => (
            <Skeleton key={key} className="h-20 w-full" />
          ))}
        </div>
        <Skeleton className="h-48 w-full" />
      </div>
    )
  }

  const summary = wall?.summary

  return (
    <div className="space-y-4">
      {/* 资产总览：四个数字回答"我有多少钱、能花多少、欠多少" */}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <SummaryTile label={t('ledger.netWorth')} value={summary?.net_worth_minor ?? 0} tone="label" />
        <SummaryTile label={t('cards.totalAssets')} value={summary?.assets_minor ?? 0} tone="positive" />
        <SummaryTile label={t('cards.available')} value={summary?.available_minor ?? 0} tone="accent" />
        <SummaryTile
          label={t('cards.creditAvailable')}
          value={summary?.credit_available_minor ?? 0}
          tone="purple"
          hint={
            summary && summary.credit_limit_minor > 0
              ? t('cards.creditUsage', {
                  used: displayMinor(summary.credit_used_minor, 'CNY', preferences.privacy_mode),
                  limit: displayMinor(summary.credit_limit_minor, 'CNY', preferences.privacy_mode),
                })
              : undefined
          }
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('nav.cards')}</h1>
        <button
          type="button"
          className="ab-chip"
          data-active={showArchived}
          onClick={() => setShowArchived((value) => !value)}
        >
          <Icon name="eye" size={12} />
          {t('ledger.showArchived')}
        </button>
      </div>

      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
      ) : null}

      {!wall || wall.groups.length === 0 ? (
        <Card flush>
          <EmptyState
            icon="cards"
            title={t('cards.emptyTitle')}
            body={t('cards.emptyBody')}
            action={
              <a href="/accounts" className="ab-btn-primary">
                <Icon name="accounts" size={14} />
                {t('nav.accounts')}
              </a>
            }
          />
        </Card>
      ) : (
        wall.groups.map((group) => (
          <section key={group.key} className="space-y-2">
            <div className="flex items-baseline gap-2">
              <h2 className="text-ab-subhead font-semibold text-label-2">{t(`cards.group.${group.key}`)}</h2>
              <span className="text-ab-caption text-label-3">
                {t('ledger.accountCount', { count: group.cards.length })}
              </span>
              <span className="ab-tnum ml-auto text-ab-caption text-label-3">
                {t('cards.groupTotal')}{' '}
                {displayMinor(
                  group.cards.reduce((sum, card) => sum + card.balance_minor, 0),
                  'CNY',
                  preferences.privacy_mode,
                )}
              </span>
            </div>
            <Reorder.Group
              axis="y"
              values={group.cards}
              onReorder={(next: CardItem[]) => void commitOrder(next)}
              className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"
            >
              {group.cards.map((card) => (
                <CardFace
                  key={card.id}
                  card={card}
                  artwork={artworks.get(card.card_style)}
                  onEdit={() => openEdit(card)}
                />
              ))}
            </Reorder.Group>
          </section>
        ))
      )}

      {/* 卡面自定义 */}
      <Modal
        open={editing !== null}
        title={t('cards.customizeTitle')}
        size="lg"
        onClose={() => setEditing(null)}
        footer={
          <>
            <button type="button" className="ab-btn-secondary" onClick={() => setEditing(null)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void submitCard()}>
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
            <span className="ab-field-label">{t('cards.artwork')}</span>
            <div className="grid grid-cols-4 gap-2">
              {(wall?.artworks ?? []).map((artwork) => (
                <button
                  key={artwork.key}
                  type="button"
                  onClick={() => setForm({ ...form, card_style: artwork.key })}
                  className={`overflow-hidden rounded-ab-sm border-2 transition-all ${
                    form.card_style === artwork.key ? 'border-accent' : 'border-transparent hover:border-separator'
                  }`}
                  aria-pressed={form.card_style === artwork.key}
                >
                  <span
                    className="flex h-12 items-end justify-start p-1.5 text-ab-caption2"
                    style={cardStyle(artwork, 'accent')}
                  >
                    <span className={inkClass(artwork.spec.ink as AvailableInk)}>{artwork.name}</span>
                  </span>
                </button>
              ))}
            </div>
          </div>

          {form.card_style ? (
            <CardImageUpload
              artworkId={(wall?.artworks ?? []).find((item) => item.key === form.card_style)?.id ?? 0}
              imageUrl={
                (wall?.artworks ?? []).find((item) => item.key === form.card_style)?.image_url ?? null
              }
              onUploaded={() => void Promise.all([load(), refreshLedger()])}
            />
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="card-institution">
                {t('cards.institution')}
              </label>
              <select
                id="card-institution"
                className="ab-select"
                value={form.brand_key}
                onChange={(event) => setForm({ ...form, brand_key: event.target.value })}
              >
                <option value="">{t('cards.noInstitution')}</option>
                {(wall?.institutions ?? []).map((institution) => (
                  <option key={institution.key} value={institution.key}>
                    {institution.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="ab-field-label" htmlFor="card-network">
                {t('cards.network')}
              </label>
              <select
                id="card-network"
                className="ab-select"
                value={form.card_network}
                onChange={(event) => setForm({ ...form, card_network: event.target.value })}
              >
                {CARD_NETWORKS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="ab-field-label" htmlFor="card-tail">
              {t('ledger.cardNoTail')}
            </label>
            <input
              id="card-tail"
              className="ab-input ab-tnum !w-32"
              maxLength={4}
              value={form.card_no_tail}
              onChange={(event) => setForm({ ...form, card_no_tail: event.target.value.replace(/\D/g, '') })}
              placeholder="1234"
            />
            <p className="mt-1 text-ab-caption1 text-label-3">{t('cards.tailHint')}</p>
          </div>
        </div>
      </Modal>
    </div>
  )
}

// -----------------------------------------------------------------------------
// 子组件
// -----------------------------------------------------------------------------
function SummaryTile({
  label,
  value,
  tone,
  hint,
}: {
  label: string
  value: number
  tone: 'label' | 'positive' | 'accent' | 'purple'
  hint?: string
}) {
  const { preferences } = usePreferences()
  const color =
    tone === 'positive'
      ? 'text-positive'
      : tone === 'accent'
        ? 'text-accent'
        : tone === 'purple'
          ? 'text-purple'
          : 'text-label'
  return (
    <Card dense>
      <div className="ab-section-label !px-0 !pt-0">{label}</div>
      <div className={`ab-tnum text-ab-title2 font-semibold tracking-tight ${color}`}>
        {displayMinor(value, 'CNY', preferences.privacy_mode)}
      </div>
      {hint ? <div className="mt-0.5 text-ab-caption1 text-label-3">{hint}</div> : null}
    </Card>
  )
}

/**
 * 一张卡。
 *
 * **刻意不给 `Reorder.Item` 挂 `variants`**：入场动画的 variants 需要父级
 * `initial`/`animate` 驱动，而 `Reorder.Group` 不提供这一层，
 * 结果是卡片停在 `initial`（`opacity: 0`）—— 界面看起来"分组标题在、卡片没了"。
 * 这个坑不报错、类型检查也过，只能靠肉眼看出来。拖拽反馈由 `whileDrag` 提供，足够。
 * * 交互细节：按下时轻微放大与上浮（Apple 的"卡片可拿起"暗示），
 * 松手后回位。拖拽由外层 `Reorder.Item` 提供。
 */
function CardFace({
  card,
  artwork,
  onEdit,
}: {
  card: CardItem
  artwork: CardArtwork | undefined
  onEdit: () => void
}) {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const ink = inkClass((artwork?.spec.ink ?? 'light') as AvailableInk)
  // 3D 倾斜：极轻的透视，让卡"有厚度"。减少动态效果时完全不做。
  const [tilt, setTilt] = useState({ x: 0, y: 0 })
  const tiltEnabled = !preferences.reduce_motion

  const handleTilt = (event: React.MouseEvent<HTMLDivElement>) => {
    if (!tiltEnabled) return
    const rect = event.currentTarget.getBoundingClientRect()
    // 归一化到 -0.5..0.5，再乘最大倾角
    const px = (event.clientX - rect.left) / rect.width - 0.5
    const py = (event.clientY - rect.top) / rect.height - 0.5
    setTilt({ x: -py * 6, y: px * 6 })
  }
  const limit = card.credit_limit_minor
  const usedRatio = limit > 0 ? Math.min(1, Math.max(0, card.credit_used_minor / limit)) : 0

  const NetworkMark = () => {
    if (!card.card_network) return null
    return (
      <span className={`text-ab-caption2 font-semibold uppercase tracking-wider ${ink} opacity-80`}>
        {card.card_network === 'unionpay' ? 'UnionPay' : card.card_network}
      </span>
    )
  }

  return (
    <Reorder.Item
      value={card}
      className="cursor-grab active:cursor-grabbing"
      whileDrag={{ scale: 1.03, zIndex: 5, cursor: 'grabbing' }}
      layout
    >
      <div
        className="relative flex h-44 flex-col justify-between overflow-hidden rounded-ab-lg p-4 shadow-ab-2 transition-shadow hover:shadow-ab-3"
        onMouseMove={handleTilt}
        // 离开必须归零：不归零卡片会停在歪掉的角度，看起来像渲染坏了
        onMouseLeave={() => setTilt({ x: 0, y: 0 })}
        style={{
          ...cardStyle(artwork, card.brand_color),
          transform: `perspective(900px) rotateX(${tilt.x}deg) rotateY(${tilt.y}deg)`,
          transformStyle: 'preserve-3d',
          transition: tiltEnabled ? 'transform 120ms cubic-bezier(0.32, 0.72, 0, 1)' : 'none',
        }}
      >
        {/* 光泽扫过：一条极淡的斜向高光，让卡面看起来有材质而不是一块纯色 */}
        {artwork && artwork.spec.texture !== 'none' ? (
          <span
            aria-hidden
            className="pointer-events-none absolute inset-0"
            style={{
              background: `linear-gradient(${artwork.spec.sheen}deg, rgb(255 255 255 / 0.16) 0%, rgb(255 255 255 / 0) 42%, rgb(0 0 0 / 0.10) 100%)`,
            }}
          />
        ) : null}
        {artwork?.spec.texture === 'grain' ? (
          <span
            aria-hidden
            className="pointer-events-none absolute inset-0 opacity-[0.14]"
            style={{
              backgroundImage:
                'radial-gradient(rgb(255 255 255 / 0.9) 0.5px, transparent 0.6px), radial-gradient(rgb(0 0 0 / 0.5) 0.5px, transparent 0.6px)',
              backgroundSize: '4px 4px, 5px 5px',
              backgroundPosition: '0 0, 2px 3px',
            }}
          />
        ) : null}

        <div className="relative flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className={`truncate text-ab-callout font-semibold ${ink}`}>{card.name}</div>
            <div className={`mt-0.5 truncate text-ab-caption ${ink} opacity-70`}>
              {[card.brand_name, card.card_no_tail && `•••• ${card.card_no_tail}`].filter(Boolean).join(' · ')}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <NetworkMark />
            <button
              type="button"
              onClick={onEdit}
              className={`rounded-ab-sm p-1 transition-colors hover:bg-white/20 ${ink}`}
              aria-label={t('cards.customizeTitle')}
            >
              <Icon name="customization" size={13} />
            </button>
          </div>
        </div>

        <div className="relative">
          <div className={`text-ab-caption ${ink} opacity-70`}>{t('ledger.balance')}</div>
          <div className={`ab-tnum text-ab-title2 font-semibold tracking-tight ${ink}`}>
            {displayMinor(card.balance_minor, card.currency, preferences.privacy_mode)}
          </div>

          {limit > 0 ? (
            <>
              <div className="mt-2 flex items-center justify-between text-ab-caption2">
                <span className={`${ink} opacity-75`}>
                  {t('cards.creditAvailableShort')}{' '}
                  {displayMinor(card.credit_available_minor, card.currency, preferences.privacy_mode)}
                </span>
                <span className={`${ink} opacity-75`}>
                  {(usedRatio * 100).toFixed(0)}%
                </span>
              </div>
              <div className="mt-1 h-1 overflow-hidden rounded-full bg-black/20">
                <div
                  className="h-full rounded-full bg-white/80 transition-all duration-500"
                  style={{ width: `${usedRatio * 100}%` }}
                />
              </div>
            </>
          ) : null}

          {card.bill_day || card.due_day ? (
            <div className={`mt-2 text-ab-caption2 ${ink} opacity-70`}>
              {[
                card.bill_day ? t('cards.billDay', { day: card.bill_day }) : '',
                card.due_day ? t('cards.dueDay', { day: card.due_day }) : '',
              ]
                .filter(Boolean)
                .join(' · ')}
            </div>
          ) : null}
        </div>
      </div>
    </Reorder.Item>
  )
}
