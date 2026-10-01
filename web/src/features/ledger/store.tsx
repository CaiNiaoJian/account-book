/**
 * 账本上下文 —— 把"全局只变一次"的数据集中加载并共享。
 *
 * 为什么需要它
 * ------------
 * 账户名、分类名与图标在界面上**到处都要用**：流水列表要显示分类名，
 * 首页要显示账户余额，记账时要选分类。如果每个页面各自请求，
 * 结果是同一份数据被拉 N 次，且各处可能不同步（改完分类名，
 * 列表还是旧名字 —— 这类"看起来没保存"的假象最消耗信任）。
 *
 * 因此这里做三件事：
 *   1. 一次性加载枚举、币种、账户总览、分类树，并暴露 refresh()；
 *   2. 提供 id → 名称/图标的查表函数（避免每个组件各自 find）；
 *   3. 顺手把币种字典注册给 `lib/format.ts`，保证金额格式化与后端一致
 *      （日元 0 位小数这类差异，靠前端硬编码一定会出错）。
 *
 * 流水列表**不放在这里**：它是分页且带筛选的，属于页面级状态。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import {
  api,
  type Account,
  type AccountOverview,
  type AccountOverviewItem,
  type Category,
  type CategoryNode,
  type Currency,
  type Enums,
} from '@/lib/api'
import { registerCurrencies } from '@/lib/format'

export type LedgerStatus = 'loading' | 'ready' | 'error'

interface LedgerContextValue {
  status: LedgerStatus
  error: string | null
  enums: Enums | null
  currencies: Currency[]
  overview: AccountOverview | null
  /** 未归档账户（含余额），按 sort_order 排序 */
  accounts: AccountOverviewItem[]
  /** 全部账户（含归档），用于设置类界面 */
  allAccounts: Account[]
  /** 扁平分类（含隐藏） */
  categories: Category[]
  /** 按方向分组的分类树 */
  tree: { expense: CategoryNode[]; income: CategoryNode[] }
  refresh: () => Promise<void>
  accountName: (id: number | null | undefined) => string
  accountIcon: (id: number | null | undefined) => string
  categoryById: (id: number | null | undefined) => Category | undefined
  categoryName: (id: number | null | undefined) => string
}

const LedgerContext = createContext<LedgerContextValue | null>(null)

/** 把分类树拍平（选择器与查表都用扁平表，树只用于展示层级） */
function flatten(nodes: CategoryNode[], out: Category[] = []): Category[] {
  for (const node of nodes) {
    out.push(node)
    if (node.children?.length) flatten(node.children, out)
  }
  return out
}

export function LedgerProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<LedgerStatus>('loading')
  const [error, setError] = useState<string | null>(null)
  const [enums, setEnums] = useState<Enums | null>(null)
  const [currencies, setCurrencies] = useState<Currency[]>([])
  const [overview, setOverview] = useState<AccountOverview | null>(null)
  const [allAccounts, setAllAccounts] = useState<Account[]>([])
  const [tree, setTree] = useState<{ expense: CategoryNode[]; income: CategoryNode[] }>({
    expense: [],
    income: [],
  })

  const refresh = useCallback(async () => {
    try {
      // 五个请求并发：它们互不依赖，串行只会让首屏多等几个来回
      const [nextEnums, nextCurrencies, nextOverview, nextAccounts, expenseTree, incomeTree] =
        await Promise.all([
          api.enums(),
          api.currencies(),
          api.accountsOverview(),
          api.accounts({ include_archived: true }),
          api.categoryTree({ kind: 'expense' }),
          api.categoryTree({ kind: 'income' }),
        ])

      registerCurrencies(nextCurrencies)
      setEnums(nextEnums)
      setCurrencies(nextCurrencies)
      setOverview(nextOverview)
      setAllAccounts(nextAccounts)
      setTree({ expense: expenseTree, income: incomeTree })
      setError(null)
      setStatus('ready')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setStatus('error')
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const categories = useMemo(() => [...flatten(tree.expense), ...flatten(tree.income)], [tree])

  const categoryIndex = useMemo(() => {
    const index = new Map<number, Category>()
    for (const item of categories) index.set(item.id, item)
    return index
  }, [categories])

  const accountIndex = useMemo(() => {
    const index = new Map<number, AccountOverviewItem>()
    for (const item of overview?.accounts ?? []) index.set(item.id, item)
    return index
  }, [overview])

  const value = useMemo<LedgerContextValue>(
    () => ({
      status,
      error,
      enums,
      currencies,
      overview,
      accounts: overview?.accounts ?? [],
      allAccounts,
      categories,
      tree,
      refresh,
      accountName: (id) => (id ? (accountIndex.get(id)?.name ?? '') : ''),
      accountIcon: (id) => (id ? (accountIndex.get(id)?.icon ?? 'accounts') : 'accounts'),
      categoryById: (id) => (id ? categoryIndex.get(id) : undefined),
      categoryName: (id) => (id ? (categoryIndex.get(id)?.name ?? '') : ''),
    }),
    [
      status,
      error,
      enums,
      currencies,
      overview,
      allAccounts,
      categories,
      tree,
      refresh,
      accountIndex,
      categoryIndex,
    ],
  )

  return <LedgerContext.Provider value={value}>{children}</LedgerContext.Provider>
}

export function useLedger(): LedgerContextValue {
  const context = useContext(LedgerContext)
  if (!context) throw new Error('useLedger 必须在 LedgerProvider 内使用')
  return context
}
