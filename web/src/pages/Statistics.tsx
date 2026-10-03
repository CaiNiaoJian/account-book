/**
 * 统计可视化（P2）。
 *
 * 一条硬规则：**这一页上的每一张图都必须能由真实数据算出来。**
 * 计划里列了 12 类图表，但其中一部分（K 线、薪酬构成、预算仪表盘）
 * 依赖 P3–P6 才有的数据。现在把它们画出来只能是示意图形，
 * 而"看起来有数字其实是编的"正是本项目从一开始就拒绝的做法。
 * 因此这里实现的都是当前数据真能支撑的图，其余留给对应的阶段。
 *
 * 口径统一由后端提供（`/api/stats/*`、`/api/calendar/*`）：
 * 前端不做聚合计算 —— 否则这一页和首页会慢慢算出两个不同的数。
 */

import { useMemo, useState } from 'react'

import { Chart } from '@/components/Chart'
import { Icon } from '@/components/Icon'
import { Card, EmptyState, Skeleton } from '@/components/ui'
import { useI18n } from '@/i18n'
import { api, type CategoryTrendRow } from '@/lib/api'
import { displayMinor } from '@/lib/format'
import { resolveToken, type TokenName } from '@/design/tokens'
import { usePreferences } from '@/app/preferences'
import { useCallback, useEffect } from 'react'

import { useLedger } from '@/features/ledger/store'
import { readSections, section } from '@/features/ledger/sections'

/** 区间档位 */
type Span = '3m' | '6m' | '12m' | 'ytd'

function spanRange(span: Span): { start: string; end: string; months: number } {
  const today = new Date()
  const end = today.toISOString().slice(0, 10)
  if (span === 'ytd') {
    return { start: `${today.getFullYear()}-01-01`, end, months: today.getMonth() + 1 }
  }
  const months = span === '3m' ? 3 : span === '6m' ? 6 : 12
  const start = new Date(today.getFullYear(), today.getMonth() - (months - 1), 1)
  return { start: start.toISOString().slice(0, 10), end, months }
}

export function StatisticsPage() {
  const { t } = useI18n()
  const { preferences } = usePreferences()
  const { tree, categoryById, refresh: refreshLedger } = useLedger()

  const [span, setSpan] = useState<Span>('12m')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [series, setSeries] = useState<Awaited<ReturnType<typeof api.netWorthSeries>>>([])
  const [cashFlow, setCashFlow] = useState<Awaited<ReturnType<typeof api.cashFlow>>>([])
  const [summary, setSummary] = useState<Awaited<ReturnType<typeof api.summary>> | null>(null)
  const [calendarDays, setCalendarDays] = useState<Awaited<ReturnType<typeof api.calendar>>['days']>([])
  const [wall, setWall] = useState<Awaited<ReturnType<typeof api.assetWall>> | null>(null)
  // 按月的分类构成：堆叠面积图与桑基图都用它
  const [trend, setTrend] = useState<{ months: string[]; rows: CategoryTrendRow[] }>({
    months: [],
    rows: [],
  })

  const range = useMemo(() => spanRange(span), [span])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      await readSections([
        section(() => api.netWorthSeries(range.start, range.end), setSeries),
        section(() => api.cashFlow({ months: range.months }), setCashFlow),
        section(() => api.summary({ start: range.start, end: range.end, top_categories: 12 }), setSummary),
        section(() => api.calendar(range.start, range.end, 'expense'), data => setCalendarDays(data.days)),
        section(api.assetWall, setWall),
        section(() => api.categoryTrend(range.months), setTrend),
      ], t('ledgerLoading.notLoaded'))
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [range])

  useEffect(() => {
    void load()
    void refreshLedger()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load])

  /**
   * 图表配色：**必须用 `resolveToken()` 取实值，不能写 `rgb(var(--ab-x))`**。
   *
   * ECharts 默认走 canvas 渲染，canvas 不认识 CSS 自定义属性 ——
   * 传进去的 `rgb(var(--ab-accent))` 会静默变成"无法解析的颜色"，
   * 结果是图表空白、控制台也不报错。这个坑只能靠肉眼看出来。
   * 颜色依然源自设计令牌，因此日夜主题切换时重新生成即可保持一致。
   */
  const palette = useMemo<TokenName[]>(
    () => ['accent', 'teal', 'indigo', 'purple', 'orange', 'pink', 'mint', 'yellow', 'red', 'gray'],
    [],
  )

  // ---- 1. 净值趋势（面积） --------------------------------------------------
  const netWorthOption = useMemo(
    () => ({
      series: [
        {
          type: 'line',
          data: series.map((item) => [item.date.slice(5), item.net_worth_minor / 100]),
          areaStyle: { opacity: 0.14 },
          showSymbol: false,
        },
      ],
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: series.map((item) => item.date.slice(5)),
        // 一年 365 个点：全部显示会糊成一片，按约 12 个标签稀疏化
        axisLabel: { interval: Math.max(0, Math.ceil(series.length / 12) - 1) },
      },
      yAxis: { type: 'value', scale: true },
      grid: { left: 4, right: 14, top: 16, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}`,
      },
    }),
    [series],
  )

  // ---- 2. 月度收支对比（分组柱） --------------------------------------------
  const monthlyOption = useMemo(
    () => ({
      legend: { data: [t('transactionType.income'), t('transactionType.expense'), t('stats.net')], top: 0 },
      color: [resolveToken('positive'), resolveToken('negative'), resolveToken('accent')],
      series: [
        { name: t('transactionType.income'), type: 'bar', data: cashFlow.map((item) => item.income_minor / 100) },
        { name: t('transactionType.expense'), type: 'bar', data: cashFlow.map((item) => item.expense_minor / 100) },
        {
          name: t('stats.net'),
          type: 'line',
          data: cashFlow.map((item) => item.net_minor / 100),
          smooth: true,
        },
      ],
      xAxis: { type: 'category', data: cashFlow.map((item) => item.month.slice(2)) },
      yAxis: { type: 'value', scale: true },
      grid: { left: 4, right: 12, top: 30, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}` },
    }),
    [cashFlow, t],
  )

  // ---- 3. 现金流瀑布（收入 → 分类支出 → 结余） ------------------------------
  const waterfallOption = useMemo(() => {
    const income = (summary?.income_minor ?? 0) / 100
    const categories = (summary?.by_category ?? []).slice(0, 8).map((item) => ({
      name: item.category_name,
      value: item.amount_minor / 100,
    }))
    // 瀑布图的实现方式：用一个"透明底座"把每根柱子顶到累计高度上。
    // 这是 ECharts 里画瀑布的标准做法 —— 它没有内置的瀑布类型。
    const base: number[] = [0]
    const delta: number[] = [income]
    let running = income
    for (const category of categories) {
      base.push(running - category.value)
      delta.push(-category.value)
      running -= category.value
    }
    base.push(0)
    delta.push(running)

    return {
      series: [
        {
          type: 'bar',
          stack: 'waterfall',
          itemStyle: { color: 'transparent' },
          emphasis: { itemStyle: { color: 'transparent' } },
          data: base,
          silent: true,
        },
        {
          type: 'bar',
          stack: 'waterfall',
          data: delta.map((value, index) => ({
            value,
            itemStyle: {
              color:
                index === 0
                  ? resolveToken('positive')
                  : index === delta.length - 1
                    ? value >= 0
                      ? resolveToken('accent')
                      : resolveToken('negative')
                    : resolveToken('negative'),
              opacity: index === 0 || index === delta.length - 1 ? 1 : 0.72,
            },
          })),
          label: {
            show: true,
            position: 'top',
            formatter: (params: { value: number }) => (params.value < 0 ? `-${Math.abs(params.value).toFixed(0)}` : params.value.toFixed(0)),
            fontSize: 10,
          },
        },
      ],
      xAxis: {
        type: 'category',
        data: [t('transactionType.income'), ...categories.map((item) => item.name), t('stats.net')],
        axisLabel: { interval: 0, rotate: categories.length > 5 ? 30 : 0, fontSize: 10 },
      },
      yAxis: { type: 'value', scale: true },
      grid: { left: 4, right: 12, top: 18, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}` },
    }
  }, [summary, t])

  // ---- 4/5/6. 分类构成（环形 / 矩形树 / 旭日） ------------------------------
  const composition = summary?.by_category ?? []

  /**
   * 分类构成堆叠面积图。
   *
   * 横轴用 `trend.months`（服务端给的**连续完整**月份轴），而不是从 rows 里
   * 反推 —— 只有一个月有支出时，反推出来的轴就只有一个月，图会缺列。
   * 缺失的 (月, 分类) 组合在这里补 0：这是**渲染需要**的补零，
   * 与在数据层伪造 0 行是两件事。
   */
  const stackedAreaOption = useMemo(() => {
    const months = trend.months
    const byCategory = new Map<string, number[]>()
    for (const row of trend.rows) {
      const key = row.category_name || t('ledger.uncategorized')
      if (!byCategory.has(key)) byCategory.set(key, new Array(months.length).fill(0))
      const index = months.indexOf(row.month)
      if (index >= 0) {
        // noUncheckedIndexedAccess 下索引访问可能是 undefined，因此显式判空
        const bucket = byCategory.get(key)
        const current = bucket?.[index]
        if (bucket !== undefined && current !== undefined) {
          bucket[index] = current + row.amount_minor / 100
        }
      }
    }
    // 分类多时只画金额最大的前 8 类，其余合并 —— 20 条堆叠带读不出任何东西
    const entries = [...byCategory.entries()].sort(
      (a, b) => b[1].reduce((x, y) => x + y, 0) - a[1].reduce((x, y) => x + y, 0),
    )
    const top = entries.slice(0, 8)
    const rest = entries.slice(8)
    if (rest.length > 0) {
      const merged = new Array(months.length).fill(0)
      for (const [, values] of rest) values.forEach((value, index) => (merged[index] += value))
      top.push([t('stats.otherCategories', { count: rest.length }), merged])
    }

    return {
      legend: { top: 0, type: 'scroll' },
      series: top.map(([name, values], index) => ({
        name,
        type: 'line',
        stack: 'total',
        areaStyle: { opacity: 0.75 },
        symbol: 'none',
        smooth: true,
        lineStyle: { width: 0 },
        itemStyle: { color: resolveToken(palette[index % palette.length] as TokenName) },
        data: values,
      })),
      xAxis: { type: 'category', boundaryGap: false, data: months },
      yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
      grid: { left: 4, right: 12, top: 30, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}` },
    }
  }, [trend, palette, t])

  /**
   * 桑基图：收入 → 资金池 → 各支出分类 + 结余。
   *
   * 不虚构中间节点 —— 这是真实的两段流向。结余只在收入大于支出时出现，
   * 否则会多画一个 0 宽的节点。
   */
  const sankeyOption = useMemo(() => {
    const income = (summary?.income_minor ?? 0) / 100
    const top = composition.slice(0, 8)
    const spent = top.reduce((sum, item) => sum + item.amount_minor, 0) / 100
    const restSpent = (summary?.expense_minor ?? 0) / 100 - spent
    const surplus = income - (summary?.expense_minor ?? 0) / 100

    const hub = t('stats.pool')
    const incomeNode = t('transactionType.income')
    const nodes = [{ name: incomeNode }, { name: hub }]
    const links = [{ source: incomeNode, target: hub, value: Math.max(0, income) }]
    for (const item of top) {
      nodes.push({ name: item.category_name })
      links.push({ source: hub, target: item.category_name, value: item.amount_minor / 100 })
    }
    if (restSpent > 0.01) {
      // 先把名字取出来：直接取 nodes[last].name 在 noUncheckedIndexedAccess
      // 下是 possibly undefined，而这里本来也不需要那个索引访问
      const otherName = t('stats.otherCategories', { count: Math.max(1, composition.length - 8) })
      nodes.push({ name: otherName })
      links.push({ source: hub, target: otherName, value: restSpent })
    }
    if (surplus > 0.01) {
      nodes.push({ name: t('stats.surplus') })
      links.push({ source: hub, target: t('stats.surplus'), value: surplus })
    }

    return {
      series: [
        {
          type: 'sankey',
          left: 8,
          right: 8,
          top: 12,
          bottom: 8,
          nodeWidth: 12,
          nodeGap: 8,
          emphasis: { focus: 'adjacency' },
          label: { fontSize: 11 },
          lineStyle: { color: 'gradient', opacity: 0.35, curveness: 0.5 },
          data: nodes,
          links,
        },
      ],
      tooltip: { trigger: 'item', valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}` },
    }
  }, [summary, composition, t])

  /**
   * 气泡图：按日支出分布。
   *
   * 横轴是日期、纵轴是当日支出、气泡大小是笔数。
   * 用按日聚合而不是逐笔流水：逐笔需要一次可能很大的分页查询，
   * 而"哪几天花得多、哪些天笔数集中"用日聚合就能回答。
   */
  const bubbleOption = useMemo(() => {
    const days = calendarDays.filter((day) => day.expense_minor > 0)
    const maxAmount = Math.max(1, ...days.map((day) => day.expense_minor))
    return {
      xAxis: { type: 'category', data: days.map((day) => day.date.slice(5)), axisLabel: { fontSize: 9 } },
      yAxis: {
        type: 'value',
        axisLabel: { fontSize: 9 },
        name: t('stats.dailyAmount'),
        nameTextStyle: { fontSize: 10 },
      },
      series: [
        {
          type: 'scatter',
          data: days.map((day) => [
            day.date.slice(5),
            day.expense_minor / 100,
            Math.max(6, Math.round((day.expense_minor / maxAmount) * 28)),
            day.tx_count,
          ]),
          symbolSize: (value: number[]) => value[2] ?? 8,
          itemStyle: { color: resolveToken('negative'), opacity: 0.55 },
        },
      ],
      grid: { left: 4, right: 14, top: 26, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'item',
        formatter: (params: { value: number[] }) =>
          `${params.value[0]}<br/>${t('transactionType.expense')} ¥${Number(params.value[1]).toLocaleString('zh-CN')}<br/>${t('calendar.txCount', { count: Number(params.value[3] ?? 0) })}`,
      },
    }
  }, [calendarDays, t])


  const donutOption = useMemo(
    () => ({
      series: [
        {
          type: 'pie',
          radius: ['52%', '78%'],
          center: ['50%', '52%'],
          label: { show: false },
          data: composition.map((item) => ({ name: item.category_name, value: item.amount_minor / 100 })),
        },
      ],
      tooltip: { trigger: 'item', valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}` },
    }),
    [composition],
  )

  const treemapOption = useMemo(
    () => ({
      series: [
        {
          type: 'treemap',
          roam: false,
          nodeClick: false,
          breadcrumb: { show: false },
          itemStyle: { borderColor: resolveToken('surface'), borderWidth: 2, gapWidth: 2 },
          label: { fontSize: 11, color: '#fff' },
          data: composition.map((item, index) => ({
            name: item.category_name,
            value: item.amount_minor / 100,
            itemStyle: { color: resolveToken(palette[index % palette.length] as TokenName) },
          })),
        },
      ],
      tooltip: { trigger: 'item', valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}` },
    }),
    [composition, palette],
  )

  /**
   * 旭日图用**两级**：支出 → 分类 → 子分类。
   *
   * 分类树已经存在（物化路径），因此这里能画出真实的层级，
   * 而不是把平铺的分类硬凑成两层。
   */
  const sunburstOption = useMemo(() => {
    const parents = new Map<number, number | null>()
    const childrenOf = new Map<number | null, typeof tree.expense>()
    for (const root of tree.expense) {
      parents.set(root.id, null)
      childrenOf.set(root.id, root.children)
      for (const child of root.children) {
        parents.set(child.id, root.id)
        childrenOf.set(child.id, [])
      }
    }

    const byRoot = new Map<string, { name: string; value: number; children: { name: string; value: number }[] }>()
    for (const item of composition) {
      const id = item.category_id
      const self = id ? categoryById(id) : undefined
      const rootId = self ? (parents.get(self.id) === null ? self.id : parents.get(self.id)) : null
      const rootName = rootId ? (categoryById(rootId)?.name ?? item.category_name) : t('ledger.uncategorized')
      const bucket = byRoot.get(rootName) ?? { name: rootName, value: 0, children: [] }
      bucket.value += item.amount_minor / 100
      bucket.children.push({ name: item.category_name, value: item.amount_minor / 100 })
      byRoot.set(rootName, bucket)
    }

    return {
      series: [
        {
          type: 'sunburst',
          radius: ['15%', '88%'],
          nodeClick: false,
          // 只展示两级：再深的层级在环形里已经细到看不清
          levels: [{}, { r0: '15%', r: '55%' }, { r0: '57%', r: '88%', label: { rotate: 'tangential' } }],
          data: [...byRoot.values()].map((bucket, index) => ({
            ...bucket,
            itemStyle: { color: resolveToken(palette[index % palette.length] as TokenName) },
          })),
        },
      ],
      tooltip: { trigger: 'item', valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}` },
    }
  }, [composition, tree, categoryById, palette, t])

  // ---- 7. 资产构成（横向堆叠条） --------------------------------------------
  const assetOption = useMemo(() => {
    const groups = wall?.groups ?? []
    const total = Math.max(
      1,
      groups.reduce(
        (sum, group) => sum + group.cards.reduce((inner, card) => inner + Math.abs(card.balance_minor), 0),
        0,
      ),
    )
    void total
    return {
      series: groups.map((group, index) => ({
        name: group.name,
        type: 'bar',
        stack: 'assets',
        barWidth: 28,
        itemStyle: { color: resolveToken(palette[index % palette.length] as TokenName) },
        data: [group.cards.reduce((sum, card) => sum + Math.abs(card.balance_minor), 0) / 100],
      })),
      xAxis: { type: 'value', show: false },
      yAxis: { type: 'category', data: [''], show: false },
      grid: { left: 0, right: 0, top: 8, bottom: 8 },
      legend: { top: 0 },
      tooltip: { trigger: 'axis', valueFormatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN')}` },
    }
  }, [wall, palette])

  // ---- 8. 每日支出分布（按星期的箱线图） ------------------------------------
  const boxplotOption = useMemo(() => {
    const buckets: number[][] = [[], [], [], [], [], [], []]
    for (const day of calendarDays) {
      if (day.expense_minor <= 0) continue
      const weekday = new Date(`${day.date}T00:00:00`).getDay()
      buckets[weekday]?.push(day.expense_minor / 100)
    }
    const labels = ['日', '一', '二', '三', '四', '五', '六']
    const boxData = buckets.map((values) => {
      if (values.length === 0) return [0, 0, 0, 0, 0]
      const sorted = [...values].sort((a, b) => a - b)
      const quantile = (p: number) => sorted[Math.min(sorted.length - 1, Math.max(0, Math.round(p * (sorted.length - 1))))] ?? 0
      return [quantile(0.05), quantile(0.25), quantile(0.5), quantile(0.75), quantile(0.95)]
    })

    return {
      series: [
        {
          type: 'boxplot',
          data: boxData,
          itemStyle: { color: resolveToken('accent'), borderColor: resolveToken('accent'), opacity: 0.8 },
        },
      ],
      xAxis: { type: 'category', data: labels.map((label) => `${label}`) },
      yAxis: { type: 'value', scale: true, name: t('stats.currencyUnit'), nameTextStyle: { fontSize: 10 } },
      grid: { left: 4, right: 12, top: 18, bottom: 4, containLabel: true },
      tooltip: { trigger: 'item', valueFormatter: (value: number) => `¥${Number(value).toFixed(2)}` },
    }
  }, [calendarDays, t])

  // ---- 9. 财务画像（雷达） --------------------------------------------------
  /**
   * 五个维度全部由既有数据算出，**没有主观打分**：
   *   储蓄率、记录完整度、支出稳定性、分类集中度、资产流动性。
   * 每个维度都归一化到 0–100，且"越高越好"的方向一致 ——
   * 否则雷达图的形状会让人做出相反的判断。
   */
  const radarOption = useMemo(() => {
    const income = summary?.income_minor ?? 0
    const expense = summary?.expense_minor ?? 0
    const savingRate = income > 0 ? Math.max(0, Math.min(1, (income - expense) / income)) : 0

    const logged = calendarDays.filter((day) => day.tx_count > 0).length
    const coverage = calendarDays.length > 0 ? logged / calendarDays.length : 0

    const expenses = calendarDays.filter((day) => day.expense_minor > 0).map((day) => day.expense_minor)
    const mean = expenses.length > 0 ? expenses.reduce((sum, value) => sum + value, 0) / expenses.length : 0
    const variance =
      expenses.length > 1
        ? expenses.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (expenses.length - 1)
        : 0
    const cv = mean > 0 ? Math.sqrt(variance) / mean : 1
    // 变异系数越小越稳定；1.2 以上视为"完全不稳定"
    const stability = Math.max(0, Math.min(1, 1 - cv / 1.2))

    const totalExpense = composition.reduce((sum, item) => sum + item.amount_minor, 0)
    const top5 = [...composition]
      .sort((a, b) => b.amount_minor - a.amount_minor)
      .slice(0, 5)
      .reduce((sum, item) => sum + item.amount_minor, 0)
    // 集中度越高说明结构越"偏"；这里转换成"越均衡越好"
    const balance = totalExpense > 0 ? Math.max(0, 1 - (top5 / totalExpense - 0.5) / 0.5) : 0

    const liquid = wall?.summary.available_minor ?? 0
    const netWorth = Math.abs(wall?.summary.net_worth_minor ?? 0)
    const liquidity = netWorth > 0 ? Math.max(0, Math.min(1, liquid / netWorth)) : 0

    const values = [savingRate, coverage, stability, balance, liquidity].map((value) =>
      Number((value * 100).toFixed(1)),
    )

    return {
      radar: {
        indicator: [
          { name: t('stats.dim.saving'), max: 100 },
          { name: t('stats.dim.coverage'), max: 100 },
          { name: t('stats.dim.stability'), max: 100 },
          { name: t('stats.dim.balance'), max: 100 },
          { name: t('stats.dim.liquidity'), max: 100 },
        ],
        radius: '66%',
        axisName: { fontSize: 11 },
        splitLine: { lineStyle: { opacity: 0.35 } },
      },
      series: [
        {
          type: 'radar',
          data: [{ value: values, name: t('stats.profile'), areaStyle: { opacity: 0.2 } }],
        },
      ],
      tooltip: { trigger: 'item' },
    }
  }, [summary, calendarDays, composition, wall, t])

  if (loading) {
    return (
      <div className="grid gap-4 xl:grid-cols-2">
        {[0, 1, 2, 3].map((key) => (
          <Skeleton key={key} className="h-64 w-full" />
        ))}
      </div>
    )
  }

  if (error) {
    return (
      <Card flush>
        <EmptyState icon="alert" title={t('stats.loadFailed')} body={error} />
      </Card>
    )
  }

  const hasData = series.some((item) => item.income_minor > 0 || item.expense_minor > 0)
  if (!hasData) {
    return (
      <Card flush>
        <EmptyState
          icon="statistics"
          title={t('stats.emptyTitle')}
          body={t('stats.emptyBody')}
          action={
            <a href="/transactions" className="ab-btn-primary">
              <Icon name="plus" size={14} />
              {t('ledger.quickAdd')}
            </a>
          }
        />
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      {/* 区间切换 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="ab-segment">
          {(['3m', '6m', '12m', 'ytd'] as Span[]).map((item) => (
            <button key={item} type="button" data-active={span === item} onClick={() => setSpan(item)}>
              {t(`stats.span.${item}`)}
            </button>
          ))}
        </div>
        <span className="ab-tnum text-ab-caption text-label-3">
          {t('stats.rangeSummary', {
            income: displayMinor(summary?.income_minor ?? 0, 'CNY', preferences.privacy_mode),
            expense: displayMinor(summary?.expense_minor ?? 0, 'CNY', preferences.privacy_mode),
          })}
        </span>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title={t('stats.netWorthTrend')} className="xl:col-span-2">
          <Chart option={netWorthOption} height={220} />
        </Card>

        <Card title={t('stats.monthlyCompare')}>
          <Chart option={monthlyOption} height={240} />
        </Card>

        <Card title={t('stats.waterfall')}>
          {composition.length === 0 ? (
            <EmptyState icon="statistics" title={t('stats.noExpense')} body={t('stats.noExpenseBody')} />
          ) : (
            <Chart option={waterfallOption} height={240} />
          )}
        </Card>

        <Card title={t('stats.compositionDonut')}>
          {composition.length === 0 ? (
            <EmptyState icon="statistics" title={t('stats.noExpense')} body={t('stats.noExpenseBody')} />
          ) : (
            <Chart option={donutOption} height={240} />
          )}
        </Card>

        <Card title={t('stats.compositionTreemap')}>
          {composition.length === 0 ? (
            <EmptyState icon="statistics" title={t('stats.noExpense')} body={t('stats.noExpenseBody')} />
          ) : (
            <Chart option={treemapOption} height={240} />
          )}
        </Card>

        <Card title={t('stats.compositionSunburst')} className="xl:col-span-2">
          <p className="mb-1 text-ab-caption1 text-label-3">{t('stats.sunburstHint')}</p>
          {composition.length === 0 ? (
            <EmptyState icon="statistics" title={t('stats.noExpense')} body={t('stats.noExpenseBody')} />
          ) : (
            <Chart option={sunburstOption} height={300} />
          )}
        </Card>

        <Card title={t('stats.assetStructure')}>
          <Chart option={assetOption} height={140} />
          <div className="mt-2 space-y-1">
            {(wall?.groups ?? []).map((group, index) => (
              <div key={group.key} className="flex items-center gap-2 text-ab-footnote">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ backgroundColor: `rgb(var(--ab-${palette[index % palette.length]}))` }}
                />
                <span className="flex-1 truncate text-label-2">{group.name}</span>
                <span className="ab-tnum text-label-3">
                  {displayMinor(
                    group.cards.reduce((sum, card) => sum + card.balance_minor, 0),
                    'CNY',
                    preferences.privacy_mode,
                  )}
                </span>
              </div>
            ))}
          </div>
        </Card>

        <Card title={t('stats.expenseByWeekday')}>
          <Chart option={boxplotOption} height={240} />
          <p className="mt-1 text-ab-caption1 text-label-3">{t('stats.boxplotHint')}</p>
        </Card>

        <Card title={t('stats.compositionStacked')} className="xl:col-span-2">
          <p className="mb-1 text-ab-caption1 text-label-3">{t('stats.stackedHint')}</p>
          {trend.rows.length === 0 ? (
            <EmptyState icon="statistics" title={t('stats.noExpense')} body={t('stats.noExpenseBody')} />
          ) : (
            <Chart option={stackedAreaOption} height={280} />
          )}
        </Card>

        <Card title={t('stats.sankeyTitle')}>
          {(summary?.income_minor ?? 0) <= 0 ? (
            <EmptyState icon="statistics" title={t('stats.noIncome')} body={t('stats.noIncomeBody')} />
          ) : (
            <Chart option={sankeyOption} height={280} />
          )}
        </Card>

        <Card title={t('stats.bubbleTitle')}>
          <p className="mb-1 text-ab-caption1 text-label-3">{t('stats.bubbleHint')}</p>
          <Chart option={bubbleOption} height={260} />
        </Card>

        <Card title={t('stats.financialProfile')} className="xl:col-span-2">
          <Chart option={radarOption} height={300} />
          <p className="mt-1 text-ab-caption1 text-label-3">{t('stats.profileHint')}</p>
        </Card>
      </div>
    </div>
  )
}
