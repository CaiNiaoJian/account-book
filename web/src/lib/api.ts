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

/**
 * 上传一个文件。
 *
 * 不能用上面的 `request()`：它总是带上 `Content-Type: application/json`，
 * 而那会让服务端按 JSON 解析二进制体。这里刻意**不设 Content-Type**，
 * 由浏览器按文件自身类型填充 —— 服务端也只信魔数，不依赖这个头
 * （客户端可以随便写这个头，它只是给服务端一个提示）。
 */
async function upload<T>(path: string, file: File): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    body: file,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = extractMessage(body, detail)
    } catch {
      /* 非 JSON 响应时沿用状态文本 */
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
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
  project_id: number | null
  project_name: string
  member_id: number | null
  member_name: string
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
  /** 标签 id 列表。传 `[]` 清空；省略表示不改动（更新时） */
  tag_ids?: number[] | null
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
  /** 近 N 天的逐日趋势。
et_worth_minor 与 
et_minor 口径不同：
   *  前者来自日结缓存（含转账与起点余额），后者只是当日收入减支出 */
  trend: {
    date: string
    income_minor: number
    expense_minor: number
    net_minor: number
    net_worth_minor: number
    transaction_count: number
  }[]
  recent_transactions: Transaction[]
  accounts: AccountOverviewItem[]
}


export interface IntegrityReport {
  ok: boolean
  issues: { code: string; count: number; severity: string; message: string }[]
  stats: { transactions: number; transfer_like: number; accounts: number; categories: number }
}

// -----------------------------------------------------------------------------
// P2：日历 / 卡片墙 / 机构 / 卡面
// -----------------------------------------------------------------------------
export type CalendarMetric = 'entry' | 'expense' | 'income' | 'net' | 'net_worth_change' | 'anomaly'

export interface CalendarDay {
  date: string
  income_minor: number
  expense_minor: number
  net_minor: number
  net_worth_minor: number
  tx_count: number
  pending_count: number
  entry_state: 'none' | 'logged' | 'confirmed'
  anomaly_score: number
  top_category_id: number | null
  top_category_name: string
  event_count: number
  has_attachment: boolean
  /** 当前主指标的原始值（`net_worth_change` 为比例，其余为金额或分数） */
  metric_value: number
  /** 服务端算好的强度分级：entry 为 0..2，其余为 0..4 */
  level: number
  badges: string[]
}

export interface CalendarResponse {
  metric: CalendarMetric
  metrics: CalendarMetric[]
  start: string
  end: string
  days: CalendarDay[]
}

export interface DayEvent {
  id: number
  date: string
  kind: 'event' | 'mood' | 'anniversary' | 'note' | 'todo'
  title: string
  body: string
  tags: unknown[]
  attachments: unknown[]
  sort_order: number
}

export interface DayDetail {
  date: string
  stat: {
    income_minor: number
    expense_minor: number
    net_minor: number
    net_worth_minor: number
    opening_net_worth_minor: number
    tx_count: number
    entry_state: string
    anomaly_score: number
    event_count: number
  }
  /** 日内余额阶梯曲线：拐点落在流水真实发生的时刻 */
  net_worth_series: { at: string; net_worth_minor: number; label: string; transaction_id?: number }[]
  composition: { category_id: number | null; category_name: string; amount_minor: number }[]
  weekday_average_expense_minor: number
  contributions: {
    transaction_id: number
    label: string
    type: string
    delta_minor: number
    category_name: string
  }[]
  events: DayEvent[]
  transactions: Transaction[]
}

export type RecurringFrequency = 'daily' | 'weekly' | 'monthly' | 'yearly'

export interface RecurringRule {
  id: number
  name: string
  enabled: boolean
  type: 'expense' | 'income' | 'transfer'
  account_id: number
  to_account_id: number | null
  category_id: number | null
  amount_minor: number
  currency: string
  payee: string
  note: string
  frequency: RecurringFrequency
  interval: number
  by_month_day: number | null
  by_weekday: number | null
  start_date: string
  end_date: string | null
  /** 下次应生成日。由服务端推算，前端不重复计算 */
  next_due_date: string | null
  last_posted_on: string | null
  auto_post: boolean
  lead_days: number
  generated_count: number
}

export interface UpcomingRule {
  rule_id: number
  name: string
  type: string
  amount_minor: number
  currency: string
  due_date: string
  days_until: number
  overdue: boolean
  auto_post: boolean
}

export interface PostDueReport {
  date: string
  created: { rule_id: number; name: string; date: string; amount_minor: number; type: string }[]
  skipped: {
    rule_id: number
    name: string
    reason: string
    limit?: number
    next_due_date?: string
  }[]
  dry_run: boolean
}

export type BudgetPeriod = 'weekly' | 'monthly' | 'quarterly' | 'yearly' | 'custom'

export interface BudgetStatus {
  id: number
  name: string
  scope: 'total' | 'category'
  category_id: number | null
  /** 分类预算的分类名，由接口补齐（服务层不碰展示文案） */
  category_name?: string
  period: BudgetPeriod
  currency: string
  amount_minor: number
  carryover_minor: number
  available_minor: number
  spent_minor: number
  /** 可以为负（超支）。刻意不夹到 0 */
  remaining_minor: number
  /** 可以大于 1。刻意不夹到 1 —— "用掉 130%" 与 "用掉 100%" 是不同处境 */
  ratio: number
  over: boolean
  alert: boolean
  alert_threshold: number
  enabled: boolean
  note: string
  start: string
  end: string
  days_total: number
  days_left: number
  daily_allowance_minor: number
  is_current: boolean
}

export interface BudgetOverview {
  date: string
  items: BudgetStatus[]
  total: BudgetStatus | null
  category_budgets: BudgetStatus[]
  alerts: BudgetStatus[]
  has_budget: boolean
}

export interface Budget {
  id: number
  name: string
  scope: 'total' | 'category'
  category_id: number | null
  period: BudgetPeriod
  amount_minor: number
  currency: string
  start_date: string | null
  end_date: string | null
  rollover: boolean
  carryover_minor: number
  alert_threshold: number
  enabled: boolean
  note: string
}

export interface DebtStatus {
  id: number
  name: string
  kind: 'lend' | 'borrow'
  counterparty: string
  principal_minor: number
  currency: string
  account_id: number | null
  mirror_account_id: number | null
  start_date: string
  due_date: string | null
  annual_rate_bps: number
  status: 'active' | 'settled' | 'written_off'
  settled_at: string | null
  note: string
  paid_principal_minor: number
  paid_interest_minor: number
  paid_total_minor: number
  payment_count: number
  /** 可以为负（多还了）。不夹到 0，否则用户对不上账 */
  remaining_minor: number
  progress: number
  overdue: boolean
  days_until_due: number | null
  /** 按年化利率估算的每日利息。界面上必须写明"估算" */
  estimated_daily_interest_minor: number
}

export interface DebtOverview {
  date: string
  items: DebtStatus[]
  receivable: DebtStatus[]
  payable: DebtStatus[]
  summary: {
    receivable_minor: number
    payable_minor: number
    net_minor: number
    active_count: number
    overdue_count: number
    settled_count: number
  }
  overdue: DebtStatus[]
  upcoming: DebtStatus[]
  has_debt: boolean
}

export interface Debt {
  id: number
  name: string
  kind: 'lend' | 'borrow'
  counterparty: string
  principal_minor: number
  currency: string
  account_id: number | null
  mirror_account_id: number | null
  start_date: string
  due_date: string | null
  annual_rate_bps: number
  status: string
  settled_at: string | null
  note: string
}

export interface DebtPayment {
  id: number
  debt_id: number
  amount_minor: number
  principal_minor: number
  interest_minor: number
  occurred_at: string
  tz_offset_minutes: number
  account_id: number | null
  transaction_id: number | null
  note: string
}

export type KlinePeriod = 'day' | 'week' | 'month' | 'year'

export interface KlineBar {
  period_start: string
  period_end: string
  open_minor: number
  high_minor: number
  low_minor: number
  close_minor: number
  /** 周期内资金流动总额（收入 + 支出），不是余额 */
  volume_minor: number
  tx_count: number
  /** 均线；样本不足时为 null —— 不缩短窗口，否则"MA60"在开头其实是 MA3 */
  ma: Record<string, number | null>
  dif: number | null
  dea: number | null
  macd: number | null
  rsi: number | null
  /** 从区间内历史最高点回落的幅度（负数或 0） */
  drawdown: number | null
}

export interface KlineResponse {
  /** **实际使用**的周期（区间过长时会被自动降级） */
  period: KlinePeriod
  /** 用户请求的周期。与实际不同即说明发生了降级 */
  requested_period: KlinePeriod
  /** 是否被自动降级 —— 界面必须明说，否则用户以为自己在看日线 */
  downsampled: boolean
  max_bars: number
  start: string
  end: string
  /** 为了指标收敛而额外多取的根数；这些根不会出现在 bars 里 */
  warmup_bars: number
  params: {
    ma_windows: number[]
    macd: { fast: number; slow: number; signal: number }
    rsi_period: number
  }
  bars: KlineBar[]
  count: number
}

export interface KlineParams {
  ma_windows: number[]
  macd: { fast: number; slow: number; signal: number }
  rsi_period: number
  warmup_bars: number
  periods: string[]
}

export interface CardArtwork {
  id: number
  key: string
  name: string
  kind: 'builtin' | 'uploaded'
  /** 卡面配方：{ stops: [[颜色, 位置]], texture, ink, sheen } */
  spec: { stops: [string, number][]; texture: string; ink: string; sheen: number }
  /** 用户上传的卡面图片；为空表示用 spec 里的自绘渐变 */
  image_url: string | null
  file_ref: string
  author: string
  license: string
  sort_order: number
}

export interface Institution {
  id: number
  key: string
  name: string
  kind: string
  brand_color: string
  logo_ref: string
  is_system: boolean
  sort_order: number
}

export interface CardItem extends AccountOverviewItem {
  brand_key: string
  brand_name: string
  brand_color: string
  card_style: string
  card_network: string
  theme_tint: string
  bill_day: number | null
  due_day: number | null
  group: string
  credit_used_minor: number
  credit_available_minor: number
}

export interface AssetWall {
  groups: { key: string; name: string; cards: CardItem[] }[]
  summary: {
    assets_minor: number
    liabilities_minor: number
    net_worth_minor: number
    account_count: number
    counted_in_net_worth: number
    available_minor: number
    credit_limit_minor: number
    credit_used_minor: number
    credit_available_minor: number
  }
  institutions: Institution[]
  artworks: CardArtwork[]
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
  updateTag: (id: number, changes: { name?: string; color?: string; note?: string }) =>
    request<Tag>(`/api/tags/${id}`, { method: 'PATCH', body: JSON.stringify(changes) }),
  deleteTag: (id: number) => request<void>(`/api/tags/${id}`, { method: 'DELETE' }),

  projects: () => request<Project[]>('/api/projects'),
  createProject: (payload: { name: string; color?: string; budget_minor?: number; note?: string }) =>
    request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
  updateProject: (
    id: number,
    changes: { name?: string; color?: string; status?: string; budget_minor?: number; note?: string },
  ) => request<Project>(`/api/projects/${id}`, { method: 'PATCH', body: JSON.stringify(changes) }),
  deleteProject: (id: number) => request<void>(`/api/projects/${id}`, { method: 'DELETE' }),

  members: () => request<Member[]>('/api/members'),
  createMember: (payload: { name: string; color?: string; is_self?: boolean; note?: string }) =>
    request<Member>('/api/members', { method: 'POST', body: JSON.stringify(payload) }),
  updateMember: (id: number, changes: { name?: string; color?: string; is_self?: boolean; note?: string }) =>
    request<Member>(`/api/members/${id}`, { method: 'PATCH', body: JSON.stringify(changes) }),
  deleteMember: (id: number) => request<void>(`/api/members/${id}`, { method: 'DELETE' }),

  // ---- 统计 ----------------------------------------------------------------
  dashboard: (params: { reference?: string; trend_days?: number; recent_limit?: number } = {}) =>
    request<DashboardData>(`/api/stats/dashboard${query(params)}`),
  cashFlow: (params: { months?: number } = {}) =>
    request<
      { month: string; income_minor: number; expense_minor: number; net_minor: number; transaction_count: number }[]
    >(`/api/stats/cash-flow${query(params)}`),
  summary: (params: { start?: string; end?: string; top_categories?: number } = {}) =>
    request<{
      income_minor: number
      expense_minor: number
      net_minor: number
      transaction_count: number
      by_category: CategoryBreakdownItem[]
    }>(`/api/stats/summary${query(params)}`),
  integrity: () => request<IntegrityReport>('/api/stats/integrity'),

  // ---- P2：日历 / 当日详情 / 事件 ------------------------------------------
  /**
   * 日历热力数据。
   *
   * ``level`` 由服务端按分位数算好 —— 前端**不要**自己定阈值，
   * 否则图例、格子和导出 PNG 会各有一套深浅标准。
   */
  calendar: (start: string, end: string, metric: CalendarMetric = 'entry') =>
    request<CalendarResponse>(`/api/calendar${query({ start, end, metric })}`),
  dayDetail: (day: string) => request<DayDetail>(`/api/calendar/day/${day}`),
  confirmDay: (day: string, confirmed = true) =>
    request<{ date: string; confirmed: boolean }>(
      `/api/calendar/day/${day}/confirm${query({ confirmed })}`,
      { method: 'POST' },
    ),
  netWorthSeries: (start: string, end: string) =>
    request<{ date: string; net_worth_minor: number; income_minor: number; expense_minor: number; net_minor: number }[]>(
      `/api/calendar/net-worth${query({ start, end })}`,
    ),
  events: (start: string, end: string) => request<DayEvent[]>(`/api/calendar/events${query({ start, end })}`),
  createEvent: (payload: { date: string; kind?: string; title: string; body?: string }) =>
    request<DayEvent>('/api/calendar/events', { method: 'POST', body: JSON.stringify(payload) }),
  deleteEvent: (id: number) => request<void>(`/api/calendar/events/${id}`, { method: 'DELETE' }),

  // ---- P2：卡片墙 ----------------------------------------------------------
  assetWall: (params: { include_archived?: boolean } = {}) =>
    request<AssetWall>(`/api/assets/wall${query(params)}`),
  reorderAccounts: (order: number[]) =>
    request<{ reordered: number }>('/api/assets/reorder', {
      method: 'POST',
      body: JSON.stringify({ order }),
    }),
  updateAccountCard: (
    accountId: number,
    changes: {
      brand_key?: string
      card_style?: string
      card_network?: string
      theme_tint?: string
      card_no_tail?: string
      sort_order?: number
    },
  ) =>
    request<{ id: number; card_style: string; brand_key: string }>(
      `/api/assets/accounts/${accountId}/card`,
      { method: 'PATCH', body: JSON.stringify(changes) },
    ),
  institutions: () => request<Institution[]>('/api/institutions'),
  createInstitution: (payload: { key: string; name: string; kind?: string; brand_color?: string }) =>
    request<Institution>('/api/institutions', { method: 'POST', body: JSON.stringify(payload) }),
  cardArtworks: () => request<CardArtwork[]>('/api/card-artworks'),
  createCardArtwork: (payload: {
    key: string
    name: string
    spec: Record<string, unknown>
    kind?: string
  }) => request<CardArtwork>('/api/card-artworks', { method: 'POST', body: JSON.stringify(payload) }),

  // ---- P1 收尾：周期记账 ---------------------------------------------------
  recurringRules: (params: { include_disabled?: boolean } = {}) =>
    request<RecurringRule[]>(`/api/recurring${query(params)}`),
  recurringUpcoming: (withinDays = 7) =>
    request<{ items: UpcomingRule[]; count: number }>(
      `/api/recurring/upcoming${query({ within_days: withinDays })}`,
    ),
  createRecurringRule: (payload: Record<string, unknown>) =>
    request<RecurringRule>('/api/recurring', { method: 'POST', body: JSON.stringify(payload) }),
  updateRecurringRule: (id: number, payload: Record<string, unknown>) =>
    request<RecurringRule>(`/api/recurring/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteRecurringRule: (id: number) => request<void>(`/api/recurring/${id}`, { method: 'DELETE' }),
  /**
   * 生成到期流水。
   *
   * ``dryRun`` 只报告将要生成什么 —— 界面必须先让用户看到后果，
   * 尤其是补记多期的时候。
   */
  postRecurringDue: (params: { on?: string; ruleId?: number; dryRun?: boolean } = {}) =>
    request<PostDueReport>(
      `/api/recurring/post${query({ on: params.on, rule_id: params.ruleId, dry_run: params.dryRun })}`,
      { method: 'POST' },
    ),

  // ---- P1 收尾：预算 -------------------------------------------------------
  budgets: (on?: string) => request<BudgetOverview>(`/api/budgets${query({ on })}`),
  createBudget: (payload: Record<string, unknown>) =>
    request<Budget>('/api/budgets', { method: 'POST', body: JSON.stringify(payload) }),
  updateBudget: (id: number, payload: Record<string, unknown>) =>
    request<Budget>(`/api/budgets/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteBudget: (id: number) => request<void>(`/api/budgets/${id}`, { method: 'DELETE' }),

  // ---- P1 收尾：债务与应收应付 ---------------------------------------------
  debts: (on?: string) => request<DebtOverview>(`/api/debts${query({ on })}`),
  createDebt: (payload: Record<string, unknown>) =>
    request<Debt>('/api/debts', { method: 'POST', body: JSON.stringify(payload) }),
  updateDebt: (id: number, payload: Record<string, unknown>) =>
    request<Debt>(`/api/debts/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteDebt: (id: number) => request<void>(`/api/debts/${id}`, { method: 'DELETE' }),
  debtPayments: (id: number) => request<DebtPayment[]>(`/api/debts/${id}/payments`),
  addDebtPayment: (id: number, payload: Record<string, unknown>) =>
    request<DebtPayment>(`/api/debts/${id}/payments`, { method: 'POST', body: JSON.stringify(payload) }),
  deleteDebtPayment: (paymentId: number) =>
    request<void>(`/api/debts/payments/${paymentId}`, { method: 'DELETE' }),
  /**
   * 还款计划（分摊表）。
   *
   * `is_estimate` 恒为真：真实计息方式（提前还款、罚息、浮动利率）
   * 不在本应用范围内，界面必须写明"估算"。
   */
  debtPlan: (id: number) => request<RepaymentPlan>(`/api/debts/${id}/plan`),
  settleDebt: (id: number, status: string) =>
    request<Debt>(`/api/debts/${id}/settle`, { method: 'POST', body: JSON.stringify({ status }) }),

  // ---- P3：净值 K 线 -------------------------------------------------------
  /**
   * K 线数据。**指标由服务端算好**（并且带预热区间）——
   * 前端自己算 MA/MACD 的话，这一页与任何其它地方的 MACD 迟早会不一样。
   */
  kline: (params: { period?: KlinePeriod; start?: string; end?: string; indicators?: boolean } = {}) =>
    request<KlineResponse>(`/api/kline${query(params)}`),
  /**
   * 按月的分类构成（堆叠面积图数据源）。
   *
   * `months` 是**连续完整的月份轴**，`rows` 只含有支出的组合 ——
   * 坐标轴必须用 `months`，从 `rows` 反推会在只有一个月有数据时缺列。
   */
  categoryTrend: (months = 12) =>
    request<{ months: string[]; rows: CategoryTrendRow[] }>(
      `/api/stats/category-trend${query({ months })}`,
    ),
  // ---- P1 尾巴：回收站 ------------------------------------------------------
  /** 回收站概览：每个实体各有多少条（一次返回，界面要显示汇总） */
  trashSummary: () => request<TrashSummary>('/api/trash'),
  trashList: (entity: string, params: { limit?: number; offset?: number } = {}) =>
    request<TrashList>(`/api/trash/${entity}${query(params)}`),
  /**
   * 恢复一条。走各领域服务的 restore（恢复流水要重算日结），
   * 因此恢复后日历与 K 线会立刻跟上。
   */
  trashRestore: (entity: string, id: number) =>
    request<TrashItem>(`/api/trash/${entity}/${id}/restore`, { method: 'POST' }),
  /**
   * 彻底删除。有**外部**引用时后端返回 409 并写明被谁引用；
   * 分账、流水标签这类"组成部分"会自动一起删除。
   */
  trashPurge: (entity: string, id: number) =>
    request<void>(`/api/trash/${entity}/${id}`, { method: 'DELETE' }),

  /** 批量修改流水（只应用显式给出的字段，因此 null 与省略是两件事） */
  batchUpdateTransactions: (payload: {
    ids: number[]
    category_id?: number | null
    project_id?: number | null
    member_id?: number | null
    status?: string | null
    tag_ids?: number[]
    add_tag_ids?: number[]
  }) =>
    request<BatchReport>('/api/transactions/batch', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  /** 批量删除流水（软删除，可从回收站恢复） */
  batchDeleteTransactions: (ids: number[]) =>
    request<BatchDeleteReport>('/api/transactions/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    }),

  // ---- P1 尾巴：附件 --------------------------------------------------------
  /**
   * 上传附件。
   *
   * 用**原始字节体**而不是 multipart：少一个依赖风险，
   * 前端只要把 `File` 直接当 body 传即可。
   */
  uploadTransactionAttachment: (transactionId: number, file: File) =>
    upload<AttachmentItem>(
      `/api/attachments/transactions/${transactionId}${query({ filename: file.name })}`,
      file,
    ),
  uploadCardImage: (artworkId: number, file: File) =>
    upload<AttachmentItem>(
      `/api/attachments/card-artworks/${artworkId}${query({ filename: file.name })}`,
      file,
    ),
  transactionAttachments: (transactionId: number) =>
    request<AttachmentItem[]>(`/api/attachments/transactions/${transactionId}`),
  deleteAttachment: (id: number) =>
    request<void>(`/api/attachments/${id}`, { method: 'DELETE' }),

// ---- P4：存钱罐 ------------------------------------------------------------
  piggyBanks: (params: { status?: string; includeDeleted?: boolean } = {}) =>
    request<PiggyBankList>(`/api/piggy/banks${query(params)}`),
  piggyBank: (id: number) => request<PiggyBankDetail>(`/api/piggy/banks/${id}`),
  createPiggyBank: (payload: {
    name: string
    target_amount_minor: number
    target_name?: string
    deadline?: string | null
    kind?: BankKind
    priority?: number
    skin?: string
    hide_amount?: boolean
    goal_id?: number | null
    note?: string
    initial_minor?: number
  }) =>
    request<PiggyBank>('/api/piggy/banks', { method: 'POST', body: JSON.stringify(payload) }),
  updatePiggyBank: (id: number, payload: Record<string, unknown>) =>
    request<PiggyBank>(`/api/piggy/banks/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deletePiggyBank: (id: number) =>
    request<void>(`/api/piggy/banks/${id}`, { method: 'DELETE' }),
  restorePiggyBank: (id: number) =>
    request<PiggyBank>(`/api/piggy/banks/${id}/restore`, { method: 'POST' }),
  piggyDeposits: (id: number) =>
    request<{ items: PiggyDeposit[]; count: number; balance_minor: number }>(
      `/api/piggy/banks/${id}/deposits`,
    ),
  addPiggyDeposit: (
    id: number,
    payload: { amount_minor: number; kind?: string; occurred_at?: string; note?: string },
  ) =>
    request<PiggyDeposit>(`/api/piggy/banks/${id}/deposits`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  deletePiggyDeposit: (id: number) =>
    request<void>(`/api/piggy/deposits/${id}`, { method: 'DELETE' }),
  setPiggyRule: (id: number, payload: Record<string, unknown>) =>
    request<PiggyBank>(`/api/piggy/banks/${id}/rule`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deletePiggyRule: (id: number) =>
    request<void>(`/api/piggy/banks/${id}/rule`, { method: 'DELETE' }),
  /** 到期规则预览（只看不写） */
  piggyDueRules: (today?: string) =>
    request<{ items: { bank_id: number; bank_name: string; strategy: string }[] }>(
      `/api/piggy/rules/due${query({ today })}`,
    ),
  /** 执行到期规则。`dryRun` 默认 true —— 对自动扣钱的功能，"先给我看"才是默认 */
  runPiggyRules: (payload: { today?: string; dry_run?: boolean } = {}) =>
    request<PiggyAction>('/api/piggy/rules/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  /** 对刚记的流水应用事件驱动规则（四舍五入 / 收入百分比 / 分类触发） */
  applyPiggyRules: (transactionIds: number[], dryRun = false) =>
    request<PiggyAction>(`/api/piggy/rules/apply${query({ dry_run: dryRun })}`, {
      method: 'POST',
      body: JSON.stringify(transactionIds),
    }),
  achievePiggyBank: (
    id: number,
    payload: { account_id?: number | null; settle?: boolean; create_transaction?: boolean; occurred_at?: string },
  ) =>
    request<PiggyBank>(`/api/piggy/banks/${id}/achieve`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  injectPiggyBank: (id: number, payload: { goal_id?: number | null; settle_bank?: boolean } = {}) =>
    request<PiggyBank>(`/api/piggy/banks/${id}/inject`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // ---- P4：储蓄目标 ----------------------------------------------------------
  goals: (params: { status?: string; includeDeleted?: boolean } = {}) =>
    request<GoalList>(`/api/goals${query(params)}`),
  goal: (id: number) => request<GoalDetail>(`/api/goals/${id}`),
  createGoal: (payload: {
    name: string
    target_amount_minor: number
    deadline?: string | null
    kind?: GoalKind
    account_id?: number | null
    priority?: number
    hide_amount?: boolean
    note?: string
  }) => request<Goal>('/api/goals', { method: 'POST', body: JSON.stringify(payload) }),
  updateGoal: (id: number, payload: Record<string, unknown>) =>
    request<Goal>(`/api/goals/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteGoal: (id: number) => request<void>(`/api/goals/${id}`, { method: 'DELETE' }),
  restoreGoal: (id: number) =>
    request<Goal>(`/api/goals/${id}/restore`, { method: 'POST' }),
  contributeGoal: (id: number, payload: { amount_minor: number; occurred_at?: string; note?: string }) =>
    request<{ items: GoalContribution[]; count: number; saved_minor: number }>(
      `/api/goals/${id}/contributions`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  deleteGoalContribution: (id: number) =>
    request<void>(`/api/goals/contributions/${id}`, { method: 'DELETE' }),

// ---- P5：台账 --------------------------------------------------------------
  ledger: (accountId: number, params: { start: string; end: string }) =>
    request<LedgerDocument>(`/api/ledger/${accountId}${query(params)}`),
  trialBalance: () => request<TrialBalance>('/api/ledger/trial-balance'),
  reconcile: (
    accountId: number,
    payload: { actual_balance_minor: number; as_of?: string; note?: string; create_adjustment?: boolean },
  ) =>
    request<ReconcileResult>(`/api/ledger/${accountId}/reconcile`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // ---- P5：报表 --------------------------------------------------------------
  buildReport: (params: { kind: ReportKind; start?: string; end?: string; include?: string }) =>
    request<ReportDocument>(`/api/reports${query(params)}`),
  reportSectionKeys: () => request<{ items: string[] }>('/api/reports/section-keys'),
  /**
   * 导出地址。
   *
   * 返回**字符串**而不是去 fetch：导出交给浏览器的下载机制，
   * 这样几 MB 的 PDF 不会先在 JS 里被完整读进内存一次。
   * 用 `<a download>` 触发，会话 Cookie 会随同源请求带上。
   */
  reportExportUrl: (params: {
    kind: ReportKind
    start?: string
    end?: string
    include?: string
    format: 'markdown' | 'html' | 'pdf'
    inline?: boolean
  }) => `/api/reports/export${query(params)}`,

// ---- P5：AI 分析 -----------------------------------------------------------
  aiConfig: () => request<AiConfig>('/api/reports/ai-config'),
  updateAiConfig: (payload: {
    enabled?: boolean
    base_url?: string
    model?: string
    timeout_seconds?: number
    redact?: boolean
    /** 三态：不传=保持，空串=清除，有值=替换 */
    api_key?: string
  }) =>
    request<AiConfig>('/api/reports/ai-config', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  aiAnalyses: (limit = 20) =>
    request<{ items: AiAnalysisRecord[]; count: number }>(
      `/api/reports/ai-analyses${query({ limit })}`,
    ),
  /** 分析的 SSE 地址。交给通用 SSE helper 消费，不是普通 JSON 请求。 */
  aiAnalyzeUrl: (params: { kind: ReportKind; start?: string; end?: string }) =>
    `/api/reports/ai-analyze${query(params)}`,

  /** 指标参数。口径说明页直接渲染它，参数一改说明页自动跟着变 */
  klineParams: () => request<KlineParams>('/api/kline/params'),

  // ---- P1 收尾：记账模板与文本解析 ------------------------------------------
  /** 模板列表。服务端已按"最常用"排好序，前端不要再排 */
  templates: () => request<TemplateItem[]>('/api/templates'),
  createTemplate: (payload: Record<string, unknown>) =>
    request<TemplateItem>('/api/templates', { method: 'POST', body: JSON.stringify(payload) }),
  updateTemplate: (id: number, payload: Record<string, unknown>) =>
    request<TemplateItem>(`/api/templates/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteTemplate: (id: number) => request<void>(`/api/templates/${id}`, { method: 'DELETE' }),
  /** 套用模板：返回草稿并累加使用次数，**不直接创建流水** */
  applyTemplate: (id: number) =>
    request<TemplateItem>(`/api/templates/${id}/apply`, { method: 'POST' }),
  /**
   * 解析一段文本成记账草稿。
   *
   * `unmatched` 是契约的一部分：界面**必须**显示它，
   * 静默丢弃会让人以为所有内容都被识别到了。
   */
  parseText: (text: string) =>
    request<ParsedDraft>('/api/templates/parse', {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
}

export type RepaymentMethod =
  | 'equal_installment'
  | 'equal_principal'
  | 'interest_first'
  | 'lump_sum'

export interface RepaymentPlanRow {
  period: number
  date: string
  principal_minor: number
  interest_minor: number
  payment_minor: number
  balance_minor: number
  due: boolean
  settled: boolean
  overdue: boolean
}

export interface RepaymentPlan {
  debt_id: number
  method: RepaymentMethod
  installments: number
  principal_minor: number
  total_interest_minor: number
  total_payable_minor: number
  rows: RepaymentPlanRow[]
  paid_principal_minor: number
  remaining_minor: number
  settled_periods: number
  overdue_rows: number
  has_plan: boolean
  /** 恒为真：分摊表是估算 */
  is_estimate: boolean
}

export interface AttachmentItem {
  id: number
  kind: 'transaction' | 'card'
  transaction_id: number | null
  card_artwork_id: number | null
  original_name: string
  mime: string
  size_bytes: number
  sha256: string
  created_at: string | null
  /** 可直接用于 `<img src>` 的同源地址（会话 Cookie 会带上） */
  url: string
}

export interface TrashItem {
  entity: string
  id: number
  title: string
  subtitle: string
  deleted_at: string | null
}

export interface TrashList {
  entity: string
  items: TrashItem[]
  total: number
}

export interface TrashSummary {
  entities: { entity: string; count: number }[]
  total: number
}

export interface BatchReport {
  updated: number[]
  /** 被跳过的 id 与原因 —— 用户选中 10 笔只改了 8 笔时必须知道为什么 */
  skipped: { id: number; reason: string }[]
  count: number
}

export interface BatchDeleteReport {
  deleted: number[]
  skipped: { id: number; reason: string }[]
  count: number
}

export interface CategoryTrendRow {
  month: string
  category_id: number | null
  category_name: string
  amount_minor: number
}

export interface TemplateItem {
  id: number
  name: string
  type: 'expense' | 'income' | 'transfer'
  account_id: number | null
  to_account_id: number | null
  category_id: number | null
  /** null 表示"只填结构，金额每次手输" */
  amount_minor: number | null
  currency: string
  payee: string
  note: string
  tag_ids: number[]
  project_id: number | null
  member_id: number | null
  sort_order: number
  usage_count: number
  last_used_at: string | null
}

export interface ParsedDraft {
  /** null 表示无法判断方向 —— 此时不猜，由用户选择 */
  type: 'expense' | 'income' | 'transfer' | null
  amount_minor: number | null
  occurred_at: string | null
  account_id: number | null
  category_id: number | null
  payee: string
  note: string
  currency: string
  matched: Record<string, string>
  unmatched: string[]
  raw: string
}

// ---- P5：AI 分析 -------------------------------------------------------------
export interface AiConfig {
  enabled: boolean
  base_url: string
  model: string
  timeout_seconds: number
  /** 默认开启脱敏 */
  redact: boolean
  /** **只有"填过没有"，没有密钥本身** —— 能被读回来的密钥会出现在每一张截图里 */
  has_key: boolean
}

export interface AiAnalysisRecord {
  id: number
  report_kind: string
  period_start: string
  period_end: string
  source: 'online' | 'offline'
  model: string
  redacted: boolean
  content: string
  /** 离线回落的原因：no_key / disabled / network / timeout / server */
  fallback_reason: string
  error: string
  created_at: string | null
}

/** SSE 的 `meta` 帧 */
export interface AiStreamMeta {
  source: 'online' | 'offline'
  model: string
  fallback_reason: string
  error: string
  analysis_id: number | null
}

// ---- P5：台账与报表 ----------------------------------------------------------
export interface LedgerEntry {
  transaction_id: number
  occurred_at: string
  tz_offset_minutes: number
  type: string
  direction: 'in' | 'out'
  amount_minor: number
  /** 带符号金额：正=流入，负=流出 */
  signed_minor: number
  running_balance_minor: number
  payee: string
  note: string
  status: string
  source: string
  category_id: number | null
  category_name: string
  category_kind: string
  to_account_id: number | null
  /** 转账时是对方账户名 */
  counterparty: string
  /** 'self' = 本账户是转出方；'incoming' = 本账户收到了这笔转账 */
  leg: 'self' | 'incoming'
  project_id: number | null
  member_id: number | null
  has_splits: boolean
}

export interface LedgerCheck {
  internal_ok: boolean
  aggregate_ok: boolean
  covers_today: boolean
  opening_balance_minor: number
  closing_balance_minor: number
  recomputed_closing_minor: number
  /** 截至区间末的余额（台账期末应当等于它） */
  aggregate_balance_minor: number
  /** 全时段余额。与上面不同说明存在未来日期的流水 */
  all_time_balance_minor: number
  difference_minor: number
  excluded_void_count: number
  future_dated_count: number
  future_dated_net_minor: number
  balanced: boolean
}

export interface LedgerDocument {
  account_id: number
  account_name: string
  currency: string
  start: string
  end: string
  opening_balance_minor: number
  closing_balance_minor: number
  entries: LedgerEntry[]
  count: number
  inflow_minor: number
  outflow_minor: number
  check: LedgerCheck
}

export interface TrialBalance {
  account_count: number
  balance_change_minor: number
  income_minor: number
  expense_minor: number
  adjust_net_minor: number
  expected_change_minor: number
  transfer_neutral_ok: boolean
  broken_transfer_ids: number[]
  balanced: boolean
}

export interface ReconcileResult {
  account_id: number
  account_name: string
  as_of: string
  computed_balance_minor: number
  actual_balance_minor: number
  difference_minor: number
  created_transaction_id: number | null
  new_balance_minor: number
}

export type ReportKind = 'daily' | 'weekly' | 'monthly' | 'yearly' | 'custom'

/** 报告里的一个块。**这是唯一的扩展点** —— 新增展示只需加一个块类型。 */
export interface ReportBlock {
  type: 'text' | 'metrics' | 'table' | 'chart' | 'note' | 'unavailable'
  text?: string
  title?: string
  items?: ReportMetric[]
  columns?: { key: string; label: string; align?: string; format?: string }[]
  rows?: Record<string, unknown>[]
  footer?: Record<string, unknown> | null
  empty?: string
  chart?: 'bar' | 'line' | 'pie'
  /** 中性数据集 —— **不是**任何图表库的配置，由渲染器决定怎么画 */
  dataset?: { points?: { date?: string; name?: string; value_minor?: number; value?: number }[] }
  unit?: 'money' | 'percent' | 'count'
}

export interface ReportMetric {
  key: string
  label: string
  kind: 'money' | 'percent' | 'count'
  value_minor?: number
  value?: number
  delta_ratio: number | null
  delta_label: string
  tone: string
  hint?: string
}

export interface ReportSection {
  key: string
  title: string
  blocks: ReportBlock[]
  insights?: ReportInsight[]
}

export interface ReportInsight {
  key: string
  level: 'good' | 'info' | 'warn' | 'critical'
  title: string
  detail: string
  evidence: string[]
  suggestion: string
  confidence: number
}

export interface ReportDocument {
  schema: string
  kind: ReportKind
  title: string
  subtitle: string
  period: { start: string; end: string; label: string; days: number }
  generated_at: string
  currency: string
  cover: { headline: string; highlights: string[] }
  kpis: ReportMetric[]
  sections: ReportSection[]
  insights: ReportInsight[]
  notes: string[]
}

// ---- P4：存钱罐与储蓄目标 ----------------------------------------------------
export type BankKind = 'one_time' | 'long_term' | 'shared'
export type BankStatus = 'active' | 'achieved' | 'paused' | 'abandoned'
export type RuleStrategy =
  | 'roundup'
  | 'daily_fixed'
  | 'weekly_fixed'
  | 'income_percent'
  | 'monthly_surplus'
  | 'category_trigger'
export type GoalKind = 'purchase' | 'emergency' | 'travel' | 'education' | 'other'

/** 一个口径的预计达成日 */
export interface EtaEstimate {
  rate_per_day_minor: number
  days: number
  eta: string
}

/**
 * 预计达成日。**两个口径并存**（见后端 docstring）：
 * 线性对"最近停止存钱了"无感，加权对节奏敏感但会被一次性大额拉飞。
 * `divergent` 为真时两个口径差距超过一倍 —— 那个分歧本身是有用的信息。
 */
export interface BankEta {
  balance_minor: number
  remaining_minor: number
  ratio: number
  deadline: string | null
  days_to_deadline: number | null
  elapsed_days: number
  /** 已达成时为真，此时下面两个口径都是 null */
  achieved: boolean
  linear: EtaEstimate | null
  weighted: EtaEstimate | null
  divergent: boolean
  on_track: boolean | null
  required_per_day_minor: number | null
}

export interface PiggyRule {
  id: number
  piggy_bank_id: number
  strategy: RuleStrategy
  enabled: boolean
  roundup_unit_minor: number
  fixed_amount_minor: number
  percent_bps: number
  category_ids: number[]
  account_id: number | null
  deduct_from_account: boolean
  last_run_date: string | null
}

export interface PiggyBank {
  id: number
  name: string
  target_name: string
  target_amount_minor: number
  balance_minor: number
  currency: string
  deadline: string | null
  kind: BankKind
  status: BankStatus
  member_id: number | null
  priority: number
  skin: string
  hide_amount: boolean
  goal_id: number | null
  achieved_at: string | null
  celebrated: boolean
  sort_order: number
  note: string
  ratio: number
  milestones: number[]
  deleted_at: string | null
  rule: PiggyRule | null
  eta?: BankEta | null
  settled_minor?: number | null
  injected_minor?: number | null
}

export interface PiggyDeposit {
  id: number
  piggy_bank_id: number
  amount_minor: number
  occurred_at: string
  tz_offset_minutes: number
  source_account_id: number | null
  transaction_id: number | null
  kind: string
  note: string
}

export interface PiggyBankDetail extends PiggyBank {
  deposits: PiggyDeposit[]
}

export interface PiggyBankList {
  items: PiggyBank[]
  count: number
  active_target_minor: number
  saved_minor: number
}

export interface Goal {
  id: number
  name: string
  target_amount_minor: number
  saved_minor: number
  currency: string
  deadline: string | null
  kind: GoalKind
  status: BankStatus
  account_id: number | null
  member_id: number | null
  priority: number
  hide_amount: boolean
  achieved_at: string | null
  celebrated: boolean
  sort_order: number
  note: string
  ratio: number
  milestones: number[]
  deleted_at: string | null
}

export interface GoalContribution {
  id: number
  goal_id: number
  amount_minor: number
  occurred_at: string
  tz_offset_minutes: number
  note: string
}

export interface GoalDetail extends Goal {
  contributions: GoalContribution[]
}

export interface GoalList {
  items: Goal[]
  count: number
  active_target_minor: number
  saved_minor: number
}

/** 归集规则执行结果（dry_run 时只是预览） */
export interface PiggyAction {
  items: {
    bank_id: number
    bank_name: string
    strategy: string
    amount_minor: number
    transaction_id?: number
  }[]
  count: number
  total_minor: number
  dry_run: boolean
}
