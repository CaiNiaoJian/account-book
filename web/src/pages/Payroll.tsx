/**
 * 薪酬与五险一金（P6 / 需求 18、19）。
 *
 * 四张图与它们各自要说清的事：
 *
 * 1. **应发实发瀑布** —— 钱从应发到实发经过了哪些扣减。
 *    用 ECharts 的"透明底座 + 可见增量"两层堆叠实现：
 *    单层的带符号柱画不出"悬空"的中间段，看起来会像加减法算错了。
 * 2. **五险一金构成环形** —— 各险种占比。
 * 3. **单位 vs 个人对比** —— 单位缴纳是"没进工资卡但确实属于你"的隐形收入，
 *    单独对比才看得见。
 * 4. **逐年累积堆叠柱** —— 缴纳是长期积累，单月数字没什么意义。
 *
 * 三个界面上的诚实约定（都在后端有对应字段）：
 * * 比例未填齐时合计为 0，**要显示成"待配置"而不是"缴得少"**（`incomplete`）；
 * * 基数被封顶/保底收敛时**要标出来**（`clamped`），否则用户以为软件算错了；
 * * 发薪日要标出置信度（`inferred` 是默认值、`assumed` 是节假日未录入）。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { Chart } from '@/components/Chart'
import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { usePreferences } from '@/app/preferences'
import { resolveToken } from '@/design/tokens'
import { useI18n } from '@/i18n'
import {
  api,
  ApiError,
  type InsuranceCompute,
  type InsuranceItem,
  type InsuranceOverview,
  type InsuranceProfile,
  type InsuranceStatement,
  type PaySource,
  type PaydayResolution,
  type PayrollCompute,
  type PayrollOverview,
  type PayrollRecord,
} from '@/lib/api'
import { displayMinor, formatDayLabel } from '@/lib/format'

import { Modal } from '@/features/ledger/parts'
import { useLedger } from '@/features/ledger/store'

/** 当前期间（YYYY-MM） */
function currentPeriod(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

/**
 * 应发实发瀑布。
 *
 * 单层的带符号柱画不出"悬空"的中间段（减项应当从累计值往下画，
 * 而不是从 0 往上画），看起来会像加减法算错了。
 * 因此用"透明底座 + 可见增量"两层堆叠。
 */
function waterfallOption(
  rows: { name: string; value_minor: number }[],
  formatValue: (value: unknown) => string,
) {
  const down = resolveToken('negative')
  const base = resolveToken('accent')
  const axis = resolveToken('text-3')
  const split = resolveToken('separator')

  let running = 0
  const bases: number[] = []
  const deltas: number[] = []
  const colors: string[] = []
  for (const row of rows) {
    if (row.value_minor >= 0 && (row.name === '应发' || row.name === '实发')) {
      // 首尾两根是"总量柱"，从 0 画起
      bases.push(0)
      deltas.push(row.value_minor)
      colors.push(base)
      running = row.value_minor
    } else {
      const magnitude = Math.abs(row.value_minor)
      bases.push(Math.max(0, running - magnitude))
      deltas.push(magnitude)
      colors.push(down)
      running -= magnitude
    }
  }

  return {
    grid: { left: 8, right: 16, top: 20, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', valueFormatter: (value: number) => formatValue(value) },
    xAxis: {
      type: 'category',
      data: rows.map((row) => row.name),
      axisLabel: { color: axis, fontSize: 11 },
      axisLine: { lineStyle: { color: split } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: axis, fontSize: 10, formatter: (value: number) => formatValue(value) },
      splitLine: { lineStyle: { color: split, type: 'dashed' } },
    },
    series: [
      {
        type: 'bar',
        stack: 'waterfall',
        silent: true,
        itemStyle: { color: 'transparent' },
        data: bases,
      },
      {
        type: 'bar',
        stack: 'waterfall',
        barMaxWidth: 38,
        data: deltas.map((value, index) => ({ value, itemStyle: { color: colors[index] } })),
        label: {
          show: true,
          position: 'top',
          fontSize: 10,
          color: axis,
          formatter: (params: { value: number }) => formatValue(params.value),
        },
      },
    ],
  }
}

/** 单位 vs 个人分组柱 */
function comparisonOption(
  rows: { name: string; personal_minor: number; employer_minor: number }[],
  formatValue: (value: unknown) => string,
  labels: { personal: string; employer: string },
) {
  const axis = resolveToken('text-3')
  const split = resolveToken('separator')
  return {
    grid: { left: 8, right: 16, top: 28, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', valueFormatter: (value: number) => formatValue(value) },
    legend: { top: 0, textStyle: { color: axis, fontSize: 11 } },
    xAxis: {
      type: 'category',
      data: rows.map((row) => row.name),
      axisLabel: { color: axis, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: split } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: axis, fontSize: 10, formatter: (value: number) => formatValue(value) },
      splitLine: { lineStyle: { color: split, type: 'dashed' } },
    },
    series: [
      {
        name: labels.personal,
        type: 'bar',
        barMaxWidth: 20,
        itemStyle: { color: resolveToken('negative'), borderRadius: [3, 3, 0, 0] },
        data: rows.map((row) => row.personal_minor),
      },
      {
        name: labels.employer,
        type: 'bar',
        barMaxWidth: 20,
        itemStyle: { color: resolveToken('positive'), borderRadius: [3, 3, 0, 0] },
        data: rows.map((row) => row.employer_minor),
      },
    ],
  }
}

/**
 * 逐月应发 / 实发堆叠柱。
 *
 * 刻意**不复用** `cumulativeOption`：那个的图例是"个人 / 单位"（五险一金的口径），
 * 套到工资上会显示成"个人 = 实发、单位 = 扣减" —— 名不副实。
 * 一张图例说错的图比没有图更糟。
 */
function monthlyPayrollOption(
  rows: { period: string; gross_minor: number; net_minor: number }[],
  formatValue: (value: unknown) => string,
  labels: { net: string; deduction: string },
) {
  const axis = resolveToken('text-3')
  const split = resolveToken('separator')
  return {
    grid: { left: 8, right: 16, top: 28, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', valueFormatter: (value: number) => formatValue(value) },
    legend: { top: 0, textStyle: { color: axis, fontSize: 11 } },
    xAxis: {
      type: 'category',
      data: rows.map((row) => row.period),
      axisLabel: { color: axis, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: split } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: axis, fontSize: 10, formatter: (value: number) => formatValue(value) },
      splitLine: { lineStyle: { color: split, type: 'dashed' } },
    },
    series: [
      {
        name: labels.net,
        type: 'bar',
        stack: 'pay',
        barMaxWidth: 26,
        itemStyle: { color: resolveToken('accent') },
        data: rows.map((row) => row.net_minor),
      },
      {
        name: labels.deduction,
        type: 'bar',
        stack: 'pay',
        barMaxWidth: 26,
        itemStyle: { color: resolveToken('warning') },
        data: rows.map((row) => row.gross_minor - row.net_minor),
      },
    ],
  }
}

/** 逐年累积堆叠柱：缴纳是长期积累，单月数字没什么意义 */
function cumulativeOption(
  rows: { period: string; personal_minor: number; employer_minor: number }[],
  formatValue: (value: unknown) => string,
) {
  const axis = resolveToken('text-3')
  const split = resolveToken('separator')
  return {
    grid: { left: 8, right: 16, top: 28, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', valueFormatter: (value: number) => formatValue(value) },
    legend: { top: 0, textStyle: { color: axis, fontSize: 11 } },
    xAxis: {
      type: 'category',
      data: rows.map((row) => row.period),
      axisLabel: { color: axis, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: split } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: axis, fontSize: 10, formatter: (value: number) => formatValue(value) },
      splitLine: { lineStyle: { color: split, type: 'dashed' } },
    },
    series: [
      {
        name: '个人',
        type: 'bar',
        stack: 'total',
        barMaxWidth: 26,
        itemStyle: { color: resolveToken('accent') },
        data: rows.map((row) => row.personal_minor),
      },
      {
        name: '单位',
        type: 'bar',
        stack: 'total',
        barMaxWidth: 26,
        itemStyle: { color: resolveToken('positive') },
        data: rows.map((row) => row.employer_minor),
      },
    ],
  }
}

function donutOption(
  rows: { name: string; value: number }[],
  formatValue: (value: unknown) => string,
) {
  const axis = resolveToken('text-3')
  return {
    color: [
      resolveToken('accent'),
      resolveToken('positive'),
      resolveToken('warning'),
      resolveToken('negative'),
      resolveToken('purple'),
      resolveToken('teal'),
      resolveToken('orange'),
      resolveToken('indigo'),
    ],
    tooltip: { trigger: 'item', valueFormatter: (value: number) => formatValue(value) },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: axis, fontSize: 10 } },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '44%'],
        itemStyle: { borderWidth: 0 },
        label: { show: false },
        data: rows,
      },
    ],
  }
}

export function PayrollPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { accounts } = useLedger()
  const privacy = preferences.privacy_mode

  const [period, setPeriod] = useState(currentPeriod)
  const [sources, setSources] = useState<PaySource[] | null>(null)
  const [sourceId, setSourceId] = useState<number | null>(null)
  const [compute, setCompute] = useState<PayrollCompute | null>(null)
  const [records, setRecords] = useState<PayrollRecord[]>([])
  const [overview, setOverview] = useState<PayrollOverview | null>(null)
  const [insurance, setInsurance] = useState<InsuranceOverview | null>(null)
  const [payday, setPayday] = useState<PaydayResolution | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [sourceOpen, setSourceOpen] = useState(false)
  const [componentOpen, setComponentOpen] = useState(false)
  const [fillFor, setFillFor] = useState<PayrollRecord | null>(null)
  const [insuranceOpen, setInsuranceOpen] = useState(false)

  // 深链：弹窗是 React 状态、没有自己的 URL，因此截图脚本（以及未来的
  // 分享链接）需要一个入口。P5 时台账就因为没有它而只拍到了空状态。
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (params.get('insurance') === '1') setInsuranceOpen(true)
    if (params.get('components') === '1') setComponentOpen(true)
  }, [])

  const formatValue = useCallback(
    (value: unknown) => displayMinor(Number(value ?? 0), 'CNY', privacy),
    [privacy],
  )

  // 五险一金的区间跟着所选期间走（往前 11 个月，用于"逐年累积"）
  const insuranceRange = useMemo(() => {
    const [year, month] = period.split('-').map(Number)
    const start = new Date(year!, (month ?? 1) - 12, 1)
    return {
      start: `${start.getFullYear()}-${String(start.getMonth() + 1).padStart(2, '0')}`,
      end: period,
    }
  }, [period])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [sourceList, recordList, payroll, cover] = await Promise.all([
        api.payrollSources(),
        api.payrollRecords({ limit: 200 }),
        api.payrollOverview(period, 12),
        api.insuranceOverview(insuranceRange.start, insuranceRange.end),
      ])
      setSources(sourceList.items)
      setRecords(recordList.items)
      setOverview(payroll)
      setInsurance(cover)
      setError(null)
      const chosen = sourceId ?? sourceList.items[0]?.id ?? null
      setSourceId(chosen)
      if (chosen !== null) {
        const [computed, date] = await Promise.all([
          api.computePayroll(chosen),
          api.payDate(chosen, period),
        ])
        setCompute(computed)
        setPayday(date)
      } else {
        setCompute(null)
        setPayday(null)
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setLoading(false)
    }
  }, [period, insuranceRange.start, insuranceRange.end, sourceId])

  useEffect(() => {
    void load()
    // 只在期间变化时整体重载；sourceId 的变化由下面的 effect 单独处理
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period, insuranceRange.start, insuranceRange.end])

  // 切换来源时只重算该来源的数据
  useEffect(() => {
    if (sourceId === null) return
    void (async () => {
      try {
        const [computed, date] = await Promise.all([
          api.computePayroll(sourceId),
          api.payDate(sourceId, period),
        ])
        setCompute(computed)
        setPayday(date)
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.detail : String(cause))
      }
    })()
  }, [sourceId, period])

  /** 本期的草稿：它们**不计入**上面的应发/实发（那笔钱还没到账） */
  const periodDrafts = records.filter(
    (row) => row.period === period && row.status === 'draft',
  )

  const periodRecord = records.find(
    (row) => row.source_id === sourceId && row.period === period,
  )

  const waterfall = useMemo(() => {
    if (!compute) return []
    const rows = [{ name: t('payroll.gross'), value_minor: compute.gross_minor }]
    for (const item of compute.items) {
      if (item.direction === 'deduct' && item.amount_minor > 0) {
        rows.push({ name: item.name, value_minor: -item.amount_minor })
      }
    }
    rows.push({ name: t('payroll.net'), value_minor: compute.net_minor })
    return rows
  }, [compute, t])

  const donut = useMemo(
    () =>
      (insurance?.by_kind ?? [])
        .filter((item) => item.personal_minor + item.employer_minor > 0)
        .map((item) => ({ name: item.name, value: item.personal_minor + item.employer_minor })),
    [insurance],
  )

  const createRecord = async () => {
    if (sourceId === null) return
    try {
      await api.createPayrollRecord({ source_id: sourceId, period })
      setNotice(t('payroll.recordCreated', { period }))
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    }
  }

  if (loading && sources === null) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  return (
    <div className="space-y-4" data-print-root>
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-ab-title3 font-semibold text-label">{t('payroll.title')}</h1>
        <div className="ml-auto flex flex-wrap items-center gap-1.5" data-print-hide>
          <input
            type="month"
            className="ab-input !w-auto"
            value={period}
            onChange={(event) => setPeriod(event.target.value || currentPeriod())}
          />
          <button type="button" className="ab-btn-secondary" onClick={() => setInsuranceOpen(true)}>
            <Icon name="goals" size={13} />
            {t('payroll.insuranceSettings')}
          </button>
          <button type="button" className="ab-btn-primary" onClick={() => setSourceOpen(true)}>
            <Icon name="plus" size={13} />
            {t('payroll.newSource')}
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

      {sources !== null && sources.length === 0 ? (
        <Card flush>
          <EmptyState
            icon="payroll"
            title={t('payroll.empty')}
            body={t('payroll.emptyBody')}
            action={
              <button type="button" className="ab-btn-primary" onClick={() => setSourceOpen(true)}>
                <Icon name="plus" size={14} />
                {t('payroll.newSource')}
              </button>
            }
          />
        </Card>
      ) : (
        <>
          {/* 来源切换 */}
          <div className="flex flex-wrap items-center gap-1.5" data-print-hide>
            {(sources ?? []).map((source) => (
              <button
                key={source.id}
                type="button"
                className="ab-chip"
                data-active={sourceId === source.id}
                onClick={() => setSourceId(source.id)}
              >
                {source.name}
                {source.employer ? (
                  <span className="ml-1 text-ab-caption2 text-label-3">{source.employer}</span>
                ) : null}
              </button>
            ))}
            {sourceId !== null ? (
              <button
                type="button"
                className="ab-chip"
                onClick={() => setComponentOpen(true)}
              >
                <Icon name="edit" size={11} />
                {t('payroll.editComponents')}
              </button>
            ) : null}
          </div>

          {/* 发薪日 + 本期状态 */}
          <Card>
            <div className="flex flex-wrap items-center gap-3">
              <div>
                <div className="text-ab-caption1 text-label-3">{t('payroll.payDate')}</div>
                <div className="ab-tnum text-ab-subhead text-label">
                  {payday ? formatDayLabel(payday.pay_date) : '—'}
                </div>
              </div>
              {payday ? (
                <div className="flex flex-wrap items-center gap-1.5">
                  {/* **置信度必须标出来**：一个"看起来算过"的日期会被用户当真 */}
                  {/* 这一条可能很长（"按规则算出，但该年节假日未录入"），
                      因此允许换行 —— 不换行时它会挤到右侧的状态 chip 上 */}
                  <span
                    className={`ab-chip !whitespace-normal ${payday.confidence === 'exact' ? '!text-positive' : '!text-warning'}`}
                    data-active
                  >
                    <Icon
                      name={payday.confidence === 'exact' ? 'check' : 'warning'}
                      size={11}
                    />
                    {t(`payroll.confidence.${payday.confidence}`)}
                  </span>
                  {payday.adjusted ? (
                    <span className="ab-chip">
                      {t('payroll.adjusted', {
                        reason: t(`payroll.reason.${payday.reason}`),
                        days: Math.abs(payday.shift_days),
                      })}
                      {payday.holiday_name ? `（${payday.holiday_name}）` : ''}
                    </span>
                  ) : null}
                </div>
              ) : null}

              <div className="ml-auto flex items-center gap-2">
                {periodRecord ? (
                  <>
                    <span
                      className={`ab-chip ${periodRecord.status === 'filled' ? '!text-positive' : periodRecord.status === 'skipped' ? '' : '!text-warning'}`}
                      data-active
                    >
                      {t(`payroll.status.${periodRecord.status}`)}
                    </span>
                    {periodRecord.status === 'draft' ? (
                      <button
                        type="button"
                        className="ab-btn-primary"
                        onClick={() => setFillFor(periodRecord)}
                      >
                        <Icon name="check" size={13} />
                        {t('payroll.fill')}
                      </button>
                    ) : null}
                  </>
                ) : (
                  <button type="button" className="ab-btn-primary" onClick={() => void createRecord()}>
                    <Icon name="plus" size={13} />
                    {t('payroll.createRecord')}
                  </button>
                )}
              </div>
            </div>
          </Card>

          {/* 应发实发瀑布 */}
          {compute ? (
            <Card>
              <div className="ab-section-label !px-0 !pt-0">{t('payroll.waterfall')}</div>
              {compute.gross_minor === 0 ? (
                <p className="py-4 text-ab-footnote text-label-3">{t('payroll.noComponents')}</p>
              ) : (
                <Chart
                  option={waterfallOption(waterfall, formatValue)}
                  height={220}
                  emptyLabel={t('payroll.noComponents')}
                />
              )}
            </Card>
          ) : null}

          {/* 五险一金 */}
          <Card>
            <div className="flex flex-wrap items-center gap-2">
              <div className="ab-section-label !px-0 !pt-0">{t('payroll.insurance')}</div>
              {insurance && insurance.record_count === 0 ? (
                <span className="text-ab-caption1 text-label-3">{t('payroll.noInsurance')}</span>
              ) : null}
            </div>
            {insurance && insurance.record_count > 0 ? (
              <>
                <div className="mt-2 grid gap-2 sm:grid-cols-3">
                  {[
                    { label: t('payroll.personal'), value: insurance.personal_total_minor },
                    { label: t('payroll.employerPaid'), value: insurance.employer_total_minor },
                    { label: t('payroll.accountTotal'), value: insurance.account_total_minor },
                  ].map((item) => (
                    <div key={item.label}>
                      <div className="text-ab-caption1 text-label-3">{item.label}</div>
                      <div className="ab-tnum text-ab-subhead text-label">
                        {formatValue(item.value)}
                      </div>
                    </div>
                  ))}
                </div>
                {insurance.employer_share !== null ? (
                  <p className="mt-1 text-ab-caption1 text-label-3">
                    {t('payroll.employerShare', {
                      percent: (insurance.employer_share * 100).toFixed(0),
                    })}
                  </p>
                ) : null}
                <div className="mt-3 grid gap-4 lg:grid-cols-2">
                  <div>
                    <div className="ab-section-label !px-0">{t('payroll.byKind')}</div>
                    <Chart option={donutOption(donut, formatValue)} height={220} />
                  </div>
                  <div>
                    <div className="ab-section-label !px-0">{t('payroll.compare')}</div>
                    <Chart
                      option={comparisonOption(insurance.by_kind, formatValue, {
                        personal: t('payroll.personal'),
                        employer: t('payroll.employerPaid'),
                      })}
                      height={220}
                    />
                  </div>
                </div>
                {insurance.by_period.length > 1 ? (
                  <div className="mt-3">
                    <div className="ab-section-label !px-0">{t('payroll.cumulative')}</div>
                    <Chart
                      option={cumulativeOption(insurance.by_period, formatValue)}
                      height={200}
                    />
                  </div>
                ) : null}
              </>
            ) : (
              <p className="mt-2 text-ab-footnote text-label-3">{t('payroll.insuranceHint')}</p>
            )}
          </Card>

          {/* 逐月工资与同比 */}
          {overview ? (
            <Card>
              <div className="ab-section-label !px-0 !pt-0">{t('payroll.history')}</div>
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                {(
                  [
                    ['gross', t('payroll.gross'), overview.current.gross_minor],
                    ['net', t('payroll.net'), overview.current.net_minor],
                    ['tax', t('payroll.tax'), overview.current.tax_minor],
                    ['insurance', t('payroll.insuranceDeduct'), overview.current.insurance_minor],
                  ] as const
                ).map(([key, label, value]) => (
                  <div key={key}>
                    <div className="text-ab-caption1 text-label-3">{label}</div>
                    <div className="ab-tnum text-ab-subhead text-label">{formatValue(value)}</div>
                    <div className="ab-tnum text-ab-caption2 text-label-3">
                      {/* 去年同月为 0 时没有可表达的百分比 —— 显示"—"而不是 0% */}
                      {t('payroll.yoy')}{' '}
                      {overview.delta[key] === null
                        ? '—'
                        : `${overview.delta[key]! >= 0 ? '+' : ''}${(overview.delta[key]! * 100).toFixed(1)}%`}
                    </div>
                  </div>
                ))}
              </div>

              <div className="mt-3">
                <div className="ab-section-label !px-0">{t('payroll.monthly')}</div>
                <Chart
                  option={monthlyPayrollOption(overview.months, formatValue, {
                    net: t('payroll.net'),
                    deduction: t('payroll.deductionTotal'),
                  })}
                  height={180}
                />
              </div>

              {periodDrafts.length > 0 ? (
                <p className="mt-2 rounded-ab-sm bg-warning/10 px-3 py-2 text-ab-caption1 text-label-2">
                  {t('payroll.draftsExcluded', { count: periodDrafts.length })}
                </p>
              ) : null}

              {records.length > 0 ? (
                <div className="mt-3 overflow-x-auto">
                  <table className="w-full border-collapse text-ab-footnote">
                    <thead>
                      <tr className="border-b border-separator">
                        {[
                          t('payroll.periodLabel'),
                          t('payroll.gross'),
                          t('payroll.insuranceDeduct'),
                          t('payroll.tax'),
                          t('payroll.net'),
                          '',
                        ].map((label, index) => (
                          <th
                            key={index}
                            className={`px-3 py-1.5 text-ab-caption1 font-medium text-label-3 ${index === 0 ? 'text-left' : 'text-right'}`}
                          >
                            {label}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {records.slice(0, 24).map((row) => (
                        <tr key={row.id} className="border-b border-separator/40">
                          <td className="ab-tnum px-3 py-1.5 text-label-2">
                            {row.period}
                            <span className="ml-1.5 text-ab-caption1 text-label-3">
                              {t(`payroll.status.${row.status}`)}
                            </span>
                          </td>
                          <td className="ab-tnum px-3 py-1.5 text-right text-label-2">
                            {formatValue(row.gross_minor)}
                          </td>
                          <td className="ab-tnum px-3 py-1.5 text-right text-label-2">
                            {formatValue(row.insurance_minor)}
                          </td>
                          <td className="ab-tnum px-3 py-1.5 text-right text-label-2">
                            {formatValue(row.tax_minor)}
                          </td>
                          <td className="ab-tnum px-3 py-1.5 text-right text-label">
                            {formatValue(row.net_minor)}
                          </td>
                          <td className="px-3 py-1.5 text-right" data-print-hide>
                            {row.status === 'draft' ? (
                              <button
                                type="button"
                                className="text-accent"
                                onClick={() => setFillFor(row)}
                              >
                                {t('payroll.fill')}
                              </button>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </Card>
          ) : null}
        </>
      )}

      <SourceDialog
        open={sourceOpen}
        accounts={accounts}
        onClose={() => setSourceOpen(false)}
        onDone={async () => {
          setSourceOpen(false)
          setNotice(t('payroll.sourceCreated'))
          await load()
        }}
      />

      <ComponentDialog
        open={componentOpen}
        sourceId={sourceId}
        onClose={() => setComponentOpen(false)}
        onDone={async () => {
          setComponentOpen(false)
          await load()
        }}
      />

      <FillDialog
        record={fillFor}
        onClose={() => setFillFor(null)}
        onDone={async () => {
          setFillFor(null)
          setNotice(t('payroll.filled'))
          await load()
        }}
      />

      <InsuranceDialog
        open={insuranceOpen}
        period={period}
        onClose={() => setInsuranceOpen(false)}
        onDone={async () => {
          setInsuranceOpen(false)
          await load()
        }}
      />
    </div>
  )
}

// -----------------------------------------------------------------------------
// 对话框
// -----------------------------------------------------------------------------
function SourceDialog({
  open,
  accounts,
  onClose,
  onDone,
}: {
  open: boolean
  accounts: { id: number; name: string }[]
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [employer, setEmployer] = useState('')
  const [accountId, setAccountId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setName('')
      setEmployer('')
      setAccountId(accounts[0] ? String(accounts[0].id) : '')
      setError(null)
    }
  }, [open, accounts])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.createPayrollSource({
        name,
        employer,
        account_id: accountId ? Number(accountId) : null,
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
      open={open}
      title={t('payroll.newSource')}
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
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
            {error}
          </div>
        ) : null}
        <div>
          <label className="ab-field-label" htmlFor="pay-name">
            {t('payroll.sourceName')}
          </label>
          <input
            id="pay-name"
            className="ab-input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={t('payroll.sourceNamePlaceholder')}
          />
        </div>
        <div>
          <label className="ab-field-label" htmlFor="pay-employer">
            {t('payroll.employer')}
          </label>
          <input
            id="pay-employer"
            className="ab-input"
            value={employer}
            onChange={(event) => setEmployer(event.target.value)}
          />
        </div>
        <div>
          <label className="ab-field-label" htmlFor="pay-account">
            {t('payroll.depositAccount')}
          </label>
          <select
            id="pay-account"
            className="ab-select"
            value={accountId}
            onChange={(event) => setAccountId(event.target.value)}
          >
            <option value="">{t('payroll.noAccount')}</option>
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.name}
              </option>
            ))}
          </select>
          <p className="mt-1 text-ab-caption1 text-label-3">{t('payroll.depositHint')}</p>
        </div>
      </div>
    </Modal>
  )
}

function ComponentDialog({
  open,
  sourceId,
  onClose,
  onDone,
}: {
  open: boolean
  sourceId: number | null
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const [items, setItems] = useState<PayComponentRow[]>([])
  const [name, setName] = useState('')
  const [kind, setKind] = useState('basic')
  const [sign, setSign] = useState<1 | -1>(1)
  const [calc, setCalc] = useState<'fixed' | 'ratio'>('fixed')
  const [amount, setAmount] = useState('')
  const [rate, setRate] = useState('')
  const [baseKey, setBaseKey] = useState<'basic' | 'gross'>('basic')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (sourceId === null) return
    const body = await api.payrollComponents(sourceId)
    setItems(body.items)
  }, [sourceId])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  const add = async () => {
    if (sourceId === null) return
    setBusy(true)
    setError(null)
    try {
      await api.createPayrollComponent({
        name,
        kind,
        sign,
        calc,
        source_id: sourceId,
        amount_minor: calc === 'fixed' ? Math.round(Number(amount || '0') * 100) : 0,
        rate_bps: calc === 'ratio' ? Math.round(Number(rate || '0') * 100) : 0,
        base_key: baseKey,
        sort_order: sign === 1 ? 1 : 9,
      })
      setName('')
      setAmount('')
      setRate('')
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title={t('payroll.editComponents')}
      size="lg"
      onClose={onClose}
      footer={
        <button type="button" className="ab-btn-secondary" onClick={onDone}>
          {t('common.close')}
        </button>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
            {error}
          </div>
        ) : null}

        <div className="space-y-1">
          {items.length === 0 ? (
            <p className="py-2 text-ab-footnote text-label-3">{t('payroll.noComponentsHint')}</p>
          ) : (
            items.map((item) => (
              <div key={item.id} className="ab-row">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-ab-footnote text-label-2">
                    {item.name}
                    <span className="ml-1.5 text-ab-caption1 text-label-3">
                      {t(`payroll.componentKind.${item.kind}`)}
                    </span>
                  </span>
                  <span className="ab-tnum block text-ab-caption1 text-label-3">
                    {item.sign === 1 ? '+' : '−'}
                    {item.calc === 'fixed'
                      ? displayMinor(item.amount_minor, 'CNY', preferences.privacy_mode)
                      : `${(item.rate_bps / 100).toFixed(2)}% × ${t(`payroll.baseKey.${item.base_key}`)}`}
                  </span>
                </span>
                <button
                  type="button"
                  className="ab-icon-btn shrink-0 hover:text-negative"
                  aria-label={t('common.delete')}
                  onClick={async () => {
                    await api.deletePayrollComponent(item.id)
                    await load()
                  }}
                >
                  <Icon name="trash" size={12} />
                </button>
              </div>
            ))
          )}
        </div>

        <div className="space-y-3 border-t border-separator/60 pt-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="ab-field-label" htmlFor="cmp-name">
                {t('payroll.componentName')}
              </label>
              <input
                id="cmp-name"
                className="ab-input"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder={t('payroll.componentNamePlaceholder')}
              />
            </div>
            <div>
              <label className="ab-field-label" htmlFor="cmp-kind">
                {t('payroll.componentKindLabel')}
              </label>
              <select
                id="cmp-kind"
                className="ab-select"
                value={kind}
                onChange={(event) => setKind(event.target.value)}
              >
                {COMPONENT_KINDS.map((item) => (
                  <option key={item} value={item}>
                    {t(`payroll.componentKind.${item}`)}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            {([1, -1] as const).map((value) => (
              <button
                key={value}
                type="button"
                className="ab-chip"
                data-active={sign === value}
                onClick={() => setSign(value)}
              >
                {value === 1 ? t('payroll.addOn') : t('payroll.deduction')}
              </button>
            ))}
            {(['fixed', 'ratio'] as const).map((value) => (
              <button
                key={value}
                type="button"
                className="ab-chip"
                data-active={calc === value}
                onClick={() => setCalc(value)}
              >
                {t(`payroll.calc.${value}`)}
              </button>
            ))}
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            {calc === 'fixed' ? (
              <div>
                <label className="ab-field-label" htmlFor="cmp-amount">
                  {t('payroll.amount')}
                </label>
                <input
                  id="cmp-amount"
                  className="ab-input ab-tnum"
                  inputMode="decimal"
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                  placeholder="0.00"
                />
              </div>
            ) : (
              <>
                <div>
                  <label className="ab-field-label" htmlFor="cmp-rate">
                    {t('payroll.rate')}
                  </label>
                  <input
                    id="cmp-rate"
                    className="ab-input ab-tnum"
                    inputMode="decimal"
                    value={rate}
                    onChange={(event) => setRate(event.target.value)}
                    placeholder="8"
                  />
                </div>
                <div>
                  <label className="ab-field-label" htmlFor="cmp-base">
                    {t('payroll.baseKeyLabel')}
                  </label>
                  <select
                    id="cmp-base"
                    className="ab-select"
                    value={baseKey}
                    onChange={(event) => setBaseKey(event.target.value as 'basic' | 'gross')}
                  >
                    <option value="basic">{t('payroll.baseKey.basic')}</option>
                    <option value="gross">{t('payroll.baseKey.gross')}</option>
                  </select>
                </div>
              </>
            )}
          </div>

          <button
            type="button"
            className="ab-btn-secondary"
            disabled={busy || !name}
            onClick={() => void add()}
          >
            <Icon name="plus" size={13} />
            {t('payroll.addComponent')}
          </button>
          <p className="text-ab-caption1 text-label-3">{t('payroll.componentHint')}</p>
        </div>
      </div>
    </Modal>
  )
}

type PayComponentRow = {
  id: number
  name: string
  kind: string
  calc: 'fixed' | 'ratio' | 'formula'
  sign: 1 | -1
  amount_minor: number
  rate_bps: number
  base_key: 'basic' | 'gross'
}

const COMPONENT_KINDS = [
  'basic',
  'performance',
  'overtime',
  'meal',
  'transport',
  'bonus',
  'commission',
  'reimbursement',
  'pretax_deduction',
  'tax',
  'insurance',
  'other',
]

function FillDialog({
  record,
  onClose,
  onDone,
}: {
  record: PayrollRecord | null
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [skipReason, setSkipReason] = useState('')
  const [showSkip, setShowSkip] = useState(false)

  useEffect(() => {
    setError(null)
    setSkipReason('')
    setShowSkip(false)
  }, [record])

  const money = (minor: number) => displayMinor(minor, 'CNY', preferences.privacy_mode)

  return (
    <Modal
      open={record !== null}
      title={t('payroll.fillTitle', { period: record?.period ?? '' })}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          {/* **跳过是必须存在的出口**：只有"必须填"会让真的没工资的月份变成死锁，
              而用户会开始随手填假数据 —— 那比不填更糟 */}
          <button
            type="button"
            className="ab-btn-secondary !text-negative"
            disabled={busy}
            onClick={() => setShowSkip((previous) => !previous)}
          >
            {t('payroll.skip')}
          </button>
          <button
            type="button"
            className="ab-btn-primary"
            disabled={busy}
            onClick={async () => {
              if (!record) return
              setBusy(true)
              try {
                await api.fillPayrollRecord(record.id, { create_transaction: true })
                onDone()
              } catch (cause) {
                setError(cause instanceof ApiError ? cause.detail : String(cause))
              } finally {
                setBusy(false)
              }
            }}
          >
            <Icon name="check" size={13} />
            {t('payroll.fillAndPost')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
            {error}
          </div>
        ) : null}

        {record ? (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              {[
                [t('payroll.gross'), record.gross_minor],
                [t('payroll.insuranceDeduct'), record.insurance_minor],
                [t('payroll.net'), record.net_minor],
              ].map(([label, value]) => (
                <div key={String(label)}>
                  <div className="text-ab-caption1 text-label-3">{label}</div>
                  <div className="ab-tnum text-ab-subhead text-label">{money(Number(value))}</div>
                </div>
              ))}
            </div>

            <div className="space-y-1 border-t border-separator/60 pt-2">
              {record.items.map((item) => (
                <div key={item.component_id} className="flex items-center gap-2 text-ab-footnote">
                  <span className="min-w-0 flex-1 truncate text-label-2">{item.name}</span>
                  <span
                    className={`ab-tnum ${item.direction === 'add' ? 'text-positive' : 'text-negative'}`}
                  >
                    {item.direction === 'add' ? '+' : '−'}
                    {money(item.amount_minor)}
                  </span>
                </div>
              ))}
            </div>
            <p className="text-ab-caption1 text-label-3">{t('payroll.postHint')}</p>

            {showSkip ? (
              <div className="space-y-2 rounded-ab-sm bg-warning/10 p-3">
                <label className="ab-field-label" htmlFor="skip-reason">
                  {t('payroll.skipReason')}
                </label>
                <input
                  id="skip-reason"
                  className="ab-input"
                  value={skipReason}
                  onChange={(event) => setSkipReason(event.target.value)}
                  placeholder={t('payroll.skipReasonPlaceholder')}
                />
                <p className="text-ab-caption1 text-label-3">{t('payroll.skipHint')}</p>
                <button
                  type="button"
                  className="ab-btn-secondary"
                  disabled={busy || !skipReason.trim()}
                  onClick={async () => {
                    if (!record) return
                    setBusy(true)
                    try {
                      await api.skipPayrollRecord(record.id, skipReason.trim())
                      onDone()
                    } catch (cause) {
                      setError(cause instanceof ApiError ? cause.detail : String(cause))
                    } finally {
                      setBusy(false)
                    }
                  }}
                >
                  {t('payroll.confirmSkip')}
                </button>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </Modal>
  )
}

/**
 * 五险一金设置。
 *
 * 这是整个 P6 里**最需要说清楚"为什么是空的"**的一处：
 * 系统不预置任何比例（比例因城市与年份而异），
 * 因此界面上必须直接说明"请按当地政策填写"，
 * 并把"还没填"与"0%"明确区分开。
 */
function InsuranceDialog({
  open,
  period,
  onClose,
  onDone,
}: {
  open: boolean
  period: string
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const [items, setItems] = useState<InsuranceItem[]>([])
  const [profiles, setProfiles] = useState<InsuranceProfile[]>([])
  const [profileId, setProfileId] = useState<number | null>(null)
  const [computed, setComputed] = useState<InsuranceCompute | null>(null)
  const [statement, setStatement] = useState<InsuranceStatement | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [profileOpen, setProfileOpen] = useState(false)

  const load = useCallback(async () => {
    const [itemList, profileList] = await Promise.all([
      api.insuranceItems(),
      api.insuranceProfiles(),
    ])
    setItems(itemList.items)
    setProfiles(profileList.items)
    const chosen = profileId ?? profileList.items[0]?.id ?? null
    setProfileId(chosen)
    if (chosen !== null) {
      setComputed(await api.computeInsurance(chosen))
      setStatement(await api.insuranceStatement(chosen, Number(period.slice(0, 4))))
    }
  }, [profileId, period])

  useEffect(() => {
    if (open) void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, period])

  const ensure = async () => {
    setBusy(true)
    try {
      await api.ensureInsuranceItems()
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const saveRate = async (kind: string, personal: string, employer: string) => {
    setBusy(true)
    setError(null)
    try {
      await api.upsertInsuranceItem({
        kind,
        personal_rate_bps: Math.round(Number(personal || '0') * 100),
        employer_rate_bps: Math.round(Number(employer || '0') * 100),
      })
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title={t('payroll.insuranceSettings')}
      size="lg"
      onClose={onClose}
      footer={
        <button type="button" className="ab-btn-secondary" onClick={onDone}>
          {t('common.close')}
        </button>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
            {error}
          </div>
        ) : null}

        {/* 不预置比例的理由必须写在界面上 */}
        <p className="rounded-ab-sm bg-warning/10 px-3 py-2 text-ab-caption1 text-label-2">
          {t('payroll.rateNotice')}
        </p>

        {items.length === 0 ? (
          <button type="button" className="ab-btn-primary" disabled={busy} onClick={() => void ensure()}>
            <Icon name="plus" size={13} />
            {t('payroll.seedItems')}
          </button>
        ) : (
          <div className="space-y-1.5">
            {items.map((item) => (
              <RateRow key={item.id} item={item} busy={busy} onSave={saveRate} />
            ))}
          </div>
        )}

        <div className="border-t border-separator/60 pt-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="ab-section-label !px-0">{t('payroll.profiles')}</div>
            <button
              type="button"
              className="ab-chip ml-auto"
              onClick={() => setProfileOpen(true)}
            >
              <Icon name="plus" size={11} />
              {t('payroll.newProfile')}
            </button>
          </div>
          {profiles.length === 0 ? (
            <p className="mt-1 text-ab-footnote text-label-3">{t('payroll.noProfile')}</p>
          ) : (
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {profiles.map((profile) => (
                <button
                  key={profile.id}
                  type="button"
                  className="ab-chip"
                  data-active={profileId === profile.id}
                  onClick={() => setProfileId(profile.id)}
                >
                  {profile.name}
                </button>
              ))}
            </div>
          )}
        </div>

        {computed ? (
          <div className="space-y-2 border-t border-separator/60 pt-3">
            {/* **"未填比例"与"缴得少"必须区分开** */}
            {computed.incomplete ? (
              <p className="rounded-ab-sm bg-warning/10 px-3 py-2 text-ab-caption1 text-label-2">
                {t('payroll.incomplete', { items: computed.unfilled_items.slice(0, 4).join('、') })}
              </p>
            ) : null}
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-ab-footnote">
                <thead>
                  <tr className="border-b border-separator">
                    {[
                      t('payroll.itemName'),
                      t('payroll.base'),
                      t('payroll.personal'),
                      t('payroll.employer'),
                      t('payroll.toAccount'),
                    ].map((label, index) => (
                      <th
                        key={label}
                        className={`px-2 py-1.5 text-ab-caption1 font-medium text-label-3 ${index === 0 ? 'text-left' : 'text-right'}`}
                      >
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {computed.items.map((item) => (
                    <tr key={item.item_id} className="border-b border-separator/40">
                      <td className="px-2 py-1.5 text-label-2">
                        {item.name}
                        {/* **收敛必须标出来**：用户填 3 万、系统按 2.4 万算，
                            不说明他会以为软件算错了 */}
                        {item.clamped ? (
                          <span className="ml-1.5 text-ab-caption1 text-warning">
                            {t(`payroll.clamped.${item.clamped}`)}
                            {item.raw_base_minor !== item.base_minor
                              ? `（${displayMinor(item.raw_base_minor, 'CNY')} → ${displayMinor(item.base_minor, 'CNY')}）`
                              : ''}
                          </span>
                        ) : null}
                      </td>
                      <td className="ab-tnum px-2 py-1.5 text-right text-label-3">
                        {displayMinor(item.base_minor, 'CNY')}
                      </td>
                      <td className="ab-tnum px-2 py-1.5 text-right text-label-2">
                        {item.personal_minor ? displayMinor(item.personal_minor, 'CNY') : '—'}
                      </td>
                      <td className="ab-tnum px-2 py-1.5 text-right text-label-2">
                        {item.employer_minor ? displayMinor(item.employer_minor, 'CNY') : '—'}
                      </td>
                      <td className="ab-tnum px-2 py-1.5 text-right text-label-3">
                        {item.to_account_minor ? displayMinor(item.to_account_minor, 'CNY') : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-separator">
                    <td className="px-2 py-1.5 font-semibold text-label" colSpan={2}>
                      {t('payroll.total')}
                    </td>
                    <td className="ab-tnum px-2 py-1.5 text-right font-semibold text-label">
                      {displayMinor(computed.personal_total_minor, 'CNY')}
                    </td>
                    <td className="ab-tnum px-2 py-1.5 text-right font-semibold text-label">
                      {displayMinor(computed.employer_total_minor, 'CNY')}
                    </td>
                    <td className="ab-tnum px-2 py-1.5 text-right font-semibold text-label">
                      {displayMinor(computed.to_account_total_minor, 'CNY')}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="ab-btn-secondary"
                disabled={busy || computed.incomplete}
                onClick={async () => {
                  if (profileId === null) return
                  setBusy(true)
                  try {
                    await api.recordInsuranceContribution(profileId, period)
                    await load()
                  } catch (cause) {
                    setError(cause instanceof ApiError ? cause.detail : String(cause))
                  } finally {
                    setBusy(false)
                  }
                }}
              >
                {t('payroll.recordThisPeriod', { period })}
              </button>
              {computed.incomplete ? (
                <span className="text-ab-caption1 text-label-3">{t('payroll.fillRatesFirst')}</span>
              ) : null}
            </div>
          </div>
        ) : null}

        {/* 年度对账：**差异是这份报表的全部意义** */}
        {statement ? (
          <div className="space-y-2 border-t border-separator/60 pt-3">
            <div className="ab-section-label !px-0">{t('payroll.statement', { year: statement.year })}</div>
            <div className="grid gap-2 sm:grid-cols-3">
              {[
                [t('payroll.computedPersonal'), statement.computed_personal_minor],
                [t('payroll.expectedPersonal'), statement.expected.personal_minor],
                [t('payroll.difference'), statement.difference.personal_minor],
              ].map(([label, value]) => (
                <div key={String(label)}>
                  <div className="text-ab-caption1 text-label-3">{label}</div>
                  <div className="ab-tnum text-ab-subhead text-label">
                    {value === null ? t('payroll.notEntered') : displayMinor(Number(value), 'CNY')}
                  </div>
                </div>
              ))}
            </div>
            <p className="text-ab-caption1 text-label-3">
              {t('payroll.monthsRecorded', {
                recorded: statement.months_recorded,
                missing: statement.missing_months,
              })}
            </p>
          </div>
        ) : null}
      </div>

      <ProfileDialog
        open={profileOpen}
        onClose={() => setProfileOpen(false)}
        onDone={async () => {
          setProfileOpen(false)
          await load()
        }}
      />
    </Modal>
  )
}

function RateRow({
  item,
  busy,
  onSave,
}: {
  item: InsuranceItem
  busy: boolean
  onSave: (kind: string, personal: string, employer: string) => Promise<void>
}) {
  const { t } = useI18n()
  const [personal, setPersonal] = useState(item.personal_rate_bps ? String(item.personal_rate_bps / 100) : '')
  const [employer, setEmployer] = useState(item.employer_rate_bps ? String(item.employer_rate_bps / 100) : '')

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-ab-sm bg-surface-2/50 px-2 py-1.5">
      <span className="min-w-0 flex-1">
        <span className="text-ab-footnote text-label-2">{item.name}</span>
        {/* 三种状态要分清：**已停用** / 比例待填写 / 已填。
            已停用是用户的主动选择（当地不缴这一项），
            把它显示成"比例待填写"会让他以为漏填了。 */}
        {!item.enabled ? (
          <span className="ml-1.5 text-ab-caption1 text-label-3">{t('payroll.itemDisabled')}</span>
        ) : !item.rates_filled ? (
          <span className="ml-1.5 text-ab-caption1 text-warning">{t('payroll.rateUnfilled')}</span>
        ) : null}
        {item.note ? (
          <span className="ml-1.5 text-ab-caption2 text-label-3">{item.note}</span>
        ) : null}
      </span>
      <input
        className="ab-input ab-tnum !w-16"
        inputMode="decimal"
        value={personal}
        placeholder={t('payroll.personalShort')}
        onChange={(event) => setPersonal(event.target.value)}
      />
      <input
        className="ab-input ab-tnum !w-16"
        inputMode="decimal"
        value={employer}
        placeholder={t('payroll.employerShort')}
        onChange={(event) => setEmployer(event.target.value)}
      />
      <button
        type="button"
        className="ab-btn-secondary shrink-0"
        disabled={busy}
        onClick={() => void onSave(item.kind, personal, employer)}
      >
        {t('common.save')}
      </button>
    </div>
  )
}

function ProfileDialog({
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
  const [city, setCity] = useState('')
  const [employer, setEmployer] = useState('')
  const [social, setSocial] = useState('')
  const [housing, setHousing] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.createInsuranceProfile({
        name,
        city,
        employer,
        social_base_minor: Math.round(Number(social || '0') * 100),
        housing_base_minor: Math.round(Number(housing || '0') * 100),
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
      open={open}
      title={t('payroll.newProfile')}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ab-btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="ab-btn-primary" disabled={busy || !name} onClick={() => void submit()}>
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error ? (
          <div className="rounded-ab-sm bg-negative/10 px-3 py-2 text-ab-footnote text-negative">
            {error}
          </div>
        ) : null}
        <div>
          <label className="ab-field-label" htmlFor="ins-name">
            {t('payroll.profileName')}
          </label>
          <input id="ins-name" className="ab-input" value={name} onChange={(event) => setName(event.target.value)} />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="ab-field-label" htmlFor="ins-city">
              {t('payroll.city')}
            </label>
            <input id="ins-city" className="ab-input" value={city} onChange={(event) => setCity(event.target.value)} />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="ins-employer">
              {t('payroll.employer')}
            </label>
            <input
              id="ins-employer"
              className="ab-input"
              value={employer}
              onChange={(event) => setEmployer(event.target.value)}
            />
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="ab-field-label" htmlFor="ins-social">
              {t('payroll.socialBase')}
            </label>
            <input
              id="ins-social"
              className="ab-input ab-tnum"
              inputMode="decimal"
              value={social}
              onChange={(event) => setSocial(event.target.value)}
              placeholder="0.00"
            />
          </div>
          <div>
            <label className="ab-field-label" htmlFor="ins-housing">
              {t('payroll.housingBase')}
            </label>
            <input
              id="ins-housing"
              className="ab-input ab-tnum"
              inputMode="decimal"
              value={housing}
              onChange={(event) => setHousing(event.target.value)}
              placeholder="0.00"
            />
          </div>
        </div>
        <p className="text-ab-caption1 text-label-3">{t('payroll.baseHint')}</p>
      </div>
    </Modal>
  )
}
