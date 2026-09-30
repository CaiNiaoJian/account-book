/**
 * 应用外壳 —— 侧边栏 + 工具栏 + 内容区的三栏布局，并负责页面转场。
 *
 * 布局决策
 * --------
 * * 窗口整体 `h-full overflow-hidden`，只有内容区滚动。
 *   这是桌面应用与网页最直观的区别之一：工具栏与侧边栏永远固定。
 * * 内容区限制最大宽度并水平居中（`max-w-[1180px]`）。
 *   在超宽显示器上，让正文无限拉伸会显著降低可读性；
 *   Apple 的通行做法是限制行宽并把留白交给背景。
 * * 页面转场用 `AnimatePresence mode="wait"`，时长很短（进场 400ms / 退场 150ms）。
 *   过长的转场会让"点一下卡一下"，对高频记账场景是负体验。
 */

import { AnimatePresence, motion } from 'framer-motion'
import { useMemo } from 'react'
import { useLocation, useOutlet } from 'react-router-dom'

import { pageVariants } from '@/design/motion'
import { useI18n } from '@/i18n'
import { findNavItemByPath } from './navigation'
import { usePreferences } from './preferences'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'

export function AppShell() {
  const { t } = useI18n()
  const location = useLocation()
  const outlet = useOutlet()
  const { preferences, update } = usePreferences()

  // 当前路由对应的模块元信息（标题与阶段徽标从导航清单派生，避免两处维护）
  const current = useMemo(() => findNavItemByPath(location.pathname), [location.pathname])
  const title = current ? t(`nav.${current.id}`) : t('app.name')
  const phase = current?.phase ?? 'P0'

  return (
    <div className="flex h-full w-full overflow-hidden bg-bg">
      <Sidebar collapsed={preferences.sidebar_collapsed} />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          title={title}
          phase={phase}
          collapsed={preferences.sidebar_collapsed}
          onToggleCollapse={() => void update({ sidebar_collapsed: !preferences.sidebar_collapsed })}
        />

        <main id="ab-main" className="flex-1 overflow-y-auto overflow-x-hidden">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={location.pathname}
              variants={pageVariants}
              initial="initial"
              animate="animate"
              exit="exit"
              className="mx-auto w-full max-w-[1180px] px-7 py-6"
            >
              {outlet}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </div>
  )
}
