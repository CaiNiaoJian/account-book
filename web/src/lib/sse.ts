/**
 * 消费一个 SSE 流。
 *
 * 为什么自己解析而不是用 `EventSource`
 * ------------------------------------
 * `EventSource` 只能发 GET 且**不能带自定义头**，而这里还需要
 * "中途取消"（用户换报表类型时旧请求必须停掉，否则两份分析会互相覆盖）。
 * 用 `fetch` + `ReadableStream` 两者都能做到，代价只是自己拆一下帧格式，
 * 而帧格式本身很简单（`event: x\ndata: {...}\n\n`）。
 *
 * 一个容易踩的坑：**帧可能在任意位置被切断**。网络分块是按字节来的，
 * 一个 `data:` 行完全可能被切成两半。因此必须用一个缓冲区，
 * 只在看到完整的空行（`\n\n`）时才解析 —— 直接对每个 chunk 做 split
 * 会在网络稍慢时随机丢帧，而且很难复现。
 */

export interface SseHandlers {
  /** 每收到一个完整帧调用一次。`data` 已 JSON.parse */
  onEvent: (event: string, data: unknown) => void
  onError?: (error: Error) => void
}

export async function streamSse(
  url: string,
  handlers: SseHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response
  try {
    response = await fetch(url, {
      credentials: 'same-origin',
      headers: { Accept: 'text/event-stream' },
      signal,
    })
  } catch (cause) {
    // 主动取消不算错误：用户换了个报表类型而已
    if ((cause as Error)?.name === 'AbortError') return
    handlers.onError?.(cause as Error)
    return
  }
  if (!response.ok || !response.body) {
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = (body as { detail?: string }).detail ?? detail
    } catch {
      /* 非 JSON 响应时沿用状态文本 */
    }
    handlers.onError?.(new Error(`[${response.status}] ${detail}`))
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      // `stream: true` 让多字节字符跨 chunk 时也能正确解码 ——
      // 一个汉字被切成两半时，缺了它就会得到 U+FFFD 乱码
      buffer += decoder.decode(value, { stream: true })
      let index = buffer.indexOf('\n\n')
      while (index >= 0) {
        const raw = buffer.slice(0, index)
        buffer = buffer.slice(index + 2)
        const event = /^event:\s*(.+)$/m.exec(raw)?.[1]?.trim() ?? 'message'
        const dataLines = raw
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trim())
        if (dataLines.length > 0) {
          try {
            handlers.onEvent(event, JSON.parse(dataLines.join('\n')))
          } catch {
            // 单个坏帧不该中断整个流：后面还有很多有用的内容
          }
        }
        index = buffer.indexOf('\n\n')
      }
    }
  } catch (cause) {
    if ((cause as Error)?.name !== 'AbortError') handlers.onError?.(cause as Error)
  } finally {
    reader.releaseLock()
  }
}
