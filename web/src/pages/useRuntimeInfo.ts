/**
 * 运行环境信息的共享数据钩子。
 *
 * 抽出来的理由：概览页与关于页都需要 `GET /api/system/info`，
 * 但两个页面永远只显示其中一个。因此**不做全局缓存**（避免为两个页面
 * 引入状态管理库），只把"加载 / 成功 / 失败"三态逻辑集中一处，
 * 保证两个页面的错误处理与重试行为完全一致。
 */

import { useCallback, useEffect, useState } from 'react'

import { api, ApiError, type RuntimeInfo } from '@/lib/api'

export type RuntimeInfoState =
  | { status: 'loading' }
  | { status: 'ready'; info: RuntimeInfo }
  | { status: 'error'; message: string }

export function useRuntimeInfo(): { state: RuntimeInfoState; reload: () => void } {
  const [state, setState] = useState<RuntimeInfoState>({ status: 'loading' })

  const load = useCallback(() => {
    setState({ status: 'loading' })
    api
      .runtimeInfo()
      .then((info) => setState({ status: 'ready', info }))
      .catch((error: unknown) => {
        // 统一把异常压成可渲染的字符串：界面上不需要区分 HTTP 错误与网络错误
        const message = error instanceof ApiError ? error.detail : String(error)
        setState({ status: 'error', message })
      })
  }, [])

  useEffect(() => {
    load()
  }, [load])

  return { state, reload: load }
}
