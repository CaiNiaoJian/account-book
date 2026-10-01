/**
 * 后端 API 客户端。
 *
 * 安全约定（与 accountbook/core/security.py 对应）
 * ------------------------------------------------
 * * 所有请求都带上 `Authorization: Bearer <token>`，并同时依赖同源 Cookie 兜底；
 * * **绝不**把令牌写进 URL 或 localStorage —— URL 会进入历史记录与日志，
 *   localStorage 会被同机其它进程读取到的风险更高（P8 引入应用锁时会重新评估）；
 * * 请求一律 `credentials: 'same-origin'`，杜绝跨源携带凭据。
 *
 * 错误处理策略：把 HTTP 错误统一转成 `ApiError`，让 UI 只需处理一种异常类型。
 */

import { boot, type Preferences } from './boot'

/** 后端返回的环境信息（对应 api/routes/system.py 的 RuntimeInfo） */
export interface RuntimeInfo {
  app_id: string
  app_name: string
  app_name_en: string
  version: string
  phase: string
  python_version: string
  platform: string
  pid: number
  port: number
  uptime_seconds: number
  is_admin: boolean
  /** 实际生效的外壳：pywebview（原生窗口）或 browser（自动降级） */
  shell: string
  /** 选定的 .NET 运行时；null 表示原生外壳不可用 */
  clr_runtime: string | null
  paths: {
    data_dir: string
    /** 数据目录来源说明（首选位置 / 回退位置） */
    data_dir_source: string
    /** 被跳过的候选位置及原因；为空表示首选位置可用 */
    data_dir_attempts: string[]
    program_root: string
    log_file: string
    database: string
    portable: boolean
    frozen: boolean
    degraded: boolean
  }
}

export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(`[${status}] ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

const JSON_HEADERS: HeadersInit = {
  'Content-Type': 'application/json',
  Authorization: `Bearer ${boot.token}`,
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      headers: { ...JSON_HEADERS, ...(init.headers ?? {}) },
    })
  } catch (cause) {
    // 网络层失败（后端已退出 / 端口被占用）：给出比 "Failed to fetch" 更有用的信息
    throw new ApiError(0, `无法连接本地服务，接口 ${path} 请求失败：${String(cause)}`)
  }

  if (!response.ok) {
    let detail = response.statusText || '请求失败'
    try {
      const body = (await response.json()) as { detail?: string }
      if (body?.detail) detail = body.detail
    } catch {
      /* 响应不是 JSON（例如 401 的 HTML 页面）时沿用状态文本 */
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  /** 探活：用于前端显示"服务已连接"状态 */
  health: () => request<{ status: string; version: string; phase: string }>('/health'),

  /** 运行环境信息 */
  runtimeInfo: () => request<RuntimeInfo>('/api/system/info'),

  /** 读取偏好 */
  getPreferences: () => request<{ preferences: Preferences }>('/api/system/preferences'),

  /**
   * 局部更新偏好。
   * 只发送真正改动的字段 —— 减少无效写入，也让"谁改了什么"在日志里一目了然。
   */
  patchPreferences: (changes: Partial<Preferences>) =>
    request<{ preferences: Preferences }>('/api/system/preferences', {
      method: 'PATCH',
      body: JSON.stringify(changes),
    }),

  /** 在文件管理器中打开数据目录 */
  revealDataDir: () =>
    request<{ status: string; path: string; detail?: string }>('/api/system/reveal-data-dir', {
      method: 'POST',
    }),
}
