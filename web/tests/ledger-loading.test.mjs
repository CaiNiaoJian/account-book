import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import ts from 'typescript'

// Run the actual shared loader using the existing TypeScript dependency, including on Node 18.
const source = readFileSync(new URL('../src/features/ledger/loading.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } })
const { loadResources, readTask } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString('base64')}`)
const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

test('success and failure are published independently while another read is pending', async () => {
  const slow = deferred()
  const values = { accounts: ['previous'], tags: ['previous tag'] }
  const settled = []
  const run = loadResources([
    readTask('accounts', async () => ['new account'], (data) => { values.accounts = data }),
    readTask('tags', async () => { throw new Error('unavailable') }, (data) => { values.tags = data }),
    readTask('projects', () => slow.promise, (data) => { values.projects = data }),
  ], new AbortController().signal, (result) => settled.push(result), 1000)
  await new Promise(setImmediate)
  assert.deepEqual(values.accounts, ['new account'])
  assert.deepEqual(values.tags, ['previous tag'])
  assert.equal(settled.find((row) => row.key === 'tags').error.message, 'unavailable')
  slow.resolve(['project'])
  await run
  assert.deepEqual(values.projects, ['project'])
})

test('retrying only failed resources preserves successful data and requests', async () => {
  const values = { accounts: ['previous'], tags: ['previous'] }
  const calls = { accounts: 0, tags: 0 }
  let fail = true
  const tasks = [
    readTask('accounts', async () => { calls.accounts++; return ['account'] }, (data) => { values.accounts = data }),
    readTask('tags', async () => { calls.tags++; if (fail) throw new Error('failed'); return ['tag'] }, (data) => { values.tags = data }),
  ]
  const first = await loadResources(tasks, new AbortController().signal, () => {})
  fail = false
  const failed = new Set(first.filter((result) => result.error).map((result) => result.key))
  await loadResources(tasks.filter((task) => failed.has(task.key)), new AbortController().signal, () => {})
  assert.deepEqual(values, { accounts: ['account'], tags: ['tag'] })
  assert.deepEqual(calls, { accounts: 1, tags: 2 })
})

test('a timed out read is aborted and a late response cannot replace cached data', async () => {
  const slow = deferred()
  let requestSignal
  let value = 'cached'
  const result = await loadResources([
    readTask('overview', (signal) => { requestSignal = signal; return slow.promise }, (data) => { value = data }),
  ], new AbortController().signal, () => {}, 10)
  assert.equal(result[0].error.name, 'LedgerReadTimeout')
  assert.equal(requestSignal.aborted, true)
  slow.resolve('late')
  await new Promise(setImmediate)
  assert.equal(value, 'cached')
})

test('a cancelled refresh cannot publish data or errors after a newer refresh', async () => {
  const slow = deferred()
  const old = new AbortController()
  let value = 'cached'
  const notifications = []
  const oldRun = loadResources([
    readTask('overview', () => slow.promise, (data) => { value = data }),
  ], old.signal, (result) => notifications.push(result))
  old.abort()
  await loadResources([
    readTask('overview', async () => 'new', (data) => { value = data }),
  ], new AbortController().signal, () => {})
  slow.resolve('stale')
  await oldRun
  assert.equal(value, 'new')
  assert.deepEqual(notifications, [])
})

test('a successful empty response clears cached content', async () => {
  let tags = ['removed tag']
  await loadResources([
    readTask('tags', async () => [], (data) => { tags = data }),
  ], new AbortController().signal, () => {})
  assert.deepEqual(tags, [])
})
