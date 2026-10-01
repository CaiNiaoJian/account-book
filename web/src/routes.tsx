/**
 * 路由表 —— 由导航清单**派生**，而非手写。
 *
 * 这样做消除了最常见的一类不一致：
 *   "侧边栏里有这一项，点进去 404" 或 "路由存在但用户找不到入口"。
 *
 * 分派规则：
 *   * 已在对应阶段实现的模块 → 真实页面组件；
 *   * 其余模块 → 统一占位页（自动带模块名、阶段与规划说明）。
 * 当某个模块完成时，只需在此登记它的路径，
 * 导航徽标由 `IMPLEMENTED_PHASES` 自动同步 —— 不要再手工改徽标。
 */

import type { ReactNode } from 'react'
import { Link, type RouteObject } from 'react-router-dom'

import { Icon } from '@/components/Icon'
import { Card, EmptyState } from '@/components/ui'
import { AppShell } from '@/app/AppShell'
import { ALL_NAV_ITEMS } from '@/app/navigation'
import { useI18n } from '@/i18n'
import { AboutPage } from '@/pages/About'
import { AccountsPage } from '@/pages/Accounts'
import { CalendarPage } from '@/pages/Calendar'
import { CardsPage } from '@/pages/Cards'
import { KlinePage } from '@/pages/Kline'
import { MetricsPage } from '@/pages/Metrics'
import { GoalsPage } from '@/pages/Goals'
import { LedgerPage } from '@/pages/Ledger'
import { PiggyPage } from '@/pages/Piggy'
import { PayrollPage } from '@/pages/Payroll'
import { ReportsPage } from '@/pages/Reports'
import { SchedulerPage } from '@/pages/Scheduler'
import { BudgetsPage, DebtsPage, RecurringPage } from '@/pages/Planning'
import { TrashPage } from '@/pages/Trash'
import { CategoriesPage } from '@/pages/Categories'
import { DashboardPage } from '@/pages/Dashboard'
import { PlaceholderPage } from '@/pages/Placeholder'
import { SettingsPage } from '@/pages/Settings'
import { StatisticsPage } from '@/pages/Statistics'
import { TagsProjectsPage } from '@/pages/TagsProjects'
import { TransactionsPage } from '@/pages/Transactions'

/** 已实现的页面（其余一律走占位页） */
const IMPLEMENTED_ROUTES: Record<string, ReactNode> = {
  '/': <DashboardPage />,
  '/transactions': <TransactionsPage />,
  '/quick-add': <TransactionsPage />,
  '/accounts': <AccountsPage />,
  '/categories': <CategoriesPage />,
  '/cards': <CardsPage />,
  '/calendar': <CalendarPage />,
  '/statistics': <StatisticsPage />,
  '/kline': <KlinePage />,
  '/metrics': <MetricsPage />,
  '/budgets': <BudgetsPage />,
  '/piggy': <PiggyPage />,
  '/goals': <GoalsPage />,
  '/ledger': <LedgerPage />,
  '/reports': <ReportsPage />,
  '/payroll': <PayrollPage />,
  '/scheduler': <SchedulerPage />,
  '/recurring': <RecurringPage />,
  '/debts': <DebtsPage />,
  '/trash': <TrashPage />,
  '/tags': <TagsProjectsPage />,
  '/settings': <SettingsPage />,
  '/about': <AboutPage />,
}

/**
 * 已完成的阶段。
 *
 * 导航徽标据此判断"这一项能不能点" —— 徽标与页面实现共用同一份事实，
 * 因此不可能出现"标着已实现、点进去是占位页"。
 */
export const IMPLEMENTED_PHASES = new Set(['P0', 'P1', 'P2', 'P3'])

/** 404 页面：保持与占位页一致的视觉语言 */
function NotFoundPage() {
  const { t } = useI18n()
  return (
    <Card flush>
      <EmptyState
        icon="search"
        title={t('common.notFoundTitle')}
        body={t('common.notFoundBody')}
        action={
          <Link to="/" className="ab-btn-primary">
            <Icon name="dashboard" size={14} />
            {t('common.goHome')}
          </Link>
        }
      />
    </Card>
  )
}

export const routes: RouteObject[] = [
  {
    path: '/',
    element: <AppShell />,
    children: [
      ...ALL_NAV_ITEMS.map((item) => {
        const implemented = IMPLEMENTED_ROUTES[item.path]
        if (item.path === '/') {
          return { index: true, element: implemented } as RouteObject
        }
        return {
          path: item.path.replace(/^\//, ''),
          element: implemented ?? <PlaceholderPage item={item} />,
        } as RouteObject
      }),
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
