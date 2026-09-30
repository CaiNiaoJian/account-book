/**
 * 应用根组件 —— 组合各个 Provider 与路由。
 *
 * Provider 嵌套顺序是有讲究的：
 *   PreferencesProvider  最外层，因为语言来自偏好；
 *     └ I18nProvider     需要 preferences.language；
 *         └ Router       页面需要 i18n 提供的标题与文案。
 *
 * 刻意**不**引入任何状态管理库：P0 的全局状态只有"用户偏好"一项，
 * 一个 Context 足够；过早引入 Redux/Zustand 只会增加理解成本。
 * P1 出现跨页面的账目缓存需求时再评估。
 */

import { RouterProvider, createBrowserRouter } from 'react-router-dom'

import { PreferencesProvider } from '@/app/preferences'
import { I18nProvider } from '@/i18n'
import { usePreferences } from '@/app/preferences'
import { routes } from './routes'

const router = createBrowserRouter(routes)

/** 把语言从偏好桥接到 i18n 层（唯一职责，故保持极小） */
function LocalizedApp() {
  const { preferences } = usePreferences()
  return (
    <I18nProvider language={preferences.language}>
      <RouterProvider router={router} />
    </I18nProvider>
  )
}

export function App() {
  return (
    <PreferencesProvider>
      <LocalizedApp />
    </PreferencesProvider>
  )
}
