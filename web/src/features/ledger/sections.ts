/** Page sections publish independently and retain their previous value on failed reads. */
export interface SectionRead { run: () => Promise<void> }
export function section<T>(read: () => Promise<T>, publish: (value: T) => void): SectionRead {
  return { run: async () => { publish(await read()) } }
}
export async function readSections(reads: SectionRead[], message: string) {
  const outcomes = await Promise.allSettled(reads.map(read => read.run()))
  if (outcomes.some(outcome => outcome.status === 'rejected')) throw new Error(message)
}
