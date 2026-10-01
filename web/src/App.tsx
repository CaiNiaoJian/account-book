/**
 * 应用根组件 —— 组合各个 Provider 与路由。
 *
 * Provider 嵌套顺序是有讲究的：
 *   PreferencesProvider  最外层，因为语言来自偏好；
 *     └ I18nProvider     需要 preferences.language；
 *         └ LedgerProvider  需要 api（令牌已在 boot 阶段就绪），且错误提示要能本地化；
 *             └ Router      页面需要 i18n 与账本数据。
 *
 * 仍然**不**引入状态管理库：全局状态只有两项 —— 用户偏好与账本字典
 * （账户 / 分类 / 枚举 / 币种）。两者都是"读多写少、变更后整体刷新"的形态，
 * Context 完全够用；引入 Redux/Zustand 只会增加理解成本。
 * 流水列表**刻意不放进全局**：它是分页且带筛选的页面级状态。
 */

import { MotionConfig } from 'framer-motion'
import { RouterProvider, createBrowserRouter } from 'react-router-dom'

import { PreferencesProvider } from '@/app/preferences'
import { I18nProvider } from '@/i18n'
import { usePreferences } from '@/app/preferences'
import { LedgerProvider } from '@/features/ledger/store'
import { routes } from './routes'

const router = createBrowserRouter(routes)

/** 把语言从偏好桥接到 i18n 层（唯一职责，故保持极小） */
function LocalizedApp() {
  const { preferences } = usePreferences()
  return (
    <I18nProvider language={preferences.language}>
      <MotionConfig reducedMotion={preferences.reduce_motion ? 'always' : 'never'}>
        <LedgerProvider>
          <RouterProvider router={router} />
        </LedgerProvider>
      </MotionConfig>
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
