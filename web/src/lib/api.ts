/**
 * 后端 API 客户端（P0 系统接口 + P1 记账接口）。
 *
 * 安全约定（与 accountbook/core/security.py 对应）
 * ------------------------------------------------
 * * 所有请求都带上 `Authorization: Bearer <token>`，并同时依赖同源 Cookie 兜底；
 * * **绝不**把令牌写进 URL 或 localStorage —— URL 会进入历史记录与日志，
 *   localStorage 被同机其它进程读取的风险更高（P8 引入应用锁时会重新评估）；
 * * 请求一律 `credentials: 'same-origin'`，杜绝跨源携带凭据。
 *
 * 错误处理策略
 * ------------
 * 后端有两套错误体：
 *   * 领域错误 → `{ code, message, details }`（服务层抛出的业务规则冲突）；
 *   * 请求校验 → `{ detail: [...] }`（Pydantic 的类型/范围错误）。
 * 这里统一转成 `ApiError`，并**保留 code 与 details** ——
 * 界面据此可以显示本地化文案或定位到具体字段，而不是把后端的
 * 中文句子直接摊在屏幕上（那会让界面语言与设置不一致）。
 */

import { boot, type Preferences } from './boot'

/** 后端返回的环境信息（对应 api/routes/system.py 的 RuntimeInfo） */
export interface RuntimeInfo {
  app_id: string
  app_name: string
  app_name_en: string
  version: string
  phase: string
  python_version: string
  platform: string
  pid: number
  port: number
  uptime_seconds: number
  is_admin: boolean
  /** 实际生效的外壳：pywebview（原生窗口）或 browser（自动降级） */
  shell: string
  /** 选定的 .NET 运行时；null 表示原生外壳不可用 */
  clr_runtime: string | null
  paths: {
    data_dir: string
    /** 数据目录来源说明（首选位置 / 回退位置） */
    data_dir_source: string
    /** 被跳过的候选位置及原因；为空表示首选位置可用 */
    data_dir_attempts: string[]
    program_root: string
    log_file: string
    database: string
    portable: boolean
    frozen: boolean
    degraded: boolean
  }
}

// -----------------------------------------------------------------------------
// 错误
// -----------------------------------------------------------------------------
export class ApiError extends Error {
  readonly status: number
  readonly detail: string
  /** 稳定的机器可读错误码（`validation_error` / `conflict` / `protected_entity` …） */
  readonly code: string
  /** 结构化详情（冲突字段、余额缺口、被引用的流水数…） */
  readonly details: Record<string, unknown>

  constructor(status: number, detail: string, code = '', details: Record<string, unknown> = {}) {
    super(`[${status}] ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.code = code
    this.details = details
  }
}

/** 从各种错误体里抽出一句可读的话 */
function extractMessage(body: unknown, fallback: string): string {
  if (!body || typeof body !== 'object') return fallback
  const record = body as Record<string, unknown>
  if (typeof record.message === 'string' && record.message) return record.message
  if (typeof record.detail === 'string' && record.detail) return record.detail
  if (Array.isArray(record.detail) && record.detail.length > 0) {
    // Pydantic 校验错误：把"哪个字段、错在哪"拼成一句话
    const first = record.detail[0] as Record<string, unknown> | undefined
    const location = Array.isArray(first?.loc) ? (first?.loc as unknown[]).join('.') : ''
    const message = typeof first?.msg === 'string' ? first.msg : ''
    return location ? `${location}: ${message}` : message || fallback
  }
  return fallback
}

const JSON_HEADERS: HeadersInit = {
  'Content-Type': 'application/json',
  Authorization: `Bearer ${boot.token}`,
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      headers: { ...JSON_HEADERS, ...(init.headers ?? {}) },
    })
  } catch (cause) {
    // 网络层失败（后端已退出 / 端口被占用）：给出比 "Failed to fetch" 更有用的信息
    throw new ApiError(0, `无法连接本地服务，接口 ${path} 请求失败：${String(cause)}`)
  }

  if (!response.ok) {
    let body: unknown = null
    try {
      body = await response.json()
    } catch {
      /* 响应不是 JSON（例如 401 的 HTML 页面）时沿用状态文本 */
    }
    const record = (body ?? {}) as Record<string, unknown>
    throw new ApiError(
      response.status,
      extractMessage(body, response.statusText || '请求失败'),
      typeof record.code === 'string' ? record.code : '',
      (record.details as Record<string, unknown>) ?? {},
    )
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** 把查询参数对象拼成 query string（过滤空值；数组展开为重复参数） */
function query(params: Record<string, unknown>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== undefined && item !== null && item !== '') search.append(key, String(item))
      }
    } else {
      search.set(key, String(value))
    }
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

// -----------------------------------------------------------------------------
// P1 类型（与 api/schemas.py 一一对应）
// -----------------------------------------------------------------------------
export type AccountType =
  | 'cash'
  | 'debit_card'
  | 'credit_card'
  | 'e_wallet'
  | 'investment'
  | 'receivable'
  | 'payable'
  | 'prepaid'
  | 'virtual'

export type TransactionType = 'expense' | 'income' | 'transfer' | 'adjust'
export type TransactionStatus = 'pending' | 'cleared' | 'reconciled' | 'void'
export type Direction = 'in' | 'out'
export type CategoryKind = 'expense' | 'income' | 'transfer'

export interface Account {
  id: number
  name: string
  type: AccountType | string
  currency: string
  initial_balance_minor: number
  icon: string
  color: string
  institution: string
  card_no_tail: string
  credit_limit_minor: number
  bill_day: number | null
  due_day: number | null
  include_in_net_worth: boolean
  is_archived: boolean
  sort_order: number
  note: string
  deleted_at: string | null
}

export interface AccountOverviewItem {
  id: number
  name: string
  type: string
  currency: string
  icon: string
  color: string
  institution: string
  card_no_tail: string
  balance_minor: number
  initial_balance_minor: number
  credit_limit_minor: number
  include_in_net_worth: boolean
  is_archived: boolean
  is_liability_type: boolean
  sort_order: number
}

export interface AccountOverview {
  accounts: AccountOverviewItem[]
  assets_minor: number
  liabilities_minor: number
  net_worth_minor: number
  account_count: number
  counted_in_net_worth: number
}

export interface Category {
  id: number
  name: string
  kind: CategoryKind | string
  parent_id: number | null
  path: string
  depth: number
  icon: string
  color: string
  is_system: boolean
  is_hidden: boolean
  sort_order: number
  note: string
  deleted_at: string | null
}

export interface CategoryNode extends Category {
  children: CategoryNode[]
}

export interface CategoryMergeResult {
  transactions: number
  splits: number
  children: number
}

export interface Split {
  id?: number
  category_id: number | null
  amount_minor: number
  note?: string
  sort_order?: number
}

export interface Transaction {
  id: number
  type: TransactionType | string
  direction: Direction | string
  occurred_at: string
  account_id: number
  account_name: string
  to_account_id: number | null
  to_account_name: string
  category_id: number | null
  category_name: string
  category_icon: string
  category_color: string
  amount_minor: number
  currency: string
  payee: string
  note: string
  status: TransactionStatus | string
  tags: string[]
  splits: { id: number; category_id: number | null; amount_minor: number; note: string }[]
}

export interface TransactionList {
  items: Transaction[]
  total: number
  limit: number
  offset: number
}

export interface TransactionInput {
  type: TransactionType
  direction?: Direction
  occurred_at?: string
  account_id: number
  to_account_id?: number | null
  category_id?: number | null
  amount_minor: number
  currency?: string
  payee?: string
  note?: string
  status?: TransactionStatus
  source?: string
  external_id?: string | null
  project_id?: number | null
  member_id?: number | null
  splits?: Split[] | null
}

export interface TransactionFilter {
  start?: string
  end?: string
  account_ids?: number[]
  category_ids?: number[]
  tag_ids?: number[]
  types?: string[]
  statuses?: string[]
  project_id?: number
  member_id?: number
  min_amount_minor?: number
  max_amount_minor?: number
  keyword?: string
  include_transfers?: boolean
  order?: 'asc' | 'desc'
  limit?: number
  offset?: number
}

export interface Tag {
  id: number
  name: string
  color: string
  note: string
}

export interface Project {
  id: number
  name: string
  color: string
  status: string
  budget_minor: number
  note: string
}

export interface Member {
  id: number
  name: string
  color: string
  is_self: boolean
  note: string
}

export interface Currency {
  code: string
  name: string
  symbol: string
  minor_units: number
}

export interface Enums {
  account_types: string[]
  category_kinds: string[]
  transaction_types: string[]
  transaction_statuses: string[]
  transaction_sources: string[]
  directions: string[]
  project_statuses: string[]
}

export interface CategoryBreakdownItem {
  category_id: number | null
  category_name: string
  amount_minor: number
}

export interface DashboardData {
  reference_date: string
  net_worth: {
    net_worth_minor: number
    assets_minor: number
    liabilities_minor: number
    account_count: number
  }
  month: {
    start: string
    end: string
    income_minor: number
    expense_minor: number
    net_minor: number
    transaction_count: number
    /** 上期为 0 时返回 null —— 环比无定义，界面应显示「—」而不是编造的百分比 */
    income_change: number | null
    expense_change: number | null
    top_categories: CategoryBreakdownItem[]
  }
  trend: { date: string; income_minor: number; expense_minor: number; transaction_count: number }[]
  recent_transactions: Transaction[]
  accounts: AccountOverviewItem[]
}

export interface CalendarDay {
  date: string
  income_minor: number
  expense_minor: number
  net_minor: number
  transaction_count: number
  has_entries: boolean
}

export interface IntegrityReport {
  ok: boolean
  issues: { code: string; count: number; severity: string; message: string }[]
  stats: { transactions: number; transfer_like: number; accounts: number; categories: number }
}

// -----------------------------------------------------------------------------
// 接口
// -----------------------------------------------------------------------------
export const api = {
  // ---- 系统（P0） ----------------------------------------------------------
  /** 探活：用于前端显示"服务已连接"状态 */
  health: () => request<{ status: string; version: string; phase: string }>('/health'),

  /** 运行环境信息 */
  runtimeInfo: () => request<RuntimeInfo>('/api/system/info'),

  /** 读取偏好 */
  getPreferences: () => request<{ preferences: Preferences }>('/api/system/preferences'),

  /**
   * 局部更新偏好。
   * 只发送真正改动的字段 —— 减少无效写入，也让"谁改了什么"在日志里一目了然。
   */
  patchPreferences: (changes: Partial<Preferences>) =>
    request<{ preferences: Preferences }>('/api/system/preferences', {
      method: 'PATCH',
      body: JSON.stringify(changes),
    }),

  /** 在文件管理器中打开数据目录 */
  revealDataDir: () =>
    request<{ status: string; path: string; detail?: string }>('/api/system/reveal-data-dir', {
      method: 'POST',
    }),

  // ---- 元数据 --------------------------------------------------------------
  enums: () => request<Enums>('/api/meta/enums'),
  currencies: () => request<Currency[]>('/api/meta/currencies'),

  // ---- 账户 ----------------------------------------------------------------
  accounts: (params: { include_archived?: boolean; include_deleted?: boolean } = {}) =>
    request<Account[]>(`/api/accounts${query(params)}`),
  accountsOverview: (params: { include_archived?: boolean } = {}) =>
    request<AccountOverview>(`/api/accounts/overview${query(params)}`),
  createAccount: (payload: Partial<Account>) =>
    request<Account>('/api/accounts', { method: 'POST', body: JSON.stringify(payload) }),
  updateAccount: (id: number, changes: Partial<Account>) =>
    request<Account>(`/api/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(changes) }),
  deleteAccount: (id: number) => request<void>(`/api/accounts/${id}`, { method: 'DELETE' }),
  restoreAccount: (id: number) => request<Account>(`/api/accounts/${id}/restore`, { method: 'POST' }),

  // ---- 分类 ----------------------------------------------------------------
  categories: (params: { kind?: string; include_hidden?: boolean } = {}) =>
    request<Category[]>(`/api/categories${query(params)}`),
  categoryTree: (params: { kind?: string; include_hidden?: boolean } = {}) =>
    request<CategoryNode[]>(`/api/categories/tree${query(params)}`),
  createCategory: (payload: {
    name: string
    kind: CategoryKind
    parent_id?: number | null
    icon?: string
    color?: string
  }) => request<Category>('/api/categories', { method: 'POST', body: JSON.stringify(payload) }),
  updateCategory: (
    id: number,
    changes: { name?: string; icon?: string; color?: string; is_hidden?: boolean; note?: string },
  ) => request<Category>(`/api/categories/${id}`, { method: 'PATCH', body: JSON.stringify(changes) }),
  moveCategory: (id: number, parentId: number | null) =>
    request<Category>(`/api/categories/${id}/move`, {
      method: 'POST',
      body: JSON.stringify({ parent_id: parentId }),
    }),
  mergeCategory: (id: number, targetId: number) =>
    request<CategoryMergeResult>(`/api/categories/${id}/merge${query({ target_id: targetId })}`, {
      method: 'POST',
    }),
  hideCategory: (id: number, hidden: boolean) =>
    request<Category>(`/api/categories/${id}/hide${query({ hidden })}`, { method: 'POST' }),
  deleteCategory: (id: number) => request<void>(`/api/categories/${id}`, { method: 'DELETE' }),

  // ---- 流水 ----------------------------------------------------------------
  transactions: (filter: TransactionFilter = {}) =>
    request<TransactionList>(`/api/transactions${query({ ...filter })}`),
  transaction: (id: number) => request<Transaction>(`/api/transactions/${id}`),
  createTransaction: (payload: TransactionInput) =>
    request<Transaction>('/api/transactions', { method: 'POST', body: JSON.stringify(payload) }),
  updateTransaction: (id: number, changes: Partial<TransactionInput>) =>
    request<Transaction>(`/api/transactions/${id}`, { method: 'PATCH', body: JSON.stringify(changes) }),
  deleteTransaction: (id: number) => request<void>(`/api/transactions/${id}`, { method: 'DELETE' }),
  restoreTransaction: (id: number) =>
    request<Transaction>(`/api/transactions/${id}/restore`, { method: 'POST' }),

  // ---- 标签 / 项目 / 成员 ---------------------------------------------------
  tags: () => request<Tag[]>('/api/tags'),
  createTag: (payload: { name: string; color?: string; note?: string }) =>
    request<Tag>('/api/tags', { method: 'POST', body: JSON.stringify(payload) }),
  projects: () => request<Project[]>('/api/projects'),
  createProject: (payload: { name: string; color?: string; budget_minor?: number }) =>
    request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
  members: () => request<Member[]>('/api/members'),
  createMember: (payload: { name: string; color?: string; is_self?: boolean }) =>
    request<Member>('/api/members', { method: 'POST', body: JSON.stringify(payload) }),

  // ---- 统计 ----------------------------------------------------------------
  dashboard: (params: { reference?: string; trend_days?: number; recent_limit?: number } = {}) =>
    request<DashboardData>(`/api/stats/dashboard${query(params)}`),
  cashFlow: (params: { months?: number } = {}) =>
    request<
      { month: string; income_minor: number; expense_minor: number; net_minor: number; transaction_count: number }[]
    >(`/api/stats/cash-flow${query(params)}`),
  calendar: (start: string, end: string, includeTransfers = false) =>
    request<CalendarDay[]>(
      `/api/stats/calendar${query({ start, end, include_transfers: includeTransfers })}`,
    ),
  summary: (params: { start?: string; end?: string; top_categories?: number } = {}) =>
    request<{
      income_minor: number
      expense_minor: number
      net_minor: number
      transaction_count: number
      by_category: CategoryBreakdownItem[]
    }>(`/api/stats/summary${query(params)}`),
  integrity: () => request<IntegrityReport>('/api/stats/integrity'),
}
