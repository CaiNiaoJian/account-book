/**
 * 存钱罐（P4 / 需求 16）。
 *
 * 三个刻意的设计
 * --------------
 *
 * **一、罐子里的液面是"进度"，不是装饰。**
 * 液面高度 = 余额 ÷ 目标，因此它必须能超过罐口（攒超了）也要能是 0（空罐子）。
 * 液面**不封顶在 100%**：把 150% 画成 100% 会让"我已经攒够了还多出一半"
 * 变成看不出来的信息，而那正是用户最想看到的。超过 100% 时罐身换成
 * 达成色，液面停在罐口 —— 这是唯一一处"取巧"，因为它表达的是
 * "满出来了"而不是"刚好满"。
 *
 * **二、两个口径的预计达成日并排显示，不挑一个。**
 * 线性稳定但对"最近停止存钱了"完全无感；加权敏感但会被一次性大额拉飞。
 * 挑一个藏起另一个等于替用户做了他没授权的假设。两者差距超过一倍时
 * 后端会给 `divergent`，界面上直说 —— 那个分歧本身是有用的信息。
 *
 * **三、庆祝只放一次。**
 * 靠后端的 `celebrated` 标记，而不是前端的"第一次看到就放"。
 * 后者会导致每次刷新都放一遍礼花，而礼花放第二遍就只有烦人了。
 */

import { motion } from 'framer-motion'
import { useCallback, useEffect, useMemo, useState } from 'react'

import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { usePreferences } from '@/app/preferences'
import { useI18n } from '@/i18n'
import {
  api,
  ApiError,
  type BankEta,
  type Goal,
  type PiggyBank,
  type PiggyBankDetail,
  type RuleStrategy,
} from '@/lib/api'
import { displayMinor, formatDayLabel } from '@/lib/format'

import { Modal } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

/** 罐体皮肤的配色。用语义 token，因此日夜主题自动跟随 */
const SKINS: Record<string, { body: string; liquid: string }> = {
  classic: { body: 'fill-surface-2', liquid: 'fill-accent' },
  glass: { body: 'fill-accent/10', liquid: 'fill-accent/70' },
  ceramic: { body: 'fill-surface-2', liquid: 'fill-positive' },
  vault: { body: 'fill-surface-2', liquid: 'fill-warning' },
}

const SKIN_KEYS = Object.keys(SKINS)

/**
 * 罐子。液面高度随余额比例变化。
 *
 * 用 SVG + clipPath 而不是"两个 div 拼一个圆角矩形"：罐子的轮廓是曲线，
 * 用 CSS 拼出来的圆角在液面处会出现明显的直角接缝。
 */
function Jar({
  ratio,
  skin,
  achieved,
  animate,
}: {
  ratio: number
  skin: string
  achieved: boolean
  animate: boolean
}) {
  // clipPath 的 id 必须唯一：多个罐子共用同一个 id 时，
  // 浏览器只会用第一个定义，于是所有罐子看起来都像第一个装的量
  const clipId = useMemo(() => `jar-clip-${Math.random().toString(36).slice(2, 9)}`, [])

  const INNER_TOP = 52
  const INNER_BOTTOM = 134
  const innerHeight = INNER_BOTTOM - INNER_TOP
  // 液面超过罐口时停在罐口（表达"满出来了"），但比例本身不夹
  const filled = Math.max(0, Math.min(1, ratio))
  const liquidHeight = innerHeight * filled
  const liquidY = INNER_BOTTOM - liquidHeight

  const palette = SKINS[skin] ?? SKINS.classic!
  // 达成态**不能只把液体染绿**：100% 的液体会铺满整个罐身，
  // 于是 `stroke-separator` 的轮廓被吞掉，罐子看起来就是一块实心圆角矩形。
  // 因此达成时刻意把罐身压淡、把轮廓换成实色，罐子的形状才留得住。
  const bodyClass = achieved ? 'fill-positive/12' : palette.body
  const liquidClass = achieved ? 'fill-positive/70' : palette.liquid
  const outlineClass = achieved ? 'stroke-positive' : 'stroke-separator'

  const outline =
    'M24 40 h72 a8 8 0 0 1 8 8 v70 a18 18 0 0 1 -18 18 h-52 a18 18 0 0 1 -18 -18 v-70 a8 8 0 0 1 8 -8 z'

  return (
    <svg viewBox="0 0 120 144" className="h-full w-full" role="img" aria-hidden>
      <defs>
        <clipPath id={clipId}>
          <path d={outline} />
        </clipPath>
      </defs>

      {/* 罐身底色 */}
      <path d={outline} className={bodyClass} />

      <g clipPath={`url(#${clipId})`}>
        {/* 液体主体。高度与位置都由比例算出，因此动画是"液面上升"而不是"淡入" */}
        <motion.rect
          x="0"
          width="120"
          className={liquidClass}
          initial={false}
          animate={{ y: liquidY, height: liquidHeight }}
          transition={animate ? { type: 'spring', stiffness: 60, damping: 18 } : { duration: 0 }}
        />
        {/* 液面波纹。只在有液体且有动画时才画 —— 空罐子顶上飘一道波纹很怪 */}
        {liquidHeight > 2 ? (
          <motion.path
            className={liquidClass}
            d="M-60 0 q15 -5 30 0 t30 0 t30 0 t30 0 t30 0 t30 0 t30 0 v6 h-210 z"
            initial={false}
            style={{ translateY: liquidY }}
            animate={animate ? { x: [0, -60] } : { x: 0 }}
            transition={animate ? { duration: 3.5, repeat: Infinity, ease: 'linear' } : { duration: 0 }}
          />
        ) : null}
      </g>

      {/* 罐口与轮廓。画在液体之后，液体才不会盖住边缘 */}
      <path d="M30 40 h60" className={outlineClass} strokeWidth="3" strokeLinecap="round" fill="none" />
      <path d={outline} className={`fill-none ${outlineClass}`} strokeWidth="2" />
      {achieved ? (
        // 勾画在**白底**上：绿底绿勾等于看不见（截图才发现）
        <motion.g
          initial={{ scale: 0.6, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ type: 'spring', stiffness: 120, damping: 14 }}
        >
          <circle cx="60" cy="96" r="15" className="fill-surface" />
          <circle cx="60" cy="96" r="15" className="fill-none stroke-positive" strokeWidth="2" />
          <path
            d="M53 96 l5 5 l9 -10"
            className="fill-none stroke-positive"
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </motion.g>
      ) : null}
    </svg>
  )
}

/** 里程碑礼花。角度写死而不是每次随机 —— 随机角度会让每次渲染都跳一下 */
const BURST = Array.from({ length: 14 }, (_, index) => {
  const angle = (index / 14) * Math.PI * 2
  return {
    x: Math.cos(angle) * 52,
    y: Math.sin(angle) * 52 - 14,
    delay: (index % 5) * 0.03,
    tone: index % 3,
  }
})

function MilestoneBurst({ onDone }: { onDone: () => void }) {
  useEffect(() => {
    const timer = window.setTimeout(onDone, 1600)
    return () => window.clearTimeout(timer)
  }, [onDone])

  return (
    <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center">
      {BURST.map((piece, index) => (
        <motion.span
          key={index}
          className={`absolute h-1.5 w-1.5 rounded-full ${
            piece.tone === 0 ? 'bg-accent' : piece.tone === 1 ? 'bg-positive' : 'bg-warning'
          }`}
          initial={{ x: 0, y: 0, opacity: 1, scale: 1 }}
          animate={{ x: piece.x, y: piece.y, opacity: 0, scale: 0.4 }}
          transition={{ duration: 1.2, delay: piece.delay, ease: 'easeOut' }}
        />
      ))}
    </div>
  )
}

/** 一个口径的预计达成日 */
function EtaRow({
  label,
  estimate,
  hint,
  reduced,
}: {
  label: string
  estimate: { rate_per_day_minor: number; days: number; eta: string } | null
  hint: string
  reduced: boolean
}) {
  const { t } = useI18n()
  return (
    <div className="min-w-0 flex-1">
      <div className="text-ab-caption1 text-label-3">{label}</div>
      {estimate ? (
        <>
          <div className="ab-tnum truncate text-ab-footnote text-label">
            {formatDayLabel(estimate.eta)}
          </div>
          <div className="text-ab-caption2 text-label-3">
            {t('piggy.etaDays', { days: estimate.days })}
          </div>
        </>
      ) : (
        <div className="text-ab-footnote text-label-3">{reduced ? '—' : t('piggy.etaNone')}</div>
      )}
      <div className="mt-0.5 text-ab-caption2 text-label-3">{hint}</div>
    </div>
  )
}

export function PiggyPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { accounts } = useLedger()
  const reduced = Boolean(preferences.reduce_motion)

  const [banks, setBanks] = useState<PiggyBank[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [detail, setDetail] = useState<PiggyBankDetail | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [depositFor, setDepositFor] = useState<PiggyBank | null>(null)
  const [ruleFor, setRuleFor] = useState<PiggyBank | null>(null)
  const [achieveFor, setAchieveFor] = useState<PiggyBank | null>(null)
  const [celebrating, setCelebrating] = useState<number | null>(null)
  const [goals, setGoals] = useState<Goal[]>([])
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const [list, goalList] = await Promise.all([api.piggyBanks(), api.goals()])
      setBanks(list.items)
      setGoals(goalList.items)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setBanks([])
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * 找到一个"刚达成但还没庆祝过"的罐子并标记已庆祝。
   *
   * 标记发生在**播放之前**：万一标记请求失败，宁可漏放一次礼花，
   * 也不要每次刷新都放 —— 后者更烦人且会让人觉得应用坏了。
   */
  useEffect(() => {
    if (!banks || reduced) return
    const pending = banks.find((bank) => bank.status === 'achieved' && !bank.celebrated)
    if (!pending) return
    setCelebrating(pending.id)
    void api.updatePiggyBank(pending.id, { celebrated: true }).catch(() => undefined)
  }, [banks, reduced])

  const openDetail = async (bank: PiggyBank) => {
    try {
      setDetail(await api.piggyBank(bank.id))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const remove = async (bank: PiggyBank) => {
    setBusy(true)
    try {
      await api.deletePiggyBank(bank.id)
      setNotice(t('piggy.deleted', { name: bank.name }))
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const totals = useMemo(() => {
    const items = banks ?? []
    return {
      saved: items.reduce((sum, bank) => sum + bank.balance_minor, 0),
      target: items
        .filter((bank) => bank.status === 'active')
        .reduce((sum, bank) => sum + bank.target_amount_minor, 0),
      achieved: items.filter((bank) => bank.status === 'achieved').length,
    }
  }, [banks])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('piggy.title')}</h1>
        {banks && banks.length > 0 ? (
          <span className="ab-tnum text-ab-caption text-label-3">
            {t('piggy.summary', {
              saved: displayMinor(totals.saved, 'CNY', preferences.privacy_mode),
              target: displayMinor(totals.target, 'CNY', preferences.privacy_mode),
            })}
          </span>
        ) : null}
        <button
          type="button"
          className="ab-btn-primary ml-auto"
          onClick={() => setCreateOpen(true)}
        >
          <Icon name="plus" size={14} />
          {t('piggy.newBank')}
        </button>
      </div>

      {notice ? (
        <div className="rounded-ab-sm bg-positive/10 px-3 py-2 text-ab-footnote text-positive">{notice}</div>
      ) : null}
      {error ? (
        <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
      ) : null}

      {banks === null ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((key) => (
            <Skeleton key={key} className="h-56 w-full" />
          ))}
        </div>
      ) : banks.length === 0 ? (
        <Card flush>
          <EmptyState
            icon="piggy"
            title={t('piggy.empty')}
            body={t('piggy.emptyBody')}
            action={
              <button type="button" className="ab-btn-primary" onClick={() => setCreateOpen(true)}>
                <Icon name="plus" size={14} />
                {t('piggy.newBank')}
              </button>
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {banks.map((bank) => {
            const eta: BankEta | undefined = bank.eta ?? undefined
            return (
              <Card key={bank.id} className="relative overflow-hidden">
                {celebrating === bank.id ? (
                  <MilestoneBurst onDone={() => setCelebrating(null)} />
                ) : null}

                <div className="flex gap-4">
                  <div className="h-28 w-24 shrink-0">
                    <Jar
                      ratio={bank.ratio}
                      skin={bank.skin}
                      achieved={bank.status === 'achieved'}
                      animate={!reduced}
                    />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start gap-1">
                      <h2 className="min-w-0 flex-1 truncate text-ab-headline font-semibold text-label">
                        {bank.name}
                      </h2>
                      <button
                        type="button"
                        className="ab-icon-btn shrink-0 hover:text-negative"
                        aria-label={t('common.delete')}
                        disabled={busy}
                        onClick={() => void remove(bank)}
                      >
                        <Icon name="trash" size={13} />
                      </button>
                    </div>
                    {bank.target_name ? (
                      <p className="truncate text-ab-caption1 text-label-3">{bank.target_name}</p>
                    ) : null}

                    <div className="ab-tnum mt-1 text-ab-title3 font-semibold text-label">
                      {bank.hide_amount
                        ? t('piggy.percentOnly', { percent: Math.round(bank.ratio * 100) })
                        : displayMinor(bank.balance_minor, 'CNY', preferences.privacy_mode)}
                    </div>
                    <div className="ab-tnum text-ab-caption1 text-label-3">
                      {t('piggy.ofTarget', {
                        target: displayMinor(bank.target_amount_minor, 'CNY', preferences.privacy_mode),
                      })}
                    </div>

                    <div className="mt-1.5 flex flex-wrap items-center gap-1">
                      {bank.status === 'achieved' ? (
                        <span className="ab-chip" data-active>
                          <Icon name="check" size={11} />
                          {t('piggy.achieved')}
                        </span>
                      ) : null}
                      {bank.status === 'paused' ? (
                        <span className="ab-chip">{t('piggy.paused')}</span>
                      ) : null}
                      {bank.rule ? (
                        <span className="ab-chip">
                          <Icon name="refresh" size={11} />
                          {t(`piggy.strategy.${bank.rule.strategy}`)}
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>

                {/* 双口径预计达成日 */}
                {eta && !eta.achieved ? (
                  <div className="mt-3 border-t border-separator/60 pt-2">
                    <div className="flex gap-3">
                      <EtaRow
                        label={t('piggy.etaLinear')}
                        estimate={eta.linear}
                        hint={t('piggy.etaLinearHint')}
                        reduced={reduced}
                      />
                      <EtaRow
                        label={t('piggy.etaWeighted')}
                        estimate={eta.weighted}
                        hint={t('piggy.etaWeightedHint')}
                        reduced={reduced}
                      />
                    </div>
                    {eta.divergent ? (
                      <p className="mt-1.5 rounded-ab-sm bg-warning/10 px-2 py-1 text-ab-caption2 text-label-2">
                        {t('piggy.divergent')}
                      </p>
                    ) : null}
                    {eta.deadline && eta.on_track === false ? (
                      <p className="mt-1 text-ab-caption2 text-negative">
                        {t('piggy.offTrack', {
                          need: displayMinor(
                            eta.required_per_day_minor ?? 0,
                            'CNY',
                            preferences.privacy_mode,
                          ),
                        })}
                      </p>
                    ) : null}
                  </div>
                ) : null}

                <div className="mt-3 flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    className="ab-btn-primary"
                    onClick={() => setDepositFor(bank)}
                  >
                    <Icon name="plus" size={13} />
                    {t('piggy.deposit')}
                  </button>
                  <button
                    type="button"
                    className="ab-btn-secondary"
                    onClick={() => setRuleFor(bank)}
                  >
                    <Icon name="refresh" size={13} />
                    {t('piggy.rule')}
                  </button>
                  <button
                    type="button"
                    className="ab-btn-secondary"
                    onClick={() => void openDetail(bank)}
                  >
                    {t('piggy.history')}
                  </button>
                  {bank.balance_minor > 0 ? (
                    <button
                      type="button"
                      className="ab-btn-secondary"
                      onClick={() => setAchieveFor(bank)}
                    >
                      {t('piggy.spend')}
                    </button>
                  ) : null}
                </div>
              </Card>
            )
          })}
        </div>
      )}

      <CreateBankDialog
        open={createOpen}
        goals={goals}
        onClose={() => setCreateOpen(false)}
        onCreated={async () => {
          setCreateOpen(false)
          setNotice(t('piggy.created'))
          await load()
        }}
      />

      <DepositDialog
        bank={depositFor}
        onClose={() => setDepositFor(null)}
        onDone={async () => {
          setDepositFor(null)
          await load()
        }}
      />

      <RuleDialog
        bank={ruleFor}
        onClose={() => setRuleFor(null)}
        onDone={async () => {
          setRuleFor(null)
          await load()
        }}
      />

      <AchieveDialog
        bank={achieveFor}
        accounts={accounts}
        goals={goals}
        onClose={() => setAchieveFor(null)}
        onDone={async (message) => {
          setAchieveFor(null)
          setNotice(message)
          await load()
        }}
      />

      <Modal
        open={detail !== null}
        title={detail ? t('piggy.historyTitle', { name: detail.name }) : ''}
        size="lg"
        onClose={() => setDetail(null)}
        footer={
          <button type="button" className="ab-btn-secondary" onClick={() => setDetail(null)}>
            {t('common.close')}
          </button>
        }
      >
        {detail && detail.deposits.length === 0 ? (
          <p className="py-6 text-center text-ab-footnote text-label-3">{t('piggy.noDeposits')}</p>
        ) : (
          <div className="max-h-[50vh] space-y-0.5 overflow-y-auto">
            {detail?.deposits.map((row) => (
              <div key={row.id} className="ab-row">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-ab-footnote text-label-2">
                    {row.note || t(`piggy.depositKind.${row.kind}`)}
                  </span>
                  <span className="block text-ab-caption1 text-label-3">
                    {formatDayLabel(row.occurred_at)}
                  </span>
                </span>
                <span
                  className={`ab-tnum shrink-0 text-ab-footnote ${
                    row.amount_minor >= 0 ? 'text-positive' : 'text-negative'
                  }`}
                >
                  {row.amount_minor >= 0 ? '+' : ''}
                  {displayMinor(row.amount_minor, 'CNY', preferences.privacy_mode)}
                </span>
                <button
                  type="button"
                  className="ab-icon-btn shrink-0 hover:text-negative"
                  aria-label={t('common.delete')}
                  onClick={async () => {
                    try {
                      await api.deletePiggyDeposit(row.id)
                      setDetail(await api.piggyBank(detail.id))
                      await load()
                    } catch (cause) {
                      setError(cause instanceof ApiError ? cause.detail : String(cause))
                    }
                  }}
                >
                  <Icon name="trash" size={12} />
                </button>
              </div>
            ))}
          </div>
        )}
      </Modal>
    </div>
  )
}

// -----------------------------------------------------------------------------
// 对话框
// -----------------------------------------------------------------------------
function CreateBankDialog({
  open,
  goals,
  onClose,
  onCreated,
}: {
  open: boolean
  goals: Goal[]
  onClose: () => void
  onCreated: () => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [targetName, setTargetName] = useState('')
  const [target, setTarget] = useState('')
  const [initial, setInitial] = useState('')
  const [deadline, setDeadline] = useState('')
  const [skin, setSkin] = useState('classic')
  const [goalId, setGoalId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /** 元 → 分。用 Math.round 而不是截断：12.34 元截断会丢 1 分 */
  const toMinor = (value: string) => Math.round(Number(value || '0') * 100)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.createPiggyBank({
        name,
        target_amount_minor: toMinor(target),
        target_name: targetName,
        deadline: deadline || null,
        skin,
        initial_minor: toMinor(initial),
        goal_id: goalId ? Number(goalId) : null,
      })
      setName('')
      setTarget('')
      setInitial('')
      setTargetName('')
      onCreated()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title={t('piggy.newBank')}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void submit()}>
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
        ) : null}
        <div>
          <label className="ab-field-label" htmlFor="bank-name">
            {t('piggy.name')}
          </label>
          <input
            id="bank-name"
            className="ab-input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={t('piggy.namePlaceholder')}
          />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="ab-field-label" htmlFor="bank-target-name">
              {t('piggy.targetName')}
            </label>
            <input
              id="bank-target-name"
              className="ab-input"
              value={targetName}
              onChange={(event) => setTargetName(event.target.value)}
              placeholder={t('piggy.targetNamePlaceholder')}
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="bank-target">
              {t('piggy.targetAmount')}
            </label>
            <input
              id="bank-target"
              className="ab-input ab-tnum"
              inputMode="decimal"
              value={target}
              onChange={(event) => setTarget(event.target.value)}
              placeholder="0.00"
            />
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="ab-field-label" htmlFor="bank-initial">
              {t('piggy.initial')}
            </label>
            <input
              id="bank-initial"
              className="ab-input ab-tnum"
              inputMode="decimal"
              value={initial}
              onChange={(event) => setInitial(event.target.value)}
              placeholder="0.00"
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="bank-deadline">
              {t('piggy.deadline')}
            </label>
            <input
              id="bank-deadline"
              type="date"
              className="ab-input"
              value={deadline}
              onChange={(event) => setDeadline(event.target.value)}
            />
          </div>
        </div>
        <div>
          <span className="ab-field-label">{t('piggy.skin')}</span>
          <div className="flex flex-wrap gap-1.5">
            {SKIN_KEYS.map((key) => (
              <button
                key={key}
                type="button"
                className="ab-chip"
                data-active={skin === key}
                onClick={() => setSkin(key)}
              >
                {t(`piggy.skinName.${key}`)}
              </button>
            ))}
          </div>
        </div>
        {goals.length > 0 ? (
          <div>
            <label className="ab-field-label" htmlFor="bank-goal">
              {t('piggy.linkGoal')}
            </label>
            <select
              id="bank-goal"
              className="ab-select"
              value={goalId}
              onChange={(event) => setGoalId(event.target.value)}
            >
              <option value="">{t('piggy.noGoal')}</option>
              {goals.map((goal) => (
                <option key={goal.id} value={goal.id}>
                  {goal.name}
                </option>
              ))}
            </select>
            <p className="mt-1 text-ab-caption1 text-label-3">{t('piggy.linkGoalHint')}</p>
          </div>
        ) : null}
      </div>
    </Modal>
  )
}

function DepositDialog({
  bank,
  onClose,
  onDone,
}: {
  bank: PiggyBank | null
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setAmount('')
    setNote('')
    setError(null)
  }, [bank])

  const submit = async (sign: 1 | -1) => {
    if (!bank) return
    setBusy(true)
    setError(null)
    try {
      await api.addPiggyDeposit(bank.id, {
        amount_minor: sign * Math.round(Number(amount || '0') * 100),
        kind: sign > 0 ? 'manual' : 'withdraw',
        note,
      })
      onDone()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={bank !== null}
      title={bank ? t('piggy.depositTitle', { name: bank.name }) : ''}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="ab-btn-secondary !text-negative"
            disabled={busy || !amount}
            onClick={() => void submit(-1)}
          >
            {t('piggy.withdraw')}
          </button>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={busy || !amount}
            onClick={() => void submit(1)}
          >
            <Icon name="plus" size={13} />
            {t('piggy.deposit')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
        ) : null}
        <div>
          <label className="ab-field-label" htmlFor="deposit-amount">
            {t('piggy.amount')}
          </label>
          <input
            id="deposit-amount"
            className="ab-input ab-tnum"
            inputMode="decimal"
            autoFocus
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            placeholder="0.00"
          />
          {bank ? (
            <p className="mt-1 ab-tnum text-ab-caption1 text-label-3">
              {t('piggy.currentBalance', { amount: displayMinor(bank.balance_minor, 'CNY') })}
            </p>
          ) : null}
        </div>
        <div>
          <label className="ab-field-label" htmlFor="deposit-note">
            {t('ledger.note')}
          </label>
          <input
            id="deposit-note"
            className="ab-input"
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
        </div>
      </div>
    </Modal>
  )
}

const STRATEGIES: RuleStrategy[] = [
  'roundup',
  'daily_fixed',
  'weekly_fixed',
  'income_percent',
  'monthly_surplus',
  'category_trigger',
]

function RuleDialog({
  bank,
  onClose,
  onDone,
}: {
  bank: PiggyBank | null
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const { accounts } = useLedger()
  const [strategy, setStrategy] = useState<RuleStrategy>('roundup')
  const [fixed, setFixed] = useState('')
  const [percent, setPercent] = useState('')
  const [accountId, setAccountId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [clock, setClock] = useState<{ count: number; total_minor: number } | null>(null)

  useEffect(() => {
    setError(null)
    setClock(null)
    if (bank?.rule) {
      setStrategy(bank.rule.strategy)
      setFixed(bank.rule.fixed_amount_minor ? String(bank.rule.fixed_amount_minor / 100) : '')
      setPercent(bank.rule.percent_bps ? String(bank.rule.percent_bps / 100) : '')
      setAccountId(bank.rule.account_id ? String(bank.rule.account_id) : '')
    } else {
      setStrategy('roundup')
      setFixed('')
      setPercent('')
      setAccountId('')
    }
  }, [bank])

  const preview = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.runPiggyRules({ dry_run: true })
      setClock({ count: result.count, total_minor: result.total_minor })
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const submit = async () => {
    if (!bank) return
    setBusy(true)
    setError(null)
    try {
      await api.setPiggyRule(bank.id, {
        strategy,
        fixed_amount_minor: Math.round(Number(fixed || '0') * 100),
        percent_bps: Math.round(Number(percent || '0') * 100),
        account_id: accountId ? Number(accountId) : null,
        deduct_from_account: Boolean(accountId),
      })
      onDone()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const needsFixed = strategy === 'daily_fixed' || strategy === 'weekly_fixed' || strategy === 'category_trigger'
  const needsPercent = strategy === 'income_percent' || strategy === 'monthly_surplus'

  return (
    <Modal
      open={bank !== null}
      title={bank ? t('piggy.ruleTitle', { name: bank.name }) : ''}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void submit()}>
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
        ) : null}
        <div>
          <span className="ab-field-label">{t('piggy.strategyLabel')}</span>
          <div className="space-y-1">
            {STRATEGIES.map((item) => (
              <button
                key={item}
                type="button"
                className={`ab-row w-full rounded-ab-sm px-2 text-left ${
                  strategy === item ? 'bg-accent/10 text-accent' : 'text-label-2'
                }`}
                onClick={() => setStrategy(item)}
              >
                <span className="flex-1 text-ab-footnote">{t(`piggy.strategy.${item}`)}</span>
                {strategy === item ? <Icon name="check" size={12} /> : null}
              </button>
            ))}
          </div>
          <p className="mt-1 text-ab-caption1 text-label-3">{t(`piggy.strategyHint.${strategy}`)}</p>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          {needsFixed ? (
            <div>
              <label className="ab-field-label" htmlFor="rule-fixed">
                {t('piggy.fixedAmount')}
              </label>
              <input
                id="rule-fixed"
                className="ab-input ab-tnum"
                inputMode="decimal"
                value={fixed}
                onChange={(event) => setFixed(event.target.value)}
                placeholder="0.00"
              />
            </div>
          ) : null}
          {needsPercent ? (
            <div>
              <label className="ab-field-label" htmlFor="rule-percent">
                {t('piggy.percent')}
              </label>
              <input
                id="rule-percent"
                className="ab-input ab-tnum"
                inputMode="decimal"
                value={percent}
                onChange={(event) => setPercent(event.target.value)}
                placeholder={t('piggy.percentPlaceholder')}
              />
            </div>
          ) : null}
          <div>
            <label className="ab-field-label" htmlFor="rule-account">
              {t('piggy.deductAccount')}
            </label>
            <select
              id="rule-account"
              className="ab-select"
              value={accountId}
              onChange={(event) => setAccountId(event.target.value)}
            >
              <option value="">{t('piggy.noDeduct')}</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* 预览而不是直接执行：自动扣钱的功能必须先给用户看 */}
          <button type="button" className="ab-btn-secondary" disabled={busy} onClick={() => void preview()}>
            {t('piggy.previewDue')}
          </button>
          {clock ? (
            <span className="ab-tnum text-ab-caption1 text-label-2">
              {t('piggy.previewResult', {
                count: clock.count,
                amount: displayMinor(clock.total_minor, 'CNY'),
              })}
            </span>
          ) : null}
        </div>
      </div>
    </Modal>
  )
}

function AchieveDialog({
  bank,
  accounts,
  goals,
  onClose,
  onDone,
}: {
  bank: PiggyBank | null
  accounts: { id: number; name: string }[]
  goals: Goal[]
  onClose: () => void
  onDone: (message: string) => void
}) {
  const { t } = useI18n()
  const [accountId, setAccountId] = useState('')
  const [goalId, setGoalId] = useState('')
  const [mode, setMode] = useState<'spend' | 'inject'>('spend')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setError(null)
    setAccountId(accounts[0] ? String(accounts[0].id) : '')
    setGoalId(bank?.goal_id ? String(bank.goal_id) : goals[0] ? String(goals[0].id) : '')
    setMode(goals.length > 0 ? 'inject' : 'spend')
  }, [bank, accounts, goals])

  const submit = async () => {
    if (!bank) return
    setBusy(true)
    setError(null)
    try {
      if (mode === 'inject') {
        const result = await api.injectPiggyBank(bank.id, { goal_id: Number(goalId) })
        onDone(t('piggy.injected', { amount: displayMinor(result.injected_minor ?? 0, 'CNY') }))
      } else {
        const result = await api.achievePiggyBank(bank.id, { account_id: Number(accountId) })
        onDone(t('piggy.settled', { amount: displayMinor(result.settled_minor ?? 0, 'CNY') }))
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={bank !== null}
      title={bank ? t('piggy.spendTitle', { name: bank.name }) : ''}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={busy || (mode === 'inject' && !goalId)}
            onClick={() => void submit()}
          >
            {mode === 'inject' ? t('piggy.inject') : t('piggy.settle')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
        ) : null}
        <p className="ab-tnum rounded-ab-sm bg-accent/8 px-3 py-2 text-ab-footnote text-label-2">
          {bank
            ? t('piggy.settleAmount', { amount: displayMinor(bank.balance_minor, 'CNY') })
            : ''}
        </p>
        {/* 金额用**罐子余额**而不是目标金额：账实相符优先于数字好看 */}
        <p className="text-ab-caption1 text-label-3">{t('piggy.settleHint')}</p>

        <div className="flex gap-1.5">
          <button
            type="button"
            className="ab-chip"
            data-active={mode === 'spend'}
            onClick={() => setMode('spend')}
          >
            {t('piggy.modeSpend')}
          </button>
          {goals.length > 0 ? (
            <button
              type="button"
              className="ab-chip"
              data-active={mode === 'inject'}
              onClick={() => setMode('inject')}
            >
              {t('piggy.modeInject')}
            </button>
          ) : null}
        </div>

        {mode === 'spend' ? (
          <div>
            <label className="ab-field-label" htmlFor="achieve-account">
              {t('piggy.payFrom')}
            </label>
            <select
              id="achieve-account"
              className="ab-select"
              value={accountId}
              onChange={(event) => setAccountId(event.target.value)}
            >
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <div>
            <label className="ab-field-label" htmlFor="achieve-goal">
              {t('piggy.injectTo')}
            </label>
            <select
              id="achieve-goal"
              className="ab-select"
              value={goalId}
              onChange={(event) => setGoalId(event.target.value)}
            >
              {goals.map((goal) => (
                <option key={goal.id} value={goal.id}>
                  {goal.name}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>
    </Modal>
  )
}
