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
import { useEffect, useMemo } from 'react'
import { useLocation, useNavigate, useOutlet } from 'react-router-dom'

import { pageVariants } from '@/design/motion'
import { useI18n } from '@/i18n'
import { LedgerLoadNotice } from '@/features/ledger/LoadNotice'
import { findNavItemByPath } from './navigation'
import { usePreferences } from './preferences'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'

/**
 * 应用内全局快捷键。
 *
 * 范围说明：这些快捷键在**应用窗口聚焦时**全局生效，任何页面都能唤起
 * 快捷记账。真正的"操作系统级热键"（应用在后台也能唤起）需要
 * Windows `RegisterHotKey`，本环境无法交互式验证，因此没有做 ——
 * 与其交付一个没验证过的热键注册，不如把这一条明确留在文档里。
 *
 * 刻意避开 `Ctrl+C/V/A/F` 这些浏览器与输入框自带的组合，
 * 也刻意在输入框内不拦截：用户正在打字时抢快捷键是最恼人的一类 bug。
 */
function useGlobalShortcuts() {
  const navigate = useNavigate()

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const typing =
        target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA' || target?.isContentEditable
      const modifier = event.metaKey || event.ctrlKey
      if (!modifier || event.altKey) return

      const key = event.key.toLowerCase()
      if (key === 'k') {
        // 快捷记账：即使正在输入也允许（它是"我现在要记一笔"的强意图），
        // 但仍然阻止浏览器默认的"聚焦地址栏"
        event.preventDefault()
        navigate('/quick-add')
        return
      }
      if (typing) return
      if (key === 'l') {
        event.preventDefault()
        navigate('/transactions')
      } else if (key === '1') {
        event.preventDefault()
        navigate('/')
      } else if (key === '2') {
        event.preventDefault()
        navigate('/statistics')
      } else if (key === '3') {
        event.preventDefault()
        navigate('/calendar')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate])
}

export function AppShell() {
  useGlobalShortcuts()
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
          <LedgerLoadNotice />
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
