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
  useRef,
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
  type Member,
  type Project,
  type Tag,
} from '@/lib/api'
import { registerCurrencies } from '@/lib/format'
import { LEDGER_RESOURCES, loadResources, readTask, type LedgerResource, type ResourceState } from './loading'

export type LedgerStatus = 'loading' | 'ready' | 'partial' | 'error'

interface LedgerContextValue {
  status: LedgerStatus
  error: string | null
  loadErrors: Partial<Record<LedgerResource, string>>
  resourceStates: Record<LedgerResource, ResourceState>
  refreshing: boolean
  retryFailed: () => Promise<void>
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
  /** 标签（横向维度：出差、报销、旅行…） */
  tags: Tag[]
  /** 项目（把一段时期的相关支出归到一处） */
  projects: Project[]
  /** 成员（分摊与家庭账本） */
  members: Member[]
  refresh: () => Promise<void>
  accountName: (id: number | null | undefined) => string
  accountIcon: (id: number | null | undefined) => string
  categoryById: (id: number | null | undefined) => Category | undefined
  categoryName: (id: number | null | undefined) => string
  tagByName: (name: string) => Tag | undefined
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
  const [loadErrors, setLoadErrors] = useState<Partial<Record<LedgerResource, string>>>({})
  const [resourceStates, setResourceStates] = useState<Record<LedgerResource, ResourceState>>(
    () => Object.fromEntries(LEDGER_RESOURCES.map((key) => [key, 'loading'])) as Record<LedgerResource, ResourceState>,
  )
  const [refreshing, setRefreshing] = useState(false)
  const currentRun = useRef<AbortController | null>(null)
  const loaded = useRef(new Set<LedgerResource>())
  const failed = useRef(new Set<LedgerResource>())
  const [enums, setEnums] = useState<Enums | null>(null)
  const [currencies, setCurrencies] = useState<Currency[]>([])
  const [overview, setOverview] = useState<AccountOverview | null>(null)
  const [allAccounts, setAllAccounts] = useState<Account[]>([])
  const [tree, setTree] = useState<{ expense: CategoryNode[]; income: CategoryNode[] }>({
    expense: [],
    income: [],
  })
  const [tags, setTags] = useState<Tag[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [members, setMembers] = useState<Member[]>([])

  const load = useCallback(async (only?: Set<LedgerResource>) => {
    currentRun.current?.abort()
    const controller = new AbortController()
    currentRun.current = controller
    setRefreshing(true)
    const tasks = [
      readTask('enums', (signal) => api.enums(signal), setEnums),
      readTask('currencies', (signal) => api.currencies(signal), (data) => {
        registerCurrencies(data)
        setCurrencies(data)
      }),
      readTask('overview', (signal) => api.accountsOverview({ include_archived: true }, signal), setOverview),
      readTask('accounts', (signal) => api.accounts({ include_archived: true }, signal), setAllAccounts),
      readTask('expenseTree', (signal) => api.categoryTree({ kind: 'expense' }, signal),
        (expense) => setTree((previous) => ({ ...previous, expense }))),
      readTask('incomeTree', (signal) => api.categoryTree({ kind: 'income' }, signal),
        (income) => setTree((previous) => ({ ...previous, income }))),
      readTask('tags', (signal) => api.tags(signal), setTags),
      readTask('projects', (signal) => api.projects(signal), setProjects),
      readTask('members', (signal) => api.members(signal), setMembers),
    ].filter((task) => !only || only.has(task.key))
    setResourceStates((previous) => ({ ...previous, ...Object.fromEntries(tasks.map((task) =>
      [task.key, loaded.current.has(task.key) ? 'ready' : 'loading'])) }))
    await loadResources(tasks, controller.signal, ({ key, error }) => {
      if (error !== undefined) failed.current.add(key)
      else {
        failed.current.delete(key)
        loaded.current.add(key)
        setStatus((previous) => previous === 'loading' ? 'partial' : previous)
      }
      setResourceStates((previous) => ({ ...previous, [key]: error === undefined ? 'ready' : 'error' }))
      setLoadErrors((previous) => {
        const next = { ...previous }
        if (error !== undefined) next[key] = error instanceof Error ? error.name : String(error)
        else delete next[key]
        return next
      })
    })
    if (controller.signal.aborted) return
    setRefreshing(false)
    setStatus(failed.current.size === 0 ? 'ready' : loaded.current.size > 0 ? 'partial' : 'error')
  }, [])

  const refresh = useCallback(() => load(), [load])
  const retryFailed = useCallback(() => load(new Set(failed.current)), [load])

  useEffect(() => {
    void refresh()
    return () => currentRun.current?.abort()
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

  const tagIndex = useMemo(() => {
    const index = new Map<string, Tag>()
    for (const item of tags) index.set(item.name, item)
    return index
  }, [tags])

  const value = useMemo<LedgerContextValue>(
    () => ({
      status,
      error: Object.keys(loadErrors).length > 0 ? Object.entries(loadErrors).map(([key, message]) => `${key}: ${message}`).join('; ') : null,
      loadErrors,
      resourceStates,
      refreshing,
      retryFailed,
      enums,
      currencies,
      overview,
      accounts: overview?.accounts.filter((account) => !account.is_archived) ?? [],
      allAccounts,
      categories,
      tree,
      tags,
      projects,
      members,
      refresh,
      accountName: (id) => (id ? (accountIndex.get(id)?.name ?? '') : ''),
      accountIcon: (id) => (id ? (accountIndex.get(id)?.icon ?? 'accounts') : 'accounts'),
      categoryById: (id) => (id ? categoryIndex.get(id) : undefined),
      categoryName: (id) => (id ? (categoryIndex.get(id)?.name ?? '') : ''),
      tagByName: (name) => tagIndex.get(name),
    }),
    [
      status,
      loadErrors,
      resourceStates,
      refreshing,
      retryFailed,
      enums,
      currencies,
      overview,
      allAccounts,
      categories,
      tree,
      tags,
      projects,
      members,
      refresh,
      accountIndex,
      categoryIndex,
      tagIndex,
    ],
  )

  return <LedgerContext.Provider value={value}>{children}</LedgerContext.Provider>
}

export function useLedger(): LedgerContextValue {
  const context = useContext(LedgerContext)
  if (!context) throw new Error('useLedger 必须在 LedgerProvider 内使用')
  return context
}
