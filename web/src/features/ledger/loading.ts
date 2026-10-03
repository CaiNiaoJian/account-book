/** Independent, bounded reads. Publication is guarded against cancelled refreshes. */
export const LEDGER_RESOURCES = [
  'enums', 'currencies', 'overview', 'accounts', 'expenseTree', 'incomeTree', 'tags', 'projects', 'members',
] as const
export type LedgerResource = typeof LEDGER_RESOURCES[number]
export type ResourceState = 'loading' | 'ready' | 'error'
export type LoadOutcome = { key: LedgerResource; error?: unknown }

export interface ReadTask {
  key: LedgerResource
  prepare: (signal: AbortSignal) => Promise<() => void>
}

export function readTask<T>(key: LedgerResource, read: (signal: AbortSignal) => Promise<T>,
  publish: (value: T) => void): ReadTask {
  return { key, prepare: async (signal) => {
    const value = await read(signal)
    return () => publish(value)
  } }
}

export async function loadResources(tasks: ReadTask[], signal: AbortSignal,
  onSettled: (outcome: LoadOutcome) => void, timeoutMs = 15000): Promise<LoadOutcome[]> {
  return Promise.all(tasks.map(async (task) => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let onAbort = () => {}
    const interrupted = new Promise<never>((_resolve, reject) => {
      onAbort = () => {
        controller.abort()
        reject(new Error('cancelled'))
      }
      signal.addEventListener('abort', onAbort, { once: true })
      timer = setTimeout(() => {
        controller.abort()
        const error = new Error('timeout')
        error.name = 'LedgerReadTimeout'
        reject(error)
      }, timeoutMs)
    })
    let outcome: LoadOutcome = { key: task.key }
    try {
      if (signal.aborted) onAbort()
      const publish = await Promise.race([task.prepare(controller.signal), interrupted])
      if (!signal.aborted) publish()
    } catch (error) {
      outcome = { key: task.key, error }
    } finally {
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
    }
    if (!signal.aborted) onSettled(outcome)
    return outcome
  }))
}
