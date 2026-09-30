/**
 * 路由表 —— 由导航清单**派生**，而非手写。
 *
 * 这样做消除了最常见的一类不一致：
 *   "侧边栏里有这一项，点进去 404" 或 "路由存在但用户找不到入口"。
 *
 * 分派规则：
 *   * 阶段为 P0 的模块 → 真实页面组件；
 *   * 其余模块 → 统一占位页（自动带模块名、阶段与规划说明）。
 * 当某个模块在后续阶段完成时，只需在此把它的 id 从 `PLACEHOLDER_ROUTES`
 * 移到 `IMPLEMENTED_ROUTES`，导航与徽标会自动同步。
 */

import type { ReactNode } from 'react'
import { Link, type RouteObject } from 'react-router-dom'

import { Icon } from '@/components/Icon'
import { Card, EmptyState } from '@/components/ui'
import { AppShell } from '@/app/AppShell'
import { ALL_NAV_ITEMS } from '@/app/navigation'
import { useI18n } from '@/i18n'
import { AboutPage } from '@/pages/About'
import { DashboardPage } from '@/pages/Dashboard'
import { PlaceholderPage } from '@/pages/Placeholder'
import { SettingsPage } from '@/pages/Settings'

/** P0 已实现的页面（其余一律走占位页） */
const IMPLEMENTED_ROUTES: Record<string, ReactNode> = {
  '/': <DashboardPage />,
  '/settings': <SettingsPage />,
  '/about': <AboutPage />,
}

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
