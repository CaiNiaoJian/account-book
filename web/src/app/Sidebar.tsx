/**
 * 侧边栏 —— Apple 风格的半透明导航栏。
 *
 * 视觉与交互要点
 * --------------
 * * **材质**：`ab-material` 类提供 `backdrop-blur` + 半透明底色，
 *   在深色主题下随壁纸透出一点层次（对应 macOS 的侧边栏材质）。
 * * **选中态**：圆角药丸 + 系统蓝，未选中悬停仅极淡填充 —— 
 *   保证"当前在哪"永远一眼可辨，而不会被悬停态淹没。
 * * **分组**：三个语义分组（记账流 / 分析与呈现 / 数据与系统），
 *   每组可折叠，折叠状态持久化在偏好里（`sidebar_order` 为 P9 的自定义排序预留）。
 * * **阶段徽标**：未实现模块显示 `P1`/`P6` 等徽标（需求 10），
 *   用户不会误以为功能缺失是缺陷。
 * * **搜索过滤**：顶部输入框即时过滤（完整命令面板 ⌘K 属 P1）。
 */

import { AnimatePresence, motion } from 'framer-motion'
import { useMemo, useState } from 'react'
import { NavLink } from 'react-router-dom'

import { Icon } from '@/components/Icon'
import { useI18n } from '@/i18n'
import { NAV_GROUPS, isImplemented, type NavItem } from './navigation'

interface SidebarProps {
  collapsed: boolean
}

export function Sidebar({ collapsed }: SidebarProps) {
  const { t } = useI18n()
  const [query, setQuery] = useState('')
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({})

  // 过滤：按当前语言下的模块名做不区分大小写的子串匹配
  const filteredGroups = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return NAV_GROUPS
    return NAV_GROUPS.map((group) => ({
      ...group,
      items: group.items.filter((item) => t(`nav.${item.id}`).toLowerCase().includes(keyword)),
    })).filter((group) => group.items.length > 0)
  }, [query, t])

  if (collapsed) {
    return (
      <aside className="ab-material flex h-full w-[64px] shrink-0 flex-col items-center gap-1 border-r border-separator/60 py-3">
        {NAV_GROUPS.flatMap((group) => group.items).map((item) => (
          <CollapsedNavItem key={item.id} item={item} label={t(`nav.${item.id}`)} />
        ))}
      </aside>
    )
  }

  return (
    <aside className="ab-material flex h-full w-[236px] shrink-0 flex-col border-r border-separator/60">
      {/* ---- 品牌区 ------------------------------------------------------- */}
      <div className="flex items-center gap-2.5 px-4 pb-2 pt-4">
        <div className="flex h-7 w-7 items-center justify-center rounded-[9px] bg-gradient-to-br from-accent to-indigo text-white shadow-ab-1">
          <Icon name="ledger" size={16} strokeWidth={1.8} />
        </div>
        <div className="min-w-0">
          <p className="truncate text-ab-subhead font-semibold leading-4 text-label">{t('app.name')}</p>
          <p className="ab-tnum truncate text-ab-caption2 leading-3 text-label-3">{`v0.1.0 · P0`}</p>
        </div>
      </div>

      {/* ---- 过滤输入框 --------------------------------------------------- */}
      <div className="px-3 pb-1 pt-2">
        <div className="flex items-center gap-1.5 rounded-ab-sm bg-hairline/5 px-2 py-[5px] transition-colors focus-within:bg-hairline/10">
          <Icon name="search" size={13} className="shrink-0 text-label-3" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('topbar.filterPlaceholder')}
            aria-label={t('topbar.filterPlaceholder')}
            className="w-full bg-transparent text-ab-footnote text-label placeholder:text-label-3 focus:outline-none"
          />
          {query ? (
            <button
              type="button"
              aria-label={t('common.retry')}
              onClick={() => setQuery('')}
              className="shrink-0 text-label-3 hover:text-label"
            >
              <Icon name="close" size={12} />
            </button>
          ) : null}
        </div>
      </div>

      {/* ---- 分组列表 ----------------------------------------------------- */}
      <nav className="flex-1 overflow-y-auto px-2 pb-3" aria-label={t('app.name')}>
        {filteredGroups.map((group) => {
          const isGroupCollapsed = collapsedGroups[group.id] ?? false
          return (
            <div key={group.id}>
              <button
                type="button"
                onClick={() => setCollapsedGroups((prev) => ({ ...prev, [group.id]: !isGroupCollapsed }))}
                aria-expanded={!isGroupCollapsed}
                className="ab-section-label flex w-full items-center gap-1 text-left transition-colors hover:text-label-2"
              >
                <motion.span
                  animate={{ rotate: isGroupCollapsed ? -90 : 0 }}
                  transition={{ duration: 0.18, ease: [0.32, 0.72, 0, 1] }}
                  className="inline-flex"
                >
                  <Icon name="chevronDown" size={11} strokeWidth={2} />
                </motion.span>
                {t(`group.${group.idKey}`)}
              </button>

              <AnimatePresence initial={false}>
                {!isGroupCollapsed && (
                  <motion.ul
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.2, ease: [0.32, 0.72, 0, 1] }}
                    className="overflow-hidden"
                  >
                    {group.items.map((item) => (
                      <li key={item.id}>
                        <SidebarLink item={item} label={t(`nav.${item.id}`)} />
                      </li>
                    ))}
                  </motion.ul>
                )}
              </AnimatePresence>
            </div>
          )
        })}

        {filteredGroups.length === 0 ? (
          <p className="px-2.5 py-6 text-center text-ab-footnote text-label-3">{t('common.unknown')}</p>
        ) : null}
      </nav>

      {/* ---- 底部：本地优先声明（需求 14 的可视化承诺） -------------------- */}
      <div className="flex items-center gap-1.5 border-t border-separator/60 px-3.5 py-2.5">
        <Icon name="shield" size={13} className="shrink-0 text-positive" />
        <p className="truncate text-ab-caption2 text-label-3">{t('app.tagline')}</p>
      </div>
    </aside>
  )
}

/** 展开态下的单个导航项 */
function SidebarLink({ item, label }: { item: NavItem; label: string }) {
  const implemented = isImplemented(item)
  return (
    <NavLink
      to={item.path}
      end={item.path === '/'}
      title={label}
      className={({ isActive }) => ['ab-nav-item', isActive ? 'is-active' : ''].filter(Boolean).join(' ')}
    >
      {({ isActive }) => (
        <>
          <Icon
            name={item.icon}
            size={16}
            className={isActive ? 'text-accent' : 'text-label-3'}
            strokeWidth={isActive ? 1.9 : 1.6}
          />
          <span className="min-w-0 flex-1 truncate">{label}</span>
          {!implemented ? <span className="ab-phase-badge">{item.phase}</span> : null}
        </>
      )}
    </NavLink>
  )
}

/** 折叠态下的图标导航项（仅图标 + tooltip） */
function CollapsedNavItem({ item, label }: { item: NavItem; label: string }) {
  return (
    <NavLink
      to={item.path}
      end={item.path === '/'}
      title={`${label} · ${item.phase}`}
      aria-label={label}
      className={({ isActive }) =>
        [
          'flex h-8 w-8 items-center justify-center rounded-ab-sm transition-colors duration-ab1 ease-ab-standard',
          isActive
            ? 'bg-accent/15 text-accent'
            : 'text-label-3 hover:bg-hairline/5 hover:text-label',
        ].join(' ')
      }
    >
      <Icon name={item.icon} size={17} />
    </NavLink>
  )
}
