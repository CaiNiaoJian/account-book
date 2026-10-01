/**
 * 记账表单 —— 快捷记账与详细填表。
 *
 * 为什么两个表单放在一起
 * ----------------------
 * 它们共享**同一套校验与提交逻辑**（金额解析、账户选择、方向推断、分账守恒）。
 * 若各写一份，最容易出的事就是"快捷记账能存、详细填表报错" ——
 * 用户会认为应用不可靠。因此这里把状态与提交抽成 `useTransactionDraft`，
 * 两种界面只是它的不同外壳。
 *
 * 快捷记账的设计立场（REQ-6）
 * ---------------------------
 * 记账的第一杀手是"麻烦"。因此快捷表单：
 *   * 打开即聚焦金额，键盘可完成全流程（数字 → Tab → 选分类 → Enter 保存）；
 *   * 只暴露四个字段（金额、方向、分类、账户），其余走默认值；
 *   * 记住上次使用的账户与分类（P2 会按时间与频次智能排序）。
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import { Icon } from '@/components/Icon'
import { useI18n } from '@/i18n'
import { api, ApiError, type Transaction, type TransactionType } from '@/lib/api'
import { localDayKey, parseAmountToMinor, displayMinor } from '@/lib/format'
import { usePreferences } from '@/app/preferences'

import { CategoryPicker, Modal, TagPicker } from './parts'
import { useLedger } from './store'

/** 表单草稿：两种表单共用 */
export interface DraftState {
  type: TransactionType
  amountMinor: number | null
  accountId: number | null
  toAccountId: number | null
  categoryId: number | null
  /** 本地日期（YYYY-MM-DD），提交时补上时间 */
  day: string
  payee: string
  note: string
  splits: { category_id: number | null; amount_minor: number }[]
  /** 标签 id 列表 */
  tagIds: number[]
  /** 项目 / 成员（可空） */
  projectId: number | null
  memberId: number | null
}

const EMPTY_DRAFT: DraftState = {
  type: 'expense',
  amountMinor: null,
  accountId: null,
  toAccountId: null,
  categoryId: null,
  day: localDayKey(),
  payee: '',
  note: '',
  splits: [],
  tagIds: [],
  projectId: null,
  memberId: null,
}

/** 从已有流水构造草稿（编辑模式） */
export function draftFrom(transaction: Transaction): DraftState {
  return {
    type: transaction.type as TransactionType,
    amountMinor: transaction.amount_minor,
    accountId: transaction.account_id,
    toAccountId: transaction.to_account_id,
    categoryId: transaction.category_id,
    day: localDayKey(transaction.occurred_at),
    payee: transaction.payee,
    note: transaction.note,
    splits: transaction.splits.map((split) => ({
      category_id: split.category_id,
      amount_minor: split.amount_minor,
    })),
    // 接口只回传标签**名字**（列表展示用），而提交需要 id。
    // 这里通过名字反查 id —— 与其让接口同时回传两种形态，
    // 不如让"名字 → id"的映射只在需要编辑的地方做一次。
    tagIds: [],
    projectId: transaction.project_id ?? null,
    memberId: transaction.member_id ?? null,
  }
}

/** 把草稿转成 API 负载 */
function toPayload(draft: DraftState, keepTime?: string) {
  const day = draft.day || localDayKey()
  // 保留原有时间（编辑时）或使用当前时刻：只让用户选日期，不强迫他填时间 —— 
  // 但"上午/下午"的顺序对流水列表的可读性有实际影响
  const time = keepTime ? keepTime.slice(11, 19) : new Date().toTimeString().slice(0, 8)
  return {
    type: draft.type,
    occurred_at: `${day}T${time}`,
    account_id: draft.accountId as number,
    to_account_id: draft.type === 'transfer' ? draft.toAccountId : null,
    category_id: draft.type === 'transfer' ? null : draft.categoryId,
    amount_minor: draft.amountMinor as number,
    payee: draft.payee,
    note: draft.note,
    splits: draft.splits.length > 0 ? draft.splits : [],
    // 标签在两种表单里都可选，因此始终显式提交：
    // 传空数组表示"清空"，这样用户把标签全部取消的操作才真的生效
    tag_ids: draft.tagIds,
    project_id: draft.projectId,
    member_id: draft.memberId,
  }
}

/**
 * 草稿状态与提交逻辑。
 *
 * 集中在这里的好处：校验规则只有一份。特别是"分账之和必须等于总额"
 * 这条 —— 侧边栏提示、保存按钮禁用、提交前的最终校验都引用同一个判断。
 */
export function useTransactionDraft(initial?: Transaction) {
  const { accounts, tags } = useLedger()
  const [draft, setDraft] = useState<DraftState>(initial ? draftFrom(initial) : EMPTY_DRAFT)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 编辑已有流水时把标签**名字**还原成 id。
  // 接口在列表里只回传名字（展示用），而提交需要 id；
  // 这个映射只在"进入编辑"时做一次，所以放在这里而不是让接口回传两套字段。
  useEffect(() => {
    if (!initial || tags.length === 0) return
    const ids = initial.tags
      .map((name) => tags.find((tag) => tag.name === name)?.id)
      .filter((id): id is number => typeof id === 'number')
    setDraft((previous) => (previous.tagIds.length > 0 ? previous : { ...previous, tagIds: ids }))
    // 只在标签字典首次就绪时补齐，之后不再干预用户选择
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial?.id, tags.length])

  // 默认账户：优先用户上次使用的（P1 先用第一个未归档账户）
  useEffect(() => {
    const first = accounts[0]
    const second = accounts[1]
    if (draft.accountId === null && first) {
      setDraft((previous) => ({ ...previous, accountId: first.id }))
    }
    if (draft.toAccountId === null && second) {
      setDraft((previous) => ({ ...previous, toAccountId: second.id }))
    }
    // 只在账户列表首次就绪时补齐默认值，之后不再干预用户选择
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts.length])

  const splitTotal = draft.splits.reduce((sum, split) => sum + split.amount_minor, 0)
  const splitsValid = draft.splits.length === 0 || splitTotal === draft.amountMinor

  const canSubmit =
    draft.amountMinor !== null &&
    draft.amountMinor > 0 &&
    draft.accountId !== null &&
    splitsValid &&
    (draft.type !== 'transfer' || (draft.toAccountId !== null && draft.toAccountId !== draft.accountId))

  const submit = async (options: { keepTime?: string; onDone?: (created: Transaction) => void } = {}) => {
    if (!canSubmit) return null
    setBusy(true)
    setError(null)
    try {
      const payload = toPayload(draft, options.keepTime)
      const created = initial
        ? await api.updateTransaction(initial.id, payload)
        : await api.createTransaction(payload)
      options.onDone?.(created)
      setDraft((previous) => ({ ...EMPTY_DRAFT, accountId: previous.accountId, day: previous.day }))
      return created
    } catch (cause) {
      // 领域错误的 message 已是可读中文，直接显示；details 里的差额能帮用户
      // 立刻明白"分账为什么存不上"
      setError(cause instanceof ApiError ? cause.detail : cause instanceof Error ? cause.message : String(cause))
      return null
    } finally {
      setBusy(false)
    }
  }

  return { draft, setDraft, busy, error, setError, canSubmit, splitsValid, splitTotal, submit }
}

// -----------------------------------------------------------------------------
// 快捷记账
// -----------------------------------------------------------------------------
interface QuickAddDialogProps {
  open: boolean
  onClose: () => void
  onSaved?: () => void
}

/** 数字键盘：手机记账应用的经典布局，桌面端同样好用（鼠标一次点击一格） */
const KEYPAD_ROWS: string[][] = [
  ['1', '2', '3'],
  ['4', '5', '6'],
  ['7', '8', '9'],
  ['.', '0', '⌫'],
]

export function QuickAddDialog({ open, onClose, onSaved }: QuickAddDialogProps) {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { accounts } = useLedger()
  const controller = useTransactionDraft()
  const { draft, setDraft, busy, error, canSubmit, submit } = controller
  const [amountText, setAmountText] = useState('')
  const amountRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (open) {
      setAmountText('')
      setDraft((previous) => ({ ...previous, amountMinor: null, payee: '', note: '', splits: [] }))
      // 焦点落到金额框：用户打开快捷记账后想做的第一件事就是输金额
      requestAnimationFrame(() => amountRef.current?.focus())
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const pushKey = (key: string) => {
    const next = key === '⌫' ? amountText.slice(0, -1) : amountText + key
    // 只允许一个小数点、最多两位小数 —— 在输入阶段就挡住非法值，比事后报错友好
    if (next.split('.').length > 2) return
    const fraction = next.split('.')[1]
    if (fraction && fraction.length > 2) return
    setAmountText(next)
    setDraft((previous) => ({ ...previous, amountMinor: parseAmountToMinor(next) }))
  }

  const save = async () => {
    const created = await submit({ onDone: () => onSaved?.() })
    if (created) onClose()
  }

  return (
    <Modal
      open={open}
      title={t('ledger.quickAdd')}
      onClose={onClose}
      footer={
        <>
          <span className="mr-auto text-ab-caption text-label-3">{t('ledger.quickAddHint')}</span>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={busy || !canSubmit}
            onClick={() => void save()}
          >
            <Icon name="check" size={14} />
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-3.5">
        {/* 支出 / 收入 / 转账 */}
        <div className="ab-segment">
          {(['expense', 'income', 'transfer'] as TransactionType[]).map((type) => (
            <button
              key={type}
              type="button"
              data-active={draft.type === type}
              onClick={() =>
                setDraft((previous) => ({
                  ...previous,
                  type,
                  categoryId: type === 'transfer' ? null : previous.categoryId,
                  splits: type === 'transfer' ? [] : previous.splits,
                }))
              }
            >
              {t(`transactionType.${type}`)}
            </button>
          ))}
        </div>

        {/* 金额：键盘可完成全部输入 */}
        <div>
          <input
            ref={amountRef}
            className="ab-tnum w-full bg-transparent text-ab-large font-bold tracking-tight text-label outline-none"
            inputMode="decimal"
            placeholder="0.00"
            value={amountText}
            onChange={(event) => {
              setAmountText(event.target.value)
              setDraft((previous) => ({
                ...previous,
                amountMinor: parseAmountToMinor(event.target.value),
              }))
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && canSubmit) {
                event.preventDefault()
                void save()
              }
            }}
          />
          <div className="mt-1 h-px bg-separator" />
        </div>

        <div className="grid grid-cols-3 gap-1.5">
          {KEYPAD_ROWS.flat().map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => pushKey(key)}
              className="rounded-ab-sm bg-surface-3/70 py-2 text-ab-callout font-medium text-label transition-colors hover:bg-surface-3 active:scale-[0.97]"
            >
              {key}
            </button>
          ))}
        </div>

        {/* 账户选择 */}
        <div className="grid gap-2 sm:grid-cols-2">
          <div>
            <label className="ab-field-label">{t('ledger.account')}</label>
            <select
              className="ab-select"
              value={draft.accountId ?? ''}
              onChange={(event) => setDraft((previous) => ({ ...previous, accountId: Number(event.target.value) }))}
            >
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}（{displayMinor(account.balance_minor, account.currency, preferences.privacy_mode)}）
                </option>
              ))}
            </select>
          </div>
          {draft.type === 'transfer' ? (
            <div>
              <label className="ab-field-label">{t('ledger.toAccount')}</label>
              <select
                className="ab-select"
                value={draft.toAccountId ?? ''}
                onChange={(event) =>
                  setDraft((previous) => ({ ...previous, toAccountId: Number(event.target.value) }))
                }
              >
                {accounts.map((account) => (
                  <option key={account.id} value={account.id} disabled={account.id === draft.accountId}>
                    {account.name}
                  </option>
                ))}
              </select>
            </div>
          ) : (
            <div>
              <label className="ab-field-label" htmlFor="quick-day">
                {t('ledger.date')}
              </label>
              <input
                id="quick-day"
                type="date"
                className="ab-input"
                value={draft.day}
                onChange={(event) => setDraft((previous) => ({ ...previous, day: event.target.value }))}
              />
            </div>
          )}
        </div>

        {/* 分类：只在收支时出现（转账没有分类，界面直接不显示，而不是显示一个禁用的空框） */}
        {draft.type !== 'transfer' ? (
          <div>
            <label className="ab-field-label">{t('ledger.category')}</label>
            <div className="max-h-52 overflow-y-auto rounded-ab-sm border border-separator/60 p-1.5">
              <CategoryPicker
                compact
                kind={draft.type === 'income' ? 'income' : 'expense'}
                value={draft.categoryId}
                onChange={(categoryId) => setDraft((previous) => ({ ...previous, categoryId }))}
              />
            </div>
          </div>
        ) : null}

        <div className="grid gap-2 sm:grid-cols-2">
          <div>
            <label className="ab-field-label" htmlFor="quick-payee">
              {t('ledger.payee')}
            </label>
            <input
              id="quick-payee"
              className="ab-input"
              value={draft.payee}
              onChange={(event) => setDraft((previous) => ({ ...previous, payee: event.target.value }))}
              placeholder={t('ledger.payeePlaceholder')}
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="quick-note">
              {t('ledger.note')}
            </label>
            <input
              id="quick-note"
              className="ab-input"
              value={draft.note}
              onChange={(event) => setDraft((previous) => ({ ...previous, note: event.target.value }))}
            />
          </div>
        </div>

        {/* 快捷记账也允许打标签：标签的价值恰恰在于"记账当下顺手记下来"。
            若只有详细填表能打标签，绝大多数流水永远不会带标签 ——
            那么标签这个维度就等于没有。 */}
        <div>
          <span className="ab-field-label">{t('ledger.tags')}</span>
          <TagPicker
            compact
            value={draft.tagIds}
            onChange={(tagIds) => setDraft((previous) => ({ ...previous, tagIds }))}
          />
        </div>

        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
        ) : null}
      </div>
    </Modal>
  )
}

// -----------------------------------------------------------------------------
// 详细填表
// -----------------------------------------------------------------------------
interface TransactionEditorProps {
  open: boolean
  /** 传入已有流水表示编辑，null 表示新建 */
  transaction: Transaction | null
  onClose: () => void
  onSaved?: () => void
  /** 新建时是否默认展开分账 */
  withSplits?: boolean
}

export function TransactionEditor({
  open,
  transaction,
  onClose,
  onSaved,
  withSplits,
}: TransactionEditorProps) {
  const { t } = useI18n()
  const { accounts, projects, members } = useLedger()
  const controller = useTransactionDraft(transaction ?? undefined)
  const { draft, setDraft, busy, error, canSubmit, splitsValid, splitTotal, submit } = controller
  const [splitMode, setSplitMode] = useState(false)

  useEffect(() => {
    if (!open) return
    setSplitMode(Boolean(withSplits) || (transaction?.splits.length ?? 0) > 0)
  }, [open, withSplits, transaction])

  const remaining = (draft.amountMinor ?? 0) - splitTotal
  const kind: 'expense' | 'income' = draft.type === 'income' ? 'income' : 'expense'
  // 分账下拉的选项必须在这里取好。
  // 不能在 JSX 的 map 回调里调用 hook —— 那会违反 Hooks 规则
  // （调用次数随渲染数据变化），React 会在开发模式下直接报错。
  const splitCategoryOptions = useLedgerCategories(kind)

  return (
    <Modal
      open={open}
      title={transaction ? t('ledger.editTransaction') : t('ledger.newTransaction')}
      size="lg"
      onClose={onClose}
      footer={
        <>
          {!splitsValid ? (
            <span className="mr-auto text-ab-caption text-negative">
              {t('ledger.splitMismatch', { remaining: displayMinor(remaining, 'CNY') })}
            </span>
          ) : null}
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={busy || !canSubmit}
            onClick={() =>
              void submit({
                keepTime: transaction?.occurred_at,
                onDone: () => onSaved?.(),
              }).then((created) => {
                if (created) onClose()
              })
            }
          >
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-3.5">
        <div className="ab-segment">
          {(['expense', 'income', 'transfer', 'adjust'] as TransactionType[]).map((type) => (
            <button
              key={type}
              type="button"
              data-active={draft.type === type}
              onClick={() => setDraft((previous) => ({ ...previous, type }))}
            >
              {t(`transactionType.${type}`)}
            </button>
          ))}
        </div>

        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">{error}</div>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <label className="ab-field-label" htmlFor="editor-amount">
              {t('ledger.amount')}
            </label>
            <input
              id="editor-amount"
              className="ab-input ab-tnum"
              inputMode="decimal"
              value={draft.amountMinor === null ? '' : String(draft.amountMinor / 100)}
              onChange={(event) =>
                setDraft((previous) => ({
                  ...previous,
                  amountMinor: parseAmountToMinor(event.target.value),
                }))
              }
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="editor-day">
              {t('ledger.date')}
            </label>
            <input
              id="editor-day"
              type="date"
              className="ab-input"
              value={draft.day}
              onChange={(event) => setDraft((previous) => ({ ...previous, day: event.target.value }))}
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="editor-account">
              {t('ledger.account')}
            </label>
            <select
              id="editor-account"
              className="ab-select"
              value={draft.accountId ?? ''}
              onChange={(event) =>
                setDraft((previous) => ({ ...previous, accountId: Number(event.target.value) }))
              }
            >
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        {draft.type === 'transfer' ? (
          <div>
            <label className="ab-field-label" htmlFor="editor-to-account">
              {t('ledger.toAccount')}
            </label>
            <select
              id="editor-to-account"
              className="ab-select"
              value={draft.toAccountId ?? ''}
              onChange={(event) =>
                setDraft((previous) => ({ ...previous, toAccountId: Number(event.target.value) }))
              }
            >
              {accounts.map((account) => (
                <option key={account.id} value={account.id} disabled={account.id === draft.accountId}>
                  {account.name}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between">
              <span className="ab-field-label !mb-0">{t('ledger.category')}</span>
              {draft.type !== 'adjust' ? (
                <button
                  type="button"
                  className="ab-chip"
                  data-active={splitMode}
                  onClick={() => {
                    setSplitMode((value) => !value)
                    setDraft((previous) => ({ ...previous, splits: [] }))
                  }}
                >
                  <Icon name="copy" size={12} />
                  {t('ledger.splitMode')}
                </button>
              ) : null}
            </div>

            {splitMode ? (
              <div className="space-y-2 rounded-ab-sm border border-separator/60 p-2.5">
                <p className="text-ab-caption text-label-3">{t('ledger.splitHint')}</p>
                {draft.splits.map((split, index) => (
                  <div key={index} className="flex items-center gap-2">
                    <select
                      className="ab-select flex-1"
                      value={split.category_id ?? ''}
                      onChange={(event) =>
                        setDraft((previous) => ({
                          ...previous,
                          // 用 map 生成新数组而不是原地赋值：
                          // 索引赋值在 noUncheckedIndexedAccess 下类型不安全，
                          // 而且容易漏掉"数组长度变化"的情况
                          splits: previous.splits.map((item, position) =>
                            position === index
                              ? { ...item, category_id: Number(event.target.value) }
                              : item,
                          ),
                        }))
                      }
                    >
                      <option value="">{t('ledger.uncategorized')}</option>
                      {splitCategoryOptions.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name}
                        </option>
                      ))}
                    </select>
                    <input
                      className="ab-input ab-tnum w-28"
                      inputMode="decimal"
                      value={split.amount_minor / 100}
                      onChange={(event) => {
                        const parsed = parseAmountToMinor(event.target.value) ?? 0
                        setDraft((previous) => ({
                          ...previous,
                          splits: previous.splits.map((item, position) =>
                            position === index ? { ...item, amount_minor: parsed } : item,
                          ),
                        }))
                      }}
                    />
                    <button
                      type="button"
                      className="ab-icon-btn hover:text-negative"
                      aria-label={t('common.delete')}
                      onClick={() =>
                        setDraft((previous) => ({
                          ...previous,
                          splits: previous.splits.filter((_, position) => position !== index),
                        }))
                      }
                    >
                      <Icon name="trash" size={14} />
                    </button>
                  </div>
                ))}
                <div className="flex items-center justify-between">
                  <button
                    type="button"
                    className="ab-btn-secondary"
                    onClick={() =>
                      setDraft((previous) => ({
                        ...previous,
                        splits: [...previous.splits, { category_id: null, amount_minor: 0 }],
                      }))
                    }
                  >
                    <Icon name="plus" size={13} />
                    {t('ledger.addSplit')}
                  </button>
                  <span className={`ab-tnum text-ab-footnote ${splitsValid ? 'text-label-3' : 'text-negative'}`}>
                    {t('ledger.splitRemaining', { amount: displayMinor(remaining, 'CNY') })}
                  </span>
                </div>
              </div>
            ) : (
              <div className="max-h-56 overflow-y-auto rounded-ab-sm border border-separator/60 p-1.5">
                <CategoryPicker
                  compact
                  kind={kind}
                  value={draft.categoryId}
                  onChange={(categoryId) => setDraft((previous) => ({ ...previous, categoryId }))}
                />
              </div>
            )}
          </>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="ab-field-label" htmlFor="editor-payee">
              {t('ledger.payee')}
            </label>
            <input
              id="editor-payee"
              className="ab-input"
              value={draft.payee}
              onChange={(event) => setDraft((previous) => ({ ...previous, payee: event.target.value }))}
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="editor-note">
              {t('ledger.note')}
            </label>
            <input
              id="editor-note"
              className="ab-input"
              value={draft.note}
              onChange={(event) => setDraft((previous) => ({ ...previous, note: event.target.value }))}
            />
          </div>
        </div>

        {/* 标签 / 项目 / 成员：横向维度，都是可选的 */}
        <div>
          <span className="ab-field-label">{t('ledger.tags')}</span>
          <TagPicker
            value={draft.tagIds}
            onChange={(tagIds) => setDraft((previous) => ({ ...previous, tagIds }))}
          />
        </div>

        {projects.length > 0 || members.length > 0 ? (
          <div className="grid gap-3 sm:grid-cols-2">
            {projects.length > 0 ? (
              <div>
                <label className="ab-field-label" htmlFor="editor-project">
                  {t('ledger.project')}
                </label>
                <select
                  id="editor-project"
                  className="ab-select"
                  value={draft.projectId ?? ''}
                  onChange={(event) =>
                    setDraft((previous) => ({
                      ...previous,
                      projectId: event.target.value ? Number(event.target.value) : null,
                    }))
                  }
                >
                  <option value="">{t('ledger.noProject')}</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}
            {members.length > 0 ? (
              <div>
                <label className="ab-field-label" htmlFor="editor-member">
                  {t('ledger.member')}
                </label>
                <select
                  id="editor-member"
                  className="ab-select"
                  value={draft.memberId ?? ''}
                  onChange={(event) =>
                    setDraft((previous) => ({
                      ...previous,
                      memberId: event.target.value ? Number(event.target.value) : null,
                    }))
                  }
                >
                  <option value="">{t('ledger.noMember')}</option>
                  {members.map((member) => (
                    <option key={member.id} value={member.id}>
                      {member.is_self ? `${member.name}（${t('ledger.self')}）` : member.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </Modal>
  )
}

/** 取指定方向的扁平分类（供分账下拉使用） */
function useLedgerCategories(kind: 'expense' | 'income') {
  const { categories } = useLedger()
  return useMemo(() => categories.filter((item) => item.kind === kind), [categories, kind])
}
