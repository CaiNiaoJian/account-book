/**
 * 侧边栏导航清单 —— **功能信息架构的单一真源**。
 *
 * 单一真源的好处：侧边栏、命令面板（P1 的 ⌘K）、路由表、占位页
 * 全部从这里派生。新增一个模块只需在此加一行，不会出现
 * "菜单里有但路由没注册"或反之的经典不一致。
 *
 * `phase` 字段对应 docs/PLAN.md 的阶段编号，用于两件事：
 *   1. 未实现模块统一渲染占位页（需求 10：诚实交代进度，而不是给个空白页）；
 *   2. 侧边栏显示阶段徽标，用户一眼可知哪些是已可用、哪些是已预留。
 *
 * 名称与说明文案不写在这里，而是放在 i18n 语言包中
 * （`nav.<id>` 与 `scope.<id>`），保证国际化与结构解耦。
 */

import type { IconName } from '@/components/Icon'

export interface NavItem {
  /** 稳定标识：同时是 i18n 键与侧边栏排序标识，**不要随意改名** */
  id: string
  /** 路由路径（相对根） */
  path: string
  icon: IconName
  /** 计划实现阶段，例如 'P0' / 'P6' */
  phase: string
}

export interface NavGroup {
  id: string
  /** i18n 键：group.<id> */
  idKey: string
  items: NavItem[]
}

/**
 * 已完成的阶段集合。
 *
 * 徽标与"能不能点"由**同一份事实**决定：侧边栏据此隐藏阶段角标，
 * 路由表（`routes.tsx` 的 `IMPLEMENTED_ROUTES`）据实挂载真实页面。
 * 两处若各写一份，就必然出现"标着已实现、点进去是占位页"。
 */
export const IMPLEMENTED_PHASES = new Set(['P0', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6'])

/** 兼容旧引用：当前最新完成阶段 */
export const IMPLEMENTED_PHASE = 'P6'

export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'bookkeeping',
    idKey: 'bookkeeping',
    items: [
      { id: 'dashboard', path: '/', icon: 'dashboard', phase: 'P0' },
      { id: 'transactions', path: '/transactions', icon: 'transactions', phase: 'P1' },
      { id: 'quickAdd', path: '/quick-add', icon: 'quickAdd', phase: 'P1' },
      { id: 'accounts', path: '/accounts', icon: 'accounts', phase: 'P1' },
      { id: 'cards', path: '/cards', icon: 'cards', phase: 'P2' },
      { id: 'categories', path: '/categories', icon: 'categories', phase: 'P1' },
      { id: 'tagsProjects', path: '/tags', icon: 'tags', phase: 'P1' },
      { id: 'recurring', path: '/recurring', icon: 'recurring', phase: 'P1' },
      // 回收站紧挨着流水：用户找它时的心理位置是「我刚才删掉的那笔账」
      { id: 'trash', path: '/trash', icon: 'trash', phase: 'P1' },
      { id: 'budgets', path: '/budgets', icon: 'budgets', phase: 'P1' },
      { id: 'piggy', path: '/piggy', icon: 'piggy', phase: 'P4' },
      { id: 'debts', path: '/debts', icon: 'debts', phase: 'P1' },
      { id: 'goals', path: '/goals', icon: 'goals', phase: 'P4' },
    ],
  },
  {
    id: 'insight',
    idKey: 'insight',
    items: [
      { id: 'statistics', path: '/statistics', icon: 'statistics', phase: 'P2' },
      { id: 'kline', path: '/kline', icon: 'kline', phase: 'P3' },
      { id: 'ledger', path: '/ledger', icon: 'ledger', phase: 'P5' },
      { id: 'reports', path: '/reports', icon: 'reports', phase: 'P5' },
      { id: 'calendar', path: '/calendar', icon: 'calendar', phase: 'P2' },
      { id: 'payroll', path: '/payroll', icon: 'payroll', phase: 'P6' },
    ],
  },
  {
    id: 'data',
    idKey: 'data',
    items: [
      { id: 'scheduler', path: '/scheduler', icon: 'scheduler', phase: 'P6' },
      { id: 'dataManager', path: '/data', icon: 'dataManager', phase: 'P8' },
      { id: 'dbExplorer', path: '/database', icon: 'dbExplorer', phase: 'P8' },
      { id: 'intelligence', path: '/intelligence', icon: 'intelligence', phase: 'P7' },
      { id: 'plugins', path: '/plugins', icon: 'plugins', phase: 'P9' },
      { id: 'customization', path: '/customization', icon: 'customization', phase: 'P9' },
      { id: 'settings', path: '/settings', icon: 'settings', phase: 'P0' },
      { id: 'about', path: '/about', icon: 'about', phase: 'P0' },
    ],
  },
]

/** 扁平化的条目列表（供路由生成与搜索使用） */
export const ALL_NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((group) => group.items)

/** 按路径查找条目（占位页据此取得模块名与阶段） */
export function findNavItemByPath(path: string): NavItem | undefined {
  return ALL_NAV_ITEMS.find((item) => item.path === path)
}

/** 判断模块是否已完成（阶段已在 IMPLEMENTED_PHASES 中） */
export function isImplemented(item: NavItem): boolean {
  return IMPLEMENTED_PHASES.has(item.phase)
}
